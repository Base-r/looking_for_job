"""Sne: живой поллинг новых вакансий hh → конвейер → POST /negotiations.

Библиотечный режим базы (тонкий слой): SavingHHApplicantTool из hh_tool даёт
api-клиент с токеном из auth.json, автопродлением и капчей. Поиск —
GET /vacancies (order_by=publication_time, date_from=now-N) по запросам из
config.toml [search]; полная карточка — GET /vacancies/{id}. Дедуп —
seen.jsonl (прошедшие конвейер) + applied.jsonl (пускы). Пуск —
POST /negotiations {resume_id, vacancy_id, message}.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .approve import AppliedStore
from .pipeline import SnePipeline

DEFERRED_KEEP_FIELDS = (
    "id",
    "name",
    "employer",
    "salary",
    "area",
    "work_format",
    "experience",
    "published_at",
    "key_skills",
    # подработка и способ оплаты (side_job.py)
    "employment_form",
    "employment",
    "salary_range",
    # куда откликаться на пост из Telegram (apply_link.py)
    "apply",
)


def hh_from_posts(
    api: Any,
    items: list[dict[str, Any]],
    known_ids: set[str],
    current: Iterable[Mapping[str, Any]] = (),
    budget: int = 20,
    pause: float = 2.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Посты, где откликаться надо на hh, → сама вакансия hh.

    Номер вакансии берётся из ссылки в посте (по ней бот не ходит),
    вакансия — по API hh, как в обычном поиске. Тогда на неё можно
    откликнуться из бота с письмом. Вернёт (вакансии hh, остальные посты,
    полные карточки для кэша описаний). Уже виденная на hh — пост не нужен;
    в архиве — тоже. Сбой API или исчерпан budget — пост остаётся как был.
    """
    from .apply_link import apply_of, hh_vacancy_id

    have = {str(i) for i in known_ids} | {str(v.get("id")) for v in current}
    cards: dict[str, dict[str, Any]] = {}
    kept: list[dict[str, Any]] = []
    last = 0.0
    for post in items:
        apply = apply_of(post)
        hid = hh_vacancy_id(apply["url"]) if apply and apply.get("kind") == "hh" else ""
        if not hid:
            kept.append(post)
            continue
        if hid in have:
            continue
        if len(cards) >= budget:
            kept.append(post)
            continue
        wait = pause - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.monotonic()
        try:
            card = api.get(f"/vacancies/{hid}")
        except Exception as ex:  # noqa: BLE001 — капча/сеть: оценим как пост
            print(f"[telegram] вакансия hh {hid} из поста не получена: {ex}")
            kept.append(post)
            continue
        if not isinstance(card, dict) or str(card.get("id") or "") != hid:
            kept.append(post)
            continue
        have.add(hid)
        if card.get("archived"):
            continue
        card["found_via"] = str(post.get("alternate_url") or "")
        cards[hid] = card
    return list(cards.values()), kept, cards


def _sort_by_published_at(vacancies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Свежие первыми: скорость отклика — весь смысл снпрера."""

    def stamp(vacancy: Mapping[str, Any]) -> str:
        return str(vacancy.get("published_at") or "")

    return sorted(vacancies, key=stamp, reverse=True)


def resolve_search_hours(
    args_hours: float,
    search_cfg: Mapping[str, Any],
    marker: Path,
    now: float | None = None,
) -> float:
    """Адаптивная глубина поиска под каденцию джобы 5 минут (18.10).

    Раньше окно было всегда 24 ч. При поллинге каждые 5 минут это
    пережёвывает всю суточную выдачу каждый такт. Теперь окно = время
    с прошлого УСПЕШНОГО прогона (маркер data/last_run.txt) + 6 минут
    страховки, но не меньше min_hours и не больше hours (24 ч —
    покрытие простоев ноутбука: woke up → окно раскрылось на паузу).
    Флаг --hours жёстко задаёт окно (ручной прогон). Маркера нет
    (первый прогон) → полный cap. Аварийные прогоны маркер НЕ трогают:
    следующий раскроет окно заново.
    """
    if args_hours > 0:
        return float(args_hours)
    cap = float(search_cfg.get("hours", 24) or 24)
    floor = float(search_cfg.get("min_hours", 0.25) or 0.25)
    try:
        last = float(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return cap
    moment = time.time() if now is None else now
    elapsed_hours = max(0.0, (moment - last) / 3600)
    return min(cap, max(floor, elapsed_hours + 0.1))


def _touch_marker(marker: Path) -> None:
    """Маркер успешного прогона (epoch-секунды). Не критичен: сдох —
    окно просто раскроется шире, ничего не теряем."""
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{time.time():.0f}\n", encoding="utf-8")
    except OSError:
        pass


def _send_max_age_hours(send_cfg: Mapping[str, Any]) -> float | None:
    """Лимит свежести автопилота. 18.10: минуты приоритетнее
    (max_age_minutes = 10), старый ключ max_age_hours — совместимость.
    0/отсутствует = гейт выключен."""
    try:
        minutes = float(send_cfg.get("max_age_minutes", 0) or 0)
        if minutes > 0:
            return minutes / 60.0
        hours = float(send_cfg.get("max_age_hours", 0) or 0)
    except (TypeError, ValueError):
        return None
    return hours or None


def _pid_alive(pid: int) -> bool:
    """Жив ли процесс-владелец лока. Windows: OpenProcess+
    GetExitCodeProcess (os.kill на винде проверку не делает — завершает)."""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes

    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint32,
    ]
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _lock_holder_alive(path: Path) -> bool | None:
    """None — pid не читается (старый/чужой формат): решает возраст."""
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore").strip()
        pid = int(raw.split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return _pid_alive(pid)


def _acquire_run_lock(path: Path, stale_after: float = 1800.0) -> int | None:
    """Лок одного прогона: каденция 5 мин, но прогон при всплеске живёт
    дольше такта, тики поверх скипаются. В файл пишем pid держателя:
    если процесс неактивн (жёсткое TerminateProcess — finally не отработал),
    лок снимаем СРАЗУ на следующем тике, не ждём 30-мин стейла.
    Поймано живьём 18.10 ~01:44: догоняющий инстанс планировщика упал
    ^C, finally не отработал, лок утёк — тики скипали бы до 02:14."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), flags)
    except FileExistsError:
        holder = _lock_holder_alive(path)
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return None
        if holder is not False and age <= stale_after:
            return None
        try:
            path.unlink()
        except OSError:
            return None
        try:
            fd = os.open(str(path), flags)
        except FileExistsError:
            return None
    try:
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass
    return fd


def _release_run_lock(path: Path, fd: int) -> None:
    """Файл лока обязан удаляться: иначе каждый следующий такт будет
    видеть «прогон идёт» до 30-минутного стейла (джоба выродится в
    раз-в-полчаса)."""
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        path.unlink()
    except OSError:
        pass

DEFAULT_QUERIES = [
    "LLM",
    "AI инженер",
    "ML инженер",
    "NLP",
    "Data Scientist",
    "Machine Learning",
]


class SeenStore:
    """Дедуп прогонов: id вакансий, уже прошедших конвейер."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def seen_ids(self) -> set[str]:
        if not self.path.exists():
            return set()
        ids: set[str] = set()
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("vacancy_id"):
                    ids.add(str(rec["vacancy_id"]))
        return ids

    def mark_seen(
        self, vacancy_id: str, name: str = "", status: str = ""
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "vacancy_id": str(vacancy_id),
            "name": name,
            "status": status,
            "ts": int(datetime.now(timezone.utc).timestamp()),
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_search_params(
    query: str,
    hours: float,
    per_page: int,
    page: int = 0,
    now: datetime | None = None,
    extra: Mapping[str, Any] | None = None,
    until_hours: float = 0,
) -> dict[str, Any]:
    """extra — доп. фильтры hh из config [search.params] (area, experience,
    search_field...). Списки уходят повторяющимися ключами: area=113&area=97."""
    moment = now or datetime.now(timezone.utc)
    date_from = (moment - timedelta(hours=hours)).isoformat(timespec="seconds")
    params: dict[str, Any] = {
        key: value for key, value in (extra or {}).items() if value not in ("", [])
    }
    params.update(
        {
            "text": query,
            "page": page,
            "per_page": per_page,
            "order_by": "publication_time",
            "date_from": date_from,
        }
    )
    return _with_date_to(params, moment, until_hours)


def _window_params(
    hours: float,
    per_page: int,
    page: int,
    extra: Mapping[str, Any],
    now: datetime | None = None,
    until_hours: float = 0,
) -> dict[str, Any]:
    """Параметры окна поиска без текста (специализации, похожие на резюме)."""
    moment = now or datetime.now(timezone.utc)
    params = {k: v for k, v in extra.items() if v not in ("", [])}
    params.update(
        {
            "page": page,
            "per_page": per_page,
            "order_by": "publication_time",
            "date_from": (moment - timedelta(hours=hours)).isoformat(timespec="seconds"),
        }
    )
    return _with_date_to(params, moment, until_hours)


def _with_date_to(
    params: dict[str, Any], moment: datetime, until_hours: float
) -> dict[str, Any]:
    """until_hours > 0 — только вакансии, вышедшие не позже чем столько
    часов назад (min_age_days)."""
    if until_hours > 0:
        params["date_to"] = (moment - timedelta(hours=until_hours)).isoformat(
            timespec="seconds"
        )
    return params


def resolve_age_window(
    search_cfg: Mapping[str, Any], min_days: float = -1, max_days: float = -1
) -> tuple[float, float] | None:
    """Возраст вакансии с публикации, [search] min_age_days/max_age_days
    (флаги CLI важнее). (глубина ч, верхняя граница ч) или None, если
    max_age_days не задан — тогда окно «с прошлого прогона»."""
    lo = min_days if min_days >= 0 else float(search_cfg.get("min_age_days", 0) or 0)
    hi = max_days if max_days >= 0 else float(search_cfg.get("max_age_days", 0) or 0)
    if hi <= 0:
        return None
    lo = max(0.0, min(lo, hi))
    return hi * 24, lo * 24


def _replace_file(tmp: Path, target: Path, attempts: int = 20) -> None:
    """Атомарная замена файла с повтором: на Windows замена падает с
    PermissionError, пока файл в ту же секунду читает другой процесс
    (страница отбора, плановый прогон)."""
    for attempt in range(attempts):
        try:
            tmp.replace(target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1 * (attempt + 1))


class DeferredStore:
    """Очередь вакансий, не закрытых в прогоне: бюджет скоринга или сбой LLM.

    Незакрытую вакансию нельзя помечать seen (иначе потеряем), а к следующему
    прогону она уже может выпасть из окна date_from — очередь сохраняет её
    между запусками. Отложенные по бюджету и упавшие по LLM лежат вместе:
    обе категории не закрыты, обе надо доскорить.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        items: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("id"):
                    items.append(rec)
        return items

    def save(self, vacancies: Iterable[Mapping[str, Any]]) -> int:
        rows = [
            {k: v for k, v in vacancy.items() if k in DEFERRED_KEEP_FIELDS}
            for vacancy in vacancies
            if vacancy.get("id")
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        _replace_file(tmp, self.path)
        return len(rows)


class ParkedStore:
    """Готовые к пуску вакансии из запланированных прогонов.

    `sne --mode park` не пускает и не отказывает: ready-кандидат
    (вакансия + скор + письмо) ложится сюда целиком и ждёт решения
    человека — `snpr approve`. В seen не пишем: вакансия не закрыта,
    потерять нельзя.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def ids(self) -> set[str]:
        return {str(rec["vacancy"].get("id")) for rec in self.load_records()}

    def load_records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                vacancy = rec.get("vacancy") if isinstance(rec, dict) else None
                if isinstance(vacancy, dict) and vacancy.get("id"):
                    records.append(rec)
        return records

    def append_record(
        self,
        vacancy: Mapping[str, Any],
        score: Any = None,
        letter: str = "",
    ) -> None:
        score_snapshot = None
        if score is not None:
            score_snapshot = {
                "score": score.score,
                "verdict": score.verdict,
                "reasons": list(score.reasons),
                "resume_id": str(getattr(score, "resume_id", "") or ""),
                "requests": list(getattr(score, "requests", []) or []),
                "matched": list(getattr(score, "matched", []) or []),
                "missing": list(getattr(score, "missing", []) or []),
            }
        record = {
            "vacancy": dict(vacancy),
            "score": score_snapshot,
            "letter": str(letter or ""),
            "ts": int(datetime.now(timezone.utc).timestamp()),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def update_letter(self, vid: str, letter: str) -> bool:
        """Заменить письмо у вакансии в очереди (кнопка «Переписать»)."""
        records = self.load_records()
        found = False
        for rec in records:
            if str(rec["vacancy"].get("id")) == str(vid):
                rec["letter"] = letter
                found = True
        if found:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                for rec in records:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            _replace_file(tmp, self.path)
        return found

    def remove(self, vids: Iterable[str]) -> int:
        """Убрать закрытые (пуск/отказ) — парковка живёт до решения."""
        drop = {str(v) for v in vids}
        records = [
            rec
            for rec in self.load_records()
            if str(rec["vacancy"].get("id")) not in drop
        ]
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _replace_file(tmp, self.path)
        return len(drop)


class ScoresStore:
    """Журнал чисел скоринга: каждая проскоренная вакансия — строка.

    seen.jsonl хранит только вердикт-статус (pass/ready), числа в нём
    нет. Аудит «какие скоры вообще бывают» и калибровка порогов
    (maybe/target/автопилот) — по этому журналу, append-only.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def append_record(
        self, vacancy: Mapping[str, Any], score: Any, ts: int | None = None
    ) -> None:
        record = {
            "vacancy_id": str(vacancy.get("id") or ""),
            "name": vacancy.get("name", ""),
            "employer": (vacancy.get("employer") or {}).get("name", ""),
            "score": int(score.score),
            "verdict": str(score.verdict),
            "reasons": list(score.reasons)[:3],
            "ts": int(ts if ts is not None else datetime.now(timezone.utc).timestamp()),
        }
        # поля для списка вакансий (`snpr list`): ссылка, вилка, гео, формат
        from .report import vacancy_fields

        record.update(vacancy_fields(vacancy))
        record["matched"] = list(getattr(score, "matched", []) or [])[:5]
        record["missing"] = list(getattr(score, "missing", []) or [])[:5]
        record["resume_id"] = str(getattr(score, "resume_id", "") or "")
        record["requests"] = list(getattr(score, "requests", []) or [])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("vacancy_id"):
                    records.append(rec)
        return records

    def ids(self) -> set[str]:
        return {str(rec.get("vacancy_id")) for rec in self.load()}


def record_scores(
    scores_store: ScoresStore, candidates: Iterable[Any], ts: int | None = None
) -> int:
    """Записать в журнал все проскоренные кандидаты прогона.

    Пишется на моменте скоринга (не закрытия): pass и parked фиксируются
    с числами, deferred/error без скора не пишутся. Повторный скоринг
    той же вакансии (после очереди) — новая строка: дрейф скора виден.
    """
    count = 0
    for candidate in candidates:
        if candidate.score is None or not candidate.vacancy_id:
            continue
        scores_store.append_record(candidate.vacancy, candidate.score, ts=ts)
        count += 1
    return count


class HHVacancyProvider:
    """Поиск новых вакансий через api-клиент базы.

    Отдаёт короткие items из выдачи (/vacancies), а не полные карточки:
    дешёвые гейты (гео/вилка/грейд/вендор по работодателю) работают по полям
    выдачи — их там хватает (name, employer, salary, area, work_format,
    experience). Полная карточка нужна только выжившим, её берёт ленивый
    VacancyCardCache — на пул в сотни вакансий это десятки GET вместо сотен.
    """

    def __init__(
        self,
        api_client: Any,
        per_page: int = 50,
        extra_params: Mapping[str, Any] | None = None,
        scopes: list[Mapping[str, Any]] | None = None,
    ):
        """scopes — отдельные поиски поверх extra_params (config
        [[search.scopes]]): например «удалёнка по СНГ» и «Ташкент любой
        формат». Каждый запрос гоняется в каждом scope; нет scopes — один
        поиск с extra_params."""
        self.api_client = api_client
        self.per_page = per_page
        self.extra_params = dict(extra_params or {})
        self.scopes: list[dict[str, Any]] = [
            {**self.extra_params, **dict(scope)} for scope in (scopes or [])
        ] or [self.extra_params]

    def search_new(
        self,
        queries: list[str],
        hours: float,
        known_ids: set[str],
        limit: int = 0,
        professional_roles: list[str] | None = None,
        similar_resume_ids: list[str] | None = None,
        until_hours: float = 0,
    ) -> list[dict[str, Any]]:
        """Короткие items новых вакансий; дедуп по known_ids и внутри прогона.

        Источники ([search.sources] в конфиге), каждый в каждом scope:
        ключевые слова (queries), специализации hh (professional_roles —
        без текста запроса) и «похожие на резюме» hh (similar_resume_ids,
        GET /resumes/{id}/similar_vacancies). Страницы обходятся round-robin
        по потокам (иначе первый съедает весь потолок), свежее первым.
        """
        collected: list[dict[str, Any]] = []
        seen_now: set[str] = set(known_ids)

        def collect(items: Any) -> bool:
            """Добавить новые items; False — потолок достигнут."""
            for short in items if isinstance(items, list) else []:
                vid = str(short.get("id") or "")
                if not vid or vid in seen_now:
                    continue
                seen_now.add(vid)
                collected.append(short)
                if limit > 0 and len(collected) >= limit:
                    return False
            return True

        # поток = (endpoint, функция параметров страницы, «мягкий»):
        # мягкий поток при ошибке hh пропускается, прогон не падает
        streams: list[tuple[str, Any, bool]] = []
        for scope in self.scopes:
            for query in queries:
                streams.append((
                    "/vacancies",
                    lambda page, q=query, sc=scope: build_search_params(
                        q, hours, self.per_page, page, extra=sc,
                        until_hours=until_hours,
                    ),
                    False,
                ))
            # без текста запроса search_field не нужен
            textless = {k: v for k, v in scope.items() if k != "search_field"}
            if professional_roles:
                streams.append((
                    "/vacancies",
                    lambda page, sc=textless: _window_params(
                        hours, self.per_page, page,
                        {**sc, "professional_role": list(professional_roles)},
                        until_hours=until_hours,
                    ),
                    True,
                ))
            for resume_id in similar_resume_ids or []:
                streams.append((
                    f"/resumes/{resume_id}/similar_vacancies",
                    lambda page, sc=textless: _window_params(
                        hours, self.per_page, page, sc, until_hours=until_hours
                    ),
                    True,
                ))

        def fetch(idx: int, page: int) -> Any:
            endpoint, params, soft = streams[idx]
            for attempt in range(3):
                try:
                    return self.api_client.get(endpoint, params(page))
                except Exception as ex:  # noqa: BLE001
                    # gateway_timeout и прочие 5xx у hh бывают разовыми
                    retryable = (
                        "ServerError" in type(ex).__name__ or "timeout" in str(ex).lower()
                    )
                    if retryable and attempt < 2:
                        time.sleep(2 * (attempt + 1))
                        continue
                    if not soft:
                        print(f"[поиск] {endpoint} {params(page)} — {ex}")
                        raise
                    print(f"[поиск] источник {endpoint} пропущен: {ex}")
                    return None
            return None

        pages_left: dict[int, int] = {}
        for idx in range(len(streams)):
            res = fetch(idx, 0)
            if not isinstance(res, dict):
                pages_left[idx] = 1
                continue
            pages_left[idx] = max(int(res.get("pages") or 1), 1)
            if not collect(res.get("items", [])):
                break

        page = 1
        while limit <= 0 or len(collected) < limit:
            progressed = False
            for idx in range(len(streams)):
                if page >= pages_left.get(idx, 0):
                    continue
                res = fetch(idx, page)
                if not isinstance(res, dict):
                    continue
                progressed = True
                if not collect(res.get("items", [])):
                    break
            if not progressed:
                break
            page += 1

        return _sort_by_published_at(collected)


class VacancyCardCache:
    """Ленивая полная карточка: description нужен только выжившим гейтов."""

    def __init__(self, api_client: Any, pause: float = 0.0):
        self.api_client = api_client
        self.cards: dict[str, dict[str, Any]] = {}
        self.calls = 0
        # пауза между запросами карточек: подряд без пауз hh через
        # несколько десятков карточек требует капчу
        self.pause = pause
        self._last = 0.0

    def description(self, vacancy: Mapping[str, Any]) -> str:
        vid = str(vacancy.get("id") or "")
        card = self.cards.get(vid)
        if card is None:
            self.calls += 1
            wait = self.pause - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            fetched = self.api_client.get(f"/vacancies/{vid}")
            card = fetched if isinstance(fetched, dict) else {}
            self.cards[vid] = card
        # короткий item из выдачи дополняем полями карточки для промпта
        if isinstance(vacancy, dict):
            for field in ("key_skills", "branding"):
                if card.get(field):
                    vacancy[field] = card[field]
        return str(card.get("description") or "")


class HHNegotiationSender:
    """Реальный пуск: POST /negotiations (резюме + письмо)."""

    def __init__(self, api_client: Any, resume_id: str):
        self.api_client = api_client
        self.resume_id = resume_id

    def send(
        self, vacancy_id: str, letter: str, resume_id: str | None = None
    ) -> None:
        """resume_id — резюме, выбранное для этой вакансии (иначе основное)."""
        res = self.api_client.post(
            "/negotiations",
            {
                "resume_id": resume_id or self.resume_id,
                "vacancy_id": vacancy_id,
                "message": letter,
            },
            delay=random.uniform(1, 3),
        )
        if res != {}:
            raise RuntimeError(f"неожиданный ответ /negotiations: {res!r:.200}")


RETRY_STATUSES = {"error", "deferred"}


def mark_seen(seen_store: SeenStore, candidates: Iterable[Any]) -> int:
    """Пометить обработанные вакансии; незакрытые — нет.

    Незакрытые: error/deferred (вернутся очередью) и parked (ждут
    решения человека в data/parked.jsonl). Иначе вакансия навсегда
    выпадет из выдачи.
    """
    marked = 0
    for candidate in candidates:
        if candidate.status in RETRY_STATUSES or candidate.status == "parked":
            continue
        seen_store.mark_seen(
            candidate.vacancy_id,
            name=str(candidate.vacancy.get("name") or ""),
            status=candidate.status,
        )
        marked += 1
    return marked


def requeue_candidates(
    deferred_store: DeferredStore, candidates: Iterable[Any]
) -> int:
    """Вернуть незакрытые вакансии в очередь; закрытые из неё убрать.

    Кейс из живого прогона 17.10: LLM лежал, вакансия из очереди получила
    статус error, из очереди выпала — и жила только до выхода из 24-часового
    окна поиска. Дальше — потеря навсегда. Теперь очередь — страховка для
    обеих категорий: не хватило бюджета или скоринг упал.
    """
    return deferred_store.save(
        candidate.vacancy
        for candidate in candidates
        if candidate.status in RETRY_STATUSES
    )


def parse_vacancy_ref(ref: str) -> str:
    """ID вакансии из аргумента: голый ID или ссылка hh.ru/vacancy/<id>."""
    text = str(ref or "").strip()
    if not text:
        return ""
    if "/" in text:
        text = text.rstrip("/").rsplit("/", 1)[-1]
    text = text.split("?")[0].split("#")[0]
    return "".join(ch for ch in text if ch.isdigit())


def _seen_record(path: Path, vid: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(rec.get("vacancy_id", "")) == vid:
                return rec
    return None


def _search_coverage(
    api: Any,
    queries: list[str],
    vid: str,
    published_at: str,
    per_page: int = 100,
    max_pages: int = 5,
    scopes: list[Mapping[str, Any]] | None = None,
) -> list[str] | None:
    """Какие из запросов находят вакансию (None — не проверялось).

    Окно — от публикации вакансии (минус час), обход свежие-первыми,
    потолок max_pages на запрос: диагностика не должна гонять сотни вызовов.
    """
    try:
        published = datetime.fromisoformat(str(published_at or ""))
    except ValueError:
        return None
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    date_from = (published - timedelta(hours=1)).isoformat(timespec="seconds")
    floor = published - timedelta(hours=1)
    found_by: list[str] = []
    # те же фильтры, что у живого поиска (области, опыт, поиск по названию)
    streams = [(q, dict(sc)) for q in queries for sc in (scopes or [{}])]
    for query, scope in streams:
        if query in found_by:
            continue
        for page in range(max_pages):
            res = api.get(
                "/vacancies",
                {
                    **{k: v for k, v in scope.items() if v not in ("", [])},
                    "text": query,
                    "page": page,
                    "per_page": per_page,
                    "order_by": "publication_time",
                    "date_from": date_from,
                },
            )
            if not isinstance(res, dict):
                break
            items = res.get("items", [])
            if not items:
                break
            if any(str(item.get("id", "")) == vid for item in items):
                found_by.append(query)
                break
            try:
                oldest = datetime.fromisoformat(
                    str(items[-1].get("published_at") or "")
                )
            except ValueError:
                continue
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=timezone.utc)
            if oldest < floor:
                break
    return found_by


def _bot_started_at(data_dir: Path) -> float | None:
    """Когда бот впервые что-то оценил (самая ранняя запись scores.jsonl)."""
    path = Path(data_dir) / "scores.jsonl"
    if not path.exists():
        return None
    first = None
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                ts = float(json.loads(line).get("ts") or 0)
            except (ValueError, json.JSONDecodeError):
                continue
            if ts and (first is None or ts < first):
                first = ts
    return first


def explain_vacancy(
    api: Any,
    config: Mapping[str, Any],
    vid: str,
    seen_path: Path,
    applied_path: Path,
    deferred_path: Path,
    queries: list[str],
) -> list[str]:
    """Полный разбор воронки для одной вакансии: где выпала и почему.

    Точки потери по порядку: поисковые запросы → дедуп сторов → гейты →
    вердикт скоринга (pass < maybe < target — письмо только с maybe).
    Только чтение: диагностика ничего не пишет в сторы.
    """
    from .filters import PipelineFilter

    lines: list[str] = []
    card = api.get(f"/vacancies/{vid}")
    if isinstance(card, dict) and card.get("id"):
        name = str(card.get("name") or "")
        employer = str((card.get("employer") or {}).get("name") or "")
        lines.append(f"Вакансия {vid} — {name} — {employer}")
        lines.append(f"Опубликовано: {card.get('published_at') or '?'}")
        lines.append(f"Ссылка: https://hh.ru/vacancy/{vid}")
    else:
        card = None
        lines.append(f"Вакансия {vid}: карточка недоступна (закрыта/снята?)")
    lines.append("")

    seen_rec = _seen_record(Path(seen_path), vid)
    applied_ids = AppliedStore(Path(applied_path)).vacancy_ids()
    queued_ids = {
        str(item.get("id", ""))
        for item in DeferredStore(Path(deferred_path)).load()
    }
    lines.append("Сторы:")
    if seen_rec:
        ts = int(seen_rec.get("ts") or 0)
        when = (
            datetime.fromtimestamp(ts).astimezone().strftime("%d.%m %H:%M")
            if ts
            else "?"
        )
        lines.append(
            f"  seen:    да — {when}, статус {seen_rec.get('status', '?')}"
        )
    else:
        lines.append("  seen:    нет — конвейер её не завершал")
    lines.append(
        f"  applied: {'да — пуск уже был' if vid in applied_ids else 'нет'}"
    )
    lines.append(
        "  очередь: "
        + ("да — доскорим в следующем прогоне" if vid in queued_ids else "нет")
    )
    lines.append("")

    gates_line = ""
    if card is not None:
        description = str(card.get("description") or "")
        verdict = PipelineFilter(config).check(card, description=description)
        lines.append("Гейты по свежей карточке:")
        for rule in verdict.rules:
            mark = "OK  " if rule.passed else "FAIL"
            lines.append(f"  {rule.rule:7} {mark} {rule.reason}")
        failed = sorted({r.rule for r in verdict.rules if not r.passed})
        gates_line = (
            "гейты забивают (" + ", ".join(failed) + ")"
            if failed
            else "гейты пропускают"
        )
        lines.append(f"  → {gates_line}")
        lines.append("")

    lines.append("Вердикт:")
    if vid in applied_ids:
        lines.append("  уже отозвались (applied) — повторного пуска не будет")
    elif seen_rec:
        status = str(seen_rec.get("status") or "?")
        note = {
            "filtered_out": "гейты забили её в том прогоне",
            "pass": "скоринг дал pass — ниже порога письма (maybe)",
            "error": "скоринг упал — вернётся из очереди",
        }.get(status, "")
        if note:
            lines.append(f"  конвейер уже обработал её ({status}): {note}")
        if gates_line:
            lines.append(f"  сейчас по карточке: {gates_line}")
        lines.append(
            "  дедуп не даст ей вернуться в пул; если считаете таргетом — "
            "это спор гейтов/скоринга, а не потери"
        )
    elif vid in queued_ids:
        lines.append("  в очереди deferred — доскорим в следующих прогонах")
    elif card is None:
        lines.append("  карточка недоступна и в сторах её нет — до бота не дошла")
    else:
        search_cfg = config.get("search", {})
        base = dict(search_cfg.get("params", {}) or {})
        scopes = [
            {**base, **dict(sc.get("params", {}))}
            for sc in search_cfg.get("scopes", []) or []
            if isinstance(sc, Mapping)
        ] or [base]
        coverage = _search_coverage(
            api, queries, vid, str(card.get("published_at") or ""), scopes=scopes
        )
        started = _bot_started_at(seen_path.parent)
        try:
            published = datetime.fromisoformat(str(card.get("published_at") or ""))
        except ValueError:
            published = None
        if started and published and published.timestamp() < started - 3600:
            lines.append(
                "  опубликована "
                + published.strftime("%d.%m.%Y")
                + ", а бот ищет с "
                + datetime.fromtimestamp(started).strftime("%d.%m.%Y")
                + " и смотрит только новые вакансии — в окно поиска не попала"
            )
        if "забивают" in gates_line:
            lines.append(f"  в сторах нет, но {gates_line} — упала бы на фильтрах")
        elif coverage is None:
            lines.append("  в сторах нет, гейты пропускают; дата публикации "
                         "не разобрана — покрытие поиска не проверено")
        elif coverage:
            lines.append(
                f"  в сторах нет, гейты пропускают, поиск находит "
                f"(запросы: {', '.join(coverage)}) — должна была попасть в "
                "пул: не хватило потолка max_new_per_run или прогон не "
                "случился в её окно публикации"
            )
        else:
            lines.append(
                f"  ни один из {len(queries)} запросов поиска её не находит — "
                "в потеря ДО бота: text-поиск hh не матчит, нужно расширять "
                "[search].queries"
            )
    return lines


def run_why(args: argparse.Namespace) -> int:
    from .cli import (
        DEFAULT_APPLIED_STORE,
        DEFAULT_DEFERRED_STORE,
        DEFAULT_SEEN_STORE,
        _load_toml,
    )
    from .hh_tool import SavingHHApplicantTool

    vid = parse_vacancy_ref(getattr(args, "vacancy", ""))
    if not vid:
        print(
            "Не разобрать ID вакансии: передайте ID или ссылку "
            "https://hh.ru/vacancy/<id>",
            file=sys.stderr,
        )
        return 2
    config = _load_toml(args.config)
    search_cfg = config.get("search", {})
    queries = (
        list(args.queries)
        if args.queries
        else list(search_cfg.get("queries", DEFAULT_QUERIES))
    )
    tool = SavingHHApplicantTool()
    lines = explain_vacancy(
        tool.api_client,
        config,
        vid,
        Path(args.seen_store),
        Path(args.applied_store),
        Path(args.deferred_store),
        queries,
    )
    print("\n".join(lines))
    return 0


def _config_resume_id(config: Mapping[str, Any]) -> str:
    """Принудительное резюме из [send].resume_id (пусто — выбор по [[resumes]])."""
    return str(config.get("send", {}).get("resume_id") or "").strip()


def default_resume_id(
    api_client: Any, config: Mapping[str, Any], explicit: str = ""
) -> str:
    """Основное резюме: флаг/[send].resume_id → первое опубликованное из
    [[resumes]] → первое на аккаунте."""
    forced = explicit or _config_resume_id(config)
    if forced:
        return forced
    usable = usable_resumes(api_client, config)
    if usable:
        return str(usable[0]["id"])
    return pick_resume_id(api_client, "")


def account_resumes(api_client: Any) -> list[dict[str, Any]]:
    """Резюме на аккаунте hh: id, title, published (да/нет)."""
    try:
        res = api_client.get("/resumes/mine")
    except Exception:  # noqa: BLE001 — без списка работаем с основным
        return []
    items = res.get("items", []) if isinstance(res, dict) else []
    return [
        {
            "id": str(item.get("id")),
            "title": str(item.get("title") or ""),
            "published": str((item.get("status") or {}).get("id") or "published")
            == "published",
        }
        for item in items
        if item.get("id")
    ]


def usable_resumes(api_client: Any, config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """[[resumes]] из конфига, которые опубликованы на hh (с неопубликованным
    отклик не пройдёт). Название берём с hh, если в конфиге пусто."""
    from .cli import _config_resumes

    configured = _config_resumes(config)
    if not configured:
        return []
    on_hh = {r["id"]: r for r in account_resumes(api_client)}
    if not on_hh:
        return configured  # hh не ответил — доверяем конфигу
    usable = []
    for item in configured:
        hh = on_hh.get(item["id"])
        if hh and hh["published"]:
            usable.append({**item, "title": item["title"] or hh["title"]})
        elif hh is None:
            print(f"[резюме] {item['id']} из конфига не найдено на аккаунте — пропускаю")
        else:
            print(f"[резюме] «{hh['title']}» не опубликовано — для откликов не предлагаю")
    return usable


def pick_resume_id(api_client: Any, preferred: str = "") -> str:
    """ID резюме для отклика: явно заданное или первое на аккаунте."""
    if preferred:
        return preferred
    res = api_client.get("/resumes/mine")
    items = res.get("items", []) if isinstance(res, dict) else []
    for item in items:
        if item.get("id"):
            return str(item["id"])
    raise RuntimeError(
        "На аккаунте не найдено резюме — POST /negotiations невозможен"
    )


def run_sne(args: argparse.Namespace) -> int:
    """Обёртка: лок против параллельных прогонов (каденция 5 мин).

    Task Scheduler тики поверх работающего прогона пропускает сам
    (IgnoreNew); ручной запуск поверх джобы ловим лок-файлом.
    """
    lock_path = Path(args.seen_store).parent / "sne.lock"
    lock = _acquire_run_lock(lock_path)
    if lock is None:
        print(
            "Другой прогон sne ещё работает — такт пропущен "
            "(окно адаптивное, ничего не теряем)."
        )
        return 0
    try:
        return _run_sne_locked(args)
    finally:
        _release_run_lock(lock_path, lock)


def _add_notes(vacancy: Mapping[str, Any], description: str) -> None:
    """Пометки «ℹ️» на карточку (только из России, тестовое) — скор не меняют."""
    from .notes import notes
    from .scoring import _strip_html

    found = notes(f"{vacancy.get('name') or ''}\n{_strip_html(description or '')}")
    if found and isinstance(vacancy, dict):
        vacancy["notes"] = found


def _run_sne_locked(args: argparse.Namespace) -> int:
    from .filters import PipelineFilter
    from .hh_tool import SavingHHApplicantTool

    from .cli import (
        DEFAULT_DEFERRED_STORE,
        DEFAULT_PARKED_STORE,
        DEFAULT_SCORES_STORE,
        _build_letter_writer,
        _build_scorers,
        _load_toml,
        _make_approver,
        _make_guard,
        _scoring_profile,
        send_candidates,
        print_summary,
        report_candidates,
    )

    config = _load_toml(args.config)
    search_cfg = config.get("search", {})
    scoring_cfg = config.get("scoring", {})
    marker = Path(args.seen_store).parent / "last_run.txt"
    hours = resolve_search_hours(args.hours, search_cfg, marker)
    until_hours = 0.0
    age = resolve_age_window(
        search_cfg,
        getattr(args, "min_age_days", -1),
        getattr(args, "max_age_days", -1),
    )
    if age and args.hours <= 0:
        hours, until_hours = age
    queries = (
        list(args.queries)
        if args.queries
        else list(search_cfg.get("queries", DEFAULT_QUERIES))
    )
    per_page = int(search_cfg.get("per_page", 50))
    limit = (
        args.limit
        if args.limit > 0
        else int(search_cfg.get("max_new_per_run", 200))
    )
    score_budget = (
        args.max_scored
        if args.max_scored > 0
        else int(search_cfg.get("max_scored_per_run", 0))
    )
    workers = (
        args.scoring_workers
        if args.scoring_workers > 0
        else int(scoring_cfg.get("workers", 1))
    )
    sources_cfg = search_cfg.get("sources", {}) or {}
    if not queries and not (
        sources_cfg.get("professional_roles") or sources_cfg.get("similar_to_resumes")
    ):
        print("Нет источников поиска: config [search].queries пуст", file=sys.stderr)
        return 2

    tool = SavingHHApplicantTool()
    api = tool.api_client

    # 18.10: автоподнятие резюме — тихий хук по правилам [raise] (окно
    # 9-19 МСК, интервал 4 ч). Сбой поднятия не роняет прогон снпрера.
    if not args.dry_run:
        from .resume_raise import maybe_auto_raise

        try:
            maybe_auto_raise(config, api, marker.parent, dry_run=False)
        except Exception as ex:
            print(f"[raise] хук не сработал, прогон продолжается: {ex}")

    seen_store = SeenStore(Path(args.seen_store))
    applied_store = AppliedStore(Path(args.applied_store))
    deferred_store = DeferredStore(Path(args.deferred_store))
    parked_store = ParkedStore(
        Path(getattr(args, "parked_store", DEFAULT_PARKED_STORE))
    )
    scores_store = ScoresStore(
        Path(getattr(args, "scores_store", DEFAULT_SCORES_STORE))
    )
    from .areas import ensure_areas

    ensure_areas(api)  # страна вакансии в списке (кэш, качается один раз)

    # отклики с аккаунта hh, включая сделанные вручную на сайте
    from .applied_sync import sync_hh_applied

    hh_applied = sync_hh_applied(
        api, Path(args.seen_store).parent, save=not args.dry_run
    )
    if not args.dry_run:
        from .applied_sync import load_hh_applied_keys
        from .dedupe import vacancy_key

        applied_keys = load_hh_applied_keys(Path(args.seen_store).parent)
        dropped = parked_store.remove(
            (parked_store.ids() & hh_applied)
            | {
                str(rec["vacancy"].get("id"))
                for rec in parked_store.load_records()
                if vacancy_key(rec["vacancy"]) in applied_keys
            }
        )
        if dropped:
            print(f"Из парковки убрано {dropped}: на них уже есть отклик на hh.")
        from .dedupe import dedupe_parked

        if merged := dedupe_parked(parked_store, seen_store):
            print(f"Из парковки убрано дублей: {merged} (оставлен лучший скор).")
        # вакансии, ушедшие в архив: маленькая порция за прогон — карточки
        # hh отдаёт ограниченно (капча), они нужнее для оценки новых;
        # остальное доделывает открытая страница отбора
        from .approve_service import sweep_archived

        if archived := sweep_archived(
            api, parked_store, seen_store, Path(args.seen_store).parent,
            budget=10, pause=2.0,
        ):
            print(f"Из парковки убрано {archived}: вакансии в архиве на hh.")
    known_ids = (
        seen_store.seen_ids()
        | applied_store.vacancy_ids()
        | parked_store.ids()
        | hh_applied
    )
    print(f"Откликов на аккаунте hh: {len(hh_applied)} — эти вакансии пропускаются.")

    print(
        f"Поиск: {len(queries)} запросов"
        + (f" × {len(search_cfg.get('scopes') or [])} области" if search_cfg.get("scopes") else "")
        + (
            f", вышли {until_hours / 24:g}–{hours / 24:g} дн. назад"
            if until_hours or age
            else f", глубина {hours:.1f} ч"
        )
        + f", пул {limit}"
        + (f", бюджет скоринга {score_budget}" if score_budget else ", скоринг без потолка")
    )
    scopes = [
        dict(scope.get("params", {}))
        for scope in search_cfg.get("scopes", []) or []
        if isinstance(scope, Mapping)
    ]
    provider = HHVacancyProvider(
        api,
        per_page=per_page,
        extra_params=search_cfg.get("params", {}),
        scopes=scopes,
    )
    sources = search_cfg.get("sources", {}) or {}
    roles = [str(r) for r in sources.get("professional_roles", []) or []]
    similar_ids = (
        [str(r["id"]) for r in usable_resumes(api, config)]
        if sources.get("similar_to_resumes")
        else []
    )
    if not sources.get("keywords", True):
        queries = []
    print(
        "Источники: "
        + ", ".join(
            part
            for part in (
                f"ключевые слова ({len(queries)})" if queries else "",
                f"специализации ({len(roles)})" if roles else "",
                f"похожие на резюме ({len(similar_ids)})" if similar_ids else "",
            )
            if part
        )
    )
    from .source_log import LOG_NAME, SourceLog

    source_log = SourceLog(Path(args.seen_store).parent / LOG_NAME)
    only = str(getattr(args, "only_source", "") or "").strip()
    if only:
        # только один внешний источник (например, Авито из входящих): hh не ищем
        print(f"Только источник {only}: поиск hh пропущен.")
        fresh = []
        sources = {
            k: v for k, v in sources.items() if not isinstance(v, bool)
        } | {only: True}
    else:
        try:
            fresh = provider.search_new(
                queries,
                hours,
                known_ids,
                limit,
                professional_roles=roles,
                similar_resume_ids=similar_ids,
                until_hours=until_hours,
            )
        except Exception as ex:
            source_log.error("hh", ex)
            raise
        source_log.ok("hh", len(fresh))
    # внешние источники (GeekJob, Habr Career, Telegram): то же окно, только новые
    from .sources import ExternalSources

    external = ExternalSources(
        sources,
        experience=(search_cfg.get("params") or {}).get("experience"),
        log=source_log,
        data_dir=Path(args.seen_store).parent,
    )
    prefetched: dict[str, dict[str, Any]] = {}
    if external.active:
        extra = external.collect(hours / 24, known_ids, fresh)
        # пост ведёт на вакансию hh — берём саму вакансию: отклик из бота
        from_hh, extra, prefetched = hh_from_posts(api, extra, known_ids, fresh)
        if from_hh:
            print(f"Telegram: посты со ссылкой на вакансию hh → вакансии hh: {len(from_hh)}")
        fresh = list(fresh) + from_hh + extra

    # очередь с прошлых прогонов: незакрытые вакансии могли выпасть из окна
    # date_from, поэтому возвращаем их в пул (свежие приоритетнее — сортировка)
    from .sources import source_of

    queued = [
        item
        for item in deferred_store.load()
        if str(item.get("id") or "") not in known_ids
        and (not only or source_of(item) == only)
    ]
    by_id: dict[str, dict[str, Any]] = {
        str(item.get("id")): item for item in fresh if item.get("id")
    }
    for item in queued:
        vid = str(item.get("id"))
        if vid and vid not in by_id:
            by_id[vid] = item
    # дубли: та же вакансия, опубликованная работодателем повторно (то же
    # название + работодатель) — и между собой, и с уже виденными
    from .dedupe import known_keys, split_duplicates

    pool, dupes = split_duplicates(
        _sort_by_published_at(list(by_id.values())),
        known_keys(Path(args.seen_store).parent),
    )
    if dupes:
        print(f"Дублей (то же название у того же работодателя) пропущено: {len(dupes)}")
        if not args.dry_run:
            for dup in dupes:
                seen_store.mark_seen(
                    str(dup.get("id") or ""),
                    name=str(dup.get("name") or ""),
                    status="duplicate",
                )
    new_vacancies = pool[:limit]

    print(
        f"Новых вакансий после дедупа: {len(new_vacancies)} "
        f"(из поиска {len(fresh)}, из очереди {len(queued)})"
    )
    if not new_vacancies:
        if not args.dry_run and not only:
            # пустой такт тоже успешен: сжимаем окно до floor
            _touch_marker(marker)
        return 0

    try:
        resume_id = default_resume_id(api, config, args.resume_id)
    except RuntimeError as ex:
        print(f"Ошибка: {ex}", file=sys.stderr)
        return 2
    print(f"Резюме для пуска: {resume_id}")

    scorers = _build_scorers(
        args,
        workers,
        maybe_threshold=int(scoring_cfg.get("maybe_min", 50)),
        target_threshold=int(scoring_cfg.get("target_min", 75)),
        profile=_scoring_profile(config),
        resumes=usable_resumes(api, config),
    )
    pipeline = SnePipeline(
        PipelineFilter(config),
        scorers=scorers,
        # по писателю на поток: письма пишутся параллельно с оценкой
        writers=[
            w for w in (_build_letter_writer(args, config) for _ in range(max(1, workers)))
            if w is not None
        ],
        letter_min_verdict=str(scoring_cfg.get("letter_min_verdict", "maybe")),
        score_budget=score_budget,
    )
    cards = VacancyCardCache(api, pause=float(search_cfg.get("card_pause_seconds", 0) or 0))
    cards.cards.update(prefetched)  # вакансии hh из постов: карточка уже есть
    from .pipeline import PipelineAbort

    def describe(vacancy: Mapping[str, Any]) -> str:
        """Описание из источника вакансии: внешний сайт или карточка hh."""
        from .pipeline import VacancyGone
        from .sources import is_external, source_of

        if not is_external(vacancy):
            text = cards.description(vacancy)
        else:
            try:
                text = external.description(vacancy)
            except Exception as ex:  # noqa: BLE001 — сбой внешнего сайта не держит hh
                raise VacancyGone(f"{source_of(vacancy)}: {ex}") from ex
        _add_notes(vacancy, text)
        return text

    def _progress(done: int, total: int) -> None:
        """Прогресс скоринга: каждая 10-я завершённая попытка и финал."""
        if done % 10 and done != total:
            return
        suffix = f"/{total}" if total > 0 else ""
        print(f"  скоринг: {done}{suffix}")

    try:
        candidates = pipeline.run(
            new_vacancies,
            descriptions=describe,
            on_progress=_progress,
        )
    except PipelineAbort as ex:
        print(f"Прогон прерван: {ex}", file=sys.stderr)
        return 3
    if pipeline.abort_reason:
        print(
            f"ОЦЕНКА ОСТАНОВЛЕНА: {pipeline.abort_reason}\n"
            "Оценённое сохраняется, остальное — в отложенных до следующего запуска.",
            file=sys.stderr,
        )
    stats = report_candidates(candidates)
    if cards.calls:
        print(f"Полных карточек забрано: {cards.calls} (только для выживших)")

    sender = None if args.dry_run else HHNegotiationSender(api, resume_id)
    from .approve import CLIApprover

    sent, forced_human, parked = send_candidates(
        candidates,
        approver=_make_approver(
            args,
            auto_park_threshold=int(scoring_cfg.get("auto_approve_min", 0)),
            max_age_hours=_send_max_age_hours(config.get("send", {})),
        ),
        guard=_make_guard(config),
        manual=CLIApprover(),
        applied_store=applied_store,
        dry_run=args.dry_run,
        sender=sender,
        parked_store=parked_store,
    )

    if args.dry_run:
        from .report import rows_from_candidates, write_html_report

        report_path = Path(args.seen_store).parent / "dry_run_vacancies.html"
        write_html_report(
            rows_from_candidates(candidates),
            report_path,
            title="Пробный прогон (dry-run): оценённые вакансии",
        )
        print(f"Отчёт пробного прогона: {report_path}")
    else:
        recorded = record_scores(scores_store, candidates)
        queued_back = requeue_candidates(deferred_store, candidates)
        mark_seen(seen_store, candidates)
        if recorded:
            print(f"Скоры записаны в журнал: {recorded} ({scores_store.path})")
        if queued_back:
            print(
                f"В очереди {queued_back} вакансий (не хватило бюджета скоринга "
                "или упал LLM): в seen не помечены, вернёмся в следующих прогонах."
            )
        if parked:
            print(
                f"В ПАРКОВКЕ {parked} готовых к пуску: решение за человеком — "
                "`python -m snpr approve` (y/n/e по каждой)."
            )
        tool._persist()
        if not only:  # окно поиска hh считается от прогонов с hh
            _touch_marker(marker)

    print()
    print_summary(len(candidates), stats, sent, forced_human, args.dry_run)
    return 0


APPROVE_LOCK_NAME = "approve.lock"
# Окно approve живёт сколько угодно (человек думает); упавший процесс
# ловится по pid, возраст — только последняя страховка.
APPROVE_LOCK_STALE = 7 * 24 * 3600.0


def approve_window_open(data_dir: Path | None = None) -> bool:
    """Открыто ли окно approve (живой держатель data/approve.lock).

    Джоба спрашивает это перед тем, как открыть новое окно: заголовок
    окна в Windows Terminal не виден tasklist, поэтому проверяем лок.
    """
    from .cli import DEFAULT_SEEN_STORE

    path = (data_dir or Path(DEFAULT_SEEN_STORE).parent) / APPROVE_LOCK_NAME
    if not path.exists():
        return False
    return _lock_holder_alive(path) is True


def run_approve(args: argparse.Namespace) -> int:
    """Одно окно approve за раз: второе сразу выходит (иначе два окна
    могут отправить отклик на одну вакансию дважды)."""
    lock_path = Path(args.seen_store).parent / APPROVE_LOCK_NAME
    lock = _acquire_run_lock(lock_path, stale_after=APPROVE_LOCK_STALE)
    if lock is None:
        print("Окно approve уже открыто — решайте в нём.")
        return 0
    try:
        return _run_approve_locked(args)
    finally:
        _release_run_lock(lock_path, lock)


def _run_approve_locked(args: argparse.Namespace) -> int:
    """Консольное решение по очереди: карточка → y/n/e/q.

    Логика решений общая со страницей `snpr web` (ApproveService):
    y — отклик, n — закрыть, e — правка письма, q/EOF/Ctrl+C — выйти,
    остальное ждёт в очереди. Каждое решение сохраняется сразу.
    """
    from .hh_tool import SavingHHApplicantTool

    from .approve import _format_card, edit_text_in_editor
    from .approve_service import ApproveService, score_of
    from .cli import DEFAULT_PARKED_STORE, _load_toml
    from .cover_letter import LetterDraft, clean_letter
    from .pipeline import Candidate

    config = _load_toml(args.config)
    if not ParkedStore(
        Path(getattr(args, "parked_store", DEFAULT_PARKED_STORE))
    ).load_records():
        print("В парковке пусто: готовых к пуску вакансий нет.")
        return 0

    tool = SavingHHApplicantTool()
    try:
        service = ApproveService(
            args, config, tool.api_client, dry_run=args.dry_run, session=getattr(tool, "session", None)
        )
    except RuntimeError as ex:
        print(f"Ошибка: {ex}", file=sys.stderr)
        return 2

    records = service.records()
    print("\a")  # звуковой сигнал: пора вводить код
    print(f"В парковке {len(records)} вакансий — решение за вами.")
    closed = 0
    sent = 0

    for rec in records:
        vacancy = rec["vacancy"]
        vid = str(vacancy.get("id") or "")
        letter_text = service.letter_of(rec)

        # свежая карточка: за время паузы вакансия могла уйти в архив
        try:
            card = service.fetch_card(vid)
        except Exception as ex:  # noqa: BLE001
            print(f"[{vid}] карточка недоступна ({ex}) — остаётся в парковке")
            continue
        done = service.check(rec, card)
        if done is not None:
            print(f"[{vid}] {vacancy.get('name', '')} — {done.message} Закрываю.")
            closed += 1
            continue

        candidate = Candidate(
            vacancy=vacancy,
            score=score_of(rec),
            letter=LetterDraft(vacancy_id=vid, text=letter_text),
        )
        violations = service.violations(letter_text, vacancy, card)
        print()
        print(_format_card(candidate))
        if score_of(rec) and score_of(rec).requests:
            print("ПРОСЬБЫ РАБОТОДАТЕЛЯ (проверьте, что письмо их выполняет):")
            for req in score_of(rec).requests:
                print(f"  - {req}")
        count = service.responses(vid)
        if count is not None:
            print(f"Откликов на вакансию уже: {count}")
        if len(service.resumes) > 1:
            print(f"Резюме для отклика: «{service.resume_title(service.resume_for(rec))}»")
        if violations:
            print("ПРОВЕРКА ПИСЬМА — НАРУШЕНИЯ (решение за вами):")
            for violation in violations:
                print(f"  - {violation}")
        try:
            answer = input(
                "Отправить? [y — да / n — нет / e — правка письма / "
                "q — выйти, остальное сохранить] (Enter = n): "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nВыход — оставшиеся вакансии ждут в парковке.")
            break
        if answer in ("q", "й", "quit", "выход"):
            print("Выход — эта и оставшиеся вакансии ждут в парковке.")
            break

        if answer not in ("y", "д", "yes", "да", "e", "у", "edit", "правка"):
            # отказ человека = закрыть навсегда (не потерять в выдаче)
            service.skip(vid)
            closed += 1
            print("  → закрыто без пуска.")
            continue

        letter = letter_text
        if answer in ("e", "у", "edit", "правка"):
            letter = edit_text_in_editor(letter_text).strip() or letter_text
            if letter != letter_text:
                print()
                print(letter)
                print()
                edit_violations = service.violations(letter, vacancy, card)
                if edit_violations:
                    # правит человек: не блокируем, но показываем
                    print("  В правке есть то, чего нет в profile.md:")
                    for violation in edit_violations:
                        print(f"    - {violation}")
                try:
                    confirm = input(
                        "Отправить исправленное письмо? [y — да / n — нет] "
                        "(Enter = n): "
                    ).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print("\nВыход — оставшиеся вакансии ждут в парковке.")
                    break
                if confirm not in ("y", "д", "yes", "да", "у"):
                    print("  → не отправлено, вакансия остаётся в парковке.")
                    continue

        # человек видел карточку и нарушения — его «да» и есть подтверждение
        outcome = service.send(vid, letter, confirm=True, card=card)
        if outcome.status == "limit":
            print("Дневной лимит откликов hh исчерпан — стоп.")
            break
        if outcome.status == "error":
            print(f"  [{vid}] {outcome.message} — остаётся в парковке.")
            continue
        if outcome.closed:
            closed += 1
        sent += 1
        print("  → ПУСК ПРОИЗВЕДЁН." if not args.dry_run else "  → [dry-run] пуск.")

    if not args.dry_run:
        tool._persist()
    left = len(service.records())
    print()
    print(f"Итог: закрыто {closed} (пусков {sent}), в парковке осталось {left}")
    return 0

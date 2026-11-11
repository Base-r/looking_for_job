"""Решения по очереди вакансий (data/parked.jsonl) — общая логика для
консольного `snpr approve` и страницы `snpr web`.

Каждое решение сохраняется сразу: отклик → applied.jsonl + seen, пропуск →
seen, вакансия уходит из очереди. Уже откликнутые на hh (в т.ч. вручную на
сайте) и архивные закрываются без отправки.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .cover_letter import add_contacts, clean_letter
from .sources import is_external, label, source_of
from .scoring import ScoreResult


@dataclass
class Outcome:
    """Результат действия. status: sent | dry_run | skipped | applied_on_hh |
    archived | needs_confirm | rewritten | limit | error | not_found."""

    status: str
    message: str = ""
    violations: list[str] = field(default_factory=list)
    letter: str = ""

    @property
    def closed(self) -> bool:
        return self.status in {"sent", "skipped", "applied_on_hh", "archived"}

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "violations": self.violations,
            "letter": self.letter,
            "closed": self.closed,
        }


def _fresh_config(args: argparse.Namespace, fallback: Mapping[str, Any]) -> Mapping[str, Any]:
    """config.toml с диска прямо сейчас (правки без перезапуска страницы)."""
    from .cli import _load_toml

    try:
        return _load_toml(args.config)
    except (OSError, ValueError):
        return fallback


ARCHIVE_CHECK_FILE = "archive_checked.json"


def _load_checked(path: Path) -> dict[str, float]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): float(v) for k, v in data.items()} if isinstance(data, dict) else {}


def sweep_archived(
    api: Any,
    parked: Any,
    seen: Any,
    data_dir: Path,
    *,
    budget: int = 0,
    pause: float = 3.0,
    recheck_hours: float = 12.0,
    lock: Any = None,
    stop: threading.Event | None = None,
) -> int:
    """Убрать из очереди вакансии, которые работодатель отправил в архив
    (или удалил). Карточки hh запрашиваются по одной с паузой — частые
    запросы hh встречает капчей; сначала самые старые вакансии, каждая
    не чаще раза в recheck_hours. budget > 0 — не больше стольких карточек.
    Ошибка hh (капча, сеть) — сверка останавливается до следующего раза.
    Возвращает, сколько убрано."""
    path = Path(data_dir) / ARCHIVE_CHECK_FILE
    checked = _load_checked(path)
    now = time.time()
    records = sorted(
        parked.load_records(),
        key=lambda r: str((r.get("vacancy") or {}).get("published_at") or ""),
    )
    todo = [
        str((r.get("vacancy") or {}).get("id") or "")
        for r in records
        if now - checked.get(str((r.get("vacancy") or {}).get("id") or ""), 0)
        >= recheck_hours * 3600
    ]
    todo = [vid for vid in todo if vid and not is_external(vid)][: budget or None]
    removed = 0
    for n, vid in enumerate(todo):
        if stop is not None and stop.is_set():
            break
        if n:
            time.sleep(pause)
        try:
            card = api.get(f"/vacancies/{vid}")
            gone = isinstance(card, dict) and bool(card.get("archived"))
        except Exception as ex:  # noqa: BLE001
            if type(ex).__name__ != "ResourceNotFound":  # удалена — как архив
                print(f"[архив] сверка остановлена: {type(ex).__name__} {ex}")
                break
            gone, card = True, {}
        checked[vid] = time.time()
        if gone:
            with lock if lock is not None else contextlib.nullcontext():
                rec = next(
                    (r for r in parked.load_records()
                     if str((r.get("vacancy") or {}).get("id")) == vid),
                    None,
                )
                if rec is not None:
                    name = str((rec.get("vacancy") or {}).get("name") or "")
                    seen.mark_seen(vid, name=name, status="archived")
                    parked.remove([vid])
                    removed += 1
                    print(f"[архив] убрана из очереди: {name} ({vid})")
    live = {str((r.get("vacancy") or {}).get("id")) for r in parked.load_records()}
    try:
        path.write_text(
            json.dumps({k: v for k, v in checked.items() if k in live}), encoding="utf-8"
        )
    except OSError:
        pass
    return removed


def score_of(rec: Mapping[str, Any]) -> ScoreResult | None:
    data = rec.get("score") if isinstance(rec.get("score"), dict) else None
    if not data:
        return None
    vid = str((rec.get("vacancy") or {}).get("id") or "")
    return ScoreResult(
        vacancy_id=vid,
        score=int(data.get("score") or 0),
        verdict=str(data.get("verdict") or ""),
        reasons=[str(r) for r in data.get("reasons") or []],
        matched=[str(r) for r in data.get("matched") or []],
        missing=[str(r) for r in data.get("missing") or []],
        resume_id=str(data.get("resume_id") or ""),
        requests=[str(r) for r in data.get("requests") or []],
    )


class ApproveService:
    def __init__(
        self,
        args: argparse.Namespace,
        config: Mapping[str, Any],
        api: Any,
        dry_run: bool = False,
        writer_factory: Callable[[], Any] | None = None,
        session: Any = None,
    ):
        from .applied_sync import sync_hh_applied
        from .approve import AppliedStore
        from .cli import DEFAULT_PARKED_STORE, _build_letter_writer, _make_guard
        from .personal import LetterEditsStore
        from .sne import (
            HHNegotiationSender,
            ParkedStore,
            SeenStore,
            default_resume_id,
            usable_resumes,
        )

        self.args = args
        self.config = config
        self.api = api
        self.dry_run = dry_run
        data_dir = Path(args.seen_store).parent
        self.data_dir = data_dir
        # веб-сессия hh (cookies): число откликов есть только на странице
        self.session = session
        self.parked = ParkedStore(
            Path(getattr(args, "parked_store", DEFAULT_PARKED_STORE))
        )
        self.seen = SeenStore(Path(args.seen_store))
        self.applied_store = AppliedStore(Path(args.applied_store))
        self.guard = _make_guard(config)
        from .cli import _contacts_always, _letter_contacts

        # подпись с контактами — к каждому письму, и к написанным раньше
        self.contacts = _letter_contacts(config) if _contacts_always(config) else {}
        # RuntimeError, если на аккаунте нет резюме — пусть видит вызывающий
        self.resume_id = default_resume_id(api, config, getattr(args, "resume_id", ""))
        self.sender = None if dry_run else HHNegotiationSender(api, self.resume_id)
        # резюме, с которыми можно откликаться (опубликованные из [[resumes]])
        self.resumes = usable_resumes(api, config) or [
            {"id": self.resume_id, "title": "основное резюме", "use_for": ""}
        ]
        self.hh_applied = sync_hh_applied(api, data_dir, save=not dry_run)
        self._applied_synced_at = time.monotonic()
        self.edits = LetterEditsStore(data_dir / "letter_edits.jsonl")
        # Писатель собирается заново на каждое «Переписать»: letter_style.md,
        # profile.md, [letter] в config.toml и свежие правки писем читаются
        # с диска в момент нажатия — страница может жить часами.
        self._writer_factory = writer_factory or (
            lambda: _build_letter_writer(args, _fresh_config(args, config))
        )
        # один запрос к hh/LLM за раз: веб-сервер многопоточный
        self.lock = threading.RLock()

    # ------------------------------------------------------------ очередь --

    def records(self) -> list[dict[str, Any]]:
        return self.parked.load_records()

    def record(self, vid: str) -> dict[str, Any] | None:
        for rec in self.records():
            if str((rec.get("vacancy") or {}).get("id") or "") == str(vid):
                return rec
        return None

    def resume_for(self, rec: Mapping[str, Any], chosen: str | None = None) -> str:
        """Резюме для отклика: выбор человека → предложение LLM → основное.
        Только из разрешённых (опубликованных) — чужой id не пройдёт."""
        allowed = [r["id"] for r in self.resumes]
        score = score_of(rec)
        for candidate in (chosen, score.resume_id if score else ""):
            if candidate and candidate in allowed:
                return candidate
        return self.resume_id if self.resume_id in allowed else allowed[0]

    def resume_title(self, resume_id: str) -> str:
        for r in self.resumes:
            if r["id"] == resume_id:
                return str(r.get("title") or resume_id)
        return resume_id

    def responses(self, vid: str) -> int | None:
        """Сколько откликов уже у вакансии (с сайта hh, кэш на час)."""
        from .responses import responses_count

        return responses_count(self.session, vid, self.data_dir)

    def violations(
        self, letter: str, vacancy: Mapping[str, Any], card: Mapping[str, Any] | None = None
    ) -> list[str]:
        from .cli import _vacancy_numbers

        return self.guard.check(
            letter, extra_numbers=_vacancy_numbers(card if card else vacancy)
        )

    def fetch_card(self, vid: str) -> dict[str, Any]:
        """Свежая карточка hh (в dry-run не запрашиваем — как раньше)."""
        if self.dry_run or is_external(vid):
            return {}
        fetched = self.api.get(f"/vacancies/{vid}")
        return fetched if isinstance(fetched, dict) else {}

    def applied_keys(self) -> set[tuple[str, str]]:
        from .applied_sync import load_hh_applied_keys

        return load_hh_applied_keys(self.data_dir)

    def is_applied(self, vacancy: Mapping[str, Any]) -> bool:
        """Отклик уже есть: на эту вакансию или на её повтор (то же
        название у того же работодателя)."""
        from .dedupe import vacancy_key

        vid = str(vacancy.get("id") or "")
        if vid in self.hh_applied:
            return True
        key = vacancy_key(vacancy)
        return key is not None and key in self.applied_keys()

    def refresh_applied(self, max_age: float = 180.0) -> int:
        """Раз в max_age секунд сверяется с откликами на hh (вы могли
        откликнуться на сайте) и убирает такие вакансии из очереди.
        Возвращает, сколько убрано."""
        from .applied_sync import sync_hh_applied

        with self.lock:
            if time.monotonic() - self._applied_synced_at >= max_age:
                self.hh_applied = sync_hh_applied(
                    self.api, self.data_dir, save=not self.dry_run
                )
                self._applied_synced_at = time.monotonic()
            gone = [
                rec.get("vacancy") or {}
                for rec in self.records()
                if self.is_applied(rec.get("vacancy") or {})
            ]
            for vacancy in gone:
                self._mark(vacancy, "applied_on_hh")
            return len(gone)

    def _mark(self, vacancy: Mapping[str, Any], status: str) -> None:
        if self.dry_run:
            return
        vid = str(vacancy.get("id") or "")
        self.seen.mark_seen(vid, name=str(vacancy.get("name") or ""), status=status)
        self.parked.remove([vid])

    def sweep_archived(self, stop: threading.Event | None = None, **kw: Any) -> int:
        """Сверка очереди с архивом hh (см. sweep_archived)."""
        if self.dry_run:
            return 0
        return sweep_archived(
            self.api, self.parked, self.seen, self.data_dir, lock=self.lock, stop=stop, **kw
        )

    def check(self, rec: Mapping[str, Any], card: Mapping[str, Any]) -> Outcome | None:
        """Закрыть без вопроса, если отклик уже есть или вакансия в архиве."""
        from .applied_sync import card_already_applied

        vacancy = rec.get("vacancy") or {}
        vid = str(vacancy.get("id") or "")
        if self.is_applied(vacancy) or card_already_applied(card):
            self._mark(vacancy, "applied_on_hh")
            return Outcome("applied_on_hh", "Вы уже откликались на эту вакансию на hh.")
        if card.get("archived"):
            self._mark(vacancy, "archived")
            return Outcome("archived", "Вакансия в архиве — отклик невозможен.")
        return None

    # ------------------------------------------------------------ решения --

    def send(
        self,
        vid: str,
        letter: str | None = None,
        confirm: bool = False,
        card: Mapping[str, Any] | None = None,
        resume_id: str | None = None,
    ) -> Outcome:
        """Отклик. Нарушения гварда без confirm → needs_confirm (не отправляем)."""
        from .approve import AppliedRecord

        with self.lock:
            rec = self.record(vid)
            if rec is None:
                return Outcome("not_found", "Вакансии уже нет в очереди.")
            vacancy = rec.get("vacancy") or {}
            # письмо из очереди могло быть написано до правки стиля
            original = self.letter_of(rec)
            text = (letter if letter is not None else original).strip() or original
            if is_external(vid):
                return self._mark_external_applied(rec, text)
            if card is None:
                try:
                    card = self.fetch_card(vid)
                except Exception as ex:  # noqa: BLE001
                    return Outcome("error", f"Карточка вакансии недоступна: {ex}")
            closed = self.check(rec, card)
            if closed is not None:
                return closed
            violations = self.violations(text, vacancy, card)
            if violations and not confirm:
                return Outcome(
                    "needs_confirm",
                    "В письме есть то, чего нет в profile.md. Отправить всё равно?",
                    violations,
                )
            chosen_resume = self.resume_for(rec, resume_id)
            if self.sender is not None:
                try:
                    self.sender.send(vid, text, resume_id=chosen_resume)
                except Exception as ex:  # noqa: BLE001
                    from hh_applicant_tool.api.errors import LimitExceeded

                    if isinstance(ex, LimitExceeded):
                        return Outcome("limit", "Дневной лимит откликов hh исчерпан.")
                    return Outcome("error", f"пуск не прошёл ({ex})")
            if self.dry_run:
                return Outcome(
                    "dry_run",
                    "Пробный режим: отклик НЕ отправлен (резюме "
                    f"«{self.resume_title(chosen_resume)}»).",
                )
            if text != original.strip():
                self.edits.append(str(vacancy.get("name") or ""), original, text)
            score = score_of(rec)
            self.applied_store.append(
                AppliedRecord(
                    vacancy_id=vid,
                    vacancy_name=str(vacancy.get("name") or ""),
                    employer=str((vacancy.get("employer") or {}).get("name") or ""),
                    score=score.score if score else 0,
                    letter=text,
                    decision="approve",
                    dry_run=False,
                    resume_id=chosen_resume,
                )
            )
            self.hh_applied.add(vid)
            self._mark(vacancy, "ready")
            return Outcome(
                "sent",
                f"Отклик отправлен (резюме «{self.resume_title(chosen_resume)}»).",
                letter=text,
            )

    def _mark_external_applied(self, rec: Mapping[str, Any], text: str) -> Outcome:
        """GeekJob, Habr, Telegram: откликнуться бот не может (нужен вход
        на сайт или контакт из поста) — вы откликаетесь сами, здесь отметка."""
        from .approve import AppliedRecord

        vacancy = rec.get("vacancy") or {}
        if self.dry_run:
            return Outcome("dry_run", "Пробный режим: отметка не сохранена.")
        score = score_of(rec)
        self.applied_store.append(
            AppliedRecord(
                vacancy_id=str(vacancy.get("id") or ""),
                vacancy_name=str(vacancy.get("name") or ""),
                employer=str((vacancy.get("employer") or {}).get("name") or ""),
                score=score.score if score else 0,
                letter=text,
                decision=source_of(vacancy),
                dry_run=False,
            )
        )
        self._mark(vacancy, "ready")
        return Outcome("sent", f"Отмечено: вы откликнулись ({label(source_of(vacancy))}).", letter=text)

    def skip(self, vid: str) -> Outcome:
        with self.lock:
            rec = self.record(vid)
            if rec is None:
                return Outcome("not_found", "Вакансии уже нет в очереди.")
            self._mark(rec.get("vacancy") or {}, "ready")
            return Outcome(
                "skipped",
                "Пробный режим: ничего не записано." if self.dry_run else "Пропущено.",
            )

    # --------------------------------------------- не прошли порог скора --

    def threshold(self) -> int:
        """Текущий порог «показывать мне» — с диска (правится без перезапуска)."""
        cfg = _fresh_config(self.args, self.config)
        return int(cfg.get("scoring", {}).get("maybe_min", 50))

    def age_days(self) -> list[float]:
        """[min_age_days, max_age_days] из [search] — с диска; [0, 0] — не задан."""
        cfg = _fresh_config(self.args, self.config).get("search", {})
        return [
            float(cfg.get("min_age_days", 0) or 0),
            float(cfg.get("max_age_days", 0) or 0),
        ]

    def rejected(self) -> list[dict[str, Any]]:
        """Оценённые вакансии ниже порога (без уже откликнутых и тех, что
        в очереди) — последний скор каждой, новые сверху."""
        from .report import rows_from_stores

        limit = self.threshold()
        rows = [
            row
            for row in rows_from_stores(self.data_dir)
            if isinstance(row.get("score"), int)
            and row["score"] < limit
            and row.get("status") not in ("applied", "parked")
            and str(row.get("vacancy_id")) not in self.hh_applied
            and self._recent_enough(row)
        ]
        rows.sort(key=lambda r: -int(r.get("ts") or 0))
        return rows

    def _recent_enough(self, row: Mapping[str, Any]) -> bool:
        """Старые непрошедшие не показываем ([approve] rejected_max_age_days,
        0 — все). Возраст — по дате публикации, без неё — по дате оценки.
        Журнал оценок не трогаем: по нему бот не оценивает вакансию повторно."""
        from .approve import vacancy_age_hours

        cfg = _fresh_config(self.args, self.config).get("approve", {})
        days = float(cfg.get("rejected_max_age_days", 0) or 0)
        if days <= 0:
            return True
        hours = vacancy_age_hours(row.get("published_at"))
        if hours is None:
            hours = (time.time() - int(row.get("ts") or 0)) / 3600
        return hours <= days * 24

    def requeue(self, vid: str) -> Outcome:
        """«Вернуть на рассмотрение»: свежая карточка → письмо → очередь."""
        from .applied_sync import card_already_applied
        from .report import rows_from_stores

        with self.lock:
            if self.record(vid) is not None:
                return Outcome("requeued", "Вакансия уже в очереди на рассмотрение.")
            row = next(
                (r for r in rows_from_stores(self.data_dir) if str(r.get("vacancy_id")) == str(vid)),
                None,
            )
            if row is None:
                return Outcome("not_found", "Вакансия не найдена в журнале оценок.")
            if vid in self.hh_applied:
                return Outcome("applied_on_hh", "Вы уже откликались на эту вакансию на hh.")
            try:
                if is_external(vid):
                    card = self._external_card(row)
                else:
                    fetched = self.api.get(f"/vacancies/{vid}")
                    card = fetched if isinstance(fetched, dict) else {}
            except Exception as ex:  # noqa: BLE001
                return Outcome("error", f"Карточка вакансии недоступна: {ex}")
            if card.get("archived"):
                return Outcome("archived", "Вакансия в архиве — отклик невозможен.")
            if card_already_applied(card):
                return Outcome("applied_on_hh", "Вы уже откликались на эту вакансию на hh.")
            score = ScoreResult(
                vacancy_id=str(vid),
                score=int(row.get("score") or 0),
                verdict="maybe",
                reasons=[str(r) for r in row.get("reasons") or []],
                matched=[str(r) for r in row.get("matched") or []],
                missing=[str(r) for r in row.get("missing") or []],
                resume_id=str(row.get("resume_id") or ""),
                requests=[str(r) for r in row.get("requests") or []],
            )
            writer = self._writer_factory()
            if writer is None or not hasattr(writer, "ai"):
                return Outcome(
                    "error",
                    "LLM-письма недоступны: проверьте ключ в .env и "
                    "[letter].mode = \"llm\" в config.toml.",
                )
            try:
                draft = writer.write(card, description=str(card.get("description") or ""), score=score)
            except Exception as ex:  # noqa: BLE001
                return Outcome("error", f"LLM не ответил: {ex}")
            text = clean_letter(draft.text.strip())
            if not text:
                return Outcome("error", "LLM вернул пустое письмо.")
            if self.dry_run:
                return Outcome("dry_run", "Пробный режим: в очередь не добавлено.", letter=text)
            vacancy = {k: v for k, v in card.items() if k != "description"}
            self.parked.append_record(vacancy, score, text)
            return Outcome(
                "requeued", "Вернул на рассмотрение — вакансия на первой вкладке.", letter=text
            )

    @staticmethod
    def _external_card(row: Mapping[str, Any]) -> dict[str, Any]:
        """Вакансия внешнего источника из журнала оценок + свежее описание."""
        from .approve import vacancy_url
        from .sources import fetch_description

        vid = str(row.get("vacancy_id") or "")
        card: dict[str, Any] = {
            "id": vid,
            "name": str(row.get("name") or ""),
            "employer": {"name": str(row.get("employer") or "")},
            "alternate_url": vacancy_url(vid),
            "source": source_of(vid),
        }
        card["description"] = fetch_description(card)
        return card

    def letter_of(self, rec: Mapping[str, Any]) -> str:
        """Письмо из очереди, как его увидит работодатель: чистка под
        текущий стиль + подпись с контактами."""
        return add_contacts(clean_letter(str(rec.get("letter") or "")), self.contacts)

    def rewrite(self, vid: str) -> Outcome:
        """Новое письмо от LLM по той же вакансии; сохраняется в очередь."""
        with self.lock:
            rec = self.record(vid)
            if rec is None:
                return Outcome("not_found", "Вакансии уже нет в очереди.")
            writer = self._writer_factory()
            if writer is None or not hasattr(writer, "ai"):
                return Outcome(
                    "error",
                    "LLM-письма недоступны: проверьте ключ в .env и "
                    "[letter].mode = \"llm\" в config.toml.",
                )
            vacancy = rec.get("vacancy") or {}
            try:
                if is_external(vid):
                    from .sources import fetch_description

                    card = {"description": fetch_description(vacancy)}
                else:
                    fetched = self.api.get(f"/vacancies/{vid}")
                    card = fetched if isinstance(fetched, dict) else {}
            except Exception:  # noqa: BLE001 — без описания письмо хуже, но пишем
                card = {}
            merged = {**vacancy, **{k: card[k] for k in ("key_skills",) if card.get(k)}}
            try:
                draft = writer.write(
                    merged,
                    description=str(card.get("description") or ""),
                    score=score_of(rec),
                )
            except Exception as ex:  # noqa: BLE001
                return Outcome("error", f"LLM не ответил: {ex}")
            text = draft.text.strip()
            if not text:
                return Outcome("error", "LLM вернул пустое письмо.")
            if not self.dry_run:
                self.parked.update_letter(vid, text)
            text = add_contacts(clean_letter(text), self.contacts)
            return Outcome(
                "rewritten",
                "Письмо переписано.",
                self.violations(text, vacancy, card),
                letter=text,
            )

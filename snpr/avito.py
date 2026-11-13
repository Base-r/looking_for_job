"""Авито Работа — вакансии, собранные Claude во встроенном браузере.

Автоматически Авито не читается: на запросы бота сайт отвечает 429 и
требует проверку IP. Поэтому бот на Авито не ходит. По вашей просьбе
(«проверь Авито») Claude открывает поиск во встроенном браузере, где вы
вошли в аккаунт, в обычном темпе читает выдачу и подходящие объявления
скриптами scripts/avito_list.js и scripts/avito_item.js и кладёт их во
входящие data/avito_inbox.jsonl:

    python -m snpr.avito pick cards.json    # какие объявления открыть
    python -m snpr.avito add items.json     # положить во входящие

Проверку Авито («Пройти проверку», капча) проходите вы сами.

Источник avito ([search.sources] avito = true) берёт вакансии из входящих
при поиске: Поиск.cmd, ежедневный запуск или только Авито:
`python -m snpr sne --mode park --only-source avito`. id «av:<номер>».
Откликаетесь вы сами на Авито по ссылке со страницы отбора.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

PREFIX = "av:"
SOURCE = "avito"
INBOX = "avito_inbox.jsonl"
KEEP_DAYS = 30
TASHKENT = "2759"

# Латинские буквы, которыми Авито подменяет русские в тексте объявлений
# («cеть прокaтoв»): внутри слова с кириллицей меняем их обратно.
_LAT_LOOK = "aceopxyABCEHKMOPTXY"
_CYR_LOOK = "асеорхуАВСЕНКМОРТХУ"
_HOMOGLYPHS = str.maketrans(_LAT_LOOK, _CYR_LOOK)
_TO_LATIN = str.maketrans(_CYR_LOOK, _LAT_LOOK)
_WORD = re.compile(r"[A-Za-zА-Яа-яЁё]+")
_CYR = re.compile(r"[А-Яа-яЁё]")
_LAT = re.compile(r"[A-Za-z]")
# буквы, у которых нет двойника в другом алфавите, выдают язык слова
_CYR_ONLY = re.compile(f"[^{_CYR_LOOK}A-Za-z]")
_LAT_ONLY = re.compile(f"[^{_LAT_LOOK}А-Яа-яЁё]")
_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}


def fix_homoglyphs(text: str) -> str:
    def fix(m: re.Match[str]) -> str:
        word = m.group(0)
        if not (_CYR.search(word) and _LAT.search(word)):
            return word
        if _LAT_ONLY.search(word) and not _CYR_ONLY.search(word):  # «Ехсеl» → «Excel»
            return word.translate(_TO_LATIN)
        return word.translate(_HOMOGLYPHS)

    return _WORD.sub(fix, text or "")


def item_id(url_or_id: str) -> str:
    """Номер объявления из ссылки «…/finansovyy_analitik_8531064323?context=…»."""
    m = re.search(r"_(\d{6,})(?:[/?#]|$)", str(url_or_id)) or re.fullmatch(r"(\d{6,})", str(url_or_id))
    return m.group(1) if m else ""


def clean_url(url: str) -> str:
    return str(url or "").split("?", 1)[0].split("#", 1)[0]


def parse_date(text: str, now: datetime | None = None) -> str:
    """«· 4 ноября в 14:37», «сегодня в 9:05», «вчера в 18:00» → ISO с поясом.

    Время Авито показывает в поясе браузера — это пояс этого компьютера.
    """
    now = (now or datetime.now().astimezone()).replace(second=0, microsecond=0)
    low = (text or "").lower()
    hm = re.search(r"(\d{1,2}):(\d{2})", low)
    hour, minute = (int(hm.group(1)), int(hm.group(2))) if hm else (12, 0)
    if "сегодня" in low:
        day = now
    elif "вчера" in low:
        day = now - timedelta(days=1)
    else:
        m = re.search(r"(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?", low)
        month = next((n for stem, n in _MONTHS.items() if m and m.group(2).startswith(stem)), 0)
        if not m or not month:
            return ""
        year = int(m.group(3)) if m.group(3) else now.year
        try:
            day = now.replace(year=year, month=month, day=int(m.group(1)))
        except ValueError:
            return ""
        if not m.group(3) and day > now + timedelta(days=1):  # «28 декабря» в январе
            day = day.replace(year=year - 1)
    return day.replace(hour=hour, minute=minute).strftime("%Y-%m-%dT%H:%M:%S%z")


def _experience(text: str) -> dict[str, str] | None:
    low = text.lower()
    if "без опыта" in low:
        return {"id": "noExperience"}
    if re.search(r"опыт\s+(?:более|от)\s+(?:5|6)", low):
        return {"id": "moreThan6"}
    if re.search(r"опыт\s+(?:более|от)\s+3", low):
        return {"id": "between3And6"}
    if re.search(r"опыт\s+(?:до\s+1|более\s+года|от\s+1|1)", low):
        return {"id": "between1And3"}
    return None


def _employment(text: str) -> str:
    m = re.search(r"занятость:\s*([^\n]+)", text, re.I)
    value = (m.group(1) if m else "").lower()
    if "подработ" in value:
        return "SIDE_JOB"
    if "частичн" in value:
        return "PART"
    if "временн" in value or "проект" in value:
        return "PROJECT"
    if "полн" in value or "постоян" in value:
        return "FULL"
    return ""


def _salary(price: str) -> dict[str, Any] | None:
    from .geekjob import parse_salary

    price = (price or "").replace("\xa0", " ")
    salary = parse_salary(re.sub(r"(?i)зарплата не указана", "", price))
    if not salary:
        return None
    low = price.lower()
    salary["gross"] = "на руки" not in low
    mode = (
        "SERVICE" if "сдельно" in low else "SHIFT" if "смену" in low
        else "HOUR" if "час" in low else "WEEK" if "недел" in low else "MONTH"
    )
    salary["mode"] = {"id": mode}
    return salary


def _area(location: str) -> dict[str, str]:
    if "ташкент" in location.lower():
        return {"id": TASHKENT, "name": "Ташкент"}
    return {"id": "", "name": location.strip()}


def to_vacancy(card: Mapping[str, Any]) -> dict[str, Any] | None:
    """Карточка из выдачи + страница объявления → вакансия в формате hh.

    Поля: id/url, title, price, params (строка условий карточки),
    location, snippet; со страницы — date, conditions, description.
    """
    vid = item_id(str(card.get("id") or "")) or item_id(str(card.get("url") or ""))
    title = fix_homoglyphs(str(card.get("title") or "")).strip()
    if not vid or not title:
        return None
    conditions = fix_homoglyphs(f"{card.get('params') or ''}\n{card.get('conditions') or ''}")
    description = fix_homoglyphs(str(card.get("description") or card.get("snippet") or "")).strip()
    remote = bool(re.search(r"удал[её]нн?о", conditions, re.I))
    employer = fix_homoglyphs(str(card.get("employer") or "")).strip()
    if employer.lower() in ("пользователь", "частное лицо"):  # Авито скрыл имя
        employer = ""
    vacancy: dict[str, Any] = {
        "id": f"{PREFIX}{vid}",
        "name": title,
        "employer": {"name": employer},
        "area": _area(str(card.get("location") or "")),
        "salary": _salary(str(card.get("price") or "")),
        "work_format": [{"id": "REMOTE"}] if remote else [],
        "published_at": parse_date(str(card.get("date") or "")),
        "alternate_url": clean_url(str(card.get("url") or "")) or f"https://www.avito.ru/{vid}",
        "source": SOURCE,
        "description": (f"Условия:\n{conditions.strip()}\n\n" if conditions.strip() else "") + description,
    }
    if exp := _experience(conditions):
        vacancy["experience"] = exp
    if form := _employment(conditions):
        vacancy["employment_form"] = {"id": form}
    return vacancy


# ------------------------------------------------------------- входящие --

def inbox_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / INBOX


def load_inbox(data_dir: Path | str) -> list[dict[str, Any]]:
    path = inbox_path(data_dir)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and isinstance(row.get("vacancy"), dict):
            rows.append(row)
    return rows


def add(data_dir: Path | str, cards: Iterable[Mapping[str, Any]], now: float | None = None) -> int:
    """Положить объявления во входящие; вернуть, сколько новых. Старше
    KEEP_DAYS дней записи выбрасываются (они давно оценены)."""
    now = now if now is not None else time.time()
    rows = [r for r in load_inbox(data_dir) if now - float(r.get("ts") or 0) < KEEP_DAYS * 86400]
    known = {r["vacancy"].get("id") for r in rows}
    added = 0
    for card in cards:
        vacancy = to_vacancy(card)
        if vacancy and vacancy["id"] not in known:
            rows.append({"ts": int(now), "vacancy": vacancy})
            known.add(vacancy["id"])
            added += 1
    path = inbox_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return added


def pick(
    cards: Iterable[Mapping[str, Any]],
    title_must: re.Pattern[str],
    exclude: re.Pattern[str] | None,
    known_ids: set[str],
    experience: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Какие объявления из выдачи открыть: подходят по названию, не из
    «шума», опыт как в поиске hh ([search.params].experience), ещё не
    видены ботом и не лежат во входящих."""
    wanted = {str(e) for e in experience}
    out = []
    for card in cards:
        title = fix_homoglyphs(str(card.get("title") or ""))
        vid = item_id(str(card.get("id") or "")) or item_id(str(card.get("url") or ""))
        if not vid or f"{PREFIX}{vid}" in known_ids:
            continue
        if not title_must.search(title) or (exclude is not None and exclude.search(title)):
            continue
        exp = _experience(str(card.get("params") or ""))
        if wanted and exp and exp["id"] not in wanted:
            continue
        out.append({**card, "id": vid, "title": title})
    return out


class AvitoSource:
    """Источник для поиска: вакансии из входящих за окно поиска."""

    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)

    def list_all(self, max_age_days: float = 0) -> list[dict[str, Any]]:
        now = time.time()
        out = []
        for row in load_inbox(self.data_dir):
            vacancy = dict(row["vacancy"])
            ts = float(row.get("ts") or 0)
            pub = str(vacancy.get("published_at") or "")
            if pub:
                try:
                    ts = datetime.strptime(pub, "%Y-%m-%dT%H:%M:%S%z").timestamp()
                except ValueError:
                    pass
            if max_age_days > 0 and now - ts > (max_age_days + 0.5) * 86400:
                continue
            out.append(vacancy)
        return out

    def description(self, vacancy: Mapping[str, Any]) -> str:
        text = str(vacancy.get("description") or "")
        if text:
            return text
        for row in load_inbox(self.data_dir):
            if row["vacancy"].get("id") == vacancy.get("id"):
                return str(row["vacancy"].get("description") or "")
        raise LookupError("объявления Авито нет во входящих")


# ------------------------------------------------------------------- CLI --

def _known_ids(data_dir: Path) -> set[str]:
    from .sne import ParkedStore, ScoresStore, SeenStore

    ids = SeenStore(data_dir / "seen.jsonl").seen_ids()
    ids |= ParkedStore(data_dir / "parked.jsonl").ids()
    ids |= ScoresStore(data_dir / "scores.jsonl").ids()
    ids |= {r["vacancy"].get("id") for r in load_inbox(data_dir)}
    return ids


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .cli import _load_toml
    from .filters import DEFAULT_CONFIG_PATH
    from .sources import DEFAULT_TITLE_MUST

    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(prog="python -m snpr.avito")
    parser.add_argument("command", choices=["pick", "add"])
    parser.add_argument("file", help="JSON-файл: список карточек (pick) или объявлений (add)")
    parser.add_argument("--data-dir", default=str(root / "data"))
    args = parser.parse_args(argv)
    cards = json.loads(Path(args.file).read_text(encoding="utf-8"))
    data_dir = Path(args.data_dir)
    if args.command == "pick":
        config = _load_toml(str(DEFAULT_CONFIG_PATH))
        sources = (config.get("search") or {}).get("sources") or {}
        title_cfg = config.get("title_filter") or {}
        exclude = (
            re.compile(str(title_cfg["exclude"]), re.I)
            if title_cfg.get("enabled", True) and title_cfg.get("exclude") else None
        )
        experience = ((config.get("search") or {}).get("params") or {}).get("experience") or []
        chosen = pick(cards, re.compile(str(sources.get("title_must") or DEFAULT_TITLE_MUST), re.I),
                      exclude, _known_ids(data_dir), experience)
        print(json.dumps(chosen, ensure_ascii=False, indent=1))
        print(f"Открыть: {len(chosen)} из {len(cards)}", flush=True)
        return 0
    print(f"Во входящие Авито добавлено: {add(data_dir, merge(cards))}")
    return 0


def merge(data: Any) -> list[dict[str, Any]]:
    """add принимает список объявлений или {"cards": [...], "items": [...]}:
    карточка из выдачи (город, условия) дополняется страницей объявления."""
    if isinstance(data, list):
        return data
    cards = {item_id(str(c.get("id") or c.get("url") or "")): c for c in data.get("cards") or []}
    out = []
    for item in data.get("items") or []:
        if item.get("blocked"):
            continue
        vid = item_id(str(item.get("id") or "")) or item_id(str(item.get("url") or ""))
        card = cards.get(vid, {})
        out.append({**card, **{k: v for k, v in item.items() if v not in ("", None)}})
    return out


if __name__ == "__main__":
    raise SystemExit(main())

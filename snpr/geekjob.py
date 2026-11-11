"""GeekJob (geekjob.ru) — второй источник вакансий.

API и RSS у GeekJob нет; страницы списка (/vacancies/N) и вакансий
(/vacancy/<id>) robots.txt разрешает (закрыты только /json/ и /rest/).
Обходим весь список (~200 открытых вакансий, ~11 страниц) с паузой,
карточки — только новых, прошедших фильтр по названию.

Вакансия приводится к формату hh (id «gj:<hex>», name, employer, area,
work_format, salary, experience, published_at, alternate_url), чтобы
работали те же фильтры, дубли, оценка и письма. Откликнуться бот не может
(нужен вход на GeekJob): на странице отбора — ссылка и готовое письмо,
откликаетесь вы, потом «Я откликнулся».
"""

from __future__ import annotations

import html
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

BASE = "https://geekjob.ru"
PREFIX = "gj:"
SOURCE = "geekjob"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) hh-sniper"
MSK = timezone(timedelta(hours=3))

_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
_ITEM_RE = re.compile(r'<li class="collection-item.*?</li>', re.S)
_ID_RE = re.compile(r'href="/vacancy/([0-9a-f]{24})"')
_TAG_RE = re.compile(r"<[^>]+>")


def is_geekjob(vacancy_or_id: Any) -> bool:
    vid = vacancy_or_id.get("id") if isinstance(vacancy_or_id, Mapping) else vacancy_or_id
    return str(vid or "").startswith(PREFIX)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", fragment))).strip()


def _first(pattern: str, text: str) -> str:
    m = re.search(pattern, text, re.S)
    return _text(m.group(1)) if m else ""


def parse_salary(text: str) -> dict[str, Any] | None:
    """«120K — 200K ₽», «от 150K ₽», «до 3 000 $» → salary как в hh."""
    text = text.replace("\xa0", " ").strip()
    if not text:
        return None
    low = text.lower()
    currency = (
        "USD" if "$" in text or "usd" in low or "долл" in low
        else "EUR" if "€" in text or "eur" in low or "евро" in low
        else "UZS" if "сум" in low or "uzs" in low
        else "RUR"
    )

    def num(s: str) -> int:
        s = s.replace(" ", "")
        mult = 1000 if s.lower().endswith(("k", "к")) else 1
        return int(float(re.sub(r"[^\d.]", "", s) or 0) * mult)

    nums = re.findall(r"\d[\d ]*(?:[.,]\d+)?\s*[KkКк]?", text)
    nums = [n for n in (num(x) for x in nums) if n]
    if not nums:
        return None
    if text.lower().startswith("до"):
        lo, hi = None, nums[0]
    elif len(nums) == 1:
        lo, hi = nums[0], None
    else:
        lo, hi = nums[0], nums[1]
    return {"from": lo, "to": hi, "currency": currency, "gross": False}


def _date_from_ru(text: str, today: date | None = None) -> date | None:
    """«10 ноября» (год не пишут) → дата, не позже сегодняшней."""
    m = re.search(r"(\d{1,2})\s+([а-я]+)(?:\s+(\d{4}))?", text.lower())
    if not m or m.group(2) not in _MONTHS:
        return None
    today = today or date.today()
    year = int(m.group(3) or today.year)
    d = date(year, _MONTHS[m.group(2)], int(m.group(1)))
    if not m.group(3) and d > today:
        d = d.replace(year=year - 1)
    return d


def _experience(text: str, level: str) -> dict[str, str] | None:
    t = f"{text} {level}".lower()
    if "без опыта" in t or "стажер" in t or "стажёр" in t or "intern" in t:
        return {"id": "noExperience"}
    if re.search(r"от 1 года до 3", t) or "джуниор" in t or "junior" in t:
        return {"id": "between1And3"}
    if re.search(r"от 3 лет|от 3х лет|3 до 6", t) or "сеньор" in t or "senior" in t:
        return {"id": "between3And6"}
    if "миддл" in t or "middle" in t:
        return {"id": "between1And3"}
    return None


def _area(location: str, tashkent_id: str = "2759") -> dict[str, str]:
    city = location.split(",")[0].strip()
    if "ташкент" in location.lower() or "tashkent" in location.lower():
        return {"id": tashkent_id, "name": "Ташкент"}
    return {"id": "", "name": city}


def parse_list(page_html: str) -> list[dict[str, Any]]:
    """Короткие карточки со страницы списка (как items поиска hh)."""
    out: list[dict[str, Any]] = []
    for block in _ITEM_RE.findall(page_html):
        m = _ID_RE.search(block)
        if not m:
            continue
        gid = m.group(1)
        name = _first(r'class="title"[^>]*>(.*?)</a>', block)
        if not name:
            continue
        info = _first(r'<div class="info"><a[^>]*>(.*?)<br>', block)
        remote = 'class="remote-label"' in block
        out.append({
            "id": PREFIX + gid,
            "name": name,
            "employer": {"name": _first(r'class="truncate company-name">(.*?)</p>', block)},
            "area": _area(info),
            "salary": parse_salary(_first(r'<span class="salary">(.*?)</span>', block)),
            "work_format": [{"id": "REMOTE"}] if remote else [],
            "published_at": _published(_first(r'<time[^>]*>(.*?)</time>', block)),
            "alternate_url": f"{BASE}/vacancy/{gid}",
            "source": SOURCE,
            "has_test": 'withtest-label' in block,
        })
    return out


def _published(text: str, posted: str = "") -> str:
    d = None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", posted or ""):
        d = date.fromisoformat(posted)
    d = d or _date_from_ru(text)
    if d is None:
        return ""
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=MSK).strftime("%Y-%m-%dT%H:%M:%S%z")


def parse_vacancy(page_html: str, short: Mapping[str, Any]) -> dict[str, Any]:
    """Полная карточка: короткая + описание, опыт, формат, дата."""
    card = dict(short)
    m = re.search(r'<div id="vacancy-description">(.*?)</div>\s*</div>', page_html, re.S)
    description = m.group(1) if m else ""
    jobformat = _first(r'<span class="jobformat">(.*?)</span>', page_html)
    level = _first(r'<div class="category">(.*?)</div>', page_html)
    location = _first(r'<div class="location">(.*?)</div>', page_html)
    posted = _first(r'"datePosted"\s*:\s*"([\d-]+)"', page_html)
    card["description"] = description
    if exp := _experience(jobformat, level):
        card["experience"] = exp
    if "удален" in jobformat.lower() or "удалён" in jobformat.lower():
        card["work_format"] = [{"id": "REMOTE"}]
    if location:
        card["area"] = _area(location)
    if salary := parse_salary(_first(r'<div class="jobinfo">.*?<span class="salary">(.*?)</span>', page_html)):
        card["salary"] = salary
    if published := _published(_first(r'<div class="time">(.*?)</div>', page_html), posted):
        card["published_at"] = published
    card["internship"] = "стаж" in f"{level} {card.get('name', '')}".lower()
    return card


class GeekJobSource:
    """Обход GeekJob с паузой между запросами (сайт маленький, не грузим)."""

    def __init__(self, session: Any = None, pause: float = 2.0, max_pages: int = 30):
        if session is None:
            import requests

            session = requests.Session()
            session.headers["User-Agent"] = USER_AGENT
        self.session = session
        self.pause = pause
        self.max_pages = max_pages
        self.cards: dict[str, dict[str, Any]] = {}
        self._last = 0.0

    def _get(self, path: str) -> str:
        wait = self.pause - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        resp = self.session.get(BASE + path, timeout=30)
        resp.raise_for_status()
        return resp.text

    def list_all(self, max_age_days: float = 0) -> list[dict[str, Any]]:
        """Все открытые вакансии (свежие первыми); max_age_days > 0 —
        старше не берём (список отсортирован по дате — дальше не идём)."""
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        limit = (
            datetime.now(MSK) - timedelta(days=max_age_days, hours=12)
            if max_age_days > 0 else None
        )
        for page in range(1, self.max_pages + 1):
            items = parse_list(self._get("/vacancies" if page == 1 else f"/vacancies/{page}"))
            fresh = [v for v in items if v["id"] not in seen]
            if not fresh:
                break
            too_old = 0
            for v in fresh:
                seen.add(v["id"])
                pub = v.get("published_at")
                if limit and pub and datetime.strptime(pub, "%Y-%m-%dT%H:%M:%S%z") < limit:
                    too_old += 1
                    continue
                found.append(v)
            if too_old == len(fresh):
                break
        return found

    def description(self, vacancy: Mapping[str, Any]) -> str:
        """Описание (и доп. поля в саму вакансию) — как VacancyCardCache."""
        vid = str(vacancy.get("id") or "")
        card = self.cards.get(vid)
        if card is None:
            card = parse_vacancy(self._get(f"/vacancy/{vid[len(PREFIX):]}"), vacancy)
            self.cards[vid] = card
        if isinstance(vacancy, dict):
            for field in ("experience", "work_format", "area", "salary", "published_at", "internship"):
                if card.get(field) not in (None, "", []):
                    vacancy[field] = card[field]
        return str(card.get("description") or "")

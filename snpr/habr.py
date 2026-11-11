"""Habr Career (career.habr.com) — источник вакансий.

Открытого API у Habr Career нет (/v1 закрыт в robots.txt). Страницы
поиска /vacancies и вакансий /vacancies/<id> разрешены и несут данные
готовым JSON (<script data-ssr-state>), поэтому HTML не разбираем.

Поиск: по каждому запросу и уровню (qid: 1 стажёр, 3 junior, 4 middle)
свежие первыми, пока вакансии не старше окна. Поиск у Habr по всему
тексту — шум отсекает фильтр по названию (title_must в [search.sources]).

Вакансия приводится к формату hh: id «hc:<число>». Откликнуться бот не
может (нужен вход на Habr): на странице отбора — ссылка и готовое письмо.
"""

from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

BASE = "https://career.habr.com"
PREFIX = "hc:"
SOURCE = "habr"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) hh-sniper"
DEFAULT_QIDS = ("1", "3", "4")  # стажёр, junior, middle

_STATE_RE = re.compile(r'<script[^>]*data-ssr-state="true"[^>]*>(.*?)</script>', re.S)
_EXPERIENCE = {
    "intern": "noExperience",
    "junior": "between1And3",
    "middle": "between1And3",
    "senior": "between3And6",
    "lead": "moreThan6",
}


def is_habr(vacancy_or_id: Any) -> bool:
    vid = vacancy_or_id.get("id") if isinstance(vacancy_or_id, Mapping) else vacancy_or_id
    return str(vid or "").startswith(PREFIX)


def ssr_state(page_html: str) -> dict[str, Any]:
    m = _STATE_RE.search(page_html)
    if not m:
        raise ValueError("на странице Habr нет данных (data-ssr-state)")
    return json.loads(html.unescape(m.group(1)))


def _published(raw: str) -> str:
    try:
        return datetime.fromisoformat(raw).strftime("%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError):
        return ""


def _salary(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    raw = raw or {}
    lo, hi = raw.get("from"), raw.get("to")
    if not lo and not hi:
        return None
    currency = str(raw.get("currency") or "rur").upper()
    return {"from": lo, "to": hi, "currency": "RUR" if currency == "RUB" else currency, "gross": False}


def _area(titles: Iterable[str], tashkent_id: str = "2759") -> dict[str, str]:
    titles = [t for t in titles if t]
    for t in titles:
        if "ташкент" in t.lower() or "tashkent" in t.lower():
            return {"id": tashkent_id, "name": "Ташкент"}
    return {"id": "", "name": titles[0] if titles else ""}


def to_hh(item: Mapping[str, Any]) -> dict[str, Any]:
    """Карточка из списка/страницы Habr → вакансия в формате hh."""
    hid = str(item.get("id") or "")
    qualification = str(item.get("qualification") or "").lower()
    vacancy: dict[str, Any] = {
        "id": PREFIX + hid,
        "name": str(item.get("title") or "").strip(),
        "employer": {"name": str((item.get("company") or {}).get("title") or "").strip()},
        "area": _area(str(loc.get("title") or "") for loc in item.get("locations") or []),
        "salary": _salary(item.get("salary")),
        "work_format": [{"id": "REMOTE"}] if item.get("remoteWork") else [],
        "published_at": _published(str((item.get("publishedDate") or {}).get("date") or "")),
        "alternate_url": f"{BASE}/vacancies/{hid}",
        "source": SOURCE,
        "internship": qualification == "intern",
    }
    if qualification in _EXPERIENCE:
        vacancy["experience"] = {"id": _EXPERIENCE[qualification]}
    if item.get("archived"):
        vacancy["archived"] = True
    return vacancy


def parse_list(page_html: str) -> tuple[list[dict[str, Any]], int]:
    """Вакансии со страницы поиска и число страниц."""
    block = ssr_state(page_html).get("vacancies") or {}
    items = [to_hh(i) for i in block.get("list") or [] if i.get("id")]
    return items, int((block.get("meta") or {}).get("totalPages") or 1)


def parse_vacancy(page_html: str, short: Mapping[str, Any]) -> dict[str, Any]:
    raw = ssr_state(page_html).get("vacancy") or {}
    card = {**short, **{k: v for k, v in to_hh(raw).items() if v not in (None, "", [])}}
    card["description"] = str(raw.get("description") or "")
    return card


class HabrSource:
    def __init__(
        self,
        session: Any = None,
        pause: float = 2.0,
        queries: Iterable[str] = (),
        qids: Iterable[str] = DEFAULT_QIDS,
        max_pages: int = 4,
    ):
        if session is None:
            import requests

            session = requests.Session()
            session.headers["User-Agent"] = USER_AGENT
        self.session = session
        self.pause = pause
        self.queries = [q for q in queries if q]
        self.qids = [str(q) for q in qids]
        self.max_pages = max_pages
        self.cards: dict[str, dict[str, Any]] = {}
        self._last = 0.0

    def _get(self, path: str, params: Any = None) -> str:
        wait = self.pause - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        resp = self.session.get(BASE + path, params=params, timeout=30)
        resp.raise_for_status()
        return resp.text

    def list_all(self, max_age_days: float = 0) -> list[dict[str, Any]]:
        limit = (
            datetime.now(timezone.utc) - timedelta(days=max_age_days, hours=12)
            if max_age_days > 0 else None
        )
        found: dict[str, dict[str, Any]] = {}
        for query in self.queries:
            for qid in self.qids:
                for page in range(1, self.max_pages + 1):
                    items, pages = parse_list(self._get("/vacancies", {
                        "q": query, "type": "all", "sort": "date", "qid": qid, "page": page,
                    }))
                    too_old = False
                    for v in items:
                        pub = v.get("published_at")
                        if limit and pub and datetime.strptime(pub, "%Y-%m-%dT%H:%M:%S%z") < limit:
                            too_old = True
                            continue
                        if not v.get("archived"):
                            found.setdefault(v["id"], v)
                    if too_old or page >= pages:
                        break
        return list(found.values())

    def description(self, vacancy: Mapping[str, Any]) -> str:
        vid = str(vacancy.get("id") or "")
        card = self.cards.get(vid)
        if card is None:
            card = parse_vacancy(self._get(f"/vacancies/{vid[len(PREFIX):]}"), vacancy)
            self.cards[vid] = card
        if isinstance(vacancy, dict):
            for field in ("experience", "work_format", "area", "salary", "published_at", "internship"):
                if card.get(field) not in (None, "", []):
                    vacancy[field] = card[field]
        return str(card.get("description") or "")

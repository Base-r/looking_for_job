"""Talent Move (talent-move.ru) — RSS-ленты категорий вакансий.

Лента /job-category/<категория>/feed/ — формат для автоматического
чтения, robots.txt её разрешает. Отдаёт 10 последних вакансий категории;
листать дальше (?paged=) robots.txt запрещает, поэтому при ежедневном
поиске в загруженный день часть вакансий может не попасть.

В записи нет отдельных полей: компания — из заголовка «О компании …»
(есть не у всех, иначе пусто), удалёнка — по тексту, зарплата не разбирается (фильтр по зарплате такие
пропускает). id «tm:<слаг ссылки>». Откликаетесь вы сами по ссылке.
"""

from __future__ import annotations

import html
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Mapping

BASE = "https://talent-move.ru"
PREFIX = "tm:"
SOURCE = "talent_move"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) hh-sniper"
DEFAULT_CATEGORIES = ("analytics", "project-manager", "product-manager")
_CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
_COMPANY = re.compile(r"<h\d>\s*О компании\s+(.+?)\s*</h\d>", re.I)
_REMOTE = re.compile(r"удал[её]нн?|remote", re.I)
_OFFICE_ONLY = re.compile(r"только\s+(?:в\s+)?офис|без\s+удал", re.I)


def text_of(fragment: str) -> str:
    text = re.sub(r"</(?:p|li|h\d)>|<br\s*/?>", "\n", fragment, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())


def _slug(link: str) -> str:
    return link.rstrip("/").rsplit("/", 1)[-1]


def to_hh(item: ET.Element) -> dict[str, Any] | None:
    link = (item.findtext("link") or "").strip()
    title = html.unescape(item.findtext("title") or "").strip()
    if not link or not title:
        return None
    body = item.findtext(_CONTENT) or item.findtext("description") or ""
    text = text_of(body)
    company = _COMPANY.search(body)
    published = ""
    try:
        published = parsedate_to_datetime(item.findtext("pubDate") or "").strftime("%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError):
        pass
    remote = bool(_REMOTE.search(text)) and not _OFFICE_ONLY.search(text)
    return {
        "id": f"{PREFIX}{_slug(link)}",
        "name": title,
        "employer": {"name": html.unescape(company.group(1)).strip() if company else ""},
        "area": {"id": "", "name": ""},
        "salary": None,
        "work_format": [{"id": "REMOTE"}] if remote else [],
        "published_at": published,
        "alternate_url": link,
        "source": SOURCE,
        "description": text,
    }


def parse_feed(xml_text: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    return [v for item in root.iter("item") if (v := to_hh(item))]


class TalentMoveSource:
    def __init__(self, categories: Iterable[str] = DEFAULT_CATEGORIES, session: Any = None, pause: float = 2.0):
        if session is None:
            import requests

            session = requests.Session()
            session.headers["User-Agent"] = USER_AGENT
        self.session = session
        self.categories = [c.strip().strip("/") for c in categories if c and c.strip()]
        self.pause = pause
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
        limit = (
            datetime.now(timezone.utc) - timedelta(days=max_age_days, hours=12)
            if max_age_days > 0 else None
        )
        found: dict[str, dict[str, Any]] = {}
        for category in self.categories:
            for v in parse_feed(self._get(f"/job-category/{category}/feed/")):
                pub = v["published_at"]
                if limit and pub and datetime.strptime(pub, "%Y-%m-%dT%H:%M:%S%z") < limit:
                    continue
                found.setdefault(v["id"], v)
        return list(found.values())

    def description(self, vacancy: Mapping[str, Any]) -> str:
        """Текст хранится в вакансии; если его нет — ищем в лентах заново."""
        text = str(vacancy.get("description") or "")
        if text:
            return text
        for v in self.list_all():
            if v["id"] == vacancy.get("id"):
                return str(v["description"])
        raise LookupError("вакансия ушла из ленты Talent Move")

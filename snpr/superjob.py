"""SuperJob — официальное API (api.superjob.ru, /2.0/vacancies/).

Ключ приложения — SJ_KEY в .env, в заголовке X-Api-App-Id. Лимит API —
120 запросов в минуту с IP; поиск отдаёт не больше 500 вакансий.

Ищем две области: удалённая работа (place_of_work=2) и Ташкент. Вакансия
приводится к формату hh, id «sj:<число>». Описание приходит сразу в
списке. Откликаетесь вы сами по ссылке (контакты API не запрашиваем).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

API = "https://api.superjob.ru/2.0/vacancies/"
PREFIX = "sj:"
SOURCE = "superjob"
KEY_ENV = "SJ_KEY"
MSK = timezone(timedelta(hours=3))
PER_PAGE = 100
MAX_PAGES = 5  # 5 × 100 = предел API в 500 вакансий на поиск
REMOTE = 2  # place_of_work: 2 — на дому
SCOPES: tuple[dict[str, Any], ...] = ({"place_of_work": REMOTE}, {"town": "Ташкент"})
_CURRENCY = {"rub": "RUR", "uzs": "UZS"}


def _experience(title: str) -> dict[str, str] | None:
    t = title.lower()
    if "без опыта" in t:
        return {"id": "noExperience"}
    if "от 1" in t:
        return {"id": "between1And3"}
    if "от 3" in t:
        return {"id": "between3And6"}
    if "от 6" in t:
        return {"id": "moreThan6"}
    return None


def _salary(item: Mapping[str, Any]) -> dict[str, Any] | None:
    lo, hi = item.get("payment_from") or None, item.get("payment_to") or None
    if not lo and not hi:
        return None
    currency = str(item.get("currency") or "rub").lower()
    return {"from": lo, "to": hi, "currency": _CURRENCY.get(currency, currency.upper()), "gross": False}


def _area(title: str, tashkent_id: str = "2759") -> dict[str, str]:
    if "ташкент" in title.lower():
        return {"id": tashkent_id, "name": "Ташкент"}
    return {"id": "", "name": title}


def to_hh(item: Mapping[str, Any]) -> dict[str, Any]:
    """Объект вакансии SuperJob → вакансия в формате hh."""
    published = item.get("date_published")
    place = (item.get("place_of_work") or {}).get("id")
    text = "\n\n".join(
        str(item[k]).strip() for k in ("candidat", "work", "compensation") if item.get(k)
    )
    vacancy: dict[str, Any] = {
        "id": f"{PREFIX}{item.get('id')}",
        "name": str(item.get("profession") or "").strip(),
        "employer": {"name": str(item.get("firm_name") or "").strip()},
        "area": _area(str((item.get("town") or {}).get("title") or "")),
        "salary": _salary(item),
        "work_format": [{"id": "REMOTE"}] if place == REMOTE else [],
        "published_at": (
            datetime.fromtimestamp(published, MSK).strftime("%Y-%m-%dT%H:%M:%S%z")
            if isinstance(published, (int, float)) and published else ""
        ),
        "alternate_url": str(item.get("link") or ""),
        "source": SOURCE,
        "description": text,
    }
    if exp := _experience(str((item.get("experience") or {}).get("title") or "")):
        vacancy["experience"] = exp
    return vacancy


class SuperJobSource:
    def __init__(
        self,
        key: str | None = None,
        session: Any = None,
        pause: float = 2.0,
        queries: Iterable[str] = (),
    ):
        self.key = (key if key is not None else os.environ.get(KEY_ENV, "")).strip()
        if session is None:
            import requests

            session = requests.Session()
        self.session = session
        self.pause = pause
        self.queries = [q for q in queries if q]
        self._last = 0.0

    def _get(self, url: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not self.key:
            raise RuntimeError(f"нет ключа {KEY_ENV} в .env")
        wait = self.pause - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        resp = self.session.get(url, params=params, headers={"X-Api-App-Id": self.key}, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def list_all(self, max_age_days: float = 0) -> list[dict[str, Any]]:
        since = (
            int((datetime.now(timezone.utc) - timedelta(days=max_age_days, hours=12)).timestamp())
            if max_age_days > 0 else 0
        )
        found: dict[str, dict[str, Any]] = {}
        for query in self.queries:
            for scope in SCOPES:
                for page in range(MAX_PAGES):
                    params: dict[str, Any] = {
                        "keyword": query, "order_field": "date", "order_direction": "desc",
                        "count": PER_PAGE, "page": page, **scope,
                    }
                    if since:
                        params["date_published_from"] = since
                    data = self._get(API, params)
                    for item in data.get("objects") or []:
                        if not item.get("is_archive") and not item.get("is_storage"):
                            vacancy = to_hh(item)
                            found.setdefault(vacancy["id"], vacancy)
                    if not data.get("more"):
                        break
        return list(found.values())

    def description(self, vacancy: Mapping[str, Any]) -> str:
        text = str(vacancy.get("description") or "")
        if text:
            return text
        vid = str(vacancy.get("id") or "")[len(PREFIX):]
        return str(to_hh(self._get(f"{API}{vid}/")).get("description") or "")

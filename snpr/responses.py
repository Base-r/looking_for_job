"""Сколько откликов уже у вакансии.

Официальный API hh этого не отдаёт; число есть только в данных страницы
вакансии на сайте (поле vacancyResponsesCount). Берём его для вакансий,
которые вы смотрите, и кэшируем на час в data/responses_cache.json, чтобы
не дёргать hh лишний раз. Не получилось — None (показываем «—»).
"""

from __future__ import annotations

import html
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

CACHE_FILE = "responses_cache.json"
CACHE_TTL = 3600.0
_RE = re.compile(r'"vacancyResponsesCount"\s*:\s*(\d+)')
_lock = threading.Lock()
SITE = "hh.uz"
# hh.ru закрыл сайт проверкой «VPN мешает работе сайта» — до этого момента
# страницы не запрашиваем (иначе 250 бесполезных запросов подряд)
BLOCK_PAUSE = 600.0
_blocked_until = 0.0
NONE_TTL = 600.0  # «нет числа» кэшируем коротко


def site_blocked() -> bool:
    return time.time() < _blocked_until


def parse_responses_count(page_html: str) -> int | None:
    text = page_html
    for _ in range(2):  # данные страницы бывают экранированы дважды
        if match := _RE.search(text):
            return int(match.group(1))
        text = html.unescape(text)
    return None


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def responses_count(session: Any, vacancy_id: str, data_dir: Path) -> int | None:
    """Число откликов (из кэша, если ему меньше часа)."""
    path = Path(data_dir) / CACHE_FILE
    vid = str(vacancy_id)
    with _lock:
        cached = _load(path).get(vid)
    if isinstance(cached, dict):
        ttl = CACHE_TTL if cached.get("count") is not None else NONE_TTL
        if time.time() - float(cached.get("ts") or 0) < ttl:
            return cached.get("count")
    if session is None or site_blocked():
        return None
    global _blocked_until
    try:
        # hh.uz — та же база hh, но без проверки VPN, которую hh.ru
        # включил для вашего подключения
        resp = session.get(f"https://{SITE}/vacancy/{vid}", timeout=20)
        if "vpncheeck" in str(getattr(resp, "url", "")):
            _blocked_until = time.time() + BLOCK_PAUSE
            return None
        count = parse_responses_count(resp.text) if resp.status_code == 200 else None
    except Exception:  # noqa: BLE001 — сеть/hh: просто нет числа
        count = None
    with _lock:
        cache = _load(path)
        cache[vid] = {"count": count, "ts": time.time()}
        # кэш не разрастается: держим последние 500 вакансий
        if len(cache) > 500:
            for old in sorted(cache, key=lambda k: cache[k].get("ts", 0))[:-500]:
                cache.pop(old, None)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return count

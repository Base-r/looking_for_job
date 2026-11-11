"""Отклики, уже отправленные с аккаунта hh (в том числе вручную на сайте).

applied.jsonl знает только отклики бота. Чтобы не показывать вакансии,
на которые вы уже откликались сами, каждый прогон (и открытая страница
отбора раз в несколько минут) читает GET /negotiations (все статусы):
id вакансий — в data/hh_applied.txt, а «название + работодатель» — в
data/hh_applied_keys.json, чтобы узнавать и повторные публикации той же
вакансии под новым id. Сбой сети — используем последний сохранённый список.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HH_APPLIED_FILE = "hh_applied.txt"
HH_APPLIED_KEYS_FILE = "hh_applied_keys.json"
# Отметки hh в карточке вакансии: отклик есть (в т.ч. приглашение/отказ)
APPLIED_RELATIONS = {"got_response", "got_invitation", "got_rejection"}


def fetch_hh_applied(
    api_client: Any, max_pages: int = 50
) -> tuple[set[str], set[tuple[str, str]]]:
    """(id вакансий, ключи «название + работодатель») всех откликов."""
    from .dedupe import vacancy_key

    ids: set[str] = set()
    keys: set[tuple[str, str]] = set()
    page = 0
    while page < max_pages:
        res = api_client.get("/negotiations", {"per_page": 100, "page": page})
        if not isinstance(res, dict):
            break
        for item in res.get("items") or []:
            vacancy = item.get("vacancy") or {}
            vid = str(vacancy.get("id") or "")
            if vid:
                ids.add(vid)
            if key := vacancy_key(vacancy):
                keys.add(key)
        page += 1
        if page >= int(res.get("pages") or 1):
            break
    return ids, keys


def fetch_hh_applied_ids(api_client: Any, max_pages: int = 50) -> set[str]:
    return fetch_hh_applied(api_client, max_pages)[0]


def load_hh_applied(data_dir: Path) -> set[str]:
    path = Path(data_dir) / HH_APPLIED_FILE
    if not path.exists():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def load_hh_applied_keys(data_dir: Path) -> set[tuple[str, str]]:
    path = Path(data_dir) / HH_APPLIED_KEYS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {tuple(k) for k in data if isinstance(k, list) and len(k) == 2}


def sync_hh_applied(api_client: Any, data_dir: Path, save: bool = True) -> set[str]:
    """Свежий список откликов с hh; при сбое — сохранённый."""
    try:
        ids, keys = fetch_hh_applied(api_client)
    except Exception as ex:  # noqa: BLE001 — сеть/hh: прогон не роняем
        cached = load_hh_applied(data_dir)
        print(f"[отклики] список с hh не получен ({ex}); беру сохранённый: {len(cached)}")
        return cached
    if save:
        path = Path(data_dir) / HH_APPLIED_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(sorted(ids)) + ("\n" if ids else ""), encoding="utf-8")
        (Path(data_dir) / HH_APPLIED_KEYS_FILE).write_text(
            json.dumps(sorted(list(k) for k in keys), ensure_ascii=False), encoding="utf-8"
        )
    return ids


def card_already_applied(card: Any) -> bool:
    """Карточка вакансии hh (GET /vacancies/{id}) говорит, что отклик есть."""
    if not isinstance(card, dict):
        return False
    return bool(APPLIED_RELATIONS & set(card.get("relations") or []))

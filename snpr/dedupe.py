"""Склейка дублей: одна и та же вакансия, опубликованная работодателем
несколько раз (то же название + тот же работодатель, разные id).

Ключ — нормализованное название и имя работодателя. Повтор того, что уже
оценено, стоит в очереди или на что был отклик, к вам не попадает.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

_SPACES = re.compile(r"\s+")
_QUOTES = re.compile(r"[«»\"'`]")


def _norm(text: object) -> str:
    return _SPACES.sub(" ", _QUOTES.sub("", str(text or ""))).strip().lower()


def key_of(name: object, employer: object) -> tuple[str, str] | None:
    n, e = _norm(name), _norm(employer)
    return (n, e) if n and e else None


def vacancy_key(vacancy: Mapping[str, Any]) -> tuple[str, str] | None:
    return key_of(vacancy.get("name"), (vacancy.get("employer") or {}).get("name"))


def _jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                rows.append(rec)
    return rows


def known_keys(data_dir: Path) -> set[tuple[str, str]]:
    """Ключи вакансий, которые бот уже видел с оценкой, ставил в очередь
    или на которые откликался."""
    keys: set[tuple[str, str]] = set()
    for rec in _jsonl(data_dir / "scores.jsonl"):
        if k := key_of(rec.get("name"), rec.get("employer")):
            keys.add(k)
    for rec in _jsonl(data_dir / "applied.jsonl"):
        if k := key_of(rec.get("vacancy_name"), rec.get("employer")):
            keys.add(k)
    for rec in _jsonl(data_dir / "parked.jsonl"):
        if k := vacancy_key(rec.get("vacancy") or {}):
            keys.add(k)
    from .applied_sync import load_hh_applied_keys

    # отклики с сайта hh (не только через бота) — по названию и работодателю
    return keys | load_hh_applied_keys(data_dir)


def split_duplicates(
    vacancies: Iterable[Mapping[str, Any]], known: set[tuple[str, str]]
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """(оставить, дубли): первый экземпляр каждого ключа остаётся, если
    такого ключа ещё не было в known; без работодателя/названия — оставляем."""
    keep: list[Mapping[str, Any]] = []
    dupes: list[Mapping[str, Any]] = []
    seen = set(known)
    for vacancy in vacancies:
        k = vacancy_key(vacancy)
        if k is None:
            keep.append(vacancy)
        elif k in seen:
            dupes.append(vacancy)
        else:
            seen.add(k)
            keep.append(vacancy)
    return keep, dupes


def dedupe_parked(parked_store: Any, seen_store: Any | None = None) -> int:
    """В очереди на рассмотрение оставить по одной вакансии на ключ —
    с наибольшим скором; остальные убрать (и пометить seen как дубль)."""
    records = parked_store.load_records()
    best: dict[tuple[str, str], dict[str, Any]] = {}

    def score(rec: Mapping[str, Any]) -> int:
        data = rec.get("score") if isinstance(rec.get("score"), dict) else {}
        return int(data.get("score") or 0)

    for rec in records:
        k = vacancy_key(rec.get("vacancy") or {})
        if k is None:
            continue
        if k not in best or score(rec) > score(best[k]):
            best[k] = rec
    drop = []
    for rec in records:
        k = vacancy_key(rec.get("vacancy") or {})
        if k is not None and best.get(k) is not rec:
            drop.append(rec)
    if not drop:
        return 0
    ids = [str((rec.get("vacancy") or {}).get("id") or "") for rec in drop]
    if seen_store is not None:
        for rec, vid in zip(drop, ids):
            seen_store.mark_seen(
                vid, name=str((rec.get("vacancy") or {}).get("name") or ""), status="duplicate"
            )
    parked_store.remove(ids)
    return len(drop)

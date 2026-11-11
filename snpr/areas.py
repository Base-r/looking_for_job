"""Страна вакансии по региону hh: справочник GET /areas кэшируется в
data/areas_country.json (id региона → страна). Нужен списку вакансий,
чтобы было видно, откуда вакансия: Ташкент · Узбекистан, Алматы · Казахстан.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .personal import PROJECT_ROOT

AREAS_CACHE = PROJECT_ROOT / "data" / "areas_country.json"
_cache: dict[str, str] | None = None


def build_country_map(tree: Any) -> dict[str, str]:
    mapping: dict[str, str] = {}

    def walk(node: Any, country: str) -> None:
        mapping[str(node.get("id"))] = country
        for child in node.get("areas") or []:
            walk(child, country)

    for top in tree if isinstance(tree, list) else []:
        walk(top, str(top.get("name") or ""))
    return mapping


def ensure_areas(api_client: Any, path: Path = AREAS_CACHE) -> None:
    """Скачать справочник один раз (он почти не меняется)."""
    global _cache
    if path.exists():
        return
    try:
        mapping = build_country_map(api_client.get("/areas"))
    except Exception as ex:  # noqa: BLE001 — без справочника просто нет страны
        print(f"[areas] справочник регионов не получен: {ex}")
        return
    if mapping:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
        _cache = mapping


def country_of(area_id: object, path: Path = AREAS_CACHE) -> str:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache = {}
    return _cache.get(str(area_id or ""), "")

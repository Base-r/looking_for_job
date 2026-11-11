"""Вакансии, ушедшие в архив на hh, убираются из очереди."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from snpr.approve_service import ARCHIVE_CHECK_FILE, sweep_archived
from snpr.sne import ParkedStore, SeenStore


class ResourceNotFound(Exception):
    pass


class FakeApi:
    def __init__(self, cards: dict[str, Any]):
        self.cards = cards
        self.calls: list[str] = []

    def get(self, endpoint: str, params: Any = None) -> Any:
        vid = endpoint.rsplit("/", 1)[-1]
        self.calls.append(vid)
        card = self.cards[vid]
        if isinstance(card, Exception):
            raise card
        return card


def _store(tmp_path: Path, ids: list[str]) -> ParkedStore:
    parked = ParkedStore(tmp_path / "parked.jsonl")
    for n, vid in enumerate(ids):
        parked.append_record(
            {"id": vid, "name": f"PM {vid}", "published_at": f"2026-10-0{n + 1}T10:00:00+0300"},
            None,
            "Письмо.",
        )
    return parked


def test_archived_and_deleted_leave_queue(tmp_path: Path) -> None:
    parked = _store(tmp_path, ["1", "2", "3"])
    seen = SeenStore(tmp_path / "seen.jsonl")
    api = FakeApi({"1": {"archived": True}, "2": {"archived": False}, "3": ResourceNotFound()})
    assert sweep_archived(api, parked, seen, tmp_path, pause=0) == 2
    assert parked.ids() == {"2"}
    # проверенную недавно вакансию повторно не запрашиваем
    api.calls.clear()
    assert sweep_archived(api, parked, seen, tmp_path, pause=0) == 0
    assert api.calls == []


def test_captcha_stops_sweep_and_keeps_queue(tmp_path: Path) -> None:
    parked = _store(tmp_path, ["1", "2"])
    seen = SeenStore(tmp_path / "seen.jsonl")
    api = FakeApi({"1": RuntimeError("captcha_required"), "2": {"archived": True}})
    assert sweep_archived(api, parked, seen, tmp_path, pause=0) == 0
    assert api.calls == ["1"]  # остановились на первой ошибке
    assert parked.ids() == {"1", "2"}
    assert json.loads((tmp_path / ARCHIVE_CHECK_FILE).read_text(encoding="utf-8")) == {}


def test_budget_checks_oldest_first(tmp_path: Path) -> None:
    parked = _store(tmp_path, ["old", "mid", "new"])
    api = FakeApi({v: {"archived": False} for v in ("old", "mid", "new")})
    sweep_archived(api, parked, SeenStore(tmp_path / "seen.jsonl"), tmp_path, budget=2, pause=0)
    assert api.calls == ["old", "mid"]

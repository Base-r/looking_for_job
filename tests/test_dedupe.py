"""Склейка дублей: та же вакансия (название + работодатель) с разными id."""

from __future__ import annotations

import json
from pathlib import Path

from snpr.dedupe import dedupe_parked, known_keys, split_duplicates, vacancy_key
from snpr.scoring import ScoreResult
from snpr.sne import ParkedStore, SeenStore


def _v(vid: str, name: str = "Ассистент аккаунт-менеджера", emp: str = "Пикасель") -> dict:
    return {"id": vid, "name": name, "employer": {"name": emp}}


def test_key_normalizes_case_quotes_spaces() -> None:
    assert vacancy_key(_v("1", "Technical  writer", "«Genesis» Group")) == vacancy_key(
        _v("2", "technical writer", "Genesis Group")
    )
    assert vacancy_key({"id": "3", "name": "PM"}) is None  # без работодателя — не склеиваем


def test_split_duplicates_within_pool_and_against_known() -> None:
    pool = [_v("1"), _v("2"), _v("3", "Project Manager"), _v("4", "PM", "Другая"), {"id": "5", "name": "X"}]
    known = {vacancy_key(_v("9", "Project Manager"))}
    keep, dupes = split_duplicates(pool, known)
    assert [v["id"] for v in keep] == ["1", "4", "5"]
    assert [v["id"] for v in dupes] == ["2", "3"]


def test_known_keys_from_stores(tmp_path: Path) -> None:
    (tmp_path / "scores.jsonl").write_text(
        json.dumps({"vacancy_id": "1", "name": "PM", "employer": "Acme"}) + "\n", encoding="utf-8"
    )
    (tmp_path / "applied.jsonl").write_text(
        json.dumps({"vacancy_id": "2", "vacancy_name": "BA", "employer": "Beta"}) + "\n", encoding="utf-8"
    )
    ParkedStore(tmp_path / "parked.jsonl").append_record(_v("3", "QA", "Gamma"), None, "x")
    assert known_keys(tmp_path) == {("pm", "acme"), ("ba", "beta"), ("qa", "gamma")}


def test_dedupe_parked_keeps_best_score(tmp_path: Path) -> None:
    parked = ParkedStore(tmp_path / "parked.jsonl")
    for vid, score in (("1", 40), ("2", 64), ("3", 55)):
        parked.append_record(_v(vid), ScoreResult(vacancy_id=vid, score=score), "письмо")
    parked.append_record(_v("4", "Project Manager", "Acme"), ScoreResult(vacancy_id="4", score=70), "x")
    seen = SeenStore(tmp_path / "seen.jsonl")
    assert dedupe_parked(parked, seen) == 2
    assert parked.ids() == {"2", "4"}  # из трёх Пикасель остался скор 64
    assert seen.seen_ids() == {"1", "3"}
    assert dedupe_parked(parked, seen) == 0


def test_replace_file_retries_on_windows_lock(tmp_path: Path, monkeypatch) -> None:
    from snpr import sne

    calls = {"n": 0}
    real = Path.replace

    def flaky(self, target):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("WinError 5")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", flaky)
    monkeypatch.setattr(sne.time, "sleep", lambda _s: None)
    tmp, target = tmp_path / "a.tmp", tmp_path / "a.jsonl"
    tmp.write_text("x", encoding="utf-8")
    sne._replace_file(tmp, target)
    assert target.read_text(encoding="utf-8") == "x" and calls["n"] == 3

"""Журнал источников: уровни, серия сбоев, сбой источника не роняет сбор."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from snpr.source_log import SourceLog
from snpr.sources import ExternalSources
from snpr.telegram_jobs import TelegramSource


def _levels(path: Path) -> list[str]:
    return [line.split(" | ")[1] for line in path.read_text(encoding="utf-8").splitlines()]


def test_third_failure_in_a_row_is_critical(tmp_path: Path) -> None:
    log = SourceLog(tmp_path / "sources.log")
    log.ok("habr", 5, 1)
    for _ in range(3):
        log.error("habr", "503")
    assert _levels(log.path) == ["INFO", "ERROR", "ERROR", "CRITICAL"]


def test_success_resets_failures(tmp_path: Path) -> None:
    log = SourceLog(tmp_path / "sources.log")
    log.error("habr", "503")
    log.error("habr", "503")
    log.ok("habr", 5, 1)
    log.error("habr", "503")
    assert _levels(log.path)[-1] == "ERROR"


def test_other_source_does_not_count(tmp_path: Path) -> None:
    log = SourceLog(tmp_path / "sources.log")
    log.error("habr", "503")
    log.error("geekjob", "503")
    log.error("habr", "503")
    assert _levels(log.path) == ["ERROR", "ERROR", "ERROR"]


def test_zero_items_is_warning(tmp_path: Path) -> None:
    log = SourceLog(tmp_path / "sources.log")
    log.ok("habr", 0, 0)
    assert _levels(log.path) == ["WARN"]


def test_failed_source_is_logged_and_others_continue(tmp_path: Path) -> None:
    class Bad:
        def list_all(self, max_age_days: float = 0) -> list[dict[str, Any]]:
            raise ConnectionError("нет связи")

    class Good:
        def list_all(self, max_age_days: float = 0) -> list[dict[str, Any]]:
            return [{"id": "hc:1", "name": "Системный аналитик", "employer": {"name": "A"}}]

    log = SourceLog(tmp_path / "sources.log")
    ext = ExternalSources({}, log=log)
    ext.active = {"geekjob": Bad(), "habr": Good()}
    assert [v["id"] for v in ext.collect(7, set())] == ["hc:1"]
    text = log.path.read_text(encoding="utf-8")
    assert "geekjob" in text and "нет связи" in text and "habr" in text


def test_telegram_channel_failure_reported_per_channel() -> None:
    class Session:
        def get(self, url: str, **_kw: Any) -> Any:
            raise TimeoutError("таймаут")

    issues: list[tuple[str, bool]] = []
    src = TelegramSource(["one"], session=Session(), pause=0)
    src.on_issue = lambda channel, _text, failed: issues.append((channel, failed))
    assert src.list_all(max_age_days=7) == []
    assert issues == [("one", True)]

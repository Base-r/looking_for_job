"""Возраст вакансии с публикации: [search] min_age_days / max_age_days."""

from __future__ import annotations

from datetime import datetime, timezone

from snpr.sne import _window_params, build_search_params, resolve_age_window

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def test_age_window_from_config() -> None:
    assert resolve_age_window({"min_age_days": 1, "max_age_days": 7}) == (168.0, 24.0)


def test_age_window_off_by_default() -> None:
    assert resolve_age_window({}) is None
    assert resolve_age_window({"min_age_days": 1, "max_age_days": 0}) is None


def test_cli_flags_override_config() -> None:
    cfg = {"min_age_days": 1, "max_age_days": 7}
    assert resolve_age_window(cfg, 0, 3) == (72.0, 0.0)
    assert resolve_age_window(cfg, 10, 3) == (72.0, 72.0)  # min не больше max


def test_search_params_get_date_to() -> None:
    params = build_search_params("pm", 168, 100, now=NOW, until_hours=24)
    assert params["date_from"] == "2026-10-19T12:00:00+00:00"
    assert params["date_to"] == "2026-10-25T12:00:00+00:00"
    assert "date_to" not in build_search_params("pm", 168, 100, now=NOW)
    window = _window_params(168, 100, 0, {}, now=NOW, until_hours=24)
    assert window["date_to"] == "2026-10-25T12:00:00+00:00"

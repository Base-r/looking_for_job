"""Тесты персонализации через config (18.10 ~12:40): профиль скоринга,
профиль-капсула и текст письма живут в config.toml, другой человек
меняет их без правки кода. Дефолты в коде — фоллбек."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import tomllib

from snpr.cli import _letter_profile, _letter_static_text, _scoring_profile
from snpr.cover_letter import (
    PROFILE_CAPSULE,
    STATIC_LETTER_TEXT,
    letter_system_prompt,
)
from snpr.letter_guard import extract_numbers
from snpr.scoring import DEFAULT_SCORING_PROFILE, VacancyScorer

CONFIG = (
    Path(__file__).parent.parent / "snpr" / "config.toml"
)


def _cfg() -> dict[str, Any]:
    with CONFIG.open("rb") as fh:
        return tomllib.load(fh)


def test_config_profiles_come_from_files() -> None:
    """Профиль — profile.md, стиль — letter_style.md ([files] в конфиге);
    скоринг и письмо видят один и тот же профиль."""
    from snpr.personal import load_letter_style, load_profile

    cfg = _cfg()
    profile = load_profile(cfg)
    assert profile and "Нагуслаев" in profile
    assert "<!--" not in profile  # комментарии для человека вырезаны
    assert _scoring_profile(cfg) == profile
    assert _letter_profile(cfg) == profile
    style = load_letter_style(cfg)
    assert style and "Тон" in style
    assert _letter_static_text(cfg) == cfg["letter"]["static_text"].strip()


def test_config_profile_helpers_fallback() -> None:
    """Без [files] — старые ключи конфига, без них — None (дефолт в коде)."""
    cfg = {
        "scoring": {"profile": " A "},
        "letter": {"profile": " B ", "static_text": " C "},
    }
    assert _scoring_profile(cfg) == "A"
    assert _letter_profile(cfg) == "B"
    assert _letter_static_text(cfg) == "C"
    assert _scoring_profile({}) is None
    assert _letter_profile({}) is None
    assert _letter_static_text({}) is None
    assert _scoring_profile({"scoring": {"profile": "  "}}) is None


class _FakeAI:
    def complete(self, message: str, **kwargs: Any) -> str:
        return "{}"


def test_scorer_profile_overrides_system_prompt() -> None:
    """Чужой профиль попадает в системный промпт, дефолт — не теряется."""
    custom = "- Middle QA, ручное тестирование, Москва"
    scorer = VacancyScorer(_FakeAI(), profile=custom)  # type: ignore[arg-type]
    assert custom in scorer.system_prompt
    assert "Senior/Lead AI-инженер" not in scorer.system_prompt

    default = VacancyScorer(_FakeAI())  # type: ignore[arg-type]
    assert "Senior/Lead" in default.system_prompt
    assert '"score": <int 0-100>' in default.system_prompt  # контракт цел


def test_letter_system_prompt_profile() -> None:
    custom = "- Middle QA из Нижнего"
    assert custom in letter_system_prompt(custom)
    assert "Ромашка" in letter_system_prompt(None)


def test_guard_numbers_follow_profile_file() -> None:
    """Числа гварда берутся из profile.md: сменил факты — сменились
    разрешённые числа."""
    cfg = _cfg()
    numbers = extract_numbers(_letter_profile(cfg))
    assert {"103", "41", "820", "270"} <= numbers
    custom = "- 15 лет опыта, 3 команды"
    assert extract_numbers(custom) == {"15", "3"}

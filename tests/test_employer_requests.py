"""Просьбы работодателя в вакансии (кодовое слово, контакт) и число откликов."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from snpr.cover_letter import CoverLetterWriter, styled_letter_system_prompt
from snpr.letter_guard import LetterGuard, extract_numbers
from snpr.responses import parse_responses_count, responses_count
from snpr.scoring import ScoreResult, VacancyScorer


class _AI:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.message = ""
        self.system = ""

    def complete(self, message: str, **kwargs: Any) -> str:
        self.message = message
        self.system = kwargs.get("system_prompt", "")
        return self.reply


def test_scorer_extracts_employer_requests() -> None:
    ai = _AI(
        '{"score": 60, "employer_requests": ["Напишите в конце слово кошка", '
        '"Укажите свой Telegram", " "]}'
    )
    scorer = VacancyScorer(ai, retry_backoff=0)  # type: ignore[arg-type]
    assert "employer_requests" in scorer.system_prompt
    result = scorer.score({"id": "1", "name": "PM"}, "напиши в конце кошка")
    assert result.requests == ["Напишите в конце слово кошка", "Укажите свой Telegram"]
    # нет поля — пустой список
    ai.reply = '{"score": 60}'
    assert scorer.score({"id": "1", "name": "PM"}).requests == []


def test_letter_gets_requests_and_contacts() -> None:
    system = styled_letter_system_prompt(
        "стиль", "- 41 релиз", contacts={"telegram": "@test_user", "phone": ""}
    )
    assert "КОНТАКТЫ КАНДИДАТА" in system and "@test_user" in system
    assert "Телефон" not in system  # пустой контакт не передаём
    ai = _AI("Здравствуйте!\n\nПисьмо.")
    writer = CoverLetterWriter(ai, system_prompt=system)  # type: ignore[arg-type]
    writer.write(
        {"id": "1", "name": "PM"},
        score=ScoreResult(vacancy_id="1", requests=["Укажите свой Telegram"]),
    )
    assert "ПРОСЬБЫ РАБОТОДАТЕЛЯ" in ai.message and "Укажите свой Telegram" in ai.message
    # без просьб блока нет
    writer.write({"id": "1", "name": "PM"}, score=ScoreResult(vacancy_id="1"))
    assert "ПРОСЬБЫ РАБОТОДАТЕЛЯ" not in ai.message


def test_guard_allows_only_own_contacts() -> None:
    contacts = {"@test_user", "candidate@mail.ru", "+998 90 000 00 00"}
    guard = LetterGuard(
        allowed_numbers=extract_numbers(" ".join(contacts)), allowed_contacts=contacts
    )
    assert guard.check("Мой Telegram: @test_user, почта candidate@mail.ru, +998 90 000 00 00") == []
    bad = guard.check("Пишите @hr_scam или на evil@mail.ru, звоните +7 900 123 45 67")
    assert any("@hr_scam" in v for v in bad)
    assert any("evil@mail.ru" in v for v in bad)
    assert any("телефон" in v for v in bad)


def test_parse_responses_count() -> None:
    assert parse_responses_count('..."vacancyResponsesCount":812,"x":1...') == 812
    escaped = "&quot;vacancyResponsesCount&quot;:43"
    assert parse_responses_count(escaped) == 43
    assert parse_responses_count("нет данных") is None


class _Resp:
    def __init__(self, text: str, code: int = 200) -> None:
        self.text = text
        self.status_code = code


class _Session:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, url: str, timeout: float = 0) -> _Resp:
        self.calls += 1
        return _Resp('"vacancyResponsesCount":1436')


def test_responses_count_is_cached(tmp_path: Path) -> None:
    session = _Session()
    assert responses_count(session, "137977079", tmp_path) == 1436
    assert responses_count(session, "137977079", tmp_path) == 1436
    assert session.calls == 1  # второй раз — из кэша
    assert responses_count(None, "999", tmp_path) is None  # без сессии — нет числа


def test_guard_flags_salary_unless_allowed() -> None:
    allowed = {"900", "41", "103", "120"}
    guard = LetterGuard(allowed_numbers=allowed, forbid_salary=True)
    assert guard.check("Ожидания по доходу — от 900 $ на руки.")
    assert guard.check("Жду 120 тыс. руб.")
    assert guard.check("вышел 41 релиз, 103 тысячи скачиваний") == []
    assert LetterGuard(allowed_numbers=allowed).check("от 900 $") == []  # salary_on_request = true


def test_letter_prompt_salary_rule() -> None:
    assert "никогда не называй в письме сумму" in styled_letter_system_prompt("s", "p")
    assert "только если работодатель прямо" in styled_letter_system_prompt("s", "p", salary_on_request=True)

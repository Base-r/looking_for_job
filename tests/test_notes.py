"""Пометки «ℹ️»: только из России, тестовое задание; без ложных срабатываний."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from snpr.notes import RU_ONLY, TEST_TASK, notes
from snpr.sne import _add_notes
from snpr.telegram_api import TelethonSource
from snpr.telegram_jobs import TelegramSource


@pytest.mark.parametrize("text", [
    "Удалённо по России, полный день",
    "Удаленная работа из РФ",
    "Работа на территории РФ обязательна",
    "Необходимо находиться в России",
    "Только налоговые резиденты РФ",
    "Работаем удаленно на территории России",
])
def test_ru_only(text: str) -> None:
    assert RU_ONLY in notes(text)


@pytest.mark.parametrize("text", [
    "Лидер рынка на территории РФ, 20 офисов",
    "Клиенты в России и Казахстане",
    "Удалённо, из любой страны",
])
def test_not_ru_only(text: str) -> None:
    assert RU_ONLY not in notes(text)


def test_test_task() -> None:
    assert notes("Этапы: тестовое задание, интервью") == [TEST_TASK]
    assert notes("Short take-home assignment") == [TEST_TASK]
    assert notes("Пишем автотесты и тестируем API") == []


def test_add_notes_strips_html() -> None:
    v = {"name": "PM"}
    _add_notes(v, "<p>Удалённо <b>по России</b></p><p>Тестовое задание</p>")
    assert v["notes"] == [RU_ONLY, TEST_TASK]


class Client:
    def __init__(self, dates):
        self.dates = dates

    async def iter_messages(self, channel, limit=0):
        for i, d in enumerate(self.dates):
            yield SimpleNamespace(id=i, message="", action=None, date=d, entities=None, chat=None)


def test_silent_channel_is_reported() -> None:
    issues = []
    src = TelethonSource(["c"], pause=0, web=TelegramSource(pause=0))
    src.on_issue = lambda *a: issues.append(a)
    now = datetime.now(timezone.utc)
    asyncio.run(src._channel(Client([now - timedelta(days=45)]), "c", now - timedelta(days=7)))
    asyncio.run(src._channel(Client([now - timedelta(days=2)]), "d", now - timedelta(days=7)))
    assert [i[0] for i in issues] == ["c"] and "молчит" in issues[0][1] and issues[0][2] is False

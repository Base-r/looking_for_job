"""Telegram через API: тот же формат, что у веб-превью, и откат на веб-превью."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from snpr.sources import ExternalSources
from snpr.telegram_api import TelethonSource, message_to_vacancy
from snpr.telegram_jobs import TelegramSource

TEXT = (
    "Вакансия: Product Analyst\n"
    "Компания: Ozon\n"
    "Удалённо, полный день. Нужен опыт SQL, A/B-тестов и продуктовых метрик.\n"
    "Писать в личку, подробности по ссылке."
)


def msg(mid: int, text: str = TEXT, days_ago: float = 0, **kw: Any) -> SimpleNamespace:
    return SimpleNamespace(
        id=mid, message=text, action=None,
        date=datetime.now(timezone.utc) - timedelta(days=days_ago),
        entities=[SimpleNamespace(url="https://hh.ru/vacancy/123"), SimpleNamespace()],
        chat=SimpleNamespace(username="Analysts_Jobs"), **kw,
    )


def test_message_to_vacancy_same_format_as_web() -> None:
    v = message_to_vacancy("Analysts_Jobs", msg(3383))
    assert v is not None
    assert v["id"] == "tg:Analysts_Jobs/3383"
    assert v["name"] == "Product Analyst" and v["employer"]["name"] == "Ozon"
    assert v["work_format"] == [{"id": "REMOTE"}]
    assert v["alternate_url"] == "https://t.me/Analysts_Jobs/3383"
    assert v["links"] == ["https://hh.ru/vacancy/123"]
    assert v["published_at"]


def test_short_service_and_digest_skipped() -> None:
    assert message_to_vacancy("c", msg(1, "коротко")) is None
    service = SimpleNamespace(id=2, message=TEXT, action=object(), date=None, entities=None)
    assert message_to_vacancy("c", service) is None
    digest = "Подборка вакансий недели\n" + "Product Analyst в Ozon, Data Analyst в Avito и ещё 10.\n" * 3
    assert message_to_vacancy("c", msg(3, digest)) is None


def test_resume_posts_skipped() -> None:
    assert message_to_vacancy("c", msg(4, "#резюме #opentowork\n" + TEXT)) is None
    assert message_to_vacancy("c", msg(5, "#vacancy #cv\n" + TEXT)) is not None


class FakeClient:
    def __init__(self, messages: list[SimpleNamespace]):
        self.messages = messages

    async def iter_messages(self, channel: str, limit: int = 0):
        for m in self.messages[:limit]:
            yield m


def test_channel_stops_at_window_and_uses_server_name() -> None:
    src = TelethonSource(["analysts_jobs"], pause=0, web=TelegramSource(pause=0))
    client = FakeClient([msg(5), msg(4, days_ago=3), msg(3, days_ago=30)])
    limit = datetime.now(timezone.utc) - timedelta(days=7)
    items = asyncio.run(src._channel(client, "analysts_jobs", limit))
    assert [v["id"] for v in items] == ["tg:Analysts_Jobs/5", "tg:Analysts_Jobs/4"]


class FakeWeb:
    on_issue: Any = None

    def __init__(self) -> None:
        self.called = False

    def list_all(self, max_age_days: float = 0) -> list[dict[str, Any]]:
        self.called = True
        return [{"id": "tg:x/1"}]


def test_no_session_falls_back_to_web(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "h")
    web = FakeWeb()
    issues: list[tuple[str, str, bool]] = []
    src = TelethonSource(["c"], pause=0, data_dir=tmp_path, web=web)  # type: ignore[arg-type]
    src.on_issue = lambda *a: issues.append(a)
    assert "нет входа" in src.ready()
    assert src.list_all(7) == [{"id": "tg:x/1"}] and web.called
    assert issues and issues[0][2] is False  # не настроено — предупреждение, не сбой


def test_api_failure_falls_back_and_is_logged_as_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "h")
    (tmp_path / "telegram.session").write_text("")
    web = FakeWeb()
    issues: list[tuple[str, str, bool]] = []
    src = TelethonSource(["c"], pause=0, data_dir=tmp_path, web=web)  # type: ignore[arg-type]
    src.on_issue = lambda *a: issues.append(a)

    async def broken(_: float) -> list[dict[str, Any]]:
        raise ConnectionError("нет сети")

    monkeypatch.setattr(src, "_read", broken)
    assert src.list_all(7) == [{"id": "tg:x/1"}]
    assert issues[0][2] is True and "нет сети" in issues[0][1]


def test_no_keys_reason(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("TG_API_ID", raising=False)
    src = TelethonSource(["c"], pause=0, data_dir=tmp_path, web=TelegramSource(pause=0))
    assert "TG_API_ID" in src.ready()


def test_backend_choice() -> None:
    web = ExternalSources({"telegram": True, "telegram_channels": ["c"]}).active["telegram"]
    api = ExternalSources(
        {"telegram": True, "telegram_channels": ["c"], "telegram_backend": "telethon"}
    ).active["telegram"]
    assert isinstance(web, TelegramSource) and isinstance(api, TelethonSource)

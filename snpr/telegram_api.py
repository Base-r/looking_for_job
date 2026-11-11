"""Telegram через официальное API (Telethon) — чтение каналов от вашего аккаунта.

Зачем: веб-превью t.me/s отдаёт по ~20 постов на страницу и закрыто
у части каналов; API читает любой публичный канал без вступления в него.

Что бот делает: только читает текст постов (и подписи к медиа) за окно
поиска. Медиа, файлы и ссылки не открывает, в каналы не вступает, ничего
не пишет. Вакансия собирается тем же разбором, что и из веб-превью, id
тот же («tg:<канал>/<номер>») — история просмотренных общая.

Ключи: TG_API_ID и TG_API_HASH в .env (my.telegram.org → API development
tools). Сессия входа — data/telegram.session: это доступ к аккаунту, файл
нельзя никому передавать (data/ в git не попадает).

Первый вход — один раз, вручную в терминале:
    .venv\\Scripts\\python.exe -X utf8 -m snpr.telegram_api login
Плановый прогон никогда не спрашивает телефон или код: нет сессии, ключей
или Telegram не ответил — посты читаются через веб-превью, а в
data/sources.log пишется причина.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .telegram_jobs import _DIGEST, TelegramSource, to_vacancy

ID_ENV = "TG_API_ID"
HASH_ENV = "TG_API_HASH"
SESSION_NAME = "telegram"  # → data/telegram.session
DEFAULT_DATA = Path(__file__).resolve().parent.parent / "data"
MAX_MESSAGES = 300  # на канал за прогон: потолок даже при длинном окне
MIN_LENGTH = 150  # как у веб-превью: короткие посты — не вакансии
SILENT_DAYS = 30  # канал без постов дольше — предупреждение в журнал
FLOOD_SLEEP = 60  # ждать «подождите N секунд» от Telegram, если N не больше


class NotLoggedIn(RuntimeError):
    pass


def keys() -> tuple[int, str] | None:
    api_id = os.environ.get(ID_ENV, "").strip()
    api_hash = os.environ.get(HASH_ENV, "").strip()
    if not api_id.isdigit() or not api_hash:
        return None
    return int(api_id), api_hash


def session_path(data_dir: Path | None = None) -> Path:
    return Path(data_dir or DEFAULT_DATA) / SESSION_NAME


def _text(raw: str) -> str:
    """Те же правила, что у веб-превью: строки без лишних пробелов, без пустых."""
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in raw.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def message_to_vacancy(channel: str, msg: Any) -> dict[str, Any] | None:
    """Сообщение Telethon → вакансия (или None: служебное, короткое, подборка)."""
    if getattr(msg, "action", None) is not None:
        return None
    text = _text(str(getattr(msg, "message", "") or ""))
    if len(text) < MIN_LENGTH:
        return None
    date = getattr(msg, "date", None)
    vacancy = to_vacancy(channel, str(msg.id), text, date.isoformat() if date else "")
    if not vacancy or _DIGEST.match(vacancy["name"]):
        return None
    # только для сверки дублей: по этим ссылкам бот не ходит
    vacancy["links"] = [
        str(e.url) for e in getattr(msg, "entities", None) or [] if getattr(e, "url", None)
    ]
    return vacancy


class TelethonSource:
    """Каналы через API; при любой беде с входом — веб-превью (fallback)."""

    def __init__(
        self,
        channels: Iterable[str] = (),
        pause: float = 2.0,
        data_dir: Path | None = None,
        web: TelegramSource | None = None,
    ):
        self.channels = [c.strip().lstrip("@") for c in channels if c and c.strip()]
        self.pause = pause
        self.session = session_path(data_dir)
        self.web = web if web is not None else TelegramSource(channels=self.channels, pause=pause)
        self.on_issue: Any = None  # (канал, текст, это_сбой) — для журнала источников

    def _issue(self, channel: str, text: str, failed: bool = False) -> None:
        print(f"[telegram] @{channel}: {text}")
        if self.on_issue is not None:
            self.on_issue(channel, text, failed)

    def ready(self) -> str:
        """'' — можно читать через API; иначе причина, почему нельзя."""
        try:
            import telethon  # noqa: F401
        except ImportError:
            return "пакет telethon не установлен"
        if keys() is None:
            return f"нет {ID_ENV}/{HASH_ENV} в .env"
        if not self.session.with_suffix(".session").exists():
            return "нет входа в Telegram (python -m snpr.telegram_api login)"
        return ""

    def list_all(self, max_age_days: float = 0) -> list[dict[str, Any]]:
        reason, failed = self.ready(), False
        if not reason:
            try:
                return asyncio.run(self._read(max_age_days))
            except Exception as ex:  # noqa: BLE001 — API подвёл: читаем как раньше
                reason, failed = f"API недоступно ({type(ex).__name__}: {ex})", True
        self._issue("api", f"{reason} — читаю через веб-превью", failed=failed)
        self.web.on_issue = self.on_issue
        return self.web.list_all(max_age_days)

    async def _read(self, max_age_days: float) -> list[dict[str, Any]]:
        from telethon import TelegramClient
        from telethon.errors import FloodWaitError

        api_id, api_hash = keys() or (0, "")
        limit = (
            datetime.now(timezone.utc) - timedelta(days=max_age_days, hours=12)
            if max_age_days > 0 else None
        )
        client = TelegramClient(
            str(self.session), api_id, api_hash,
            flood_sleep_threshold=FLOOD_SLEEP, connection_retries=2, retry_delay=3, timeout=20,
        )
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise NotLoggedIn("сессия недействительна — войдите заново")
            out: list[dict[str, Any]] = []
            for n, channel in enumerate(self.channels):
                if n:
                    await asyncio.sleep(self.pause)
                try:
                    out.extend(await self._channel(client, channel, limit))
                except FloodWaitError as ex:
                    left = ", ".join(self.channels[n:])
                    self._issue(channel, f"Telegram просит подождать {ex.seconds} с; не прочитаны: {left}", failed=True)
                    break
                except Exception as ex:  # noqa: BLE001 — один канал не держит остальные
                    self._issue(channel, f"не прочитан: {type(ex).__name__}: {ex}", failed=True)
            return out
        finally:
            await client.disconnect()

    async def _channel(self, client: Any, channel: str, limit: datetime | None) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        newest = None
        async for msg in client.iter_messages(channel, limit=MAX_MESSAGES):
            newest = newest or msg.date
            if limit and msg.date and msg.date < limit:
                break
            # имя канала с сервера: «Analysts_Jobs», как в ссылках веб-превью
            name = getattr(getattr(msg, "chat", None), "username", None) or channel
            if vacancy := message_to_vacancy(name, msg):
                found.append(vacancy)
        if newest is None or datetime.now(timezone.utc) - newest > timedelta(days=SILENT_DAYS):
            when = newest.strftime("%d.%m.%Y") if newest else "никогда"
            self._issue(channel, f"канал молчит больше {SILENT_DAYS} дней (последний пост: {when}) — убрать из списка?")
        return found

    def description(self, vacancy: Mapping[str, Any]) -> str:
        """Текст поста хранится в вакансии; если нет — веб-превью."""
        return self.web.description(vacancy)


def login(data_dir: Path | None = None) -> None:
    """Первый вход: телефон, код из Telegram и облачный пароль вводите вы."""
    from telethon.sync import TelegramClient

    pair = keys()
    if pair is None:
        raise SystemExit(f"Нет {ID_ENV}/{HASH_ENV} в .env — сначала добавьте их.")
    path = session_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(path), *pair)
    client.start()  # спросит телефон, код и пароль двухэтапной проверки
    try:
        print("Вход выполнен. Сессия сохранена в", path.with_suffix(".session"))
        print("Этот файл — доступ к аккаунту: никому не передавайте.")
    finally:
        client.disconnect()


if __name__ == "__main__":
    import sys

    from .cli import load_dotenv

    load_dotenv()
    if sys.argv[1:] == ["login"]:
        login()
    else:
        print("Использование: python -X utf8 -m snpr.telegram_api login")

"""Вакансии из открытых Telegram-каналов.

Читаем веб-превью канала t.me/s/<канал> — его Telegram отдаёт без входа
в аккаунт; ни логина, ни API-ключей не нужно. Листаем назад (?before=)
пока посты не старше окна поиска.

Пост — свободный текст, поэтому название, компания, удалёнка и город
угадываются по тексту; описание вакансии — сам пост (хранится в вакансии,
повторно не скачивается). Отклик — по контактам в посте, вручную: на
странице отбора ссылка на пост и готовое письмо.
"""

from __future__ import annotations

import html
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from .geekjob import _experience, parse_salary

BASE = "https://t.me"
PREFIX = "tg:"
SOURCE = "telegram"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) hh-sniper"

_WRAP = 'class="tgme_widget_message_wrap'
_POST_RE = re.compile(r'data-post="([\w]+)/(\d+)"')
_TEXT_RE = re.compile(r'<div class="tgme_widget_message_text js-message_text"[^>]*>(.*?)</div>', re.S)
_TIME_RE = re.compile(r'<time datetime="([^"]+)"')
_LABEL_NAME = re.compile(r"^(?:должность|вакансия|позиция|vacancy|position|role|job title)\s*[:：]\s*(.+)$", re.I)
_LABEL_COMPANY = re.compile(r"^(?:компания|company|работодатель|employer)\s*[:：]\s*(.+)$", re.I)
_LABEL_SALARY = re.compile(r"^(?:зарплата|зп|з/п|оклад|salary|вилка)\s*[:：]\s*(.+)$", re.I)
_EMOJI_EDGE = re.compile(r"^[^\w«\"(]+|[^\w»\")+]+$")
_REMOTE_NO = re.compile(r"удал[её]нк\w*\s+нет|без удал|no remote|not remote|удаленка\s*[-—–]\s*нет", re.I)
_REMOTE = re.compile(r"удал[её]н|удаленк|удалёнк|\bremote\b|#remote|#удаленка|#удалёнка", re.I)
_TASHKENT = re.compile(r"ташкент|tashkent", re.I)
_DIGEST = re.compile(r"подборка\b|дайджест\b|digest\b", re.I)
_LEAD_IN = re.compile(
    r"^(?:в команду\s+)?(?:ищем|ищу|нужен|нужна|требуется|открыта вакансия|вакансия|"
    r"we are looking for|we're looking for|looking for|hiring)\s*[:\-—–]?\s*(?:an?\s+)?",
    re.I,
)


def is_telegram(vacancy_or_id: Any) -> bool:
    vid = vacancy_or_id.get("id") if isinstance(vacancy_or_id, Mapping) else vacancy_or_id
    return str(vid or "").startswith(PREFIX)


def post_text(fragment: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", fragment)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _clean(line: str) -> str:
    line = re.sub(r"#\S+", "", line)
    return _EMOJI_EDGE.sub("", line).strip(" -—–:|*")


def guess_title(text: str) -> tuple[str, str]:
    """Название и компания из текста поста."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    name = company = ""
    for ln in lines[:15]:
        bare = _clean(ln)
        if not name and (m := _LABEL_NAME.match(bare)):
            name = m.group(1)
        if not company and (m := _LABEL_COMPANY.match(bare)):
            company = m.group(1)
    if not name:
        for ln in lines[:5]:
            bare = _clean(ln)
            if len(re.findall(r"[A-Za-zА-Яа-яЁё]", bare)) >= 3 and not _LABEL_COMPANY.match(bare):
                name = bare
                break
    name = _LEAD_IN.sub("", _clean(name))
    # «Product Analyst в Ozon» → компания; «… в сегменте Pharma» — нет
    m = re.match(r"(.+?)\s+в\s+([«\"A-ZА-ЯЁ].*)$", name)
    if not company and m:
        name, company = m.group(1), m.group(2)
    name = _clean(name)[:120]
    company = _clean(company)[:80]
    return name, company


def _salary(text: str) -> dict[str, Any] | None:
    for ln in text.splitlines():
        if m := _LABEL_SALARY.match(_clean(ln)):
            return parse_salary(m.group(1))
    return None


def to_vacancy(channel: str, msg_id: str, text: str, posted: str, tashkent_id: str = "2759") -> dict[str, Any] | None:
    name, company = guess_title(text)
    if not name:
        return None
    remote = bool(_REMOTE.search(text)) and not _REMOTE_NO.search(text)
    tags = " ".join(re.findall(r"#\w+", text))
    published = ""
    try:
        published = datetime.fromisoformat(posted).strftime("%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError):
        pass
    vacancy: dict[str, Any] = {
        "id": f"{PREFIX}{channel}/{msg_id}",
        "name": name,
        "employer": {"name": company or f"@{channel}"},
        "area": {"id": tashkent_id, "name": "Ташкент"} if _TASHKENT.search(text) else {"id": "", "name": ""},
        "salary": _salary(text),
        "work_format": [{"id": "REMOTE"}] if remote else [],
        "published_at": published,
        "alternate_url": f"{BASE}/{channel}/{msg_id}",
        "source": SOURCE,
        "channel": channel,
        "description": text,
        "internship": bool(re.search(r"стаж[её]р|стажировк|intern", f"{name} {tags}", re.I)),
    }
    if exp := _experience(name, tags):
        vacancy["experience"] = exp
    return vacancy


def parse_channel(page_html: str, min_length: int = 150) -> list[dict[str, Any]]:
    """Посты-вакансии со страницы канала (служебные и короткие — мимо)."""
    out: list[dict[str, Any]] = []
    for block in page_html.split(_WRAP)[1:]:
        if "service_message" in block[:600]:
            continue
        post = _POST_RE.search(block)
        text_m = _TEXT_RE.search(block)
        time_m = _TIME_RE.search(block)
        if not post or not text_m:
            continue
        text = post_text(text_m.group(1))
        if len(text) < min_length:
            continue
        vacancy = to_vacancy(post.group(1), post.group(2), text, time_m.group(1) if time_m else "")
        # «Подборка вакансий…» — много вакансий в одном посте, письмо писать не на что
        if vacancy and not _DIGEST.match(vacancy["name"]):
            out.append(vacancy)
    return out


def min_post_id(page_html: str) -> int:
    ids = [int(m.group(2)) for m in _POST_RE.finditer(page_html)]
    return min(ids) if ids else 0


class TelegramSource:
    def __init__(self, channels: Iterable[str] = (), session: Any = None, pause: float = 2.0, max_pages: int = 5):
        if session is None:
            import requests

            session = requests.Session()
            session.headers["User-Agent"] = USER_AGENT
        self.session = session
        self.channels = [c.strip().lstrip("@") for c in channels if c and c.strip()]
        self.pause = pause
        self.max_pages = max_pages
        self._last = 0.0
        self.on_issue: Any = None  # (канал, текст, это_сбой) — для журнала источников

    def _issue(self, channel: str, text: str, failed: bool = False) -> None:
        print(f"[telegram] @{channel}: {text}")
        if self.on_issue is not None:
            self.on_issue(channel, text, failed)

    def _get(self, path: str) -> str:
        wait = self.pause - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        resp = self.session.get(BASE + path, timeout=30)
        resp.raise_for_status()
        return resp.text

    def list_channel(self, channel: str, max_age_days: float = 0) -> list[dict[str, Any]]:
        limit = (
            datetime.now(timezone.utc) - timedelta(days=max_age_days, hours=12)
            if max_age_days > 0 else None
        )
        found: list[dict[str, Any]] = []
        before = 0
        for _ in range(self.max_pages):
            page = self._get(f"/s/{channel}" + (f"?before={before}" if before else ""))
            if not before and not min_post_id(page):
                self._issue(channel, "веб-превью закрыто или канала нет — без входа не прочитать")
                break
            items = parse_channel(page)
            too_old = False
            for v in items:
                pub = v.get("published_at")
                if limit and pub and datetime.strptime(pub, "%Y-%m-%dT%H:%M:%S%z") < limit:
                    too_old = True
                    continue
                found.append(v)
            oldest = min_post_id(page)
            if too_old or not oldest or oldest == before or oldest <= 1:
                break
            before = oldest
        return found

    def list_all(self, max_age_days: float = 0) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for channel in self.channels:
            try:
                out.extend(self.list_channel(channel, max_age_days))
            except Exception as ex:  # noqa: BLE001 — один канал не держит остальные
                self._issue(channel, f"не прочитан: {ex}", failed=True)
        return out

    def description(self, vacancy: Mapping[str, Any]) -> str:
        """Текст поста; если его нет (вакансия из журнала) — перечитываем."""
        text = str(vacancy.get("description") or "")
        vid = str(vacancy.get("id") or "")[len(PREFIX):]
        if text or "/" not in vid:
            return text
        channel, msg_id = vid.split("/", 1)
        page = self._get(f"/s/{channel}?before={int(msg_id) + 1}")
        for post in parse_channel(page, min_length=0):
            if post["id"] == PREFIX + vid:
                if isinstance(vacancy, dict):
                    vacancy["description"] = post["description"]
                return str(post["description"])
        raise LookupError("пост удалён из канала")

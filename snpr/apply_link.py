"""Куда откликаться на вакансию из Telegram-поста.

Бот по ссылкам из постов не ходит: он только выбирает из текста поста ту
ссылку или контакт, где принимают отклик, а открываете её вы кнопкой на
странице отбора. Выбор — по подписи ссылки и строке вокруг:

- ссылка на вакансию hh → отклик прямо из бота (вакансия берётся по API hh);
- подпись «Откликнуться», «Apply», «Job description», «Подробнее» или
  название самой вакансии → эта ссылка;
- голая ссылка в тексте → она;
- @контакт в строке «пишите / присылайте резюме / HR» → чат в Telegram;
- почта → письмо.

Служебное не берётся: приглашения в каналы (t.me/+…), хэштеги, рекламу
других каналов, «ещё вакансии». Метки utm_* из ссылки убираются.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

HH_VACANCY = re.compile(r"^https?://(?:[\w-]+\.)?hh\.(?:ru|uz|kz)/vacancy/(\d+)", re.I)
_APPLY_WORDS = re.compile(
    r"отклик|откликн|apply|job description|описани\w* ваканси|подробн|more details|details|"
    r"ссылк\w* на ваканси|анкет|форм[ауе]|заявк|resume|резюме|vacancy|ваканси[яю]\b",
    re.I,
)
_LINKEDIN_POST = re.compile(r"post on linkedin|пост\w* (?:на|в) linkedin|linkedin", re.I)
_CONTACT_WORDS = re.compile(
    r"пиш|напис|присыл|пришл|отправ|резюме|\bcv\b|контакт|связ|отклик|\bhr\b|рекрутер|recruiter|"
    r"telegram|телеграм|\btg\b|тг|лс|личк|write|send|contact|dm\b",
    re.I,
)
_NOT_CONTACT = re.compile(r"больше ваканс|ещё ваканс|еще ваканс|подпис|канал|more jobs|наш чат", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_MENTION = re.compile(r"(?<![\w/@.])@([A-Za-z]\w{3,31})\b")
_URL = re.compile(r"https?://[^\s<>\"'«»)]+")


def clean_url(url: str) -> str:
    """Без меток utm_* (они нужны только каналу для статистики)."""
    parts = urlsplit(url.strip())
    # некоторые каналы кодируют & дважды: «?x=1&amp;utm_medium=…»
    parts = parts._replace(query=parts.query.replace("&amp;", "&").replace("amp;", ""))
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def hh_vacancy_id(url: str) -> str:
    m = HH_VACANCY.match(url or "")
    return m.group(1) if m else ""


def _norm(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.lower()).strip()


def _line_of(text: str, needle: str) -> str:
    for line in text.splitlines():
        if needle in line:
            return line
    return ""


def _service_tg(url: str, channel: str) -> bool:
    """t.me-ссылка, по которой не откликаются: инвайт, сам канал, пост, хэштег."""
    parts = urlsplit(url)
    if parts.netloc.lower() not in ("t.me", "telegram.me", "www.t.me"):
        return False
    path = parts.path.strip("/")
    return (
        not path or path.startswith(("+", "joinchat", "s/", "addlist"))
        or "/" in path or path.lower() == channel.lower()
    )


def choose(
    links: Iterable[tuple[str, str]], text: str, title: str = "", channel: str = ""
) -> dict[str, str] | None:
    """links — (адрес, подпись) по порядку в посте. Вернёт {"url", "kind",
    "label"}: kind = hh | link | contact | email. None — отклик неясен."""
    best: tuple[int, int, dict[str, str]] | None = None
    order = 0
    title_n = _norm(title)

    def offer(score: int, item: dict[str, str]) -> None:
        nonlocal best, order
        order += 1
        if score > 0 and (best is None or score > best[0]):
            best = (score, -order, item)

    seen: set[str] = set()
    for url, label in links:
        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        label = (label or "").strip()
        if (vid := hh_vacancy_id(url)):
            offer(10, {"url": f"https://hh.ru/vacancy/{vid}", "kind": "hh", "label": label or "hh.ru"})
            continue
        if _service_tg(url, channel):
            continue
        is_tg = urlsplit(url).netloc.lower() in ("t.me", "telegram.me", "www.t.me")
        if is_tg:  # t.me/<имя>: контакт, только если строка про отклик
            line = _line_of(text, label) if label else ""
            ok = label.startswith("@") and _CONTACT_WORDS.search(line) and not _NOT_CONTACT.search(line)
            offer(4 if ok else 0, {"url": url, "kind": "contact", "label": label})
            continue
        if _APPLY_WORDS.search(label):
            score = 6
        elif title_n and _norm(label) and (_norm(label) in title_n or title_n in _norm(label)):
            score = 5
        elif label in ("", url) or label.startswith(("http", "www.")):
            score = 4  # голая ссылка в тексте
        elif _LINKEDIN_POST.search(label):
            score = 2
        else:
            score = 0  # «Content Manager» — ссылка на другую вакансию, «выбрать» — реклама
        offer(score, {"url": clean_url(url), "kind": "link", "label": label})

    # голые ссылки, которых нет среди размеченных (веб-превью их размечает, API — нет)
    for url in _URL.findall(text):
        url = url.rstrip(".,;:!?")
        if url not in seen:
            seen.add(url)
            if (vid := hh_vacancy_id(url)):
                offer(10, {"url": f"https://hh.ru/vacancy/{vid}", "kind": "hh", "label": "hh.ru"})
            elif not _service_tg(url, channel) and "t.me/" not in url:
                offer(4, {"url": clean_url(url), "kind": "link", "label": ""})

    for line in text.splitlines():
        if _NOT_CONTACT.search(line) or not _CONTACT_WORDS.search(line):
            continue
        for m in _MENTION.finditer(line):
            name = m.group(1)
            if name.lower() != channel.lower() and f"https://t.me/{name}" not in seen:
                seen.add(f"https://t.me/{name}")
                offer(4, {"url": f"https://t.me/{name}", "kind": "contact", "label": f"@{name}"})
    for m in _EMAIL.finditer(text):
        offer(3, {"url": f"mailto:{m.group(0)}", "kind": "email", "label": m.group(0)})
    return best[2] if best else None


def apply_of(vacancy: Mapping[str, Any]) -> dict[str, str] | None:
    """Сохранённый выбор; у постов, разобранных до этой функции, — по
    сохранённым ссылкам без подписей (первая не служебная)."""
    raw = vacancy.get("apply")
    if isinstance(raw, Mapping) and raw.get("url"):
        return dict(raw)
    vid = str(vacancy.get("id") or "")
    if not vid.startswith("tg:"):
        return None
    channel = vid[3:].split("/", 1)[0]
    return choose(
        [(str(u), "") for u in vacancy.get("links") or []],
        str(vacancy.get("description") or ""), str(vacancy.get("name") or ""), channel,
    )

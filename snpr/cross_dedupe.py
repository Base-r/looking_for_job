"""Дубли между источниками: один и тот же найм на разных площадках.

Три проверки, первая найденная копия остаётся (hh — всегда):
1. ссылка на вакансию hh, Habr или GeekJob в посте: id берётся из текста
   регулярным выражением, по ссылке бот не ходит;
2. та же компания и почти то же название;
3. посты без компании: то же название и почти тот же текст.
Грейд из названия не убирается: Junior и Senior — разные вакансии.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping

TITLE_SIMILAR = 0.9
TEXT_SIMILAR = 0.85
LONG_TITLE_WORDS = 6
_TEXT_CHARS = 500

_LINKS = (
    (re.compile(r"(?:^|[/.])hh\.(?:ru|uz|kz)/vacancy/(\d+)", re.I), ""),
    (re.compile(r"career\.habr\.com/vacancies/(\d+)", re.I), "hc:"),
    (re.compile(r"geekjob\.ru/vacancy/([0-9a-f]{24})", re.I), "gj:"),
)
_GRADE = re.compile(
    r"\b(?:junior|middle|senior|lead|head|intern|trainee|джуниор|джун|миддл|мидл|"
    r"сеньор|синьор|стаж[её]р|младший|старший|ведущий|главный)\b",
    re.I,
)
_PUNCT = re.compile(r"[^\w]+", re.U)
_LEGAL = re.compile(r"\b(?:ооо|оао|зао|пао|ао|ип|тоо|llc|ltd|inc|gmbh|group|груп+а|гк)\b", re.I)


def linked_ids(vacancy: Mapping[str, Any]) -> set[str]:
    """Id вакансий других площадок, на которые ссылается пост."""
    text = " ".join([*(str(u) for u in vacancy.get("links") or []), str(vacancy.get("description") or "")])
    return {prefix + m.group(1).lower() for pattern, prefix in _LINKS for m in pattern.finditer(text)}


def _title(vacancy: Mapping[str, Any]) -> str:
    return _PUNCT.sub(" ", str(vacancy.get("name") or "").lower()).strip()


def _company(vacancy: Mapping[str, Any]) -> str:
    """Нормализованная компания; у постов без компании там имя канала — это ''."""
    name = str((vacancy.get("employer") or {}).get("name") or "")
    if not name or name.startswith("@"):
        return ""
    return _PUNCT.sub(" ", _LEGAL.sub(" ", name.lower())).strip()


def _similar(a: str, b: str, threshold: float) -> bool:
    return a == b or SequenceMatcher(None, a, b).ratio() >= threshold


def _same_title(a: str, b: str) -> bool:
    """Почти то же название и тот же грейд: Junior и Senior похожи по буквам."""
    return set(_GRADE.findall(a)) == set(_GRADE.findall(b)) and _similar(a, b, TITLE_SIMILAR)


def drop_cross_duplicates(
    items: list[dict[str, Any]],
    known_ids: Iterable[str] = (),
    current: Iterable[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(оставленные, отброшенные). current — найденное на hh в этом прогоне."""
    current = list(current)
    pool = {str(i) for i in known_ids} | {str(v.get("id")) for v in current} | {str(v.get("id")) for v in items}
    by_company: dict[str, list[str]] = {}
    for vacancy in current:
        if company := _company(vacancy):
            by_company.setdefault(company, []).append(_title(vacancy))
    by_title: dict[str, list[str]] = {}
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for vacancy in items:
        vid = str(vacancy.get("id"))
        title, company = _title(vacancy), _company(vacancy)
        text = str(vacancy.get("description") or "")[:_TEXT_CHARS]
        duplicate = bool((linked_ids(vacancy) - {vid}) & pool)
        if not duplicate and company:
            duplicate = any(_same_title(title, t) for t in by_company.get(company, []))
        if not duplicate and not company and title in by_title:
            # длинное название («PM / Менеджер по запуску … — Rythm Group») само
            # по себе уникально: каналы оформляют один пост по-разному
            duplicate = len(title.split()) >= LONG_TITLE_WORDS or any(
                _similar(text, t, TEXT_SIMILAR) for t in by_title[title] if text
            )
        if duplicate:
            dropped.append(vacancy)
            continue
        kept.append(vacancy)
        if company:
            by_company.setdefault(company, []).append(title)
        else:
            by_title.setdefault(title, []).append(text)
    return kept, dropped

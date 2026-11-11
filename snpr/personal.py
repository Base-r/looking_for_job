"""Личные файлы пользователя: профиль и стиль письма (markdown в корне).

profile.md      — кто я и что ищу: идёт в промпт скорера и письма,
                  числа из него разрешены гвардом письма.
letter_style.md — тон, длина и структура письма для LLM.

Пути задаются в config.toml [files]; относительные — от корня проекта.
HTML-комментарии <!-- ... --> вырезаются: это заметки для человека.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE_FILE = "profile.md"
DEFAULT_LETTER_STYLE_FILE = "letter_style.md"
DEFAULT_LETTER_EDITS_STORE = PROJECT_ROOT / "data" / "letter_edits.jsonl"

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _resolve(value: object, default: str) -> Path:
    raw = str(value or "").strip() or default
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


def read_markdown(path: Path) -> str | None:
    """Текст файла без HTML-комментариев; нет файла/пусто — None."""
    if not path.exists():
        return None
    text = _COMMENT_RE.sub("", path.read_text(encoding="utf-8"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def profile_path(config: Mapping[str, Any]) -> Path:
    return _resolve(config.get("files", {}).get("profile"), DEFAULT_PROFILE_FILE)


def letter_style_path(config: Mapping[str, Any]) -> Path:
    return _resolve(
        config.get("files", {}).get("letter_style"), DEFAULT_LETTER_STYLE_FILE
    )


def load_profile(config: Mapping[str, Any]) -> str | None:
    """Профиль из profile.md — только если [files] задан в конфиге."""
    if not config.get("files"):
        return None
    return read_markdown(profile_path(config))


def load_letter_style(config: Mapping[str, Any]) -> str | None:
    if not config.get("files"):
        return None
    return read_markdown(letter_style_path(config))


class LetterEditsStore:
    """Письма, исправленные человеком в approve: примеры стиля для LLM.

    Каждая правка — строка {vacancy_name, original, edited}. В промпт
    письма идут последние N исправленных текстов: модель подстраивается
    под то, как человек на самом деле пишет.
    """

    def __init__(self, path: Path = DEFAULT_LETTER_EDITS_STORE):
        self.path = Path(path)

    def append(self, vacancy_name: str, original: str, edited: str) -> None:
        if not edited.strip() or edited.strip() == original.strip():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "vacancy_name": vacancy_name,
            "original": original,
            "edited": edited,
            "ts": int(time.time()),
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def recent(self, limit: int = 3) -> list[dict[str, Any]]:
        if limit <= 0 or not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("edited"):
                    records.append(rec)
        return records[-limit:]

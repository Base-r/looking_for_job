"""Журнал источников вакансий: data/sources.log.

Строка: «время | уровень | источник | текст». Уровни: INFO, WARN, ERROR,
CRITICAL (ошибка в CRITICAL_AFTER прогонах подряд — источник перестал
отвечать). Запись в журнал никогда не роняет поиск.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

LOG_NAME = "sources.log"
CRITICAL_AFTER = 3
MAX_BYTES = 1_000_000


class SourceLog:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def _write(self, level: str, source: str, text: str) -> None:
        line = " | ".join((
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            level,
            source,
            " ".join(str(text).split()),
        ))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                self.path.replace(self.path.with_suffix(".log.1"))
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as ex:
            print(f"[журнал источников] не записан: {ex}")

    def ok(self, source: str, received: int, relevant: int | None = None) -> None:
        """Источник ответил; ноль вакансий — повод проверить разбор."""
        text = f"получено {received}" + (f", по профилю {relevant}" if relevant is not None else "")
        self._write("WARN" if received == 0 else "INFO", source, text)

    def warn(self, source: str, text: str) -> None:
        self._write("WARN", source, text)

    def error(self, source: str, text: object) -> None:
        failures = self._failures_in_a_row(source) + 1
        level = "CRITICAL" if failures >= CRITICAL_AFTER else "ERROR"
        self._write(level, source, f"[сбой подряд: {failures}] {text}")
        print(f"[{source}] сбой: {text}")

    def _failures_in_a_row(self, source: str) -> int:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        count = 0
        for line in reversed(lines):
            parts = line.split(" | ", 3)
            if len(parts) < 4 or parts[2] != source:
                continue
            if parts[1] not in ("ERROR", "CRITICAL"):
                if parts[3].startswith("получено"):
                    break  # успешный ответ обрывает серию
                continue
            count += 1
        return count

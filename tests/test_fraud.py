"""Подозрительные признаки: пометка вакансии, отсева нет."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from snpr.fraud import suspicious
from snpr.source_log import SourceLog
from snpr.sources import ExternalSources


@pytest.mark.parametrize(
    "text,flag",
    [
        ("Нужна предоплата за оборудование", "просят предоплату"),
        ("Оплатите обучение перед стартом", "просят предоплату"),
        ("Установите приложение по ссылке", "просят установить приложение"),
        ("скачайте наш клиент, файл app.apk", "просят установить приложение"),
        ("Пришлите код из смс", "просят коды или доступы"),
        ("передайте логин и пароль", "просят коды или доступы"),
        ("нужны данные карты и CVV", "просят коды или доступы"),
    ],
)
def test_flags(text: str, flag: str) -> None:
    assert flag in suspicious(text)


@pytest.mark.parametrize(
    "text",
    [
        "Системный аналитик: требования, BPMN, SQL, работа с заказчиком.",
        "Развитие депозитных продуктов банка, вклады и накопительные счета.",
        "Project Manager, удалённо, оплата два раза в месяц.",
    ],
)
def test_clean_vacancy_not_flagged(text: str) -> None:
    assert suspicious(text) == []


def test_html_tags_do_not_hide_flag() -> None:
    assert suspicious("<p>Нужна <b>предоплата</b></p>") == ["просят предоплату"]


def test_flags_saved_on_vacancy_and_logged(tmp_path: Path) -> None:
    class Src:
        def description(self, vacancy: Any) -> str:
            return "Установите приложение и пришлите код из смс"

    log = SourceLog(tmp_path / "sources.log")
    ext = ExternalSources({}, log=log)
    ext.active = {"telegram": Src()}
    vacancy: dict[str, Any] = {"id": "tg:ch/1", "name": "Аналитик"}
    assert ext.description(vacancy).startswith("Установите")
    assert vacancy["flags"] == ["просят установить приложение", "просят коды или доступы"]
    assert "tg:ch/1" in log.path.read_text(encoding="utf-8")

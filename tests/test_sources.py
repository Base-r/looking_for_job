"""Habr Career и Telegram: разбор страниц, формат как у hh, общий сбор."""

from __future__ import annotations

import html
import json
from typing import Any

from snpr.approve import vacancy_url
from snpr.habr import parse_list as habr_list
from snpr.habr import parse_vacancy as habr_vacancy
from snpr.sources import ExternalSources, dedupe, is_external, source_of
from snpr.telegram_jobs import TelegramSource, guess_title, parse_channel


def _ssr(state: dict[str, Any]) -> str:
    return (
        '<html><script type="application/json" data-ssr-state="true">'
        + html.escape(json.dumps(state, ensure_ascii=False))
        + "</script></html>"
    )


HABR_ITEM = {
    "id": 1000169166,
    "title": "Product Manager (B2C, подписка)",
    "remoteWork": True,
    "qualification": "Middle",
    "publishedDate": {"date": "2026-11-03T12:00:00+03:00"},
    "company": {"title": "Интроверт"},
    "salary": {"from": 150000, "to": None, "currency": "rur"},
    "locations": [{"title": "Ташкент"}],
}


def test_habr_list_to_hh_format() -> None:
    items, pages = habr_list(_ssr({"vacancies": {"list": [HABR_ITEM], "meta": {"totalPages": 3}}}))
    assert pages == 3
    v = items[0]
    assert v["id"] == "hc:1000169166" and source_of(v) == "habr"
    assert v["employer"]["name"] == "Интроверт"
    assert v["area"] == {"id": "2759", "name": "Ташкент"}
    assert v["work_format"] == [{"id": "REMOTE"}]
    assert v["experience"] == {"id": "between1And3"}
    assert v["salary"] == {"from": 150000, "to": None, "currency": "RUR", "gross": False}
    assert v["published_at"] == "2026-11-03T12:00:00+0300"
    assert v["alternate_url"] == "https://career.habr.com/vacancies/1000169166"


def test_habr_vacancy_description() -> None:
    short = habr_list(_ssr({"vacancies": {"list": [HABR_ITEM]}}))[0][0]
    card = habr_vacancy(_ssr({"vacancy": {**HABR_ITEM, "description": "<p>Задачи</p>"}}), short)
    assert card["description"] == "<p>Задачи</p>"
    assert card["name"] == "Product Manager (B2C, подписка)"


def _post(channel: str, mid: int, text: str, when: str = "2026-11-09T10:00:00+00:00") -> str:
    return (
        '<div class="tgme_widget_message_wrap js-widget_message_wrap">'
        f'<div class="tgme_widget_message js-widget_message" data-post="{channel}/{mid}">'
        f'<div class="tgme_widget_message_text js-message_text" dir="auto">{text}</div>'
        f'<a class="tgme_widget_message_date"><time datetime="{when}">x</time></a></div></div>'
    )


POST_REMOTE = (
    "#dataanalyst #remote #middle<br/>🚀 Middle Data Analyst в Ozon<br/>"
    "Формат: удалённо, полный день.<br/>Зарплата: от 2 000 USD<br/>"
    "Задачи: SQL, дашборды, A/B тесты, работа с продуктовой командой и бизнесом."
)
POST_OFFICE = (
    "Компания: Kapitalbank<br/>Должность: Бизнес-аналитик<br/>Формат: офис в Ташкенте, "
    "удаленки нет<br/>Требования: BPMN, SQL, опыт от года, общение с заказчиками, "
    "описание бизнес-процессов и подготовка ТЗ для разработки."
)


def test_telegram_posts_to_vacancies() -> None:
    page = _post("analysts_jobs", 10, POST_REMOTE) + _post("analysts_jobs", 11, POST_OFFICE) + _post(
        "analysts_jobs", 12, "Коротко."
    )
    items = parse_channel(page)
    assert [v["id"] for v in items] == ["tg:analysts_jobs/10", "tg:analysts_jobs/11"]
    remote, office = items
    assert remote["name"] == "Middle Data Analyst" and remote["employer"]["name"] == "Ozon"
    assert remote["work_format"] == [{"id": "REMOTE"}]
    assert remote["salary"]["currency"] == "USD" and remote["salary"]["from"] == 2000
    assert remote["experience"] == {"id": "between1And3"}
    assert "SQL" in remote["description"]
    assert office["name"] == "Бизнес-аналитик" and office["employer"]["name"] == "Kapitalbank"
    assert office["work_format"] == [] and office["area"]["id"] == "2759"
    assert vacancy_url("tg:analysts_jobs/10") == "https://t.me/analysts_jobs/10"
    assert is_external(office) and source_of(office) == "telegram"


def test_guess_title_drops_lead_in() -> None:
    assert guess_title("В команду ищем Senior Data Analyst (remote)\nтекст")[0] == "Senior Data Analyst (remote)"
    assert guess_title("Ищем аналитика в сегменте Pharma\nтекст") == ("аналитика в сегменте Pharma", "")


class FakeResp:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self) -> None:
        pass


class FakeSession:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.urls: list[str] = []

    def get(self, url: str, **_kw: Any) -> FakeResp:
        self.urls.append(url)
        return FakeResp(self.pages.get(url, ""))


def test_telegram_pages_back_until_old() -> None:
    session = FakeSession({
        "https://t.me/s/ch": _post("ch", 20, POST_REMOTE, "2099-01-01T00:00:00+00:00"),
        "https://t.me/s/ch?before=20": _post("ch", 19, POST_OFFICE, "2000-01-01T00:00:00+00:00"),
    })
    src = TelegramSource(["@ch"], session=session, pause=0)
    items = src.list_all(max_age_days=7)
    assert [v["id"] for v in items] == ["tg:ch/20"]
    assert session.urls == ["https://t.me/s/ch", "https://t.me/s/ch?before=20"]


def test_telegram_closed_preview_is_reported(capsys) -> None:
    src = TelegramSource(["closed"], session=FakeSession({"https://t.me/s/closed": "<html></html>"}), pause=0)
    assert src.list_all(max_age_days=7) == []
    assert "@closed" in capsys.readouterr().out


def test_telegram_digest_post_skipped() -> None:
    digest = "Подборка вакансий для продактов и проджект-менеджеров<br/>" + "Product Manager, компания, ссылка. " * 8
    assert parse_channel(_post("ch", 1, digest)) == []


def test_collect_filters_by_title_and_experience() -> None:
    class Src:
        def list_all(self, max_age_days: float = 0) -> list[dict[str, Any]]:
            return [
                {"id": "hc:1", "name": "Системный аналитик", "employer": {"name": "A"}},
                {"id": "hc:2", "name": "Экономист в AI-проект", "employer": {"name": "B"}},
                {"id": "hc:3", "name": "Senior Data Analyst", "employer": {"name": "C"},
                 "experience": {"id": "between3And6"}},
                {"id": "hc:4", "name": "Системный  аналитик", "employer": {"name": "a"}},
                {"id": "hc:5", "name": "Project Manager", "employer": {"name": "D"}},
            ]

    ext = ExternalSources({}, experience=["noExperience", "between1And3"])
    ext.active = {"habr": Src()}
    got = ext.collect(7, known_ids={"hc:5"})
    assert [v["id"] for v in got] == ["hc:1"]  # hc:4 — повтор hc:1


def test_dedupe_keeps_first() -> None:
    a = {"id": "tg:x/2", "name": "Data Analyst", "employer": {"name": "@x"}}
    b = {"id": "tg:x/1", "name": "Data analyst!", "employer": {"name": "@X"}}
    assert dedupe([a, b]) == [a]

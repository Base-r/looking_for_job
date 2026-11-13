"""Авито: разбор объявлений, входящие, выбор, источник (синтетические данные)."""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

from snpr.avito import (
    AvitoSource, add, fix_homoglyphs, item_id, load_inbox, merge, parse_date, pick, to_vacancy,
)
from snpr.side_job import is_side_job, salary_mode
from snpr.sources import ExternalSources, source_of

NOW = datetime(2026, 11, 13, 10, 0, tzinfo=timezone(timedelta(hours=5)))
URL = "https://www.avito.ru/moskva/vakansii/analitik_dannyh_8500000001"


def _card(**kw):
    base = {
        "id": "8500000001", "url": URL, "title": "Аналитик данных",
        "price": "40 000 — 60 000 ₽ на руки", "params": "Гибкий график · Без опыта",
        "location": "Москва", "date": "· 4 ноября в 14:37",
        "conditions": "Условия\nФормат работы: Удалённо\nЗанятость: Частичная",
        "description": "Ищем aнaлитикa в кoмaнду",
    }
    return {**base, **kw}


def test_homoglyphs_fixed_only_in_cyrillic_words():
    assert fix_homoglyphs("Ищем aнaлитикa, cеть прокaтoв Getski.me") == "Ищем аналитика, сеть прокатов Getski.me"
    assert fix_homoglyphs("SQL и Python") == "SQL и Python"
    # русские буквы в английском слове
    assert fix_homoglyphs("знание Ехсеl, Роwеr роint") == "знание Excel, Power point"


def test_item_id():
    assert item_id(URL + "?context=abc") == "8500000001"
    assert item_id("8500000001") == "8500000001"
    assert item_id("https://www.avito.ru/") == ""


def test_parse_date():
    assert parse_date("· 4 ноября в 14:37", NOW).startswith("2026-11-04T14:37")
    assert parse_date("сегодня в 9:05", NOW).startswith("2026-11-13T09:05")
    assert parse_date("вчера в 18:00", NOW).startswith("2026-11-12T18:00")
    assert parse_date("28 декабря в 10:00", NOW).startswith("2025-12-28")
    assert parse_date("3 мая 2026 в 10:00", NOW).startswith("2026-05-03")
    assert parse_date("", NOW) == ""


def test_to_vacancy():
    v = to_vacancy(_card())
    assert v["id"] == "av:8500000001" and source_of(v) == "avito"
    assert v["alternate_url"] == URL
    assert v["salary"]["from"] == 40000 and v["salary"]["gross"] is False
    assert v["work_format"] == [{"id": "REMOTE"}]
    assert v["experience"] == {"id": "noExperience"}
    assert is_side_job(v)
    assert "аналитика" in v["description"] and "Занятость: Частичная" in v["description"]
    assert "-11-04T14:37" in v["published_at"]


def test_to_vacancy_pay_modes_and_experience():
    assert salary_mode(to_vacancy(_card(price="от 10 500 ₽ сдельно"))) == "SERVICE"
    assert salary_mode(to_vacancy(_card(price="2 000 ₽ за смену"))) == "SHIFT"
    assert to_vacancy(_card(price="Зарплата не указана"))["salary"] is None
    v = to_vacancy(_card(params="Опыт более 3 лет", conditions="Занятость: Постоянная"))
    assert v["experience"] == {"id": "between3And6"} and not is_side_job(v)
    assert to_vacancy(_card(params="Опыт более года", conditions=""))["experience"] == {"id": "between1And3"}
    assert to_vacancy(_card(id="", url="")) is None
    assert to_vacancy(_card(employer="Пользователь"))["employer"]["name"] == ""
    assert to_vacancy(_card(employer="ООО Ромашка"))["employer"]["name"] == "ООО Ромашка"


def test_inbox_add_dedupe_and_expiry(tmp_path):
    now = time.time()
    assert add(tmp_path, [_card()], now=now - 40 * 86400) == 1
    assert add(tmp_path, [_card()], now=now) == 1  # старая запись выброшена
    assert add(tmp_path, [_card()], now=now) == 0
    assert len(load_inbox(tmp_path)) == 1


def test_merge_cards_and_items():
    data = {
        "cards": [{"id": "8500000001", "url": URL, "title": "Аналитик", "location": "Ташкент", "params": "Без опыта"}],
        "items": [
            {"id": "8500000001", "url": URL, "title": "Аналитик данных", "location": "", "description": "текст"},
            {"id": "8500000002", "url": URL.replace("01", "02"), "blocked": True},
        ],
    }
    merged = merge(data)
    assert len(merged) == 1
    assert merged[0]["location"] == "Ташкент" and merged[0]["title"] == "Аналитик данных"
    assert to_vacancy(merged[0])["area"]["id"] == "2759"


def test_pick():
    must = re.compile("аналит|project", re.I)
    exclude = re.compile("маркетплейс", re.I)
    cards = [
        {"id": "1000001", "title": "Менеджер-aнaлитик"},
        {"id": "1000002", "title": "Аналитик маркетплейсов"},
        {"id": "1000003", "title": "Логист"},
        {"id": "1000004", "title": "Финансовый аналитик"},
    ]
    chosen = pick(cards, must, exclude, {"av:1000004"})
    assert [c["id"] for c in chosen] == ["1000001"]
    assert chosen[0]["title"] == "Менеджер-аналитик"
    senior = [{"id": "1000005", "title": "Аналитик", "params": "Гибкий график · Опыт более 3 лет"},
              {"id": "1000006", "title": "Аналитик", "params": "Без опыта"}]
    assert [c["id"] for c in pick(senior, must, None, set(), ["noExperience", "between1And3"])] == ["1000006"]


def test_source_reads_inbox_window(tmp_path):
    add(tmp_path, [_card(), _card(id="8500000009", url=URL.replace("01", "09"), date="")])
    rows = load_inbox(tmp_path)
    rows[0]["vacancy"]["published_at"] = (datetime.now(timezone.utc) - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%S%z")
    (tmp_path / "avito_inbox.jsonl").write_text(
        "".join(__import__("json").dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    src = AvitoSource(tmp_path)
    assert [v["id"] for v in src.list_all(7)] == ["av:8500000009"]
    assert len(src.list_all(0)) == 2
    assert "аналитика" in src.description({"id": "av:8500000001"})


def test_external_sources_registers_avito(tmp_path):
    add(tmp_path, [_card(date="сегодня в 9:00")])
    ext = ExternalSources({"avito": True, "title_must": "аналит"}, data_dir=tmp_path)
    assert "avito" in ext.active
    got = ext.collect(7, set())
    assert [v["id"] for v in got] == ["av:8500000001"]
    assert ext.collect(7, {"av:8500000001"}) == []

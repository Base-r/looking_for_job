"""GeekJob: разбор страниц, формат как у hh, ручной отклик."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from snpr.approve_service import sweep_archived
from snpr.geekjob import (
    _date_from_ru,
    is_geekjob,
    parse_list,
    parse_salary,
    parse_vacancy,
)
from snpr.sne import ParkedStore, SeenStore

LIST_HTML = """
<ul><li class="collection-item avatar ">
 <div class="info"><a href="/vacancy/6af2e214294db25d8c00b7e1" target="_blank">Ташкент, Узбекистан
   <br><span class="salary">120K — 200K ₽</span></a></div>
 <p class="truncate vacancy-name"><a href="/vacancy/6af2e214294db25d8c00b7e1" class="title"
   target="_blank">Junior Project Manager</a></p>
 <p class="truncate company-name"><a href="/vacancy/6af2e214294db25d8c00b7e1"> ООО &quot;Ромашка&quot;</a></p>
 <div class="info"><span class="withtest-label">тестовое задание</span>&nbsp;</div>
 <time class="truncate datetime-info"><a href="/vacancy/6af2e214294db25d8c00b7e1">10 ноября</a></time>
</li><li class="collection-item avatar ">
 <div class="info"><a href="/vacancy/6af2b309d47a5f2f0e07eb7c"><br><span class="salary"></span></a></div>
 <p class="truncate vacancy-name"><a href="/vacancy/6af2b309d47a5f2f0e07eb7c" class="title">Middle Product Manager</a></p>
 <p class="truncate company-name"><a href="/vacancy/6af2b309d47a5f2f0e07eb7c"> Keepgo</a></p>
 <div class="info"><span class="remote-label">remote</span>&nbsp;</div>
 <time class="truncate datetime-info"><a href="/vacancy/6af2b309d47a5f2f0e07eb7c">9 ноября</a></time>
</li></ul>
"""

VACANCY_HTML = """
<script type="application/ld+json"> { "datePosted": "2026-11-09", broken json </script>
<h1>Middle Product Manager</h1>
<div class="location"></div>
<div class="category">Миддл</div>
<div class="time">9 ноября</div>
<div class="jobinfo"><span class="jobformat">Удаленная работа<br> Опыт работы от 1 года до 3х лет</span><span class="salary">от 2 000 $</span></div>
<div class="description"><div id="vacancy-description"><p>Ищем PM в SaaS.</p></div>
</div>
"""


def test_parse_list_to_hh_format() -> None:
    items = parse_list(LIST_HTML)
    assert [v["id"] for v in items] == ["gj:6af2e214294db25d8c00b7e1", "gj:6af2b309d47a5f2f0e07eb7c"]
    pm = items[0]
    assert pm["name"] == "Junior Project Manager"
    assert pm["employer"]["name"] == 'ООО "Ромашка"'
    assert pm["area"] == {"id": "2759", "name": "Ташкент"}  # Ташкент проходит гео-фильтр
    assert pm["salary"] == {"from": 120000, "to": 200000, "currency": "RUR", "gross": False}
    assert pm["alternate_url"] == "https://geekjob.ru/vacancy/6af2e214294db25d8c00b7e1"
    assert items[1]["work_format"] == [{"id": "REMOTE"}]
    assert all(is_geekjob(v) for v in items)


def test_parse_vacancy_fills_details() -> None:
    short = parse_list(LIST_HTML)[1]
    card = parse_vacancy(VACANCY_HTML, short)
    assert card["description"] == "<p>Ищем PM в SaaS.</p>"
    assert card["experience"] == {"id": "between1And3"}
    assert card["work_format"] == [{"id": "REMOTE"}]
    assert card["salary"] == {"from": 2000, "to": None, "currency": "USD", "gross": False}
    assert card["published_at"].startswith("2026-11-09T")


def test_salary_and_dates() -> None:
    assert parse_salary("до 150K ₽") == {"from": None, "to": 150000, "currency": "RUR", "gross": False}
    assert parse_salary("") is None
    today = date(2026, 1, 5)
    assert _date_from_ru("30 декабря", today) == date(2025, 12, 30)  # год не пишут
    assert _date_from_ru("3 января", today) == date(2026, 1, 3)


def _parked(tmp_path: Path, vid: str) -> ParkedStore:
    parked = ParkedStore(tmp_path / "parked.jsonl")
    parked.append_record({"id": vid, "name": "PM", "employer": {"name": "Keepgo"}}, None, "Письмо.")
    return parked


def test_archive_sweep_skips_geekjob(tmp_path: Path) -> None:
    class NoApi:
        def get(self, *a, **k):
            raise AssertionError("в hh за вакансией GeekJob не ходим")

    parked = _parked(tmp_path, "gj:abc")
    assert sweep_archived(NoApi(), parked, SeenStore(tmp_path / "seen.jsonl"), tmp_path, pause=0) == 0
    assert parked.ids() == {"gj:abc"}

"""SuperJob: формат как у hh, области поиска, ключ только в заголовке."""

from __future__ import annotations

from typing import Any

from snpr.approve import vacancy_url
from snpr.sources import ExternalSources, is_external, source_of
from snpr.superjob import SuperJobSource, to_hh

ITEM: dict[str, Any] = {
    "id": 52294909,
    "profession": "Бизнес-аналитик процесса KYC",
    "firm_name": "Альфа-Банк",
    "town": {"id": 4, "title": "Москва"},
    "place_of_work": {"id": 2, "title": "Удалённая работа (на дому)"},
    "experience": {"id": 2, "title": "От 1 года"},
    "payment_from": 150000,
    "payment_to": 0,
    "currency": "rub",
    "date_published": 1794300218,
    "link": "https://www.superjob.ru/vakansii/biznes-analitik-52294909.html",
    "candidat": "Опыт описания процессов",
    "work": "Анализ KYC",
    "compensation": "ДМС",
}


class Resp:
    def __init__(self, data: dict[str, Any]):
        self.data = data

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self.data


class Session:
    def __init__(self, *pages: dict[str, Any]):
        self.pages = list(pages)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params: Any = None, headers: Any = None, timeout: int = 0) -> Resp:
        self.calls.append({"url": url, "params": params, "headers": headers})
        return Resp(self.pages.pop(0) if self.pages else {"objects": [], "more": False})


def test_to_hh_format() -> None:
    v = to_hh(ITEM)
    assert v["id"] == "sj:52294909" and source_of(v) == "superjob" and is_external(v)
    assert v["name"] == "Бизнес-аналитик процесса KYC"
    assert v["employer"]["name"] == "Альфа-Банк"
    assert v["work_format"] == [{"id": "REMOTE"}]
    assert v["area"] == {"id": "", "name": "Москва"}
    assert v["experience"] == {"id": "between1And3"}
    assert v["salary"] == {"from": 150000, "to": None, "currency": "RUR", "gross": False}
    assert v["published_at"].endswith("+0300")
    assert v["description"] == "Опыт описания процессов\n\nАнализ KYC\n\nДМС"
    assert v["alternate_url"] == ITEM["link"]
    assert vacancy_url(v) == ITEM["link"]


def test_tashkent_area_and_uzs() -> None:
    v = to_hh({**ITEM, "town": {"title": "Ташкент"}, "currency": "uzs", "place_of_work": {"id": 1}})
    assert v["area"]["id"] == "2759" and v["salary"]["currency"] == "UZS" and v["work_format"] == []


def test_no_salary_is_none() -> None:
    assert to_hh({**ITEM, "payment_from": 0, "payment_to": 0})["salary"] is None


def test_list_all_scopes_paging_and_key_header() -> None:
    session = Session({"objects": [ITEM], "more": True}, {"objects": [{**ITEM, "id": 2}], "more": False})
    src = SuperJobSource(key="test-key", session=session, pause=0, queries=["аналитик"])
    items = src.list_all(max_age_days=7)
    assert [v["id"] for v in items] == ["sj:52294909", "sj:2"]
    assert session.calls[0]["params"]["place_of_work"] == 2 and session.calls[0]["params"]["page"] == 0
    assert session.calls[1]["params"]["page"] == 1
    assert session.calls[2]["params"]["town"] == "Ташкент"
    assert session.calls[0]["headers"] == {"X-Api-App-Id": "test-key"}
    assert "test-key" not in str(session.calls[0]["params"])
    assert session.calls[0]["params"]["date_published_from"] > 0


def test_archived_skipped() -> None:
    session = Session({"objects": [{**ITEM, "is_archive": True}], "more": False})
    src = SuperJobSource(key="k", session=session, pause=0, queries=["a"])
    assert src.list_all() == []


def test_description_is_read_from_list() -> None:
    session = Session()
    src = SuperJobSource(key="k", session=session, pause=0)
    assert src.description(to_hh(ITEM)).startswith("Опыт") and session.calls == []


def test_no_key_source_is_skipped(monkeypatch) -> None:
    monkeypatch.delenv("SJ_KEY", raising=False)
    assert "superjob" not in ExternalSources({"superjob": True}).active

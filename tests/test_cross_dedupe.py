"""Дубли между источниками: ссылка, компания + название, текст поста."""

from __future__ import annotations

from typing import Any

from snpr.cross_dedupe import drop_cross_duplicates, linked_ids
from snpr.sources import ExternalSources
from snpr.telegram_jobs import parse_channel


def _v(vid: str, name: str, company: str, **extra: Any) -> dict[str, Any]:
    return {"id": vid, "name": name, "employer": {"name": company}, **extra}


def _ids(items: list[dict[str, Any]]) -> list[str]:
    return [v["id"] for v in items]


def test_linked_ids_from_text_and_links() -> None:
    v = _v("tg:c/1", "PM", "@c", links=["https://geekjob.ru/vacancy/6aed87db9939a8cc2c060907"],
           description="подробнее https://tashkent.hh.uz/vacancy/123 и https://career.habr.com/vacancies/77")
    assert linked_ids(v) == {"gj:6aed87db9939a8cc2c060907", "123", "hc:77"}


def test_post_linking_to_found_vacancy_is_dropped() -> None:
    post = _v("tg:c/1", "Аналитик", "@c", description="https://hh.ru/vacancy/123")
    kept, dropped = drop_cross_duplicates([post], current=[_v("123", "Другое", "X")])
    assert kept == [] and _ids(dropped) == ["tg:c/1"]


def test_post_linking_to_already_seen_vacancy_is_dropped() -> None:
    post = _v("tg:c/1", "Аналитик", "@c", description="https://career.habr.com/vacancies/9")
    assert drop_cross_duplicates([post], known_ids={"hc:9"})[0] == []


def test_post_with_unknown_link_stays() -> None:
    post = _v("tg:c/1", "Аналитик", "@c", description="https://hh.ru/vacancy/555")
    assert _ids(drop_cross_duplicates([post], known_ids={"1"})[0]) == ["tg:c/1"]


def test_same_company_similar_title_hh_wins() -> None:
    copy = _v("hc:1", "Системный аналитик.", 'ООО "Ромашка"')
    kept, _ = drop_cross_duplicates([copy], current=[_v("5", "Системный аналитик", "Ромашка")])
    assert kept == []


def test_different_grade_is_not_duplicate() -> None:
    items = [_v("hc:1", "Junior Product Manager", "Ромашка"), _v("hc:2", "Senior Product Manager", "Ромашка")]
    assert len(drop_cross_duplicates(items)[0]) == 2


def test_different_company_same_title_stays() -> None:
    items = [_v("hc:1", "Системный аналитик", "Ромашка"), _v("hc:2", "Системный аналитик", "Лютик")]
    assert len(drop_cross_duplicates(items)[0]) == 2


def test_posts_without_company_compared_by_text() -> None:
    body = "Ищем аналитика в продуктовую команду, SQL, Python, A/B тесты, удалённо. " * 3
    items = [
        _v("tg:a/1", "Data Analyst", "@a", description=body),
        _v("tg:b/2", "Data Analyst", "@b", description=body + " Пишите."),
        _v("tg:b/3", "Data Analyst", "@b", description="Совсем другая вакансия: банк, офис, отчётность. " * 3),
    ]
    assert _ids(drop_cross_duplicates(items)[0]) == ["tg:a/1", "tg:b/3"]


def test_collect_passes_hh_vacancies() -> None:
    class Src:
        def list_all(self, max_age_days: float = 0) -> list[dict[str, Any]]:
            return [_v("hc:1", "Системный аналитик", "Ромашка")]

    ext = ExternalSources({})
    ext.active = {"habr": Src()}
    assert ext.collect(7, set(), [_v("5", "Системный аналитик", "Ромашка")]) == []


def test_telegram_keeps_links_from_post() -> None:
    text = "Аналитик данных в Ozon<br/>" + "Описание вакансии, SQL и отчётность. " * 6
    page = (
        '<div class="tgme_widget_message_wrap js-widget_message_wrap">'
        '<div class="tgme_widget_message js-widget_message" data-post="c/7">'
        f'<div class="tgme_widget_message_text js-message_text" dir="auto">{text}'
        '<a href="https://hh.ru/vacancy/42" target="_blank">ссылка</a></div>'
        '<time datetime="2026-11-09T10:00:00+00:00">x</time></div></div>'
    )
    assert linked_ids(parse_channel(page)[0]) == {"42"}

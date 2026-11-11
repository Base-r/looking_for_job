"""Talent Move: RSS → формат hh, окно свежести, без лишних запросов."""

from __future__ import annotations

from typing import Any

from snpr.approve import vacancy_url
from snpr.sources import ExternalSources, source_of
from snpr.talent_move import TalentMoveSource, parse_feed

ITEM = """<?xml version="1.0"?><rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
<item><title>Middle системный аналитик</title>
<link>https://talent-move.ru/jobs/middle-sa-acme-101126-1/</link>
<pubDate>{date}</pubDate>
<content:encoded><![CDATA[<h3>Что за роль</h3><p>Работа удалённо, кандидаты, находящиеся в России.</p>
<h3>О компании Acme &amp; Co</h3><p>Делаем софт.</p>]]></content:encoded></item>
</channel></rss>"""


def test_fields() -> None:
    v = parse_feed(ITEM.format(date="Tue, 10 Nov 2026 21:31:16 +0000"))[0]
    assert v["id"] == "tm:middle-sa-acme-101126-1"
    assert v["employer"]["name"] == "Acme & Co"
    assert v["work_format"] == [{"id": "REMOTE"}]
    assert v["salary"] is None
    assert "Работа удалённо" in v["description"] and "<" not in v["description"]
    assert vacancy_url({"id": v["id"]}) == "https://talent-move.ru/jobs/middle-sa-acme-101126-1/"


class Session:
    def __init__(self, body: str):
        self.body = body
        self.urls: list[str] = []

    def get(self, url: str, timeout: int = 0) -> Any:
        self.urls.append(url)
        body = self.body

        class R:
            text = body

            def raise_for_status(self) -> None:
                pass

        return R()


def test_window_and_categories() -> None:
    old = ITEM.format(date="Tue, 01 Jan 2019 10:00:00 +0000")
    s = Session(old)
    src = TalentMoveSource(categories=["analytics", "project-manager"], session=s, pause=0)
    assert src.list_all(max_age_days=7) == []
    assert s.urls == [
        "https://talent-move.ru/job-category/analytics/feed/",
        "https://talent-move.ru/job-category/project-manager/feed/",
    ]
    assert len(src.list_all()) == 1  # одна вакансия в двух категориях — один раз


def test_registered() -> None:
    assert "talent_move" in ExternalSources({"talent_move": True}).active

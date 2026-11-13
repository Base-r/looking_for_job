"""Куда откликаться на пост из Telegram и посты со ссылкой на hh (синтетика)."""

from __future__ import annotations

from snpr.apply_link import apply_of, choose, clean_url
from snpr.sne import hh_from_posts
from snpr.telegram_jobs import parse_channel


def test_apply_button_beats_channel_invites():
    links = [
        ("https://jobs.example.com/108564?utm_source=telegram&utm_medium=x&ref=7", "Откликнуться"),
        ("https://t.me/+AAAAinvite", "Вакансии"),
        ("https://t.me/+BBBBinvite", "Удалёнка"),
        ("https://t.me/+CCCCads", "Софи - ассистент для автооткликов"),
    ]
    got = choose(links, "Аналитик\nОткликнуться", "Аналитик", "somechannel")
    assert got == {"url": "https://jobs.example.com/108564?ref=7", "kind": "link", "label": "Откликнуться"}


def test_job_description_beats_linkedin_post():
    links = [("https://acme.example/jobs/1", "Job description"), ("https://lnkd.in/p/x", "post on LinkedIn")]
    assert choose(links, "PM", "PM")["url"] == "https://acme.example/jobs/1"
    assert choose([("https://lnkd.in/p/x", "пост на LinkedIn")], "PM", "PM")["url"] == "https://lnkd.in/p/x"


def test_title_anchor_and_other_vacancies():
    links = [
        ("https://board.example/v/1", "Аналитик разработчик"),
        ("https://t.me/somechannel_ads", "Yandex for Analytics"),
        ("https://board.example/cats", "выбрать"),
    ]
    assert choose(links, "текст", "Аналитик разработчик")["url"] == "https://board.example/v/1"
    others = [("https://board.example/a", "→ Откликнуться тут"), ("https://board.example/b", "Content Manager")]
    assert choose(others, "текст", "Project Manager")["url"] == "https://board.example/a"


def test_contact_and_email():
    text = "Требования…\n@recruiter_anna Пришлите резюме и кейсы\nБольше вакансий: @otherjobs"
    links = [("https://t.me/recruiter_anna", "@recruiter_anna"), ("https://t.me/otherjobs", "@otherjobs")]
    assert choose(links, text, "PM", "chan") == {
        "url": "https://t.me/recruiter_anna", "kind": "contact", "label": "@recruiter_anna"}
    # без разметки (API отдаёт упоминание текстом)
    assert choose([], text, "PM", "chan")["url"] == "https://t.me/recruiter_anna"
    assert choose([], "Резюме на hr.person@example.com", "PM")["url"] == "mailto:hr.person@example.com"
    assert choose([], "Больше вакансий: @otherjobs", "PM") is None


def test_hh_link_wins_and_plain_url():
    links = [("https://jobs.example.com/x", "Откликнуться"), ("https://tashkent.hh.uz/vacancy/123456?from=tg", "hh")]
    assert choose(links, "", "PM") == {"url": "https://hh.ru/vacancy/123456", "kind": "hh", "label": "hh"}
    assert choose([], "Подробнее: https://site.example/job/5?utm_campaign=a.", "PM")["url"] == "https://site.example/job/5"


def test_clean_url():
    assert clean_url("https://a.example/p?utm_source=x&id=2#top") == "https://a.example/p?id=2#top"
    assert clean_url("https://a.example/j/5?utm_source=t&amp;utm_medium=s&amp;id=3") == "https://a.example/j/5?id=3"


def test_apply_of_backfill_for_old_posts():
    old = {"id": "tg:chan/5", "name": "PM", "description": "текст",
           "links": ["https://t.me/+invite", "https://acme.example/jobs/9", "https://lnkd.in/p/z"]}
    assert apply_of(old)["url"] == "https://acme.example/jobs/9"
    assert apply_of({"id": "123", "links": ["https://x.example"]}) is None


def test_web_preview_parsing_sets_apply():
    body = "Project Manager<br/>" + "Описание обязанностей и требований. " * 6 + \
        '<br/><a href="https://jobs.example.com/1?utm_source=telegram" target="_blank">Откликнуться</a>' \
        '<br/><a href="https://t.me/+invite">Вакансии</a>'
    page = ('<div class="tgme_widget_message_wrap"><div data-post="chan/77">'
            f'<div class="tgme_widget_message_text js-message_text" dir="auto">{body}</div>'
            '<time datetime="2026-11-12T10:00:00+00:00"></time></div>')
    [v] = parse_channel(page)
    assert v["apply"] == {"url": "https://jobs.example.com/1", "kind": "link", "label": "Откликнуться"}


class _Api:
    def __init__(self, cards):
        self.cards, self.calls = cards, []

    def get(self, path, params=None):
        self.calls.append(path)
        vid = path.rsplit("/", 1)[-1]
        if vid not in self.cards:
            raise RuntimeError("captcha")
        return self.cards[vid]


def _post(n, hid=""):
    v = {"id": f"tg:chan/{n}", "name": "PM", "alternate_url": f"https://t.me/chan/{n}"}
    if hid:
        v["apply"] = {"url": f"https://hh.ru/vacancy/{hid}", "kind": "hh", "label": "hh"}
    return v


def test_hh_from_posts():
    api = _Api({"111": {"id": "111", "name": "PM", "description": "<p>x</p>"},
                "222": {"id": "222", "name": "Old", "archived": True}})
    posts = [_post(1, "111"), _post(2, "222"), _post(3, "333"), _post(4, "444"), _post(5)]
    hh, kept, cards = hh_from_posts(api, posts, known_ids={"444"}, pause=0)
    assert [v["id"] for v in hh] == ["111"] and hh[0]["found_via"] == "https://t.me/chan/1"
    assert set(cards) == {"111"}
    # 222 в архиве и 444 уже виденная — пост не нужен; 333 сбой API и 5 без hh — остаются постами
    assert [v["id"] for v in kept] == ["tg:chan/3", "tg:chan/5"]
    assert "/vacancies/444" not in api.calls


def test_hh_from_posts_budget():
    api = _Api({"1": {"id": "1"}, "2": {"id": "2"}})
    hh, kept, _ = hh_from_posts(api, [_post(1, "1"), _post(2, "2")], set(), budget=1, pause=0)
    assert [v["id"] for v in hh] == ["1"] and [v["id"] for v in kept] == ["tg:chan/2"]

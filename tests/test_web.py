"""Страница отбора `snpr web`: настоящий HTTP-сервер на 127.0.0.1 с
фейковым hh и фейковым LLM; проверяем токен, очередь, отклик, пропуск,
переписывание письма, подтверждение при нарушениях гварда, лок."""

from __future__ import annotations

import argparse
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from snpr.approve_service import ApproveService
from snpr.cover_letter import LetterDraft
from snpr.scoring import ScoreResult
from snpr.sne import ParkedStore, SeenStore
from snpr.web import approve_ui, make_server

CONFIG = Path(__file__).resolve().parent.parent / "snpr" / "config.toml"


def _vacancy(vid: str) -> dict[str, Any]:
    return {
        "id": vid,
        "name": f"Junior PM {vid}",
        "employer": {"name": "Acme"},
        "area": {"id": "2759", "name": "Ташкент"},
        "alternate_url": f"https://hh.ru/vacancy/{vid}",
        "salary": {"from": 900, "currency": "USD", "gross": False},
    }


class _Api:
    def __init__(self, applied: list[str] | None = None, cards: dict | None = None):
        self.posts: list[tuple[str, dict]] = []
        self.applied = applied or []
        self.cards = cards or {}

    def get(self, endpoint: str, params: dict | None = None) -> Any:
        if endpoint == "/resumes/mine":
            return {"items": [{"id": "R-1"}]}
        if endpoint == "/negotiations":
            return {"pages": 1, "items": [{"vacancy": {"id": v}} for v in self.applied]}
        vid = endpoint.rsplit("/", 1)[-1]
        return self.cards.get(vid) or {**_vacancy(vid), "description": "<p>Ищем PM</p>"}

    def post(self, endpoint: str, params: dict | None = None, delay: Any = None) -> Any:
        self.posts.append((endpoint, params or {}))
        return {}


class _Writer:
    ai = object()

    def write(self, vacancy: Any, description: str = "", score: Any = None) -> LetterDraft:
        return LetterDraft(vacancy_id=str(vacancy["id"]), text="Новое письмо.")


@pytest.fixture
def web(tmp_path: Path):
    def start(api: _Api | None = None, dry_run: bool = False):
        api = api or _Api()
        parked = ParkedStore(tmp_path / "parked.jsonl")
        parked.append_record(
            _vacancy("1"), ScoreResult(vacancy_id="1", score=80, verdict="target"),
            "Здравствуйте! Письмо без цифр.",
        )
        parked.append_record(
            _vacancy("2"), ScoreResult(vacancy_id="2", score=60, verdict="maybe"),
            "Письмо два.",
        )
        args = argparse.Namespace(
            config=str(CONFIG),
            parked_store=str(tmp_path / "parked.jsonl"),
            applied_store=str(tmp_path / "applied.jsonl"),
            seen_store=str(tmp_path / "seen.jsonl"),
            resume_id="",
        )
        import tomllib

        config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
        service = ApproveService(args, config, api, dry_run=dry_run, writer_factory=_Writer)
        server, state, url = make_server(service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        base = url.split("/?")[0]

        def call(path: str, body: dict | None = None, token: str | None = None) -> tuple[int, Any]:
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(base + path, data=data, method="POST" if data else "GET")
            req.add_header("X-Token", state.token if token is None else token)
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as ex:
                return ex.code, None

        return call, api, parked, url, state

    started: list[Any] = []
    yield start
    for server in started:
        server.shutdown()
        server.server_close()


def test_page_requires_token(web) -> None:
    call, _api, _parked, url, _state = web()
    with urllib.request.urlopen(url, timeout=10) as resp:
        page = resp.read().decode("utf-8")
    assert "Отбор вакансий" in page and "Переписать письмо" in page
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(url.split("?")[0] + "?t=wrong", timeout=10)
    assert err.value.code == 403
    assert call("/api/queue", token="wrong")[0] == 403
    assert call("/api/skip", {"vid": "1"}, token="")[0] == 403


def test_queue_items(web) -> None:
    call, *_ = web()
    code, data = call("/api/queue")
    assert code == 200 and data["dry_run"] is False
    items = data["items"]
    assert [i["vid"] for i in items] == ["1", "2"]  # сортировка по скору
    assert items[0]["url"] == "https://hh.ru/vacancy/1"
    assert items[0]["letter"].startswith("Здравствуйте")
    assert "900" in items[0]["salary"]


def test_send_with_edited_letter(web, tmp_path: Path) -> None:
    call, api, parked, *_ = web()
    code, res = call("/api/send", {"vid": "1", "letter": "Исправленное письмо."})
    assert code == 200 and res["status"] == "sent" and res["closed"]
    assert api.posts == [
        ("/negotiations", {"resume_id": "R-1", "vacancy_id": "1", "message": "Исправленное письмо."})
    ]
    assert parked.ids() == {"2"}
    assert "1" in SeenStore(tmp_path / "seen.jsonl").seen_ids()
    edits = (tmp_path / "letter_edits.jsonl").read_text(encoding="utf-8")
    assert "Исправленное письмо." in edits  # правка — пример стиля
    # повторный отклик на ту же — её уже нет
    assert call("/api/send", {"vid": "1"})[1]["status"] == "not_found"


def test_send_needs_confirm_on_guard_violation(web) -> None:
    call, api, parked, *_ = web()
    res = call("/api/send", {"vid": "1", "letter": "У меня 15 лет опыта."})[1]
    assert res["status"] == "needs_confirm" and res["violations"]
    assert api.posts == [] and "1" in parked.ids()
    res = call("/api/send", {"vid": "1", "letter": "У меня 15 лет опыта.", "confirm": True})[1]
    assert res["status"] == "sent" and len(api.posts) == 1


def test_already_applied_on_hh_is_closed(web) -> None:
    call, api, parked, *_ = web(api=_Api(applied=["1"]))
    res = call("/api/send", {"vid": "1"})[1]
    assert res["status"] == "applied_on_hh" and res["closed"]
    assert api.posts == [] and parked.ids() == {"2"}


def test_skip_and_rewrite(web) -> None:
    call, api, parked, *_ = web()
    res = call("/api/rewrite", {"vid": "2"})[1]
    assert res["status"] == "rewritten" and res["letter"].split("\n\n")[0] == "Новое письмо."
    assert next(r for r in parked.load_records() if r["vacancy"]["id"] == "2")["letter"] == "Новое письмо."
    assert call("/api/skip", {"vid": "2"})[1]["status"] == "skipped"
    assert parked.ids() == {"1"} and api.posts == []


def test_dry_run_sends_nothing(web) -> None:
    call, api, parked, *_ = web(dry_run=True)
    assert call("/api/queue")[1]["dry_run"] is True
    res = call("/api/send", {"vid": "1"})[1]
    assert res["status"] == "dry_run" and not res["closed"]
    assert api.posts == [] and parked.ids() == {"1", "2"}


def test_approve_ui_setting(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text('[approve]\nui = "console"\n', encoding="utf-8")
    assert approve_ui(cfg) == "console"
    assert approve_ui(tmp_path / "missing.toml") == "web"
    assert approve_ui() == "web"  # живой конфиг


def test_run_web_respects_approve_lock(tmp_path: Path, capsys, monkeypatch) -> None:
    import os

    from snpr import sne, web as web_mod

    (tmp_path / sne.APPROVE_LOCK_NAME).write_text(f"{os.getpid()}\n", encoding="utf-8")
    (tmp_path / web_mod.WEB_URL_FILE).write_text("http://127.0.0.1:1/?t=x", encoding="utf-8")
    opened: list[str] = []
    monkeypatch.setattr(web_mod.webbrowser, "open", lambda u: opened.append(u))
    args = argparse.Namespace(seen_store=str(tmp_path / "seen.jsonl"), no_browser=False)
    assert web_mod.run_web(args) == 0
    assert opened == ["http://127.0.0.1:1/?t=x"]  # второй экземпляр — открыть первый
    assert "уже открыта" in capsys.readouterr().out


def test_rewrite_rereads_letter_style_each_time(tmp_path: Path, monkeypatch) -> None:
    """«Переписать письмо» читает letter_style.md заново при каждом нажатии:
    правка файла действует без перезапуска страницы."""
    import snpr.cli as cli_mod

    style = tmp_path / "style.md"
    profile = tmp_path / "profile.md"
    profile.write_text("- 41 релиз", encoding="utf-8")
    style.write_text("СТИЛЬ-ПЕРВЫЙ", encoding="utf-8")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f'[files]\nprofile = "{profile.as_posix()}"\nletter_style = "{style.as_posix()}"\n'
        '[letter]\nmode = "llm"\nstyle_examples_from_edits = 0\n',
        encoding="utf-8",
    )

    prompts: list[str] = []

    class _AI:
        def complete(self, message: str, **kwargs: Any) -> str:
            prompts.append(kwargs["system_prompt"])
            return "Письмо."

    monkeypatch.setattr(cli_mod, "_make_ai_client", lambda *a, **k: _AI())

    ParkedStore(tmp_path / "parked.jsonl").append_record(
        _vacancy("1"), ScoreResult(vacancy_id="1", score=80), "Старое письмо."
    )
    args = argparse.Namespace(
        config=str(cfg_path),
        parked_store=str(tmp_path / "parked.jsonl"),
        applied_store=str(tmp_path / "applied.jsonl"),
        seen_store=str(tmp_path / "seen.jsonl"),
        resume_id="",
        ai=True,
        ai_base_url="https://gateway.invalid/v1",
        ai_model="m",
    )
    import tomllib

    service = ApproveService(args, tomllib.loads(cfg_path.read_text(encoding="utf-8")), _Api())
    assert service.rewrite("1").status == "rewritten"
    style.write_text("СТИЛЬ-ВТОРОЙ", encoding="utf-8")  # правка файла между нажатиями
    assert service.rewrite("1").status == "rewritten"
    assert "СТИЛЬ-ПЕРВЫЙ" in prompts[0]
    assert "СТИЛЬ-ВТОРОЙ" in prompts[1] and "СТИЛЬ-ПЕРВЫЙ" not in prompts[1]


def test_rejected_tab_and_requeue(web, tmp_path: Path) -> None:
    """Вкладка «Не прошли порог»: вакансии ниже maybe_min с датой и скором;
    «Вернуть на рассмотрение» пишет письмо и ставит в очередь."""
    import json as _json
    import tomllib

    from snpr.sne import ScoresStore

    threshold = int(tomllib.loads(CONFIG.read_text(encoding="utf-8"))["scoring"]["maybe_min"])
    scores = ScoresStore(tmp_path / "scores.jsonl")
    low = threshold - 5
    scores.append_record(_vacancy("7"), ScoreResult(vacancy_id="7", score=low, verdict="pass",
                         reasons=["смежная роль"]), ts=1_760_000_000)
    scores.append_record(_vacancy("8"), ScoreResult(vacancy_id="8", score=max(0, low - 10),
                         verdict="pass"), ts=1_760_000_500)
    scores.append_record(_vacancy("1"), ScoreResult(vacancy_id="1", score=80, verdict="target"))
    call, api, parked, *_ = web(api=_Api(applied=["8"]))

    code, data = call("/api/rejected")
    assert code == 200 and data["threshold"] == threshold
    items = {i["vid"]: i for i in data["items"]}
    assert set(items) == {"7"}  # 8 — уже откликались, 1 — выше порога
    assert items["7"]["score"] == low and items["7"]["date"]
    assert items["7"]["url"] == "https://hh.ru/vacancy/7"
    assert items["7"]["reasons"] == ["смежная роль"]

    res = call("/api/requeue", {"vid": "7"})[1]
    assert res["status"] == "requeued" and res["letter"] == "Новое письмо."
    assert "7" in parked.ids()
    assert call("/api/requeue", {"vid": "7"})[1]["status"] == "requeued"  # повторно — уже в очереди
    assert "7" not in {i["vid"] for i in call("/api/rejected")[1]["items"]}
    assert api.posts == []  # ничего не отправлено
    _ = _json


def test_queue_items_have_publication_ts(web) -> None:
    """Сортировка по дате выхода: в очереди есть момент публикации."""
    call, *_ = web()
    items = call("/api/queue")[1]["items"]
    assert all("pub_ts" in i for i in items)


def test_page_has_sort_options(web) -> None:
    _call, _api, _parked, url, _state = web()
    with urllib.request.urlopen(url, timeout=10) as resp:
        page = resp.read().decode("utf-8")
    for label in ("По скору", "По дате выхода вакансии", "Скор и свежесть вместе"):
        assert label in page


def test_queue_drops_vacancy_applied_on_site_while_page_open(web) -> None:
    """Откликнулись на сайте hh, пока страница открыта — вакансия уходит из
    очереди при ближайшей сверке (не дожидаясь прогона)."""
    call, api, parked, _url, state = web()
    assert {i["vid"] for i in call("/api/queue")[1]["items"]} == {"1", "2"}
    api.applied.append("1")  # отклик сделан вручную на сайте
    state.service._applied_synced_at = 0  # пора сверяться
    assert {i["vid"] for i in call("/api/queue")[1]["items"]} == {"2"}
    assert parked.ids() == {"2"}


def test_queue_drops_repost_of_applied_vacancy(web, tmp_path: Path) -> None:
    """Повтор вакансии с новым id (то же название у того же работодателя)."""
    import json as _json

    call, _api, parked, _url, state = web()
    # ключи откликов с hh (их пишет сверка с /negotiations)
    (tmp_path / "hh_applied_keys.json").write_text(
        _json.dumps([["junior pm 2", "acme"]]), encoding="utf-8"
    )
    state.service._applied_synced_at = 1e18  # без сетевой сверки
    assert {i["vid"] for i in call("/api/queue")[1]["items"]} == {"1"}
    assert parked.ids() == {"1"}


def test_queue_reports_publication_age_range(web) -> None:
    """Странице отдаётся диапазон «вышла N–M дней назад» из конфига."""
    call, _api, _parked, _url, state = web()
    state.service.age_days = lambda: [1.0, 7.0]
    assert call("/api/queue")[1]["age"] == [1.0, 7.0]


def test_responses_vpn_check_pauses_site_requests(tmp_path: Path) -> None:
    """Сайт hh закрыт проверкой VPN: не кэшируем «нет данных» и не дёргаем
    hh по каждой вакансии."""
    from snpr import responses

    class Resp:
        status_code = 200
        url = "https://hh.ru/vpncheeck?backUrl=%2Fvacancy%2F1"
        text = "VPN мешает работе сайта"

    class Session:
        calls = 0

        def get(self, url, timeout=None):
            Session.calls += 1
            return Resp()

    responses._blocked_until = 0.0
    try:
        assert responses.responses_count(Session(), "1", tmp_path) is None
        assert responses.site_blocked()
        assert responses.responses_count(Session(), "2", tmp_path) is None
        assert Session.calls == 1
        assert not (tmp_path / responses.CACHE_FILE).exists()
    finally:
        responses._blocked_until = 0.0


def test_geekjob_vacancy_marked_applied_without_hh(web) -> None:
    """GeekJob: «Я откликнулся» — только отметка, в hh ничего не уходит."""
    call, api, parked, _url, _state = web()
    parked.append_record(
        {**_vacancy("gj:abc"), "alternate_url": "https://geekjob.ru/vacancy/abc"},
        ScoreResult(vacancy_id="gj:abc", score=70, verdict="maybe"),
        "Письмо для GeekJob.",
    )
    items = {i["vid"]: i for i in call("/api/queue")[1]["items"]}
    assert items["gj:abc"]["source"] == "geekjob" and items["1"]["source"] == "hh"
    res = call("/api/send", {"vid": "gj:abc", "letter": "Письмо для GeekJob."})[1]
    assert res["status"] == "sent" and res["closed"]
    assert api.posts == []
    assert "gj:abc" not in parked.ids()


def test_page_has_source_filters(web) -> None:
    """Фильтр «Источник» на обеих вкладках."""
    _call, _api, _parked, url, _state = web()
    with urllib.request.urlopen(url, timeout=10) as resp:
        page = resp.read().decode("utf-8")
    assert 'id="qsrc"' in page and 'id="rjsrc"' in page
    assert '<option value="geekjob">GeekJob</option>' in page
    assert '<option value="habr">Habr Career</option>' in page
    assert '<option value="telegram">Telegram</option>' in page


def test_telegram_and_habr_marked_applied_without_hh(web) -> None:
    """Habr и Telegram: как GeekJob — отметка, в hh ничего не уходит."""
    call, api, parked, _url, _state = web()
    for vid in ("hc:1000", "tg:analysts_jobs/7"):
        parked.append_record(_vacancy(vid), ScoreResult(vacancy_id=vid, score=70, verdict="maybe"), "Письмо.")
    items = {i["vid"]: i for i in call("/api/queue")[1]["items"]}
    assert items["hc:1000"]["source"] == "habr" and items["tg:analysts_jobs/7"]["source"] == "telegram"
    for vid in ("hc:1000", "tg:analysts_jobs/7"):
        res = call("/api/send", {"vid": vid, "letter": "Письмо."})[1]
        assert res["status"] == "sent" and res["closed"]
    assert api.posts == []

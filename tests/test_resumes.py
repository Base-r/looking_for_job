"""Выбор резюме для отклика: [[resumes]] в конфиге, DeepSeek предлагает
резюме при оценке, человек может поменять на странице; неопубликованные
и чужие id не проходят."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from snpr.approve_service import ApproveService
from snpr.cover_letter import CoverLetterWriter
from snpr.scoring import ScoreResult, VacancyScorer, pick_resume
from snpr.sne import HHNegotiationSender, ParkedStore, usable_resumes

PM = {"id": "R-PM", "title": "IT Project manager", "use_for": "PM, Delivery"}
BA = {"id": "R-BA", "title": "Бизнес-аналитик", "use_for": "бизнес-аналитик, Data Analyst"}
CONFIG = {"resumes": [PM, BA], "letter_guard": {"allowed_url_prefixes": []}}


class _Api:
    def __init__(self, resumes: list[dict[str, Any]] | None = None) -> None:
        self.posts: list[dict[str, Any]] = []
        self.resumes = resumes if resumes is not None else [
            {"id": "R-PM", "title": "IT Project manager", "status": {"id": "published"}},
            {"id": "R-BA", "title": "Бизнес-аналитик", "status": {"id": "published"}},
        ]

    def get(self, endpoint: str, params: dict | None = None) -> Any:
        if endpoint == "/resumes/mine":
            return {"items": self.resumes}
        if endpoint == "/negotiations":
            return {"pages": 1, "items": []}
        vid = endpoint.rsplit("/", 1)[-1]
        return {"id": vid, "name": "Аналитик"}

    def post(self, endpoint: str, params: dict | None = None, delay: Any = None) -> Any:
        self.posts.append(params or {})
        return {}


class _AI:
    def __init__(self, reply: str = "") -> None:
        self.reply = reply
        self.system = ""
        self.message = ""

    def complete(self, message: str, **kwargs: Any) -> str:
        self.system = kwargs.get("system_prompt", "")
        self.message = message
        return self.reply


def test_scorer_offers_resumes_and_parses_choice() -> None:
    ai = _AI('{"score": 70, "verdict": "maybe", "resume_id": "R-BA"}')
    scorer = VacancyScorer(ai, resumes=[PM, BA], retry_backoff=0)  # type: ignore[arg-type]
    assert '"R-BA": Бизнес-аналитик' in scorer.system_prompt
    assert scorer.score({"id": "1", "name": "Аналитик"}).resume_id == "R-BA"
    # неизвестный id или пусто → основное (первое)
    ai.reply = '{"score": 70, "resume_id": "чужое"}'
    assert scorer.score({"id": "1", "name": "PM"}).resume_id == "R-PM"
    # одно резюме — выбора нет, промпт не меняется
    single = VacancyScorer(_AI(), resumes=[PM])  # type: ignore[arg-type]
    assert "resume_id" not in single.system_prompt
    assert pick_resume([], "x") == ""


def test_usable_resumes_skips_unpublished_and_unknown(capsys) -> None:
    api = _Api([
        {"id": "R-PM", "title": "IT Project manager", "status": {"id": "published"}},
        {"id": "R-BA", "title": "Бизнес-аналитик", "status": {"id": "not_published"}},
    ])
    config = {"resumes": [PM, BA, {"id": "R-GONE", "title": "Удалённое"}]}
    assert [r["id"] for r in usable_resumes(api, config)] == ["R-PM"]
    out = capsys.readouterr().out
    assert "не опубликовано" in out and "R-GONE" in out


def test_sender_uses_chosen_resume() -> None:
    api = _Api()
    sender = HHNegotiationSender(api, "R-PM")
    sender.send("1", "Письмо")
    sender.send("2", "Письмо", resume_id="R-BA")
    assert [p["resume_id"] for p in api.posts] == ["R-PM", "R-BA"]


def _service(tmp_path: Path, api: _Api) -> ApproveService:
    parked = ParkedStore(tmp_path / "parked.jsonl")
    parked.append_record(
        {"id": "1", "name": "Аналитик"},
        ScoreResult(vacancy_id="1", score=60, resume_id="R-BA"),
        "Здравствуйте! Письмо.",
    )
    args = argparse.Namespace(
        config="x",
        parked_store=str(tmp_path / "parked.jsonl"),
        applied_store=str(tmp_path / "applied.jsonl"),
        seen_store=str(tmp_path / "seen.jsonl"),
        resume_id="",
    )
    return ApproveService(args, CONFIG, api, writer_factory=lambda: None)


def test_service_sends_with_llm_choice_or_human_override(tmp_path: Path) -> None:
    api = _Api()
    service = _service(tmp_path, api)
    rec = service.record("1")
    assert service.resume_for(rec) == "R-BA"  # предложение DeepSeek
    assert service.resume_for(rec, "R-PM") == "R-PM"  # выбор человека
    assert service.resume_for(rec, "R-ЧУЖОЕ") == "R-BA"  # чужой id не пройдёт
    out = service.send("1", resume_id="R-PM", card={})
    assert out.status == "sent" and "IT Project manager" in out.message
    assert api.posts[-1]["resume_id"] == "R-PM"
    applied = json.loads((tmp_path / "applied.jsonl").read_text(encoding="utf-8"))
    assert applied["resume_id"] == "R-PM"


def test_parked_record_keeps_resume_choice(tmp_path: Path) -> None:
    rec = ParkedStore(tmp_path / "p.jsonl")
    rec.append_record({"id": "1"}, ScoreResult(vacancy_id="1", resume_id="R-BA"), "x")
    assert rec.load_records()[0]["score"]["resume_id"] == "R-BA"


def test_letter_accent_follows_resume() -> None:
    ai = _AI("Здравствуйте!\n\nПисьмо.")
    writer = CoverLetterWriter(
        ai, system_prompt="s", resume_titles={"R-PM": "IT Project manager", "R-BA": "Бизнес-аналитик"}  # type: ignore[arg-type]
    )
    writer.write({"id": "1", "name": "Аналитик"}, score=ScoreResult(vacancy_id="1", resume_id="R-BA"))
    assert "резюме «Бизнес-аналитик»" in ai.message
    # одно резюме — строки про резюме нет
    single = CoverLetterWriter(ai, system_prompt="s", resume_titles={"R-PM": "PM"})  # type: ignore[arg-type]
    single.write({"id": "1", "name": "PM"}, score=ScoreResult(vacancy_id="1", resume_id="R-PM"))
    assert "Отклик идёт с резюме" not in ai.message


def test_web_queue_offers_resume_dropdown(tmp_path: Path) -> None:
    import threading
    import urllib.request

    from snpr.web import make_server

    api = _Api()
    server, state, url = make_server(_service(tmp_path, api))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = url.split("/?")[0]

        def call(path: str, body: dict | None = None) -> Any:
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(base + path, data=data, method="POST" if data else "GET")
            req.add_header("X-Token", state.token)
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())

        queue = call("/api/queue")
        assert [r["id"] for r in queue["resumes"]] == ["R-PM", "R-BA"]
        assert queue["items"][0]["resume_id"] == "R-BA"
        res = call("/api/send", {"vid": "1", "resume_id": "R-PM"})
        assert res["status"] == "sent" and api.posts[-1]["resume_id"] == "R-PM"
    finally:
        server.shutdown()
        server.server_close()

"""Персонализация: profile.md / letter_style.md, LLM-письма RU/EN,
пол зарплаты в долларах, гео Ташкент, фильтры поиска, список вакансий,
правка письма в редакторе и память правок."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from snpr.approve import edit_text_in_editor, vacancy_url
from snpr.cli import _build_letter_writer
from snpr.cover_letter import (
    CoverLetterWriter,
    StaticLetterWriter,
    detect_language,
    styled_letter_system_prompt,
)
from snpr.filters import PipelineFilter
from snpr.personal import LetterEditsStore, read_markdown
from snpr.report import rows_from_stores, run_list, write_html_report
from snpr.scoring import LLMUnavailable, ScoreResult
from snpr.sne import ScoresStore, build_search_params


class _CaptureAI:
    def __init__(self, reply: str = "Здравствуйте! Письмо.") -> None:
        self.reply = reply
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def complete(self, message: str, **kwargs: Any) -> str:
        self.calls.append((message, kwargs))
        return self.reply


class _BrokenAI:
    def complete(self, message: str, **kwargs: Any) -> str:
        raise RuntimeError("502 Bad Gateway")


# ------------------------------------------------------------ личные файлы --


def test_read_markdown_strips_comments(tmp_path: Path) -> None:
    path = tmp_path / "p.md"
    path.write_text("# Я\n<!-- заметка\nдля себя -->\n\n\n\n- факт 1\n", encoding="utf-8")
    assert read_markdown(path) == "# Я\n\n- факт 1"
    assert read_markdown(tmp_path / "missing.md") is None


def test_letter_edits_store(tmp_path: Path) -> None:
    store = LetterEditsStore(tmp_path / "edits.jsonl")
    store.append("PM", "текст", "текст")  # без изменений — не пишем
    assert store.recent() == []
    for i in range(5):
        store.append("PM", "было", f"стало {i}")
    assert [r["edited"] for r in store.recent(2)] == ["стало 3", "стало 4"]


# ----------------------------------------------------------------- письма --


def test_detect_language() -> None:
    assert detect_language("Project Manager", "We are looking for a PM, Jira, Scrum") == "en"
    assert detect_language("Project Manager", "Ищем PM в команду, Jira, Scrum, Agile") == "ru"
    assert detect_language("", "") == "ru"


def test_styled_letter_writer_prompt_and_language() -> None:
    ai = _CaptureAI()
    system = styled_letter_system_prompt(
        "Живой тон, на вы", "- 41 релиз", examples=["Моё исправленное письмо"]
    )
    writer = CoverLetterWriter(ai, system_prompt=system)  # type: ignore[arg-type]
    draft = writer.write(
        {"id": "1", "name": "Project Manager", "employer": {"name": "Acme"}},
        description="<p>We build <b>mobile apps</b>. Remote, English.</p>",
        score=ScoreResult(vacancy_id="1", score=70, matched=["релизы"]),
    )
    assert draft.text == "Здравствуйте! Письмо."
    message, kwargs = ai.calls[0]
    assert "английский" in message  # вакансия на английском → письмо на английском
    assert "<p>" not in message and "mobile apps" in message  # HTML вырезан
    assert "Живой тон" in kwargs["system_prompt"]
    assert "41 релиз" in kwargs["system_prompt"]
    assert "Моё исправленное письмо" in kwargs["system_prompt"]


def test_letter_writer_llm_failure_is_llm_unavailable() -> None:
    writer = CoverLetterWriter(
        _BrokenAI(), system_prompt="x", retries=1, retry_backoff=0  # type: ignore[arg-type]
    )
    with pytest.raises(LLMUnavailable):
        writer.write({"id": "1", "name": "PM"})


def _args(**kw: Any) -> argparse.Namespace:
    base = {"ai": True, "ai_base_url": "https://gateway.invalid/v1", "ai_model": "m"}
    base.update(kw)
    return argparse.Namespace(**base)


def test_build_letter_writer_modes(tmp_path: Path) -> None:
    (tmp_path / "profile.md").write_text("- 41 релиз", encoding="utf-8")
    (tmp_path / "style.md").write_text("Коротко и по делу", encoding="utf-8")
    cfg = {
        "files": {
            "profile": str(tmp_path / "profile.md"),
            "letter_style": str(tmp_path / "style.md"),
        },
        "letter": {"mode": "llm", "style_examples_from_edits": 0},
    }
    writer = _build_letter_writer(_args(), cfg)
    assert isinstance(writer, CoverLetterWriter)
    assert "Коротко и по делу" in writer.system_prompt
    assert "41 релиз" in writer.system_prompt
    # без LLM письма не пишутся (а не уходят пустыми)
    assert _build_letter_writer(_args(ai_base_url=""), cfg) is None
    # static — фиксированный текст
    cfg["letter"] = {"mode": "static", "static_text": "Привет"}
    static = _build_letter_writer(_args(), cfg)
    assert isinstance(static, StaticLetterWriter) and static.text == "Привет"


# ---------------------------------------------------------------- фильтры --

CFG = {
    "floor_net_usd": 600,
    "min_from_ratio": 0.8,
    "geo": {"enabled": True, "remote_ok": True, "areas": ["2759"]},
    "currency_rates_rub": {"USD": 100.0, "UZS": 0.008},
    "grade": {"enabled": False},
    "vendor": {"enabled": False},
    "gov": {"enabled": False},
}


def _check(**vacancy: Any):
    vacancy.setdefault("id", "1")
    return PipelineFilter(CFG).check(vacancy)


def test_salary_floor_usd_and_currencies() -> None:
    # 8 млн сумов на руки ≈ 640$ — проходит
    assert _check(
        salary={"to": 8_000_000, "currency": "UZS", "gross": False},
        area={"id": "2759"},
    ).passed
    # до 50 000 ₽ на руки = 500$ < 600$ — отсекается
    verdict = _check(
        salary={"to": 50_000, "currency": "RUR", "gross": False},
        work_format=[{"id": "REMOTE"}],
    )
    assert not verdict.passed and "salary" in verdict.reject_reasons[0]
    # неизвестная валюта — не блок, решит LLM
    assert _check(
        salary={"to": 10, "currency": "XXX"}, work_format=[{"id": "REMOTE"}]
    ).passed
    # вилка не указана — проходит
    assert _check(work_format=[{"id": "REMOTE"}]).passed


def test_geo_tashkent_any_format_else_remote_only() -> None:
    assert _check(area={"id": "2759"}, work_format=[{"id": "OFFICE"}]).passed
    assert _check(area={"id": "2759"}, work_format=[{"id": "HYBRID"}]).passed
    assert _check(area={"id": "1"}, work_format=[{"id": "REMOTE"}]).passed
    assert not _check(area={"id": "1"}, work_format=[{"id": "HYBRID"}]).passed
    assert not _check(area={"id": "160"}, work_format=[{"id": "OFFICE"}]).passed


def test_build_search_params_extra() -> None:
    params = build_search_params(
        "project manager",
        1,
        50,
        extra={"area": ["113", "97"], "search_field": "name", "experience": []},
    )
    assert params["area"] == ["113", "97"]
    assert params["search_field"] == "name"
    assert "experience" not in params  # пустой список не отправляем
    assert params["text"] == "project manager"


# ------------------------------------------------------- список вакансий --


def _vacancy(vid: str, name: str = "PM") -> dict[str, Any]:
    return {
        "id": vid,
        "name": name,
        "employer": {"name": "Acme"},
        "alternate_url": f"https://hh.uz/vacancy/{vid}",
        "salary": {"from": 900, "currency": "USD", "gross": False},
        "area": {"name": "Ташкент"},
        "work_format": [{"id": "HYBRID", "name": "Гибрид"}],
    }


def test_vacancy_url() -> None:
    assert vacancy_url({"id": "5"}) == "https://hh.ru/vacancy/5"
    assert vacancy_url(_vacancy("5")) == "https://hh.uz/vacancy/5"
    assert vacancy_url("7") == "https://hh.ru/vacancy/7"


def test_scores_store_keeps_link_and_details(tmp_path: Path) -> None:
    store = ScoresStore(tmp_path / "scores.jsonl")
    store.append_record(
        _vacancy("1"), ScoreResult(vacancy_id="1", score=80, verdict="target")
    )
    rec = store.load()[0]
    assert rec["url"] == "https://hh.uz/vacancy/1"
    assert rec["area"] == "Ташкент" and "900" in rec["salary"]


def test_vacancy_list_statuses_and_html(tmp_path: Path, capsys) -> None:
    scores = ScoresStore(tmp_path / "scores.jsonl")
    for vid, score, verdict in (("1", 85, "target"), ("2", 65, "maybe"), ("3", 20, "pass")):
        scores.append_record(
            _vacancy(vid, f"PM {vid}"),
            ScoreResult(vacancy_id=vid, score=score, verdict=verdict),
        )
    (tmp_path / "applied.jsonl").write_text(
        json.dumps({"vacancy_id": "1", "dry_run": False}) + "\n", encoding="utf-8"
    )
    (tmp_path / "parked.jsonl").write_text(
        json.dumps({"vacancy": {"id": "2"}, "letter": "Письмо 2"}) + "\n",
        encoding="utf-8",
    )
    rows = {r["vacancy_id"]: r for r in rows_from_stores(tmp_path)}
    assert rows["1"]["status"] == "applied"
    assert rows["2"]["status"] == "parked" and rows["2"]["letter"] == "Письмо 2"
    assert rows["3"]["status"] == "pass"

    args = argparse.Namespace(all=False, min_score=-1, limit=0, open=False)
    assert run_list(args, tmp_path, min_score=60) == 0
    out = capsys.readouterr().out
    # PM 1 — уже откликнулись: в подборку не попадает
    assert "PM 1" not in out and "PM 2" in out and "PM 3" not in out
    assert "https://hh.uz/vacancy/2" in out
    page = (tmp_path / "vacancies.html").read_text(encoding="utf-8")
    assert "https://hh.uz/vacancy/2" in page and "Письмо 2" in page


def test_html_report_escapes(tmp_path: Path) -> None:
    path = write_html_report(
        [{"name": "<script>x</script>", "score": 70, "url": "https://hh.ru/vacancy/1"}],
        tmp_path / "r.html",
        title="t",
    )
    assert "<script>x" not in path.read_text(encoding="utf-8")


# ----------------------------------------------------------- правка письма --


def test_edit_text_in_editor(monkeypatch) -> None:
    import subprocess

    def fake_run(cmd: list[str], check: bool = False) -> None:
        Path(cmd[1]).write_text("Исправленное письмо", encoding="utf-8")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert edit_text_in_editor("Старое", editor="notepad.exe") == "Исправленное письмо"


def test_edit_text_in_editor_empty_keeps_original(monkeypatch) -> None:
    import subprocess

    monkeypatch.setattr(
        subprocess, "run", lambda cmd, check=False: Path(cmd[1]).write_text("", encoding="utf-8")
    )
    assert edit_text_in_editor("Старое", editor="notepad.exe") == "Старое"


# ---------------------------------------------------------------- джоба --


def test_job_interval_from_config(tmp_path: Path) -> None:
    from snpr import job

    cfg = tmp_path / "c.toml"
    cfg.write_text("[job]\ninterval_minutes = 30\n", encoding="utf-8")
    assert job.interval_minutes(cfg) == 30
    assert job.interval_minutes(tmp_path / "missing.toml") == 20
    assert job.interval_minutes() == 20  # живой конфиг


# ------------------------------------------------------- ошибка ключа LLM --


class _UnauthorizedAI:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, message: str, **kwargs: Any) -> str:
        self.calls += 1
        raise RuntimeError("Network error: 401 Client Error: Unauthorized")


def test_auth_error_fails_fast_without_retries() -> None:
    from snpr.filters import PipelineFilter
    from snpr.pipeline import SnePipeline
    from snpr.scoring import LLMAuthError, VacancyScorer

    ai = _UnauthorizedAI()
    scorer = VacancyScorer(ai, retry_backoff=0)  # type: ignore[arg-type]
    with pytest.raises(LLMAuthError, match="sk-"):
        scorer.score({"id": "1", "name": "PM"})
    assert ai.calls == 1  # без повторов

    pipeline = SnePipeline(PipelineFilter(CFG), scorers=[scorer])
    vacancy = {"id": "1", "name": "PM", "work_format": [{"id": "REMOTE"}]}
    candidates = pipeline.run([vacancy, {**vacancy, "id": "2"}])
    # ключ неверный — дальше не оцениваем, обе вакансии ждут следующего запуска
    assert "ключ" in pipeline.abort_reason
    assert [c.status for c in candidates] == ["deferred", "deferred"]
    assert ai.calls == 2  # вторая вакансия в LLM уже не ушла


def test_guard_ignores_numbers_inside_words() -> None:
    from snpr.letter_guard import LetterGuard

    guard = LetterGuard(allowed_numbers={"41"})
    assert guard.check("Работал в Битрикс24 и 1С, B2B-продукт, 41 релиз.") == []
    assert guard.check("Вилка 300К на руки") == ["число вне капсулы: 300"]
    assert guard.check("Опыт 15 лет") == ["число вне капсулы: 15"]


def test_provider_searches_each_scope() -> None:
    from snpr.sne import HHVacancyProvider

    calls: list[dict[str, Any]] = []

    class _Api:
        def get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
            calls.append(params)
            area = params.get("area")
            return {"pages": 1, "items": [{"id": f"{params['text']}-{area}"}]}

    provider = HHVacancyProvider(
        _Api(),
        extra_params={"search_field": "name"},
        scopes=[{"area": ["113"], "work_format": ["REMOTE"]}, {"area": ["2759"]}],
    )
    found = provider.search_new(["pm"], 1, set())
    assert len(calls) == 2
    assert calls[0]["work_format"] == ["REMOTE"] and calls[0]["search_field"] == "name"
    assert calls[1]["area"] == ["2759"] and "work_format" not in calls[1]
    assert len(found) == 2


def test_extract_numbers_grouped_thousands() -> None:
    from snpr.letter_guard import LetterGuard, extract_numbers

    numbers = extract_numbers("завёл 1 200+ задач, 103 тыс. скачиваний")
    assert {"1", "200", "1200", "103"} <= numbers
    assert LetterGuard(allowed_numbers=numbers).check("1200 задач в трекере") == []


def test_approve_single_window(tmp_path: Path, capsys) -> None:
    import os

    from snpr import sne

    assert sne.approve_window_open(tmp_path) is False
    (tmp_path / sne.APPROVE_LOCK_NAME).write_text(f"{os.getpid()}\n", encoding="utf-8")
    assert sne.approve_window_open(tmp_path) is True
    args = argparse.Namespace(seen_store=str(tmp_path / "seen.jsonl"))
    assert sne.run_approve(args) == 0
    assert "уже открыто" in capsys.readouterr().out
    # упавший держатель (мёртвый pid) — лок не мешает
    (tmp_path / sne.APPROVE_LOCK_NAME).write_text("999999\n", encoding="utf-8")
    assert sne.approve_window_open(tmp_path) is False



# ------------------------------------------- отклики, сделанные на сайте --


class _NegotiationsApi:
    def __init__(self, pages: list[list[str]]) -> None:
        self.pages = pages

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        assert endpoint == "/negotiations"
        page = int((params or {}).get("page", 0))
        return {
            "pages": len(self.pages),
            "items": [{"vacancy": {"id": vid}} for vid in self.pages[page]],
        }


def test_sync_hh_applied_paginates_and_caches(tmp_path: Path) -> None:
    from snpr.applied_sync import load_hh_applied, sync_hh_applied

    ids = sync_hh_applied(_NegotiationsApi([["1", "2"], ["3"]]), tmp_path)
    assert ids == {"1", "2", "3"}
    assert load_hh_applied(tmp_path) == {"1", "2", "3"}

    class _Down:
        def get(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("сеть")

    # hh недоступен — берём сохранённый список
    assert sync_hh_applied(_Down(), tmp_path) == {"1", "2", "3"}


def test_card_already_applied() -> None:
    from snpr.applied_sync import card_already_applied

    assert card_already_applied({"relations": ["got_response"]})
    assert card_already_applied({"relations": ["got_rejection"]})
    assert not card_already_applied({"relations": ["favorited"]})
    assert not card_already_applied({})


def test_country_map(tmp_path: Path) -> None:
    from snpr import areas

    tree = [
        {"id": "97", "name": "Узбекистан", "areas": [{"id": "2759", "name": "Ташкент", "areas": []}]},
        {"id": "40", "name": "Казахстан", "areas": [{"id": "160", "name": "Алматы", "areas": []}]},
    ]
    mapping = areas.build_country_map(tree)
    assert mapping["2759"] == "Узбекистан" and mapping["160"] == "Казахстан"


def test_provider_role_and_similar_sources() -> None:
    """Специализации (без текста) и «похожие на резюме» — отдельные потоки
    в каждом scope; сбой мягкого источника не роняет поиск."""
    from snpr.sne import HHVacancyProvider

    calls: list[tuple[str, dict[str, Any]]] = []

    class _Api:
        def get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
            calls.append((endpoint, params))
            if endpoint.endswith("/R-BAD/similar_vacancies"):
                raise RuntimeError("404")
            tag = params.get("text") or ("roles" if "professional_role" in params else endpoint)
            return {"pages": 1, "items": [{"id": f"{tag}-{params.get('area')}"}]}

    provider = HHVacancyProvider(
        _Api(),
        extra_params={"search_field": "name", "experience": ["between1And3"]},
        scopes=[{"area": ["113"], "work_format": ["REMOTE"]}, {"area": ["2759"]}],
    )
    found = provider.search_new(
        ["pm"], 1, set(), professional_roles=["107", "150"], similar_resume_ids=["R-1", "R-BAD"]
    )
    role_calls = [p for e, p in calls if "professional_role" in p]
    assert len(role_calls) == 2 and role_calls[0]["professional_role"] == ["107", "150"]
    assert all("search_field" not in p and "text" not in p for p in role_calls)
    assert all(p["experience"] == ["between1And3"] and "date_from" in p for p in role_calls)
    similar = [e for e, _ in calls if e.endswith("similar_vacancies")]
    assert similar.count("/resumes/R-1/similar_vacancies") == 2
    # pm×2 + роли×2 + похожие R-1×2; R-BAD упал и пропущен
    assert len(found) == 6


def test_title_filter_cuts_noise() -> None:
    cfg = {**CFG, "title_filter": {"enabled": True, "exclude": r"\b1[СC]\b|маркетплейс|продаж"}}
    flt = PipelineFilter(cfg)
    bad = flt.check({"id": "1", "name": "Аналитик 1С", "work_format": [{"id": "REMOTE"}]})
    assert not bad.passed and "title" in bad.reject_reasons[0]
    assert flt.check({"id": "2", "name": "Prodject Manager в IT", "work_format": [{"id": "REMOTE"}]}).passed
    off = PipelineFilter({**cfg, "title_filter": {"enabled": False, "exclude": "1С"}})
    assert off.check({"id": "1", "name": "Аналитик 1С", "work_format": [{"id": "REMOTE"}]}).passed

"""Подработка: метка, фильтр зарплаты, строка в промпте, SuperJob."""

from __future__ import annotations

from typing import Any

from snpr.filters import PipelineFilter
from snpr.report import vacancy_fields
from snpr.scoring import VacancyScorer
from snpr.side_job import employment_label, employment_line, is_side_job
from snpr.superjob import to_hh

CFG: dict[str, Any] = {
    "floor_net_usd": 600,
    "min_from_ratio": 0.8,
    "geo": {"enabled": False},
    "grade": {"enabled": False},
    "vendor": {"enabled": False},
    "gov": {"enabled": False},
    "title_filter": {"enabled": False},
    "blacklist": {"enabled": False},
}


def _vac(form: str | None = None, salary: dict | None = None, mode: str | None = None) -> dict:
    v: dict[str, Any] = {"id": "1", "name": "Аналитик данных", "employer": {"name": "X"}}
    if form:
        v["employment_form"] = {"id": form}
    if salary:
        v["salary"] = salary
        if mode:
            v["salary_range"] = {**salary, "mode": {"id": mode}}
    return v


def _salary_rule(cfg: dict, vacancy: dict):
    verdict = PipelineFilter(cfg).check(vacancy)
    return next(r for r in verdict.rules if r.rule == "salary")


LOW = {"from": 30000, "to": 40000, "currency": "RUR", "gross": False}


def test_detection():
    assert is_side_job(_vac("PART"))
    assert is_side_job(_vac("PROJECT"))
    assert is_side_job(_vac("SIDE_JOB"))
    assert not is_side_job(_vac("FULL"))
    assert not is_side_job(_vac())
    assert is_side_job({"employment": {"id": "part"}})  # старое поле hh
    assert employment_label(_vac("PART")) == "частичная занятость"
    assert employment_label(_vac("FULL")) == ""
    assert employment_line(_vac("PROJECT")) == "проектная работа (подработка)"
    assert employment_line(_vac()) == "не указана"


def test_full_time_low_salary_still_rejected():
    assert not _salary_rule(CFG, _vac("FULL", LOW)).passed
    assert not _salary_rule(CFG, _vac(None, LOW)).passed


def test_side_job_skips_floor_by_default():
    rule = _salary_rule(CFG, _vac("PART", LOW))
    assert rule.passed and "подработка" in rule.reason


def test_side_job_own_floor():
    cfg = {**CFG, "side_job": {"floor_net_usd": 500}}  # 500 $ ≈ 47 500 ₽
    assert not _salary_rule(cfg, _vac("PART", LOW)).passed
    rich = {"from": 50000, "to": 60000, "currency": "RUR", "gross": False}
    assert _salary_rule(cfg, _vac("PART", rich)).passed


def test_hourly_or_service_pay_not_compared_with_month():
    rule = _salary_rule(CFG, _vac("FULL", {"from": 300, "to": 500, "currency": "USD", "gross": False}, "SERVICE"))
    assert rule.passed and "за услугу" in rule.reason
    assert not _salary_rule(CFG, _vac("FULL", LOW, "MONTH")).passed


class _AI:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def complete(self, message: str, **kwargs: Any) -> str:
        self.messages.append(message)
        return '{"role":"r","reasons":[],"matched":[],"missing":[],"employer_requests":[],"score":50,"verdict":"maybe"}'


def test_prompt_has_employment_and_pay_mode():
    ai = _AI()
    VacancyScorer(ai, profile="p", retries=0).score(
        _vac("PART", {"from": 300, "to": 500, "currency": "USD", "gross": False}, "SERVICE"), "текст"
    )
    assert "Занятость: частичная занятость (подработка)" in ai.messages[0]
    assert "300-500 USD (net) за услугу" in ai.messages[0]


def test_vacancy_fields_label():
    fields = vacancy_fields(_vac("PROJECT", LOW, "HOUR"))
    assert fields["employment"] == "проектная работа"
    assert fields["salary"].endswith("за час")
    assert vacancy_fields(_vac("FULL"))["employment"] == ""


def test_superjob_type_of_work():
    base = {"id": 5, "profession": "Аналитик", "town": {"title": "Москва"}}
    assert to_hh({**base, "type_of_work": {"id": 13}})["employment_form"] == {"id": "PART"}
    assert to_hh({**base, "type_of_work": {"id": 7}})["employment_form"] == {"id": "PROJECT"}
    assert is_side_job(to_hh({**base, "type_of_work": {"id": 14}}))
    assert not is_side_job(to_hh({**base, "type_of_work": {"id": 6}}))
    assert "employment_form" not in to_hh(base)

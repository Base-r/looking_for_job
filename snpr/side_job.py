"""Подработка: частичная занятость, проектная работа, подработка.

Тип занятости берётся из полей вакансии в формате hh: employment_form
(PART / PROJECT / SIDE_JOB) или старое employment (part / project).
SuperJob переводит свой type_of_work в employment_form при разборе.

Для подработки месячный минимум зарплаты не сравнивается напрямую
([side_job] в config.toml), а LLM видит строку «Занятость».
"""

from __future__ import annotations

from typing import Any, Mapping

SIDE_FORMS = {
    "PART": "частичная занятость",
    "PROJECT": "проектная работа",
    "SIDE_JOB": "подработка",
}
FULL_FORMS = {"FULL": "полная занятость", "FLY_IN_FLY_OUT": "вахта"}
_LEGACY = {"part": "PART", "project": "PROJECT", "full": "FULL"}
# SuperJob type_of_work → employment_form hh
SUPERJOB_TYPES = {6: "FULL", 7: "PROJECT", 9: "FLY_IN_FLY_OUT", 10: "PART", 13: "PART", 14: "PART"}
# hh salary_range.mode: как считается сумма
SALARY_MODES = {"HOUR": "за час", "SHIFT": "за смену", "SERVICE": "за услугу", "FLY_IN_FLY_OUT": "за вахту"}


def employment_form(vacancy: Mapping[str, Any]) -> str:
    """ID формы занятости hh (PART, PROJECT, …) или пустая строка."""
    raw = vacancy.get("employment_form")
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if isinstance(raw, Mapping) and raw.get("id"):
        return str(raw["id"]).upper()
    legacy = vacancy.get("employment")
    if isinstance(legacy, Mapping):
        return _LEGACY.get(str(legacy.get("id") or ""), "")
    return ""


def is_side_job(vacancy: Mapping[str, Any]) -> bool:
    return employment_form(vacancy) in SIDE_FORMS


def employment_label(vacancy: Mapping[str, Any]) -> str:
    """«подработка» / «частичная занятость» / … ; полная и неизвестная — пусто."""
    return SIDE_FORMS.get(employment_form(vacancy), "")


def employment_line(vacancy: Mapping[str, Any]) -> str:
    """Строка для промпта оценки."""
    form = employment_form(vacancy)
    if form in SIDE_FORMS:
        return f"{SIDE_FORMS[form]} (подработка)"
    return FULL_FORMS.get(form, "не указана")


def salary_mode(vacancy: Mapping[str, Any]) -> str:
    """Как считается зарплата: MONTH, HOUR, SERVICE… Пусто — неизвестно (месяц)."""
    for key in ("salary_range", "salary"):
        mode = (vacancy.get(key) or {}).get("mode")
        if isinstance(mode, Mapping) and mode.get("id"):
            return str(mode["id"]).upper()
    return ""


def salary_mode_label(vacancy: Mapping[str, Any]) -> str:
    return SALARY_MODES.get(salary_mode(vacancy), "")

"""Список вакансий: консоль + HTML-таблица со ссылками.

`python -m snpr list` собирает журнал скоринга (data/scores.jsonl) и
статусы (отклик отправлен / ждёт решения / отклонено) в одну таблицу,
отсортированную по скору. Пробный прогон (`sne --dry-run`) пишет такую
же таблицу из текущих кандидатов в data/dry_run_vacancies.html.
"""

from __future__ import annotations

import argparse
import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .approve import vacancy_url


def _salary_text(salary: Mapping[str, Any] | None) -> str:
    salary = salary or {}
    parts = [str(salary.get(k)) for k in ("from", "to") if salary.get(k) is not None]
    if not parts:
        return ""
    text = parts[0] if len(parts) == 1 else f"{parts[0]}–{parts[1]}"
    prefix = "от " if salary.get("from") is not None and salary.get("to") is None else (
        "до " if salary.get("from") is None else ""
    )
    gross = salary.get("gross")
    tax = " до вычета" if gross else (" на руки" if gross is False else "")
    return f"{prefix}{text} {salary.get('currency') or ''}{tax}".strip()


def vacancy_fields(vacancy: Mapping[str, Any]) -> dict[str, Any]:
    """Человекочитаемые поля вакансии для списка."""
    formats = [
        str(x.get("name") or x.get("id") or "")
        for x in vacancy.get("work_format") or []
        if isinstance(x, Mapping)
    ]
    from .areas import country_of
    from .side_job import employment_label, salary_mode_label

    area = vacancy.get("area") or {}
    salary = _salary_text(vacancy.get("salary"))
    if salary and (mode := salary_mode_label(vacancy)):
        salary += f" {mode}"
    city = str(area.get("name") or "")
    country = country_of(area.get("id"))
    return {
        "url": vacancy_url(dict(vacancy)),
        "salary": salary,
        "employment": employment_label(vacancy),
        "area": f"{city} · {country}" if country and country != city else city,
        "country": country,
        "work_format": ", ".join(f for f in formats if f),
        "experience": str((vacancy.get("experience") or {}).get("name") or ""),
        "published_at": str(vacancy.get("published_at") or ""),
    }


STATUS_LABELS = {
    "ready": "письмо готово",
    "parked": "ждёт вашего решения",
    "pass": "не подходит",
    "maybe": "без письма",
    "target": "без письма",
    "filtered_out": "отсеяно фильтром",
    "error": "ошибка LLM",
    "deferred": "отложено",
    "applied": "отклик отправлен",
    "closed": "вы отклонили",
}


def rows_from_candidates(candidates: Iterable[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        vacancy = candidate.vacancy
        score = candidate.score
        row = {
            "vacancy_id": candidate.vacancy_id,
            "name": str(vacancy.get("name") or ""),
            "employer": str((vacancy.get("employer") or {}).get("name") or ""),
            "score": score.score if score else None,
            "verdict": score.verdict if score else "",
            "reasons": list(score.reasons) if score else list(candidate.reject_reasons),
            "matched": list(score.matched) if score else [],
            "missing": list(score.missing) if score else [],
            "letter": candidate.letter.text if candidate.letter else "",
            "status": candidate.status,
        }
        row.update(vacancy_fields(vacancy))
        rows.append(row)
    return rows


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                rows.append(rec)
    return rows


def rows_from_stores(data_dir: Path) -> list[dict[str, Any]]:
    """Последний скор каждой вакансии + её текущий статус."""
    latest: dict[str, dict[str, Any]] = {}
    for rec in _load_jsonl(data_dir / "scores.jsonl"):
        vid = str(rec.get("vacancy_id") or "")
        if vid:
            latest[vid] = rec

    from .applied_sync import load_hh_applied

    applied = {
        str(r.get("vacancy_id"))
        for r in _load_jsonl(data_dir / "applied.jsonl")
        if not r.get("dry_run")
    } | load_hh_applied(data_dir)  # и отклики, сделанные вручную на сайте
    parked: dict[str, dict[str, Any]] = {}
    for rec in _load_jsonl(data_dir / "parked.jsonl"):
        vid = str((rec.get("vacancy") or {}).get("id") or "")
        if vid:
            parked[vid] = rec

    rows: list[dict[str, Any]] = []
    for vid, rec in latest.items():
        row = dict(rec)
        row.setdefault("url", vacancy_url(vid))
        if vid in applied:
            row["status"] = "applied"
        elif vid in parked:
            row["status"] = "parked"
            row["letter"] = parked[vid].get("letter", "")
        elif rec.get("verdict") == "pass":
            row["status"] = "pass"
        else:
            row["status"] = "closed"
        rows.append(row)
    return rows


def _sort_key(row: Mapping[str, Any]) -> tuple[int, str]:
    score = row.get("score")
    return (-(score if isinstance(score, int) else -1), str(row.get("name") or ""))


def print_rows(rows: list[dict[str, Any]], limit: int = 0) -> None:
    shown = rows[:limit] if limit > 0 else rows
    for row in shown:
        score = row.get("score")
        score_text = f"{score:3d}" if isinstance(score, int) else "  —"
        status = STATUS_LABELS.get(str(row.get("status")), str(row.get("status") or ""))
        details = " | ".join(
            part
            for part in (row.get("salary"), row.get("area"), row.get("work_format"))
            if part
        )
        print(f"{score_text}  {row.get('name', '')} — {row.get('employer', '')}  [{status}]")
        if details:
            print(f"     {details}")
        print(f"     {row.get('url', '')}")


def _score_class(score: Any) -> str:
    if not isinstance(score, int):
        return "none"
    if score >= 75:
        return "high"
    if score >= 50:
        return "mid"
    return "low"


def render_html(rows: list[dict[str, Any]], title: str) -> str:
    esc = html.escape
    body: list[str] = []
    for row in rows:
        score = row.get("score")
        status = STATUS_LABELS.get(str(row.get("status")), str(row.get("status") or ""))
        reasons = "".join(f"<li>{esc(str(r))}</li>" for r in row.get("reasons") or [])
        matched = "; ".join(esc(str(r)) for r in row.get("matched") or [])
        missing = "; ".join(esc(str(r)) for r in row.get("missing") or [])
        letter = row.get("letter") or ""
        extra = ""
        if matched:
            extra += f"<div class='ok'>Совпало: {matched}</div>"
        if missing:
            extra += f"<div class='miss'>Не хватает: {missing}</div>"
        if letter:
            extra += (
                "<details><summary>Письмо</summary>"
                f"<pre>{esc(str(letter))}</pre></details>"
            )
        body.append(
            "<tr>"
            f"<td class='score {_score_class(score)}'>"
            f"{score if isinstance(score, int) else '—'}</td>"
            f"<td><a href='{esc(str(row.get('url') or ''))}' target='_blank' rel='noopener'>"
            f"{esc(str(row.get('name') or ''))}</a>"
            f"<div class='emp'>{esc(str(row.get('employer') or ''))}</div></td>"
            f"<td>{esc(str(row.get('salary') or '—'))}</td>"
            f"<td>{esc(str(row.get('area') or ''))}"
            f"<div class='emp'>{esc(str(row.get('work_format') or ''))}</div></td>"
            f"<td>{esc(status)}</td>"
            f"<td><ul>{reasons}</ul>{extra}</td>"
            "</tr>"
        )
    generated = datetime.now().strftime("%d.%m.%Y %H:%M")
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>
:root {{ --bg:#fff; --fg:#1d1d1f; --muted:#6e6e73; --line:#e5e5ea;
  --high:#1f8a4c; --mid:#b26a00; --low:#b3261e; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#161617; --fg:#f2f2f7; --muted:#a1a1a6; --line:#2c2c2e;
    --high:#4cc27c; --mid:#f0a33a; --low:#ff6b60; }} }}
body {{ background:var(--bg); color:var(--fg);
  font:14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; margin:16px; }}
h1 {{ font-size:20px; margin:0 0 4px; }}
.meta {{ color:var(--muted); margin-bottom:16px; }}
table {{ border-collapse:collapse; width:100%; }}
th, td {{ border-bottom:1px solid var(--line); padding:8px; text-align:left;
  vertical-align:top; }}
th {{ color:var(--muted); font-weight:600; position:sticky; top:0; background:var(--bg); }}
a {{ color:inherit; font-weight:600; }}
.emp {{ color:var(--muted); font-size:13px; }}
.score {{ font-weight:700; font-size:16px; text-align:center; }}
.high {{ color:var(--high); }} .mid {{ color:var(--mid); }} .low {{ color:var(--low); }}
ul {{ margin:0; padding-left:18px; }}
.ok {{ color:var(--high); margin-top:4px; }} .miss {{ color:var(--mid); margin-top:2px; }}
pre {{ white-space:pre-wrap; font:inherit; background:rgba(127,127,127,.08);
  padding:8px; border-radius:6px; }}
details {{ margin-top:6px; }}
.wrap {{ overflow-x:auto; }}
</style></head><body>
<h1>{esc(title)}</h1>
<div class="meta">Вакансий: {len(rows)} · сформировано {generated}</div>
<div class="wrap"><table>
<thead><tr><th>Скор</th><th>Вакансия</th><th>Зарплата</th><th>Где</th>
<th>Статус</th><th>Почему</th></tr></thead>
<tbody>
{''.join(body)}
</tbody></table></div>
</body></html>
"""


def write_html_report(rows: list[dict[str, Any]], path: Path, title: str) -> Path:
    rows = sorted(rows, key=_sort_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(rows, title), encoding="utf-8")
    return path


def run_list(args: argparse.Namespace, data_dir: Path, min_score: int) -> int:
    rows = rows_from_stores(data_dir)
    if not rows:
        print(
            "Список пуст: оценённых вакансий ещё нет (data/scores.jsonl). "
            "Запустите прогон: python -m snpr sne --mode park"
        )
        return 0
    threshold = 0 if args.all else (args.min_score if args.min_score >= 0 else min_score)
    # уже откликнулись — в подборку не попадают (видны с --all)
    selected = sorted(
        (
            r
            for r in rows
            if isinstance(r.get("score"), int)
            and r["score"] >= threshold
            and (args.all or r.get("status") != "applied")
        ),
        key=_sort_key,
    )
    print(
        f"Подходящие вакансии (скор от {threshold}): {len(selected)} "
        f"из {len(rows)} оценённых"
    )
    print()
    print_rows(selected, args.limit)
    path = write_html_report(
        selected, data_dir / "vacancies.html", title="Подходящие вакансии"
    )
    print()
    print(f"Таблица со ссылками и письмами: {path}")
    if args.open:
        import webbrowser

        webbrowser.open(path.as_uri())
    return 0

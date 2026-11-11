from __future__ import annotations

import enum
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .pipeline import Candidate


def vacancy_age_hours(
    published_at: object, now: datetime | None = None
) -> float | None:
    """Возраст вакансии в часах: aware-сравнение, рассинхрон поясов исключён.

    published_at из API hh — московское серверное время с явным смещением
    («2026-10-17T15:01:47+03:00»). Часы локальные (Екб, +05): разность
    МОМЕНТОВ не зависит от пояса — сравниваем aware с aware. Строка без
    смещения трактуется как +03:00 (сервер hh). None — не разобралось:
    возраст неизвестен, автопилоту пускать нельзя.
    """
    try:
        moment = datetime.fromisoformat(str(published_at or ""))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone(timedelta(hours=3)))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    seconds = (current - moment).total_seconds()
    return max(0.0, seconds / 3600)


class Decision(enum.Enum):
    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"
    SKIP = "skip"
    PARK = "park"


@dataclass
class ApproveDecision:
    decision: Decision
    letter_override: str | None = None


ApprovePrompt = Callable[[str], str]


def vacancy_url(vacancy: object) -> str:
    """Ссылка на вакансию для человека (alternate_url из API или по id)."""
    if isinstance(vacancy, dict):
        url = str(vacancy.get("alternate_url") or "").strip()
        if url:
            return url
        vid = str(vacancy.get("id") or "").strip()
    else:
        vid = str(vacancy or "").strip()
    if vid.startswith("gj:"):
        return f"https://geekjob.ru/vacancy/{vid[3:]}"
    if vid.startswith("hc:"):
        return f"https://career.habr.com/vacancies/{vid[3:]}"
    if vid.startswith("tg:"):
        return f"https://t.me/{vid[3:]}"
    if vid.startswith("sj:"):  # адрес вакансии строится со слагом, по id его не собрать
        return "https://www.superjob.ru/"
    if vid.startswith("tm:"):
        return f"https://talent-move.ru/jobs/{vid[3:]}/"
    return f"https://hh.ru/vacancy/{vid}" if vid else ""


def edit_text_in_editor(
    text: str, prompt: ApprovePrompt = input, editor: str | None = None
) -> str:
    """Правка письма целиком в редакторе (Windows — Блокнот).

    Открывает временный файл и ждёт закрытия окна; вернёт новый текст.
    Нет редактора — фоллбек на ввод одной строкой.
    """
    import os
    import subprocess
    import tempfile

    command = editor or os.environ.get("SNE_EDITOR") or (
        "notepad.exe" if os.name == "nt" else os.environ.get("EDITOR", "")
    )
    if not command:
        return prompt("Новый текст письма (одной строкой): ").strip() or text
    fd, path = tempfile.mkstemp(prefix="snpr_letter_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("Открыл письмо в редакторе: поправьте, сохраните (Ctrl+S) и закройте окно.")
        subprocess.run([command, path], check=False)
        with open(path, encoding="utf-8-sig") as fh:
            edited = fh.read().strip()
        return edited or text
    except OSError as ex:
        print(f"Редактор не открылся ({ex}) — ввод одной строкой.")
        return prompt("Новый текст письма (одной строкой): ").strip() or text
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _format_card(candidate: Candidate) -> str:
    vacancy = candidate.vacancy
    salary = vacancy.get("salary") or {}
    salary_parts = [
        str(salary.get(k)) for k in ("from", "to") if salary.get(k) is not None
    ]
    salary_line = (
        f"{salary_parts[0]}-{salary_parts[-1]} {salary.get('currency', '')}".strip()
        if salary_parts
        else "—"
    )
    score = candidate.score
    score_line = f"{score.score} ({score.verdict})" if score else "нет"
    reasons = "; ".join(score.reasons[:3]) if score else ""
    letter = candidate.letter
    similarity = f"{letter.similarity:.2f}" if letter else "—"
    letter_text = letter.text if letter else ""
    age_line = _published_age_line(vacancy.get("published_at"))
    from .areas import country_of

    area = (vacancy.get("area") or {}).get("name", "")
    country = country_of((vacancy.get("area") or {}).get("id"))
    if country and country != area:
        area = f"{area} · {country}"
    formats = ", ".join(
        str(x.get("name") or x.get("id") or "")
        for x in vacancy.get("work_format") or []
        if isinstance(x, dict)
    )
    lines = [
        "=" * 72,
        f"{vacancy.get('name', '')} — {(vacancy.get('employer') or {}).get('name', '')}",
        f"Ссылка: {vacancy_url(vacancy)}",
        f"Вилка: {salary_line} | Скор: {score_line} | Свежесть письма: {similarity}"
        + age_line,
    ]
    if area or formats:
        lines.append(f"Где: {area}" + (f" | Формат: {formats}" if formats else ""))
    if reasons:
        lines.append(f"Причины скора: {reasons}")
    if score and score.matched:
        lines.append(f"Совпало: {'; '.join(score.matched[:5])}")
    if score and score.missing:
        lines.append(f"Не хватает: {'; '.join(score.missing[:5])}")
    if letter_text:
        lines.append("")
        lines.append(letter_text)
    lines.append("=" * 72)
    return "\n".join(lines)


def _published_age_line(published_at: object) -> str:
    """«опубликована 2.4 ч назад» — скорость отклика решает, показываем."""
    age = vacancy_age_hours(published_at)
    if age is None:
        return ""
    if age < 1.0:
        return f" | опубликована {max(1, int(age * 60))} мин назад"
    return f" | опубликована {age:.1f} ч назад"


class CLIApprover:
    def __init__(self, prompt: ApprovePrompt = input):
        self.prompt = prompt

    def decide(self, candidate: Candidate) -> ApproveDecision:
        if not candidate.letter or not candidate.letter.text:
            return ApproveDecision(Decision.SKIP)
        print(_format_card(candidate))
        try:
            answer = self.prompt(
                "Отправить? [y — да / n — нет / e — правка письма] (Enter = n): "
            ).strip().lower()
        except EOFError:
            # пайп-режим ('n' | snpr ...): ввод кончился — отказ, прогон
            # не ронять (иначе теряются уже проскоренные вакансии)
            print()
            return ApproveDecision(Decision.REJECT)
        if answer in ("y", "д", "yes", "да"):
            return ApproveDecision(Decision.APPROVE)
        if answer in ("e", "у", "edit", "правка"):
            if self.prompt is input:
                new_letter = edit_text_in_editor(candidate.letter.text)
            else:
                new_letter = self.prompt("Новый текст письма (одной строкой): ")
            if new_letter.strip():
                return ApproveDecision(Decision.EDIT, letter_override=new_letter.strip())
            return ApproveDecision(Decision.REJECT)
        return ApproveDecision(Decision.REJECT)


class ParkingApprover:
    """Режим для запланированных прогонов: ready-кандидаты — в парковку.

    Ни пуска, ни отказа: вакансия со скором и письмом сохраняется
    в data/parked.jsonl и ждёт решения человека (snpr approve).
    Очередь «сделать пуск» не закрывается в seen — не теряется.
    """

    def decide(self, candidate: Candidate) -> ApproveDecision:
        return ApproveDecision(Decision.PARK)


class AutoParkApprover:
    """Парк-режим с автопилотом (18.10, решение пользователя).

    Скор >= порога И возраст <= max_age_hours — пуск без человека
    (гвард проверяется ДО решения: нарушение = принудительно ручной
    путь, автопилот не пускает). Ниже порога, без письма, без скора,
    письмо неуникально, вакансия старше лимита или возраст неизвестен —
    парковка на решение человека (`snpr approve`). 0 = чистый парк.
    """

    def __init__(
        self,
        score_threshold: int = 55,
        require_unique: bool = True,
        max_age_hours: float | None = None,
    ):
        self.score_threshold = score_threshold
        self.require_unique = require_unique
        self.max_age_hours = max_age_hours

    def decide(self, candidate: Candidate) -> ApproveDecision:
        score = candidate.score
        letter = candidate.letter
        if not letter or not letter.text or score is None:
            return ApproveDecision(Decision.PARK)
        if self.max_age_hours is not None:
            age = vacancy_age_hours(candidate.vacancy.get("published_at"))
            if age is None or age > self.max_age_hours:
                return ApproveDecision(Decision.PARK)
        if score.score < self.score_threshold:
            return ApproveDecision(Decision.PARK)
        if self.require_unique and not letter.is_unique_enough:
            return ApproveDecision(Decision.PARK)
        return ApproveDecision(Decision.APPROVE)


class AutoApprover:
    def __init__(self, score_threshold: int = 75, require_unique: bool = True):
        self.score_threshold = score_threshold
        self.require_unique = require_unique

    def decide(self, candidate: Candidate) -> ApproveDecision:
        score = candidate.score
        letter = candidate.letter
        if not letter or not letter.text:
            return ApproveDecision(Decision.SKIP)
        if score is None or score.score < self.score_threshold:
            return ApproveDecision(Decision.SKIP)
        if self.require_unique and not letter.is_unique_enough:
            return ApproveDecision(Decision.SKIP)
        return ApproveDecision(Decision.APPROVE)


@dataclass
class AppliedRecord:
    vacancy_id: str
    vacancy_name: str
    employer: str
    score: int
    letter: str
    decision: str
    dry_run: bool
    resume_id: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "vacancy_id": self.vacancy_id,
                "vacancy_name": self.vacancy_name,
                "employer": self.employer,
                "score": self.score,
                "letter": self.letter,
                "decision": self.decision,
                "dry_run": self.dry_run,
                "resume_id": self.resume_id,
                "ts": int(time.time()),
            },
            ensure_ascii=False,
        )


class AppliedStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, record: AppliedRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(record.to_json() + "\n")

    def vacancy_ids(self) -> set[str]:
        """ID вакансий с историей пусков (для дедупа живого поллинга)."""
        if not self.path.exists():
            return set()
        ids: set[str] = set()
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("vacancy_id"):
                    ids.add(str(rec["vacancy_id"]))
        return ids

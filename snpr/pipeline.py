from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from .cover_letter import LetterDraft
from .filters import PipelineFilter, Verdict
from .scoring import LLMAuthError, LLMUnavailable, ScoreResult, VacancyScorer

DescriptionSource = Callable[[Mapping[str, Any]], str]


class VacancyGone(Exception):
    """Описание вакансии не получить (удалена, страница сломана) —
    пропускаем только её, остальные оцениваем дальше."""


class PipelineAbort(RuntimeError):
    """Прогон прерван: эндпоинт LLM упал подряд на нескольких вакансиях."""


class LetterWriter(Protocol):
    """Любой источник письма: StaticLetterWriter, CoverLetterWriter."""

    def write(
        self,
        vacancy: Mapping[str, Any],
        description: str = "",
        score: ScoreResult | None = None,
    ) -> LetterDraft: ...


@dataclass
class Candidate:
    vacancy: Mapping[str, Any]
    verdict: Verdict | None = None
    score: ScoreResult | None = None
    letter: LetterDraft | None = None
    reject_reasons: list[str] = field(default_factory=list)
    error: str = ""
    deferred: bool = False
    parked: bool = False

    @property
    def vacancy_id(self) -> str:
        return str(self.vacancy.get("id") or "")

    @property
    def status(self) -> str:
        if self.error:
            return "error"
        if self.deferred:
            return "deferred"
        if self.parked:
            return "parked"
        if self.reject_reasons:
            return "filtered_out"
        if self.score is None:
            return "unscored"
        if self.letter is None:
            return self.score.verdict
        if self.letter.text:
            return "ready"
        return self.score.verdict


class SnePipeline:
    def __init__(
        self,
        prefilter: PipelineFilter,
        scorer: VacancyScorer | None = None,
        writer: LetterWriter | None = None,
        letter_min_verdict: str = "maybe",
        llm_error_abort: int = 4,
        score_budget: int = 0,
        scorers: Sequence[VacancyScorer] | None = None,
        writers: Sequence[LetterWriter] | None = None,
    ):
        self.prefilter = prefilter
        self.scorer = scorer
        self.writer = writer
        # письма пишутся в том же потоке, что и оценка: у каждого потока свой
        # писатель (свой LLM-клиент), иначе письма идут строго по одному
        self.writers: list[LetterWriter] = list(writers) if writers else (
            [writer] if writer is not None else []
        )
        self.letter_min_verdict = letter_min_verdict
        self.llm_error_abort = llm_error_abort
        # почему оценка остановилась раньше времени (нет баланса LLM и т.п.)
        self.abort_reason = ""
        self.score_budget = score_budget
        # 17.10: скоринг волнами. У ChatOpenAI лок держится весь HTTP-запрос,
        # один инстанс = строго последовательные вызовы. Поэтому параллелизм
        # это N независимых скореров (у cli каждый со своим клиентом);
        # один скорер = прежнее последовательное поведение.
        self.scorers: list[VacancyScorer] = list(scorers) if scorers else (
            [scorer] if scorer is not None else []
        )

    def run(
        self,
        vacancies: Iterable[Mapping[str, Any]],
        descriptions: Mapping[str, str] | DescriptionSource | None = None,
        now: Any = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[Candidate]:
        """Прогон: дешёвые гейты → карточки+гейты описания → скоринг → письма.

        Порядок кандидатов на выходе — порядок вакансий на входе.
        Скоринг идёт волнами по len(self.scorers) параллельных вызовов;
        ошибки LLM бюджет не съедают (как и раньше). Нет баланса/ключа или
        4 провала подряд — новые не берём, оценённое возвращается как обычно,
        остальное отложено; причина — в self.abort_reason.
        """
        self.abort_reason = ""
        candidates: list[Candidate] = []
        pending: list[tuple[Candidate, Callable[[str], str]]] = []

        for vacancy in vacancies:
            candidate = Candidate(vacancy=vacancy)
            candidates.append(candidate)

            get_desc: Callable[[str], str]
            if descriptions is None:
                get_desc = lambda _vid: ""
            elif callable(descriptions):
                source: DescriptionSource = descriptions
                # vacancy биндим дефолт-аргументом: замыкание по переменной
                # цикла ловит ПОСЛЕДНЮЮ вакансию (живой баг 17.10: весь пул
                # получил описание последней вакансии — Rambler Data Engineer)
                get_desc = lambda _vid, _vac=vacancy: source(_vac) or ""
            else:
                get_desc = lambda vid: str(descriptions.get(vid, ""))

            verdict = self.prefilter.check(vacancy, now=now)
            if not verdict.passed:
                candidate.verdict = verdict
                candidate.reject_reasons = verdict.reject_reasons
                continue
            pending.append((candidate, get_desc))

        if self.scorers:
            self._run_waves(pending, now=now, on_progress=on_progress)
        else:
            for candidate, get_desc in pending:
                description = get_desc(candidate.vacancy_id)
                full_verdict = self.prefilter.check(
                    candidate.vacancy, description=description, now=now
                )
                candidate.verdict = full_verdict
                if not full_verdict.passed:
                    candidate.reject_reasons = full_verdict.reject_reasons
        return candidates

    def _run_waves(
        self,
        pending: list[tuple[Candidate, Callable[[str], str]]],
        now: Any = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        scored = 0
        attempts = 0
        consecutive_errors = 0
        workers = max(1, len(self.scorers))
        limit = self.score_budget if self.score_budget > 0 else len(pending)
        total = len(pending)
        idx = 0
        executor = ThreadPoolExecutor(max_workers=workers)
        # Непрерывный конвейер вместо волн: освободился поток — сразу берёт
        # следующую вакансию. Один медленный ответ LLM больше не держит всех.
        # У каждого слота свой скорер и писатель (свой LLM-клиент).
        free_slots = list(range(workers))
        inflight: dict[Any, tuple[Candidate, int]] = {}
        feed_stopped = False
        try:
            while True:
                # дозаполнить свободные слоты; ошибки бюджет не съедают
                while (
                    not feed_stopped
                    and idx < total
                    and free_slots
                    and scored + len(inflight) < limit
                ):
                    candidate, get_desc = pending[idx]
                    # карточки ленивые: только те, кто реально идёт в скоринг
                    try:
                        description = get_desc(candidate.vacancy_id)
                    except Exception as ex:  # noqa: BLE001 — капча/сеть hh
                        if isinstance(ex, VacancyGone) or type(ex).__name__ == "ResourceNotFound":
                            # вакансию удалили — пропустить, идти дальше
                            idx += 1
                            candidate.reject_reasons = ["вакансия удалена с hh"]
                            continue
                        # hh перестал отдавать карточки: оценённое сохраняем,
                        # остальное (с этой вакансии) — в отложенные
                        print(f"[карточки] hh не отдал вакансию: {ex}; "
                              "остальные оценим в следующий запуск")
                        feed_stopped = True
                        break
                    idx += 1
                    full_verdict = self.prefilter.check(
                        candidate.vacancy, description=description, now=now
                    )
                    candidate.verdict = full_verdict
                    if not full_verdict.passed:
                        candidate.reject_reasons = full_verdict.reject_reasons
                        continue
                    slot = free_slots.pop()
                    writer = (
                        self.writers[slot % len(self.writers)] if self.writers else None
                    )
                    fut = executor.submit(
                        self._score_and_write,
                        self.scorers[slot],
                        writer,
                        candidate.vacancy,
                        description,
                    )
                    inflight[fut] = (candidate, slot)
                if not inflight:
                    break

                done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
                for fut in done:
                    candidate, slot = inflight.pop(fut)
                    free_slots.append(slot)
                    attempts += 1
                    try:
                        score, letter, letter_error = fut.result()
                        candidate.score = score
                        scored += 1
                        consecutive_errors = 0
                        if letter_error is not None:
                            raise letter_error
                        if letter is not None:
                            candidate.letter = letter
                    except LLMAuthError as ex:
                        # неверный ключ/нет баланса — дальше будет то же самое:
                        # новых не берём, оценённое сохраняется, остальное —
                        # в отложенные до следующего запуска
                        candidate.deferred = True
                        if not self.abort_reason:
                            self.abort_reason = str(ex)
                        feed_stopped = True
                    except LLMUnavailable as ex:
                        candidate.error = str(ex)
                        consecutive_errors += 1
                        if consecutive_errors >= self.llm_error_abort and not self.abort_reason:
                            self.abort_reason = (
                                f"LLM-эндпоинт не отвечает {consecutive_errors} "
                                f"раз подряд: {ex}"
                            )
                            feed_stopped = True
                    if on_progress is not None:
                        on_progress(attempts, limit)

            # хвост за бюджетом — отложен, вернёмся в следующем прогоне
            for candidate, _get_desc in pending[idx:]:
                candidate.deferred = True
        finally:
            executor.shutdown(wait=True)

    def _score_and_write(
        self,
        scorer: VacancyScorer,
        writer: LetterWriter | None,
        vacancy: Mapping[str, Any],
        description: str,
    ) -> tuple[ScoreResult, LetterDraft | None, Exception | None]:
        """Оценка и (если прошла порог) письмо — в одном рабочем потоке.
        Сбой письма возвращаем, а не бросаем: оценка не теряется."""
        score = scorer.score(vacancy, description)
        if writer is None or not self._wants_letter(score):
            return score, None, None
        try:
            return score, writer.write(vacancy, description=description, score=score), None
        except Exception as ex:  # noqa: BLE001 — разберёт основной цикл
            return score, None, ex

    def _wants_letter(self, score: ScoreResult) -> bool:
        order = {"pass": 0, "maybe": 1, "target": 2}
        min_level = order.get(self.letter_min_verdict, 1)
        return order.get(score.verdict, 0) >= min_level

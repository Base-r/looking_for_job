"""Внешние источники вакансий (не hh): GeekJob, Habr Career, Telegram.

Общее у них: вакансия в формате hh с префиксом id, откликаетесь вы сами
по ссылке (бот только готовит письмо и ставит отметку «Я откликнулся»).
"""

from __future__ import annotations

import re
from typing import Any, Mapping

PREFIXES = {"gj:": "geekjob", "hc:": "habr", "tg:": "telegram", "sj:": "superjob"}
LABELS = {
    "hh": "hh", "geekjob": "GeekJob", "habr": "Habr Career",
    "telegram": "Telegram", "superjob": "SuperJob",
}

# по умолчанию: что из вакансий внешних источников вообще смотреть
DEFAULT_TITLE_MUST = (
    r"аналит|analyst|analytics|\bBI\b|data scien|дата-сайент|проектов|проектами|project|"
    r"менеджер продукта|владелец продукта|продакт|product|delivery|деливери|scrum|"
    r"координатор|coordinator|\bPMO?\b|owner|требовани|бизнес-процесс"
)
DEFAULT_HABR_QUERIES = [
    "аналитик", "analyst", "project manager", "руководитель проектов",
    "менеджер проектов", "product manager", "менеджер продукта",
]
DEFAULT_TELEGRAM_CHANNELS = ["analysts_jobs", "datajobschannel", "uzdev_jobs", "getitrussia"]


def vacancy_id(vacancy_or_id: Any) -> str:
    vid = vacancy_or_id.get("id") if isinstance(vacancy_or_id, Mapping) else vacancy_or_id
    return str(vid or "")


def source_of(vacancy_or_id: Any) -> str:
    vid = vacancy_id(vacancy_or_id)
    for prefix, name in PREFIXES.items():
        if vid.startswith(prefix):
            return name
    return "hh"


def is_external(vacancy_or_id: Any) -> bool:
    return source_of(vacancy_or_id) != "hh"


def label(source: str) -> str:
    return LABELS.get(source, source)


class ExternalSources:
    """Сбор новых вакансий со всех включённых внешних источников и
    описания для оценки. Сбой одного источника остальные не держит."""

    def __init__(self, cfg: Mapping[str, Any] | None, experience: Any = None, log: Any = None):
        cfg = cfg or {}
        self.log = log
        # опыт, который ищем на hh ([search.params].experience): внешним
        # источникам такой фильтр не передать — отсекаем сами, если опыт известен
        self.experience = {str(e) for e in experience or []}
        pause = float(cfg.get("external_pause_seconds", cfg.get("geekjob_pause_seconds", 2)) or 2)
        self.title_must = re.compile(str(cfg.get("title_must") or DEFAULT_TITLE_MUST), re.I)
        self.active: dict[str, Any] = {}
        if cfg.get("geekjob"):
            from .geekjob import GeekJobSource

            self.active["geekjob"] = GeekJobSource(pause=pause)
        if cfg.get("habr"):
            from .habr import HabrSource

            self.active["habr"] = HabrSource(
                pause=pause,
                queries=cfg.get("habr_queries") or DEFAULT_HABR_QUERIES,
                qids=[str(q) for q in cfg.get("habr_qids") or ("1", "3", "4")],
            )
        channels = cfg.get("telegram_channels")
        if cfg.get("telegram"):
            from .telegram_jobs import TelegramSource

            self.active["telegram"] = TelegramSource(
                channels=channels if channels is not None else DEFAULT_TELEGRAM_CHANNELS,
                pause=pause,
            )
            self.active["telegram"].on_issue = self._telegram_issue
        if cfg.get("superjob"):
            from .superjob import KEY_ENV, SuperJobSource

            source = SuperJobSource(
                pause=pause, queries=cfg.get("superjob_queries") or DEFAULT_HABR_QUERIES
            )
            if source.key:
                self.active["superjob"] = source
            elif log is not None:  # ключа нет — это не сбой поиска
                log.warn("superjob", f"пропущен: нет {KEY_ENV} в .env")

    def _telegram_issue(self, channel: str, text: str, failed: bool) -> None:
        if self.log is None:
            return
        name = f"telegram:{channel}"
        self.log.error(name, text) if failed else self.log.warn(name, text)

    def collect(
        self, max_age_days: float, known_ids: set[str], current: Any = ()
    ) -> list[dict[str, Any]]:
        """current — вакансии hh этого прогона: копии с других площадок мимо."""
        from .cross_dedupe import drop_cross_duplicates

        out: list[dict[str, Any]] = []
        for name, src in self.active.items():
            try:
                items = src.list_all(max_age_days=max_age_days)
            except Exception as ex:  # noqa: BLE001 — сайт недоступен: остальные ищем
                print(f"[{name}] список не получен: {ex}")
                if self.log is not None:
                    self.log.error(name, f"список не получен: {ex}")
                continue
            new = [v for v in items if v["id"] not in known_ids]
            # сайты и каналы — все профессии подряд: только наши по названию
            relevant = [
                v for v in new
                if self.title_must.search(str(v.get("name") or "")) and self._experience_ok(v)
            ]
            print(f"{label(name)}: новых {len(new)}, по профилю {len(relevant)}")
            if self.log is not None:
                self.log.ok(name, len(items), len(relevant))
            out.extend(relevant)
        kept, dropped = drop_cross_duplicates(dedupe(out), known_ids, current)
        if dropped:
            print(f"Дублей между источниками отброшено: {len(dropped)}")
        return kept

    def _experience_ok(self, vacancy: Mapping[str, Any]) -> bool:
        exp = str((vacancy.get("experience") or {}).get("id") or "")
        return not self.experience or not exp or exp in self.experience

    def description(self, vacancy: Mapping[str, Any]) -> str:
        source = source_of(vacancy)
        src = self.active.get(source)
        if src is None:  # источник выключили, а вакансия осталась в отложенных
            src = self._make(source)
        try:
            text = src.description(vacancy)
        except Exception as ex:
            if self.log is not None:
                self.log.warn(source, f"описание {vacancy.get('id')} не получено: {ex}")
            raise
        self._mark_suspicious(vacancy, source, text)
        return text

    def _mark_suspicious(self, vacancy: Mapping[str, Any], source: str, text: str) -> None:
        from .fraud import suspicious

        flags = suspicious(f"{vacancy.get('name') or ''}\n{text}")
        if flags and isinstance(vacancy, dict):
            vacancy["flags"] = flags
            if self.log is not None:
                self.log.warn(source, f"подозрительно {vacancy.get('id')}: {', '.join(flags)}")

    @staticmethod
    def _make(source: str) -> Any:
        if source == "geekjob":
            from .geekjob import GeekJobSource

            return GeekJobSource(pause=0)
        if source == "habr":
            from .habr import HabrSource

            return HabrSource(pause=0)
        if source == "superjob":
            from .superjob import SuperJobSource

            return SuperJobSource(pause=0)
        from .telegram_jobs import TelegramSource

        return TelegramSource(pause=0)


def dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Один пост в канале часто повторяют — оставляем первый."""
    seen: set[tuple[str, str]] = set()
    out = []
    for v in items:
        key = (
            re.sub(r"\W+", " ", str(v.get("name") or "")).strip().lower(),
            str((v.get("employer") or {}).get("name") or "").strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


def fetch_description(vacancy: Mapping[str, Any]) -> str:
    """Описание вакансии внешнего источника (для «Переписать письмо»)."""
    return ExternalSources._make(source_of(vacancy)).description(dict(vacancy))

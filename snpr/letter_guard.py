from __future__ import annotations

import re

MAX_LETTER_LENGTH = 2000

# Ссылки: явные http(s)://, www., и голые домены (habr.com/..., t.me/...)
URL_RE = re.compile(
    r"(?i)\b(?:https?://|www\.)\S+"
    r"|\b[a-z0-9][a-z0-9-]{0,30}\.(?:ru|com|io|ai|org|net|dev|me|tech|рф)\b\S*"
)
EMAIL_RE = re.compile(
    r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b"
)
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+7|8)[\s(-]?\d{3}[\s)-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}(?!\d)"
)
TG_RE = re.compile(r"(?<![а-яёa-z0-9_])@[a-zа-яё0-9_]{3,}(?![а-яёa-z0-9_])")
NUM_RE = re.compile(r"\d+")
# зарплата в письме: суммы с валютой и слова про доход
SALARY_RE = re.compile(
    # сумма считается зарплатой только с валютой (не «103 тысячи скачиваний»)
    r"(?i)(\d[\d \s.,]*\s*(?:тыс\.?\s*)?(?:\$|usd|долл|€|eur|₽|руб|rub|сум\b|uzs))"
    r"|ожидани[яй] по (?:доходу|зарплате|оплате)|зарплатн[а-я]+ ожидани|на руки"
)
# Число внутри слова (Битрикс24, B2B, 1С, Web3) — часть названия, а не факт
_IN_WORD_RE = re.compile(r"[^\W\d_]")


_GROUPED_RE = re.compile(r"\d{1,3}(?:[   ]\d{3})+")


def extract_numbers(text: str) -> set[str]:
    """Числа текста; «1 200» даёт и «1», «200», и слитное «1200»."""
    numbers = set(NUM_RE.findall(text))
    for match in _GROUPED_RE.finditer(text):
        numbers.add(re.sub(r"\D", "", match.group(0)))
    return numbers


def _normalize_url(url: str) -> str:
    value = url.strip().lower()
    for prefix in ("https://", "http://", "www."):
        if value.startswith(prefix):
            value = value[len(prefix) :]
    return value


class LetterGuard:
    """Пост-валидация письма перед пуском.

    Description вакансии — атакующий текст на входе LLM: письмо (в т.ч.
    статическое, после ручной правки) проверяется на следы инъекции
    (чужие ссылки, контакты, числа вне капсулы). Свои ссылки (github,
    habr) — через белый список префиксов. Нарушения не блокируют пуск,
    а переводят его на ручное решение.
    """

    def __init__(
        self,
        allowed_numbers: set[str] | None = None,
        allowed_url_prefixes: set[str] | None = None,
        max_length: int = MAX_LETTER_LENGTH,
        allowed_contacts: set[str] | None = None,
        forbid_salary: bool = False,
    ):
        self.allowed_numbers = allowed_numbers or set()
        # свои контакты кандидата ([letter.contacts]) — письмо может их
        # содержать, если работодатель просил; сравнение без регистра,
        # телефон — по цифрам
        self.forbid_salary = forbid_salary
        self.allowed_contacts = {c.strip().lower() for c in allowed_contacts or set() if c.strip()}
        self.allowed_phone_digits = {
            re.sub(r"\D", "", c) for c in self.allowed_contacts if re.sub(r"\D", "", c)
        }
        self.allowed_url_prefixes = {
            _normalize_url(prefix).rstrip("/") + "/"
            for prefix in (allowed_url_prefixes or set())
        }
        self.max_length = max_length

    def _url_violation(self, url: str) -> str | None:
        normalized = _normalize_url(url)
        for prefix in self.allowed_url_prefixes:
            if normalized.startswith(prefix):
                return None
        return f"ссылка вне белого списка: {url[:60]}"

    def check(
        self, text: str, extra_numbers: set[str] | None = None
    ) -> list[str]:
        """extra_numbers — допустимые числа этой вакансии (вилка).

        Числа из структурированного поля salary (не свободный текст) легитимны
        в вопросах про вилку: «в поле 300-450К gross, в тексте net — что верно?».
        Числа из description вакансии в whitelist НЕ попадают.
        """
        if not text:
            return []
        allowed = self.allowed_numbers | (extra_numbers or set())
        violations: list[str] = []
        if len(text) > self.max_length:
            violations.append(
                f"объём {len(text)} знаков > {self.max_length}"
            )
        allowed_url_spans: list[tuple[int, int]] = []
        # домен внутри почты (name@mail.ru) — не ссылка: почту
        # проверяет блок email ниже
        email_spans = [(m.start(), m.end()) for m in EMAIL_RE.finditer(text)]
        for match in URL_RE.finditer(text):
            if any(start <= match.start() < end for start, end in email_spans):
                continue
            violation = self._url_violation(match.group(0))
            if violation:
                violations.append(violation)
            else:
                allowed_url_spans.append((match.start(), match.end()))
        if self.forbid_salary and (salary := SALARY_RE.search(text)):
            violations.append(f"зарплата в письме: «{salary.group(0).strip()[:40]}»")
        for match in EMAIL_RE.finditer(text):
            if match.group(0).lower() not in self.allowed_contacts:
                violations.append(f"email: {match.group(0)[:60]}")
        for match in PHONE_RE.finditer(text):
            if re.sub(r"\D", "", match.group(0)) not in self.allowed_phone_digits:
                violations.append(f"телефон: {match.group(0)[:60]}")
        for match in TG_RE.finditer(text):
            fragment = match.group(0)
            if fragment.lower() in self.allowed_contacts:
                continue
            if "@" in fragment and EMAIL_RE.search(fragment) is None:
                violations.append(f"контакт @: {fragment[:60]}")
        for match in NUM_RE.finditer(text):
            token = match.group(0)
            if len(token) < 2 or token in allowed:
                continue
            before = text[match.start() - 1] if match.start() > 0 else ""
            after = text[match.end()] if match.end() < len(text) else ""
            # «300К» — это сумма, а не название: К/k после числа проверяем
            if _IN_WORD_RE.match(before) or (
                _IN_WORD_RE.match(after) and after not in "кКkK"
            ):
                continue
            # числа внутри разрешённых ссылок (github.com/example-user) — ок
            if any(
                start <= match.start() < end for start, end in allowed_url_spans
            ):
                continue
            violations.append(f"число вне капсулы: {token}")
        return violations

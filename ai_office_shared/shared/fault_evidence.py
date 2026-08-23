"""
ai_office_shared.shared.fault_evidence — улика отказа, а не догадка о нём.

ПРОБЛЕМА (инцидент 23.08.2026):
    В офис-группу пришло «⛔ баг в villy-bot — команда за 3 попыток не дала код».
    Симптом, который аудит вписал сам: «Missing traceback content in logs, BUT
    code analysis reveals critical issue: await log(event, msg) called with only
    2 args». Проверка показала: функция объявлена с двумя параметрами, все
    четыре вызова передают два, файл компилируется, pyflakes чист. А в логах
    сервиса — двенадцать строк подряд «getUpdates 200 OK» и ни одного признака
    отказа. Бот был здоров и простаивал.

    Аудит не увидел ошибку — он её ВООБРАЗИЛ, прочитав исходник, и отдал
    выдумку цепочке отдела на три попытки.

ПОЧЕМУ ТАК ВЫШЛО:
    analyze_logs получает логи И ПОЛНЫЙ ИСХОДНИК файла (а с 15.08 к исходнику
    ещё подклеиваются структурные события из Redis). Когда в логах пусто,
    модель делает то, что на её месте делает любой ревьюер: находит, что
    сказать, про код. Единственным гейтом было поле is_bug — суждение той же
    модели о собственной находке.

ПРАВИЛО:
    Баг — это то, что показали ЛОГИ. Чтение кода объясняет уже наблюдавшийся
    отказ, но не заменяет его. Нет улики — нет и работы для отдела, чей прогон
    стоит десятков минут и живых денег.
"""

from __future__ import annotations

import re

# Строки, которые действительно означают отказ. Отличаются от ERROR_PATTERNS в
# coder.py намеренно: те отбирают КАНДИДАТОВ на анализ (и ловят, например, любой
# «Error:» в INFO-строке), а здесь решается, есть ли основание тратить отдел.
FAILURE_SIGNATURES = (
    "traceback (most recent call last)",
    "unhandled exception",
    "exception:",
    "critical",
    "fatal",
    "exit code",
    "exited with code",
    "crashed",
    "crashloop",
    "segmentation fault",
    "sigkill",
    "sigsegv",
    "oomkilled",
    "errno",
    "modulenotfounderror",
    "importerror",
    "syntaxerror",
    "nameerror",
    "attributeerror",
    "typeerror",
    "keyerror",
    "indexerror",
    "valueerror",
    "zerodivisionerror",
)

# Обороты, которыми анализатор САМ признаётся, что улики нет. Если модель это
# написала — верим ей на слово: она сообщает о своём основании, а не о баге.
_NO_EVIDENCE_TELLS = (
    "missing traceback",
    "no traceback",
    "without traceback",
    "traceback content is missing",
    "нет трейсбека",
    "трейсбек отсутствует",
    "no error in logs",
    "logs show no error",
    "нет ошибок в логах",
)


def failure_evidence(lines) -> str:
    """
    Первая строка логов, которая действительно свидетельствует об отказе.
    Пустая строка — улики нет.

    Возвращаем саму строку, а не bool: она уходит в отчёт как основание, и по
    ней видно, ЧТО именно посчитали отказом. «Улика — наблюдённое значение, а
    не слово „проверено“».
    """
    for raw in (lines or []):
        text = str(raw or "")
        low = text.lower()
        if any(sig in low for sig in FAILURE_SIGNATURES):
            return text.strip()[:300]
    return ""


def admits_no_evidence(analysis: dict) -> str:
    """Оборот, которым анализатор сам признал отсутствие улики. '' — не признавал."""
    if not isinstance(analysis, dict):
        return ""
    blob = " ".join(str(analysis.get(k, "")) for k in
                    ("description", "fix_description", "lesson_symptom")).lower()
    for tell in _NO_EVIDENCE_TELLS:
        if tell in blob:
            return tell
    return ""


def dispatch_refusal(analysis: dict, log_lines) -> str:
    """
    Причина, по которой находку НЕЛЬЗЯ отдавать отделу. '' — можно.

    Смотрим не на уверенность модели, а на её основание. Проверка нужна ровно
    потому, что модель видит исходник целиком и на пустых логах начинает
    ревьюить код: это полезно человеку и разорительно для автоматики.
    """
    if not isinstance(analysis, dict) or not analysis.get("is_bug"):
        return ""
    tell = admits_no_evidence(analysis)
    if tell:
        return f"анализатор сам отметил отсутствие улики («{tell}»)"
    if not failure_evidence(log_lines):
        return "в логах нет ни одной строки с признаком отказа"
    return ""


def strip_source_tail(text: str, marker: str = "--- Redis") -> str:
    """Отрезает приклеенный к исходнику хвост логов. Нужен тестам и отчётам."""
    idx = str(text or "").find(marker)
    return text if idx < 0 else text[:idx].rstrip()


def looks_like_arity_claim(text: str) -> bool:
    """
    Правда ли находка — заявление об арности вызова («called with only N args»).

    Такое заявление проверяется компилятором за миллисекунды и потому не имеет
    права уходить в отдел непроверенным: 23.08 именно оно оказалось выдумкой.
    """
    low = str(text or "").lower()
    return bool(re.search(r"(called with only|takes \d+ positional|missing \d+ required|"
                          r"вызвана с|аргумент[а-я]* не хватает)", low))

"""Статическая проверка кода, который выдал воркер.

Зачем: до 31.07.2026 в dev-dept НИЧТО не проверяло результат. Тести «тестировал»
код, который никто не запускал, Секки искал уязвимости в тексте, а «финальным
кодом» пайплайна был ответ Рикки — то есть текст ревью. Замер того же дня: Девви
вернул файл с `undefined name 'web'`, а после починки чтения — файл, который не
компилируется (`unterminated triple-quoted string literal`). Оба дефекта ловятся
за миллисекунды, но ловить их было нечем.

Код здесь НЕ ИСПОЛНЯЕТСЯ. `compile()` разбирает исходник в байт-код и не
запускает его; pyflakes работает по AST. Поэтому проверка безопасна прямо в
процессе воркера и не требует песочницы. Запуск тестов — отдельная задача, и вот
для НЕГО песочница обязательна.

Ровно эти две проверки стоят в CI офиса (`compileall` + `pyflakes`), так что
воркер теперь меряет себя тем же, чем меряют его результат.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("ai_office_shared.verify")

_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)


def _compiles(code: str) -> bool:
    if not code.strip():
        return False
    try:
        compile(code, "<probe>", "exec")
        return True
    except Exception:
        return False


def _outermost(text: str) -> str:
    """Срез от ПЕРВОГО открывающего забора до ПОСЛЕДНЕГО закрывающего.

    Нужен, когда сам файл содержит ```. Такие файлы в офисе реальны:
    devvy-bot/bot.py держит в SYSTEM_PROMPT пример ответа в ```python-блоке.
    """
    m = re.search(r"```(?:python|py)?\s*\n", text)
    if not m:
        return ""
    end = text.rfind("```")
    return text[m.end():end].strip() if end > m.end() else ""


def extract_code(text: str) -> str:
    """Код из ответа воркера. '' — блока нет.

    🔴 Забор ``` НЕ вложенный: если файл сам содержит тройные кавычки, наивный
    нежадный разбор обрежет его на внутреннем заборе и отдаст обрубок, который
    не компилируется. Именно это 01.08.2026 давало «unterminated triple-quoted
    string literal» три попытки подряд, и это легко было списать на слабую
    модель — а виноват транспорт.

    Поэтому берём НЕСКОЛЬКО кандидатов и предпочитаем тот, что компилируется:
    внешний срез (первый забор → последний), затем самый длинный нежадный блок.
    Если не компилируется ни один — отдаём самый длинный, чтобы verify_code
    показал настоящую ошибку, а не молчал.

    Пустая строка НЕ означает «плохо»: у ревьюеров ответ текстовый.
    """
    if not text:
        return ""
    blocks = [b.strip() for b in _FENCE.findall(text) if b.strip()]
    longest = max(blocks, key=len) if blocks else ""
    candidates = [_outermost(text), longest]
    for c in candidates:
        if c and _compiles(c):
            return c
    return longest or (candidates[0] if candidates[0] else "")


def _pyflakes_report(code: str, filename: str = "<worker>") -> list:
    """Замечания pyflakes. Пустой список, если pyflakes недоступен."""
    try:
        from pyflakes.api import check
        from pyflakes.reporter import Reporter
    except Exception:
        logger.debug("pyflakes недоступен — проверка ограничена синтаксисом")
        return []

    import io

    out, err = io.StringIO(), io.StringIO()
    try:
        check(code, filename, Reporter(out, err))
    except Exception as e:                    # pyflakes не должен ронять воркера
        logger.warning("pyflakes упал: %s", e)
        return []
    return [ln for ln in out.getvalue().splitlines() if ln.strip()]


def verify_code(code: str) -> tuple:
    """(ok, отчёт). ok=True — синтаксис валиден и pyflakes молчит.

    Отчёт — человекочитаемый текст ошибок, который отправляется обратно модели:
    воркеру нужна КОНКРЕТНАЯ ошибка со строкой, а не «перепиши получше».
    """
    if not code.strip():
        return True, ""                        # нечего проверять

    try:
        compile(code, "<worker>", "exec")      # разбор, БЕЗ выполнения
    except SyntaxError as e:
        where = f"строка {e.lineno}" if e.lineno else "неизвестно"
        return False, f"SyntaxError ({where}): {e.msg}"
    except ValueError as e:                    # напр. нулевые байты в исходнике
        return False, f"Код не разобрался: {e}"

    issues = _pyflakes_report(code)
    if issues:
        head = "; ".join(i.split(":", 1)[-1].strip() for i in issues[:5])
        return False, f"pyflakes ({len(issues)}): {head}"
    return True, ""


def retry_prompt(report: str, attempt: int, total: int) -> str:
    """Блок, который дописывается к задаче при переделке.

    Намеренно требует ПОЛНЫЙ файл: частичная правка от модели, которая уже
    ошиблась, обычно ломает файл сильнее.
    """
    return (
        f"\n\n[ПРОВЕРКА НЕ ПРОЙДЕНА — попытка {attempt} из {total}]\n"
        f"Твой предыдущий ответ не проходит те же проверки, что стоят в CI офиса:\n"
        f"  {report}\n"
        f"Исправь ИМЕННО это и верни ПОЛНЫЙ файл целиком одним ```python-блоком. "
        f"Не объясняй, не сокращай, не пиши «...» вместо кода."
    )

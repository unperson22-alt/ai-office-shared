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


def extract_code(text: str) -> str:
    """Самый длинный ```python-блок из ответа воркера. '' — блока нет.

    Пустая строка НЕ означает «плохо»: у ревьюеров (Тести, Секки) ответ по
    смыслу текстовый, и проверять там нечего.
    """
    if not text:
        return ""
    blocks = _FENCE.findall(text)
    return max(blocks, key=len).strip() if blocks else ""


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

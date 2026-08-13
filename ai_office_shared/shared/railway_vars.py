"""
ai_office_shared.shared.railway_vars — что Силли можно писать в Railway.

Гвард живёт здесь, а не в agents/coder.py, ровно по двум причинам:
  1. coder.py правится только вручную с ревью Влада (правило CLAUDE — СТАРТ
     после инцидента 2026-07-01) и не покрыт тестами — логика допуска к
     production-переменным не должна жить в непроверяемом месте;
  2. отсюда её видят тесты в CI, а значит список запретов нельзя молча ослабить.
"""
from __future__ import annotations

# Имена, которые агентная петля НЕ имеет права переписывать.
# Секреты заводит и ротирует человек: у модели нет способа проверить, что новое
# значение валидно, а неверный токен кладёт сервис молча и надолго. Совпадение
# ищем по подстроке в UPPERCASE — TELEGRAM_TOKEN, GH_PAT_KEY и REDIS_URL должны
# ловиться одинаково.
SET_VAR_FORBIDDEN: tuple[str, ...] = (
    "TOKEN", "SECRET", "KEY", "PASSWORD", "PASSWD", "CREDENTIAL", "PAT",
    "REDIS_URL", "DATABASE_URL", "DSN", "API_KEY", "WEBHOOK",
)


def set_var_allowed(name: str) -> tuple[bool, str]:
    """
    Можно ли агентной петле писать эту переменную.

    Returns:
        (можно, причина_отказа). При можно=True причина пустая.
    """
    upper = (name or "").strip().upper()
    if not upper:
        return False, "пустое имя переменной"
    for bad in SET_VAR_FORBIDDEN:
        if bad in upper:
            return False, (
                f"'{name}' похоже на секрет ({bad}). Секреты меняет человек "
                f"в Railway UI — модель не может проверить валидность значения."
            )
    return True, ""

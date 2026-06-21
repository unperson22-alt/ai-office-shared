"""
telegram_safe.py — устойчивая отправка в Telegram с ретраями и graceful degradation.

ЗАЧЕМ: транзиентные сетевые ошибки Telegram/Railway (NetworkError: Bad Gateway,
TimedOut, RetryAfter) — это норма на нестабильной сети, а НЕ повод ронять бота.
Раньше боты звали bot.send_message()/message.reply_text() «голым» вызовом, и любой
502 от Telegram валил процесс (урок #8 / #43). Этот модуль оборачивает отправку в
retry с экспоненциальным backoff, уважает RetryAfter.retry_after и после исчерпания
попыток деградирует мягко (возвращает None + структурный лог), не бросая исключение.

ИСПОЛЬЗОВАНИЕ:
    from ai_office_shared.shared.telegram_safe import safe_send, safe_reply, safe_edit

    await safe_send(bot, chat_id, "текст")
    await safe_reply(update.message, "текст")
    await safe_edit(bot, chat_id, message_id, "новый текст")

Или произвольный вызов:
    await with_tg_retry(lambda: bot.send_photo(chat_id, photo), op_name="send_photo")

Модуль импорт-безопасен: если python-telegram-bot не установлен (например, у
aiogram-воркеров), ретраи всё равно работают по именам типов исключений.
"""

import asyncio
import logging
import random

logger = logging.getLogger(__name__)

# ── Структурный лог (мягкая зависимость) ─────────────────────────────────────
try:  # не делаем жёсткой связки — модуль должен импортироваться где угодно
    from ai_office_shared.shared.logging import log_event as _log_event
except Exception:  # pragma: no cover
    _log_event = None

# ── Классы ошибок Telegram ───────────────────────────────────────────────────
# python-telegram-bot (v20+). Bad Gateway приходит как NetworkError.
try:
    from telegram.error import NetworkError, TimedOut, RetryAfter
    # Ошибки, которые ретраить БЕСПОЛЕЗНО (логика/доступ) — пробрасываем сразу.
    try:
        from telegram.error import BadRequest, Forbidden
        _NON_RETRYABLE = (BadRequest, Forbidden)
    except Exception:  # pragma: no cover
        _NON_RETRYABLE = ()
    _TRANSIENT = (NetworkError, TimedOut)
    _HAVE_PTB = True
except Exception:  # pragma: no cover - telegram не установлен
    RetryAfter = None
    _TRANSIENT = ()
    _NON_RETRYABLE = ()
    _HAVE_PTB = False


def _is_transient(exc: Exception) -> bool:
    """Транзиентная сетевая ошибка, которую имеет смысл ретраить."""
    if _NON_RETRYABLE and isinstance(exc, _NON_RETRYABLE):
        return False
    if _TRANSIENT and isinstance(exc, _TRANSIENT):
        return True
    # Фолбэк по имени класса/тексту — на случай иной версии библиотеки.
    name = type(exc).__name__.lower()
    if any(k in name for k in ("network", "timedout", "timeout", "retryafter")):
        return True
    text = str(exc).lower()
    return any(k in text for k in ("bad gateway", "502", "503", "timed out", "connection"))


async def with_tg_retry(
    factory,
    *,
    attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    op_name: str = "tg_call",
    raise_on_fail: bool = False,
):
    """
    Выполнить Telegram-операцию с ретраями.

    factory       — фабрика без аргументов, возвращающая СВЕЖИЙ awaitable на каждую
                    попытку (например, lambda: bot.send_message(chat_id, text)).
                    Важно: передавать именно фабрику, а не уже созданную корутину —
                    корутину нельзя await дважды.
    attempts      — максимум попыток (по умолчанию 5).
    base_delay    — базовая задержка для экспоненциального backoff.
    max_delay     — потолок задержки.
    op_name       — имя операции для логов.
    raise_on_fail — если True, после исчерпания попыток пробросить последнее
                    исключение; если False (по умолчанию) — вернуть None (graceful).

    Возвращает результат вызова или None при мягкой деградации.
    """
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return await factory()
        except Exception as exc:  # noqa: BLE001 — классифицируем ниже
            last_exc = exc

            # RetryAfter — Telegram сам сказал, сколько ждать (flood control).
            retry_after = None
            if RetryAfter is not None and isinstance(exc, RetryAfter):
                retry_after = float(getattr(exc, "retry_after", 0) or 0)
            elif not _is_transient(exc):
                # Не сетевая ошибка — ретраи не помогут.
                logger.warning("%s: non-retryable %s: %s", op_name, type(exc).__name__, exc)
                if raise_on_fail:
                    raise
                return None

            if attempt >= attempts:
                break

            if retry_after is not None:
                delay = min(retry_after + random.uniform(0, 0.5), max_delay)
            else:
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                delay += random.uniform(0, delay * 0.25)  # jitter

            logger.warning(
                "%s: transient %s (попытка %d/%d), retry через %.1fс: %s",
                op_name, type(exc).__name__, attempt, attempts, delay, exc,
            )
            await asyncio.sleep(delay)

    # Попытки исчерпаны — структурный лог + мягкая деградация.
    logger.error("%s: провал после %d попыток: %s", op_name, attempts, last_exc)
    if _log_event is not None:
        try:
            _log_event(
                "telegram_send_failed",
                level="error",
                op=op_name,
                error=str(last_exc),
                error_type=type(last_exc).__name__ if last_exc else "unknown",
                attempts=attempts,
            )
        except Exception:  # лог не должен ломать вызывающего
            pass
    if raise_on_fail and last_exc is not None:
        raise last_exc
    return None


async def safe_send(bot, chat_id, text, *, op_name: str = "send_message", **kwargs):
    """bot.send_message(...) с ретраями. Возвращает Message или None."""
    return await with_tg_retry(
        lambda: bot.send_message(chat_id=chat_id, text=text, **kwargs),
        op_name=op_name,
    )


async def safe_reply(message, text, *, op_name: str = "reply_text", **kwargs):
    """message.reply_text(...) с ретраями. Возвращает Message или None."""
    return await with_tg_retry(
        lambda: message.reply_text(text, **kwargs),
        op_name=op_name,
    )


async def safe_edit(bot, chat_id, message_id, text, *, op_name: str = "edit_message_text", **kwargs):
    """bot.edit_message_text(...) с ретраями. Возвращает результат или None."""
    return await with_tg_retry(
        lambda: bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, **kwargs),
        op_name=op_name,
    )

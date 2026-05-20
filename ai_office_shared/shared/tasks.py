"""
ai_office_shared.shared.tasks — фоновые задачи общие для всех ботов.

auto_extract_interests — Haiku извлекает факты из сообщения пользователя.
weekly_review         — Haiku компактизирует профиль раз в неделю.
weekly_review_loop    — asyncio loop для запуска review.

Зависимости передаются явно: redis_client, bot_name, anthropic_client.

ИСПОЛЬЗОВАНИЕ:
    from ai_office_shared.shared.tasks import (
        auto_extract_interests,
        weekly_review,
        weekly_review_loop,
    )

    # В process():
    asyncio.create_task(
        auto_extract_interests(redis_client, "билли", user_id, message, anthropic_client)
    )

    # В main():
    asyncio.create_task(weekly_review_loop(redis_client, "билли", anthropic_client))
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from typing import Any

from .redis_helpers import (
    redis_get_notes,
    redis_add_note,
    redis_set_notes,
    redis_get_history,
    redis_get_all_user_ids,
)

logger = logging.getLogger("ai_office_shared.tasks")

HAIKU_MODEL = "claude-haiku-4-5-20251001"
WEEK_SECONDS = 7 * 24 * 3600


async def auto_extract_interests(
    redis_client,
    bot_name: str,
    user_id: int,
    message: str,
    anthropic_client,
) -> None:
    """
    Фоновое авто-извлечение фактов о пользователе через Haiku.
    Fail-silent — никогда не бросает исключение.

    Записывает строку вида "[auto] факт" в notes пользователя.
    """
    try:
        existing = await redis_get_notes(redis_client, bot_name, user_id)
        r = anthropic_client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=150,
            system=(
                "Ты извлекаешь факты о пользователе из его сообщений. "
                "Найди конкретные факты, интересы, предпочтения или важные детали О ПОЛЬЗОВАТЕЛЕ. "
                "Если нашёл что-то новое и конкретное — верни одну строку начинающуюся с [auto]. "
                "Если ничего конкретного нет или это уже есть в заметках — верни пустую строку. "
                "Не записывай временные состояния (устал, болит голова сегодня). "
                "Только устойчивые факты: работа, семья, питание, хобби, предпочтения."
            ),
            messages=[{"role": "user", "content":
                f"Сообщение: {message}\n\nУже известно:\n{existing or '(ничего)'}"}],
        )
        fact = r.content[0].text.strip()
        if fact.startswith("[auto]"):
            await redis_add_note(redis_client, bot_name, user_id, fact)
            logger.info("Auto-extracted for %s/%s: %s", bot_name, user_id, fact)
    except Exception as e:
        logger.warning("auto_extract_interests(%s, %s) failed: %s", bot_name, user_id, e)


async def weekly_review(
    redis_client,
    bot_name: str,
    user_id: int,
    anthropic_client,
) -> None:
    """
    Еженедельная компактизация профиля пользователя через Haiku.
    Читает историю + старые заметки → создаёт компактный профиль.
    """
    try:
        history = await redis_get_history(redis_client, bot_name, user_id)
        notes = await redis_get_notes(redis_client, bot_name, user_id)
        if not history and not notes:
            return

        history_text = "\n".join(
            f"{m['role']}: {m['content'][:200]}" for m in history[-30:]
        )
        r = anthropic_client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=400,
            system=(
                "Ты обновляешь профиль пользователя на основе переписки и старых заметок. "
                "Создай компактный список фактов о пользователе (не более 10 строк). "
                "Каждый факт с новой строки, начинается с [auto]. "
                "Убери дубли, объедини похожее, удали устаревшее. "
                "Только конкретные устойчивые факты."
            ),
            messages=[{"role": "user", "content":
                f"Старые заметки:\n{notes or '(нет)'}\n\n"
                f"Последние сообщения:\n{history_text}"}],
        )
        new_profile = r.content[0].text.strip()
        await redis_set_notes(redis_client, bot_name, user_id, new_profile)
        logger.info("Weekly review done for %s/%s", bot_name, user_id)
    except Exception as e:
        logger.warning("weekly_review(%s, %s) failed: %s", bot_name, user_id, e)


async def weekly_review_loop(
    redis_client,
    bot_name: str,
    anthropic_client,
    interval_check_sec: int = 3600,
) -> None:
    """
    asyncio loop — раз в неделю запускает weekly_review для всех пользователей бота.
    Запускается через asyncio.create_task() при старте бота.

    interval_check_sec — как часто проверяем не пришла ли неделя (default 1 час).
    """
    from .identity import display as _display
    last_key = f"weekly_review:last_run:{_display(bot_name) or bot_name}"

    while True:
        try:
            last_raw = await redis_client.get(last_key)
            now = int(_time.time())
            last = int(last_raw.decode()) if last_raw else 0

            if (now - last) > WEEK_SECONDS:
                await redis_client.set(last_key, str(now))
                user_ids = await redis_get_all_user_ids(redis_client, bot_name)
                logger.info("Weekly review: starting for %d users (%s)", len(user_ids), bot_name)
                for uid in user_ids:
                    await weekly_review(redis_client, bot_name, uid, anthropic_client)
                logger.info("Weekly review: done (%s)", bot_name)
        except Exception as e:
            logger.warning("weekly_review_loop(%s) error: %s", bot_name, e)

        await asyncio.sleep(interval_check_sec)

"""
ai_office_shared.shared.redis_helpers — общие Redis-операции для всех ботов.

Раньше каждый бот копировал redis_get_history / redis_save_history / redis_get_notes
/ redis_add_note. Здесь единая реализация.

Зависимости приходят явно — никаких глобалов.
redis_client и bot_name передаются в каждую функцию.
bot_name — canonical lowercase ("билли", "крисс", ...).

ИСТОРИЯ КЛЮЧЕЙ:
    Старый формат (Display-имя):  history:Билли:123
    Новый формат (canonical):     history:билли:123   ← планируем, пока не мигрировали

    Для обратной совместимости get/save принимают key_name=None и
    берут Display из identity.display(bot_name). Когда все боты переедут
    на новый формат — уберём этот параметр.

ИСПОЛЬЗОВАНИЕ:
    from ai_office_shared.shared.redis_helpers import (
        redis_get_history, redis_save_history,
        redis_get_notes, redis_add_note,
    )

    # вместо:
    #   raw = await redis_client.get(f"history:{BOT_NAME}:{user_id}")
    # пишем:
    #   history = await redis_get_history(redis_client, "билли", user_id)
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from .identity import display as _display

logger = logging.getLogger("ai_office_shared.redis_helpers")

HISTORY_TTL = 86400 * 30   # 30 дней


def _history_key(bot_name: str, user_id: int) -> str:
    """history:БотDisplay:user_id  (обратная совместимость со старым форматом)"""
    disp = _display(bot_name) or bot_name
    return f"history:{disp}:{user_id}"


def _notes_key(bot_name: str, user_id: int) -> str:
    """notes:БотDisplay:user_id"""
    disp = _display(bot_name) or bot_name
    return f"notes:{disp}:{user_id}"


async def redis_get_history(redis_client, bot_name: str, user_id: int) -> list:
    """
    Читает историю переписки из Redis.
    Возвращает список dict (формат Anthropic messages).
    При ошибке → пустой список, не бросает.
    """
    try:
        raw = await redis_client.get(_history_key(bot_name, user_id))
        return json.loads(raw) if raw else []
    except Exception as e:
        logger.warning("redis_get_history(%s, %s) failed: %s", bot_name, user_id, e)
        return []


async def redis_save_history(redis_client, bot_name: str, user_id: int, history: list) -> None:
    """
    Сохраняет историю переписки в Redis с TTL 30 дней.
    При ошибке → тихий warning.
    """
    try:
        await redis_client.setex(
            _history_key(bot_name, user_id),
            HISTORY_TTL,
            json.dumps(history, ensure_ascii=False),
        )
    except Exception as e:
        logger.warning("redis_save_history(%s, %s) failed: %s", bot_name, user_id, e)


async def redis_get_notes(redis_client, bot_name: str, user_id: int) -> str:
    """
    Читает заметки о пользователе. Возвращает строку или "".
    """
    try:
        raw = await redis_client.get(_notes_key(bot_name, user_id))
        if raw is None:
            return ""
        return raw.decode() if isinstance(raw, bytes) else raw
    except Exception as e:
        logger.warning("redis_get_notes(%s, %s) failed: %s", bot_name, user_id, e)
        return ""


async def redis_add_note(redis_client, bot_name: str, user_id: int, note: str) -> None:
    """
    Добавляет строку к заметкам пользователя.
    """
    try:
        existing = await redis_get_notes(redis_client, bot_name, user_id)
        updated = (existing + "\n" + note).strip()
        await redis_client.set(_notes_key(bot_name, user_id), updated)
    except Exception as e:
        logger.warning("redis_add_note(%s, %s) failed: %s", bot_name, user_id, e)


async def redis_set_notes(redis_client, bot_name: str, user_id: int, notes: str) -> None:
    """
    Перезаписывает заметки целиком (используется при weekly review).
    """
    try:
        await redis_client.set(_notes_key(bot_name, user_id), notes)
    except Exception as e:
        logger.warning("redis_set_notes(%s, %s) failed: %s", bot_name, user_id, e)


async def redis_get_all_user_ids(redis_client, bot_name: str) -> list[int]:
    """
    Возвращает список всех user_id у которых есть история для данного бота.
    Используется weekly_review_loop.
    """
    try:
        disp = _display(bot_name) or bot_name
        pattern = f"history:{disp}:*"
        keys = await redis_client.keys(pattern)
        user_ids = []
        for key in keys:
            suffix = (key.decode() if isinstance(key, bytes) else key).split(":")[-1]
            if suffix.isdigit():
                user_ids.append(int(suffix))
        return user_ids
    except Exception as e:
        logger.warning("redis_get_all_user_ids(%s) failed: %s", bot_name, e)
        return []

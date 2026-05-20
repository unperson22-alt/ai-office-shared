"""
ai_office_shared.shared.quality — единый владелец контракта обратной связи (feedback loop).

ПРОБЛЕМА, которую закрывает этот модуль
---------------------------------------
Реакции 👍/👎 раньше реализовывались дважды и расходились:

    Живой код (Силли, Билли):   HASH office:quality:{bot}, поля  up / down
                                владелец сообщения  office:msg:{chat}:{msg} = bot
    ptb-reactions/SKILL.md:     HASH office:quality:{bot}, поля  quality_up / quality_down
                                владелец  office:my_messages:{bot}:{chat}  (LIST)

Консьюмер (Филли /metrics, Силли /weekly-report) читал одно, бот-по-скиллу писал
другое → голоса терялись молча. Контракт ни за кем не был закреплён: identity.py
владеет ключом, но не полями хэша и не схемой владельца.

КАНОН (этот файл — источник истины)
------------------------------------
    Ключ качества:   office:quality:{canon}          (через identity.redis_key)
    Поля хэша:       "up"  /  "down"                  (живые данные уже такие)
    Владелец msg:    office:msg:{chat_id}:{msg_id} = canonical bot, TTL 7д

Все боты импортируют отсюда. SKILL.md обновляется под этот канон.
Никаких локальных копий и compensation-слоёв.

КОНТРАКТ ОШИБОК
---------------
Как и logging/redis_helpers — ничего не бросаем наружу. Падение Redis = warning,
бот продолжает работать. Feedback loop не должен ронять ответы.

ИСПОЛЬЗОВАНИЕ (бот на PTB)
-------------------------
    from ai_office_shared.shared.quality import (
        REACTION_UP, REACTION_DOWN,
        remember_my_message, reaction_owner,
        classify_reaction, record_reaction, get_quality,
    )

    # после каждого ответа:
    sent = await update.message.reply_text(text)
    await remember_my_message(redis, "билли", sent.chat_id, sent.message_id)

    # в MessageReactionHandler:
    owner = await reaction_owner(redis, chat_id, msg_id)
    if owner != "билли":
        return
    du, dd = classify_reaction(reaction.old_reaction, reaction.new_reaction)
    await record_reaction(redis, "билли", du, dd, user_id=user_id)

ИСПОЛЬЗОВАНИЕ (консьюмер — Филли /metrics, Силли /weekly-report)
---------------------------------------------------------------
    stats = await get_quality(redis, "билли")          # {"up": 12, "down": 2}
    alls  = await get_all_quality(redis, bots_list)    # {"билли": {...}, ...}
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from .identity import canonical as _canonical, redis_key as _redis_key
from .logging import log_event

logger = logging.getLogger("ai_office_shared.quality")

# ── Единый словарь эмодзи. Один источник — больше не дублируется по ботам. ──
REACTION_UP = frozenset({"👍", "❤️", "🔥", "🥰", "👏", "🎉", "🤩", "🙏", "💯"})
REACTION_DOWN = frozenset({"👎", "💩", "🤬", "🤮", "😢", "🤡"})

MSG_OWNER_TTL = 86400 * 7        # 7 дней — столько живёт право реакции
_FIELD_UP = "up"
_FIELD_DOWN = "down"


def _owner_key(chat_id: int, msg_id: int) -> str:
    """office:msg:{chat}:{msg} — кто из ботов автор этого сообщения."""
    return f"office:msg:{chat_id}:{msg_id}"


def _quality_key(bot_name: str) -> Optional[str]:
    """office:quality:{canon} через единый identity-владелец ключа."""
    return _redis_key(bot_name, "quality")


# ────────────────────────── ЗАПИСЬ (со стороны бота) ──────────────────────────

async def remember_my_message(redis_client, bot_name: str, chat_id: int, msg_id: int) -> None:
    """
    Запоминает, что сообщение (chat, msg) принадлежит боту bot_name.
    Вызывать после КАЖДОГО reply_text / send_message.
    Без этого реакция не будет атрибутирована и голос пропадёт.
    """
    canon = _canonical(bot_name)
    if canon is None:
        logger.warning("remember_my_message: unknown bot %r", bot_name)
        return
    try:
        await redis_client.setex(_owner_key(chat_id, msg_id), MSG_OWNER_TTL, canon)
    except Exception as e:
        logger.warning("remember_my_message(%s, %s:%s) failed: %s", canon, chat_id, msg_id, e)


async def reaction_owner(redis_client, chat_id: int, msg_id: int) -> Optional[str]:
    """
    Возвращает canonical-имя бота-автора сообщения, или None.
    None → реакция не на наше сообщение (или маппинг истёк) → игнорировать.
    """
    try:
        raw = await redis_client.get(_owner_key(chat_id, msg_id))
    except Exception as e:
        logger.warning("reaction_owner(%s:%s) lookup failed: %s", chat_id, msg_id, e)
        return None
    if raw is None:
        return None
    val = raw.decode() if isinstance(raw, bytes) else raw
    return _canonical(val) or val


def classify_reaction(old_reaction, new_reaction) -> tuple[int, int]:
    """
    Чистая функция. Считает дельту голосов между старым и новым набором реакций.
    Принимает списки telegram ReactionType (у которых есть .emoji) либо None.
    Возвращает (delta_up, delta_down) — могут быть отрицательными (реакцию убрали).
    """
    def emojis(rs) -> set[str]:
        return {getattr(r, "emoji", None) for r in (rs or [])} - {None}

    old, new = emojis(old_reaction), emojis(new_reaction)
    added, removed = new - old, old - new
    du = sum(e in REACTION_UP for e in added) - sum(e in REACTION_UP for e in removed)
    dd = sum(e in REACTION_DOWN for e in added) - sum(e in REACTION_DOWN for e in removed)
    return du, dd


async def record_reaction(
    redis_client,
    bot_name: str,
    delta_up: int,
    delta_down: int,
    *,
    user_id: Optional[int] = None,
    chat_id: Optional[int] = None,
) -> None:
    """
    Применяет дельту к office:quality:{bot} (HINCRBY up/down) и пишет структурный лог.
    No-op если обе дельты нулевые.
    """
    if not delta_up and not delta_down:
        return
    canon = _canonical(bot_name)
    key = _quality_key(canon or bot_name)
    if key is None:
        logger.warning("record_reaction: unknown bot %r", bot_name)
        return
    try:
        if delta_up:
            await redis_client.hincrby(key, _FIELD_UP, delta_up)
        if delta_down:
            await redis_client.hincrby(key, _FIELD_DOWN, delta_down)
    except Exception as e:
        logger.warning("record_reaction(%s) hincrby failed: %s", canon, e)
        return
    # Лог не должен ронять запись голоса — отдельный try внутри log_event уже есть.
    await log_event(
        redis_client, canon, "reaction_received",
        user_id=user_id, chat_id=chat_id,
        delta_up=delta_up, delta_down=delta_down,
    )


# ────────────────────────── ЧТЕНИЕ (со стороны консьюмера) ─────────────────────

async def get_quality(redis_client, bot_name: str) -> dict[str, int]:
    """
    Возвращает {"up": int, "down": int} для бота. Пустой/ошибка → нули.
    Никогда не висит на сканах: одно HGETALL по известному ключу.
    """
    key = _quality_key(bot_name)
    out = {"up": 0, "down": 0}
    if key is None:
        return out
    try:
        raw = await redis_client.hgetall(key)
    except Exception as e:
        logger.warning("get_quality(%s) failed: %s", bot_name, e)
        return out
    for k, v in (raw or {}).items():
        k = k.decode() if isinstance(k, bytes) else k
        v = v.decode() if isinstance(v, bytes) else v
        if k in (_FIELD_UP, _FIELD_DOWN):
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                pass
    return out


async def get_all_quality(redis_client, bot_names: Iterable[str]) -> dict[str, dict[str, int]]:
    """
    {bot: {"up":..,"down":..}} по явному списку ботов.
    Берём СПИСОК, а не KEYS-скан — поэтому /metrics не блокируется (это и чинит http=000).
    """
    result: dict[str, dict[str, int]] = {}
    for name in bot_names:
        canon = _canonical(name) or name
        result[canon] = await get_quality(redis_client, canon)
    return result

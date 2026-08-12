"""
ai_office_shared.shared.group_history — общая лента офис-группы.

ПРОБЛЕМА, которую решаем (инцидент 2026-08-07):
    Ленту `office:group:history` вела только Филли и писала в неё исключительно
    сообщения человека. Ответы ботов уходили в Telegram через их собственный
    `send_to_group()` — мимо ленты. В итоге `group_ctx`, который Филли раздаёт
    агентам, выглядел как монолог одного человека:

        Yodka: @gosling_friend_bot слушай вот влад хотя бы благодарит
        Yodka: Поблагодари меня за то что я вас вообще создал

    Бот физически не мог узнать, что между этими строками отвечали Гослинг и
    Билли. Отсюда и «боты не понимают кто что кому сказал», и невозможность
    перекинуться репликой: болталке нечего продолжать.

РЕШЕНИЕ:
    Один модуль, который зовут ВСЕ — и Филли на входящем сообщении, и каждый бот
    на своей отправке в группу. Формат ключей и записей ровно тот, что был у
    Филли, чтобы уже накопленная история пережила выкат.

Использование:
    from ai_office_shared.shared.group_history import push, get_context

    await push(redis, "Билли", "Йодка, спасибо конечно, но...")
    ctx = await get_context(redis, n=10)
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("ai_office_shared.group_history")

HISTORY_KEY   = "office:group:history"
CTX_KEY       = "office:group:ctx"        # сжатая сводка (пишет Филли)
MEMBERS_KEY   = "office:members"          # профиль команды (пишет Филли)
HISTORY_MAX   = 20
RECENT_KEEP   = 5                         # сырых сообщений после сводки
HISTORY_TTL   = 86400 * 7


async def push(redis_client, sender_name: str, text: str) -> bool:
    """
    Дописать реплику в общую ленту. Fail-silent: лента — удобство, а не критичный
    путь, ронять из-за неё отправку сообщения нельзя.

    Args:
        sender_name: display-имя автора — «Билли», «Гослинг», «Влад».
                     Именно оно потом попадёт в group_ctx как «Билли: ...».
    """
    if redis_client is None or not sender_name or not text:
        return False
    try:
        entry = json.dumps({"from": sender_name, "text": text[:300]},
                           ensure_ascii=False)
        await redis_client.lpush(HISTORY_KEY, entry)
        await redis_client.ltrim(HISTORY_KEY, 0, HISTORY_MAX - 1)
        await redis_client.expire(HISTORY_KEY, HISTORY_TTL)
        return True
    except Exception as e:
        logger.warning(f"group_history.push failed: {e}")
        return False


def _decode(raw) -> str:
    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw


async def read(redis_client, n: int = HISTORY_MAX) -> list[dict]:
    """Последние n записей в хронологическом порядке (старые → новые)."""
    if redis_client is None:
        return []
    try:
        raw = await redis_client.lrange(HISTORY_KEY, 0, n - 1)
        return [json.loads(_decode(item)) for item in reversed(raw)] if raw else []
    except Exception as e:
        logger.warning(f"group_history.read failed: {e}")
        return []


async def get_context(redis_client, n: int = 10, include_members: bool = True) -> str:
    """
    Готовый текст группового контекста для промта: профиль команды + сводка +
    последние сырые сообщения. Поведение совпадает с прежним
    `group_history_get()` Филли — она была единственным читателем, теперь читать
    может любой бот.
    """
    if redis_client is None:
        return ""
    try:
        summary = await redis_client.get(CTX_KEY)
        recent  = await read(redis_client, RECENT_KEEP)
        recent_text = "\n".join(f"{m['from']}: {m['text']}" for m in recent)

        members = ""
        if include_members:
            try:
                raw_members = await redis_client.get(MEMBERS_KEY)
                members = _decode(raw_members) if raw_members else ""
            except Exception:
                members = ""

        if summary:
            parts = []
            if members:
                parts.append(f"[Профиль команды]:\n{members}")
            parts.append(f"[Сводка беседы]: {_decode(summary)}")
            if recent_text:
                parts.append(f"[Последние сообщения]:\n{recent_text}")
            return "\n".join(parts)

        if recent_text:
            msgs_all = await read(redis_client, n)
            ctx = "\n".join(f"{m['from']}: {m['text']}" for m in msgs_all)
            return f"[Профиль команды]:\n{members}\n\n{ctx}" if members else ctx
        return ""
    except Exception as e:
        logger.warning(f"group_history.get_context failed: {e}")
        return ""

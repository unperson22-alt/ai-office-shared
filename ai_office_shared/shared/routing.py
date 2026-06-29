"""
ai_office_shared.shared.routing — утилиты для межботового роутинга через Филли.

Используется всеми ботами которые:
1. Форвардят входящие DM на Филли (forward_to_filly)
2. Принимают /reply от Филли и отправляют пользователю (handle_reply_endpoint)
3. Возвращают JSON вместо bot.send_message при роутированных запросах (is_routed_request)

АРХИТЕКТУРА:
  Пользователь → Бот-приёмник → Филли → Глава отдела → Филли → /reply → Бот-приёмник → Пользователь
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx
from aiohttp import web
from telegram import Bot

logger = logging.getLogger("ai_office_shared.routing")

FILLY_URL = os.environ.get("FILLY_URL", "").rstrip("/")


# ── 1. Форвард входящего сообщения на Филли ───────────────────────────────────

async def forward_to_filly(
    message: str,
    user_id: int,
    reply_bot: str,
    reply_chat_id: int,
    group_ctx: str = "",
    timeout: float = 30.0,
) -> bool:
    """
    Форвардит сообщение пользователя на Филли.

    Args:
        message:       текст сообщения
        user_id:       Telegram user ID
        reply_bot:     имя бота-источника (БИЛЛИ / РЭЙ / etc.) — куда вернуть ответ
        reply_chat_id: chat_id куда Филли должен прислать /reply
        group_ctx:     контекст групповой беседы (опционально)

    Returns:
        True если Филли принял запрос, False если недоступен
    """
    if not FILLY_URL:
        logger.warning("forward_to_filly: FILLY_URL not set")
        return False

    payload = {
        "message":       message,
        "user_id":       user_id,
        "group_ctx":     group_ctx,
        "reply_bot":     reply_bot,
        "reply_chat_id": reply_chat_id,
    }
    try:
        from .auth import office_headers
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(f"{FILLY_URL}/task", json=payload, headers=office_headers())
            if r.status_code == 200:
                logger.info(f"forward_to_filly: ok → filly routed for user={user_id}")
                return True
            logger.warning(f"forward_to_filly: filly returned {r.status_code}")
            return False
    except Exception as e:
        logger.error(f"forward_to_filly: failed: {e}")
        return False


# ── 2. /reply endpoint — принимает ответ от Филли и отправляет юзеру ─────────

def make_reply_handler(bot: Bot, bot_name: str):
    """
    Фабрика: возвращает aiohttp handler для POST /reply.

    Использование в main():
        app_http.router.add_post("/reply", make_reply_handler(bot, BOT_NAME))

    Ожидаемый JSON от Филли:
        { "chat_id": 123456, "text": "...", "from_agent": "ЛЕКС" }
    """
    async def handle_reply(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)

        chat_id    = data.get("chat_id")
        text       = data.get("text", "")
        from_agent = data.get("from_agent", "")

        if not chat_id or not text:
            return web.json_response({"error": "chat_id and text required"}, status=400)

        try:
            prefix = f"[{from_agent}] " if from_agent else ""
            await bot.send_message(chat_id=int(chat_id), text=prefix + text)
            logger.info(f"[{bot_name}] /reply → chat_id={chat_id} from={from_agent}")
            return web.json_response({"status": "ok"})
        except Exception as e:
            logger.error(f"[{bot_name}] /reply send failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    return handle_reply


# ── 3. Проверка — запрос роутированный (от Филли) или прямой (от юзера) ──────

def is_routed(task_data: dict) -> bool:
    """
    Возвращает True если /task вызван Филли (не напрямую пользователем).
    В этом случае бот должен вернуть JSON {"response": "..."}
    вместо самостоятельной отправки в Telegram.
    """
    return task_data.get("source", "").upper() in ("ФИЛЛИ", "FILLY", "DISPATCHER")

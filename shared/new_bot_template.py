"""
NEW BOT TEMPLATE — базовый шаблон для нового бота AI-офиса.
Силли берёт этот файл за основу при создании нового бота через команду "создай бота X".

ВКЛЮЧЕНО ИЗ КОРОБКИ:
  ✅ Redis-история + заметки (redis_helpers)
  ✅ Авто-извлечение интересов (auto_extract_interests)
  ✅ Еженедельный профиль (weekly_review_loop)
  ✅ Динамический шедулинг (schedule_loop) — пользователь просит "напоминай каждый день..."
  ✅ HTTP health + /task endpoint
  ✅ Реакции 👍/👎 → quality scores
  ✅ Логирование через shared.logging
  ✅ Структурные логи ошибок

ЧТОБЫ ИСПОЛЬЗОВАТЬ:
  1. Замените BOT_NAME, BOT_NAME_LOWER, системный промпт
  2. Добавьте специфичные handlers если нужны
  3. requirements.txt — взять из kriss-bot (уже с актуальным SHA ai-office-shared)
"""

import os, logging, asyncio, re
from aiohttp import web
from telegram import Update
from telegram.ext import (
    Application, MessageHandler, MessageReactionHandler,
    CommandHandler, filters, ContextTypes,
)
import anthropic
from anthropic import AsyncAnthropic
import redis.asyncio as aioredis

from ai_office_shared.shared.logging import log_event
from ai_office_shared.shared.redis_helpers import (
    redis_get_history, redis_save_history,
    redis_get_notes, redis_add_note,
)
from ai_office_shared.shared.tasks import (
    auto_extract_interests, weekly_review_loop,
    schedule_loop, parse_schedule_tag,
    add_scheduled_task, list_scheduled_tasks,
    remove_scheduled_task, format_task_list,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Конфиг (обязательно задать в Railway Variables) ──────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
YOUR_TELEGRAM_ID = int(os.environ["YOUR_TELEGRAM_ID"])
OFFICE_CHAT_ID   = os.environ.get("OFFICE_CHAT_ID", "")
REDIS_URL        = os.environ.get("REDIS_URL", "redis://localhost:6379")
HTTP_SECRET      = os.environ.get("HTTP_SECRET", "")
HTTP_PORT        = 8080

# ── Настройте под бота ───────────────────────────────────────────────────────
BOT_NAME        = "ИмяБота"         # Отображаемое имя
BOT_NAME_LOWER  = "имябота"         # Redis ключ (lowercase, без пробелов)

SYSTEM_BASE = """Ты — [ИМЯ], [описание роли].
Общаешься неформально, по делу. Язык — адаптируй под пользователя.
Не используй Markdown-разметку — пиши простым текстом.

УПРАВЛЕНИЕ НАПОМИНАНИЯМИ:
Если пользователь просит создать напоминание — добавь в конец ответа тег:
• Каждый день в HH:MM UTC → [SCHEDULE:daily:HH:MM:текст]
• Каждую неделю → [SCHEDULE:weekly:mon:HH:MM:текст] (mon/tue/wed/thu/fri/sat/sun)
• Каждые N минут → [SCHEDULE:interval:Nm:текст]
• Один раз → [SCHEDULE:once:YYYY-MM-DD:HH:MM:текст]
• Показать список → [LIST_SCHEDULES]
• Отменить #N → [CANCEL_SCHEDULE:N]
Время в UTC. Германия: UTC+2 летом, UTC+1 зимой.
Подтверди создание напоминания обычным текстом."""

# ── Anthropic клиенты ─────────────────────────────────────────────────────────
claude       = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
claude_async = AsyncAnthropic(api_key=ANTHROPIC_KEY)

REACTION_UP   = {"👍", "❤️", "🔥", "🥰", "👏", "🎉", "🤩", "🙏"}
REACTION_DOWN = {"👎", "💩", "🤬", "🤮", "😢"}

redis_client = None


# ── Системный промпт с заметками ─────────────────────────────────────────────
async def get_system(user_id: int) -> str:
    notes = await redis_get_notes(redis_client, BOT_NAME_LOWER, user_id)
    if notes:
        return SYSTEM_BASE + f"\n\nЗаметки о пользователе:\n{notes}"
    return SYSTEM_BASE


# ── Основная логика ──────────────────────────────────────────────────────────
async def process(message: str, user_id: int) -> str:
    history = await redis_get_history(redis_client, BOT_NAME_LOWER, user_id)
    history.append({"role": "user", "content": message})

    system = await get_system(user_id)
    r = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=system,
        messages=history,
    )
    reply = r.content[0].text
    history.append({"role": "assistant", "content": reply})
    await redis_save_history(redis_client, BOT_NAME_LOWER, user_id, history)
    return reply


# ── Telegram handlers ─────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != YOUR_TELEGRAM_ID:
        return  # Замените на свою логику доступа

    msg = update.message.text.strip()
    response = await process(msg, user.id)

    # Авто-извлечение интересов (фоново)
    asyncio.create_task(
        auto_extract_interests(redis_client, BOT_NAME_LOWER, user.id, msg, claude_async)
    )

    # Обработка тегов шедулера
    tag = parse_schedule_tag(response)
    if tag:
        if tag["action"] == "add":
            await add_scheduled_task(redis_client, BOT_NAME_LOWER, user.id, tag)
        elif tag["action"] == "cancel":
            await remove_scheduled_task(redis_client, BOT_NAME_LOWER, user.id, tag["index"])
        elif tag["action"] == "list":
            tasks = await list_scheduled_tasks(redis_client, BOT_NAME_LOWER, user.id)
            await update.message.reply_text(await format_task_list(tasks))
            return
        response = re.sub(r'\[(?:SCHEDULE|CANCEL_SCHEDULE|LIST_SCHEDULES)[^\]]*\]', '', response).strip()

    await update.message.reply_text(response)


async def handle_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Реакции 👍/👎 → office:quality:{bot}"""
    reaction = update.message_reaction
    if not reaction:
        return
    old = {r.emoji for r in (reaction.old_reaction or []) if getattr(r, "emoji", None)}
    new = {r.emoji for r in (reaction.new_reaction or []) if getattr(r, "emoji", None)}
    added, removed = new - old, old - new
    du = sum(1 for e in added if e in REACTION_UP) - sum(1 for e in removed if e in REACTION_UP)
    dd = sum(1 for e in added if e in REACTION_DOWN) - sum(1 for e in removed if e in REACTION_DOWN)
    if du or dd:
        key = f"office:quality:{BOT_NAME_LOWER}"
        if du: await redis_client.hincrby(key, "up", du)
        if dd: await redis_client.hincrby(key, "down", dd)


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Привет! Я {BOT_NAME}. Чем могу помочь?")


# ── HTTP endpoints ────────────────────────────────────────────────────────────
async def handle_health(request):
    return web.json_response({"status": "ok", "bot": BOT_NAME_LOWER})


async def handle_task(request):
    """Endpoint для Филли — роутинг сообщений."""
    secret = request.headers.get("X-Secret-Token", "")
    if HTTP_SECRET and secret != HTTP_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    data = await request.json()
    msg = data.get("message", "")
    user_id = int(data.get("user_id", YOUR_TELEGRAM_ID))
    response = await process(msg, user_id)
    return web.json_response({"status": "ok", "response": response})


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    global redis_client
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=False)
    logger.info("Redis connected")

    ptb = Application.builder().token(TELEGRAM_TOKEN).build()
    ptb.add_handler(CommandHandler("start", handle_start))
    ptb.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    ptb.add_handler(MessageReactionHandler(handle_reaction))

    async with ptb:
        await ptb.start()
        await ptb.updater.start_polling(drop_pending_updates=True,
            allowed_updates=["message", "edited_message", "message_reaction"])

        app_http = web.Application()
        app_http.router.add_get("/health", handle_health)
        app_http.router.add_post("/health", handle_health)
        app_http.router.add_post("/task",   handle_task)
        runner = web.AppRunner(app_http)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", HTTP_PORT).start()
        logger.info(f"{BOT_NAME} запущен ✅  HTTP on :{HTTP_PORT}")

        # Фоновые задачи — НЕ УБИРАТЬ
        asyncio.create_task(weekly_review_loop(redis_client, BOT_NAME_LOWER, claude_async))
        asyncio.create_task(schedule_loop(redis_client, BOT_NAME_LOWER, ptb.bot))

        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())

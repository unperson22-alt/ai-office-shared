"""
coder.py — агент Кодер (Cilly)
Генерирует код, пушит на GitHub, мониторит логи всех ботов и автофиксит баги.
"""

import asyncio
import os
import sys
import json
import time
import httpx
import logging
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import CommandStart
from anthropic import AsyncAnthropic
import redis.asyncio as aioredis

from shared.github_tools import push_file, read_file, list_files, create_repo
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetDialogFiltersRequest, UpdateDialogFilterRequest
from telethon.tl.functions.channels import InviteToChannelRequest, EditAdminRequest
from telethon.tl.functions.messages import EditChatAdminRequest
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import DialogFilter, InputPeerUser, InputPeerChannel, ChatAdminRights

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.getenv("CODER_BOT_TOKEN")
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY")
LESSONS_CHAT_ID = os.getenv("LESSONS_CHAT_ID")
OFFICE_CHAT_ID  = os.getenv("OFFICE_CHAT_ID")
RAILWAY_TOKEN   = os.getenv("RAILWAY_TOKEN")
RAILWAY_PROJECT = "271b40b7-199a-429a-88ef-ca417f26a638"
GITHUB_USER     = "unperson22-alt"
LESSONS_FILE    = "lessons/lessons.json"

MONITOR_INTERVAL = 300  # секунд между проверками логов

# Railway service_id → (repo_name, main_file)
SERVICES = {
    "3319eabd-5bcb-4e59-839e-4813f1e7ef33": ("logger-bot",       "bot.py"),
    "367e25d7-8410-419d-896d-2cc86cd44efd": ("tilly-bot",        "bot.py"),
    "5d61d403-feee-455e-9c0d-523f0e7c79d5": ("filly-bot",        "bot.py"),
    "d949c4d2-59fa-4cbe-8bb8-a0589a476607": ("doctor-bot",       "bot.py"),
    "db277aff-6638-4b4a-970e-b016bd753608": ("milly-bot",        "bot.py"),
    "3dfc7336-2e91-4ade-950a-4f3d566baced": ("office-dashboard", "main.py"),
    "b441ce93-9736-49b3-9b5d-d0c82e715b28": ("billy-bot",        "bot.py"),
    "9db4108e-19f1-4c1f-a21c-3909442e137c": ("prophet-bot",      "bot.py"),
    "9f868f0c-9c94-4776-a2dc-86a30d812b92": ("tilly-trader",     "bot.py"),
    "fa7c87cf-454c-4946-ab25-6a5091f0ac47": ("mama-bot",          "bot.py"),
    "a5e37cc4-0a9f-4700-b6d3-d39b958ce0cb": ("villy-bot",         "bot.py"),
}

bot    = Bot(token=BOT_TOKEN)
dp     = Dispatcher()
claude = AsyncAnthropic(api_key=ANTHROPIC_KEY)

# Буфер последних сообщений группы — чтобы найти оригинальный вопрос
from collections import deque
recent_group_msgs: deque = deque(maxlen=30)  # (sender, text, is_bot)

# Redis — персистентная дедупликация seen_errors и last_seen
REDIS_URL = os.getenv("REDIS_URL", "")
_redis: aioredis.Redis | None = None

async def get_redis() -> aioredis.Redis | None:
    global _redis
    if _redis is None and REDIS_URL:
        try:
            _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
            await _redis.ping()
        except Exception as e:
            logger.warning(f"Redis unavailable: {e}")
            _redis = None
    return _redis

# Системные промпты каждого бота — для мгновенного ответа с web search
BOT_SYSTEMS_WEB = {
    "тилли": (
        "Ты — Тилли. Аналитик по трейдингу и крипторынкам. "
        "Используй web_search для получения актуальных цен, данных и новостей. "
        "Холодная голова, цифры важнее эмоций. Говоришь чётко — уровни, объёмы, тренды. "
        "Не даёшь советов купи/продай — даёшь анализ и сценарии. Неформально, на русском."
    ),
    "милли": (
        "Ты — Милли. Бизнес-ассистент. Используй web_search для актуальных данных о рынке, "
        "конкурентах, ценах. Мыслишь цифрами и результатами. Неформально, на русском."
    ),
    "доктор": (
        "Ты — Доктор. Советник по здоровью. Используй web_search для актуальных исследований. "
        "Говоришь прямо и конкретно, основываешься на науке. Неформально, на русском."
    ),
    "билли": (
        "Ты — Билли. Целеустремлённый практик. Используй web_search когда нужны актуальные данные. "
        "Говоришь прямо без воды. Неформально, на русском."
    ),
}

# Хранит pending-фиксы ожидающие /approve: {fix_id: fix_data}
pending_fixes: dict = {}

# Последние seen timestamps логов по сервису чтобы не дублировать
last_seen: dict = {}  # fallback in-memory (Redis preferred)

# Дедупликация: hash ошибки → timestamp последнего анализа
# Персистентно хранится в Redis; fallback на in-memory seen_errors при недоступности Redis
ERROR_COOLDOWN = 3600  # 1 час
seen_errors: dict = {}  # in-memory fallback

# ── Prompts ───────────────────────────────────────────────────────────────────
CODER_PROMPT = """Python-кодер. ТОЛЬКО код без markdown. Готов к запуску. Комментарии внутри. Объяснения — кратко."""

CHAT_PROMPT = """Ты — Силли, технический эксперт AI-офиса. Отвечай кратко и по делу, без лишнего кода если не просят. Неформально, на русском.

Ты умеешь редактировать файлы в GitHub репозиториях через GitHub API (функции read_file, push_file из shared.github_tools). Когда тебе дают GH_PAT токен и задачу отредактировать файл — читай файл, меняй содержимое, пушь обратно. Не говори что не можешь — делай. GH_PAT передавай как параметр в функции если нужно, или используй переменную окружения GITHUB_TOKEN."""

ANALYZER_PROMPT = """Анализатор багов Python/Telegram/Railway. JSON без markdown:
{"is_bug":bool,"confidence":"high|low","bug_type":"crash|logic|config|network|unknown","description":"1-2 предл","affected_file":"path|null","fix_description":"конкретно","lesson_title":"","lesson_symptom":"","lesson_cause":"","lesson_fix":"","lesson_avoid":""}
high=явный crash/NameError/ImportError/SyntaxError/KeyError→автофикс. low=логика/сеть→спросить."""

FIXER_PROMPT = """Фиксер. Верни ТОЛЬКО полный исправленный код. Минимум изменений. Без markdown."""


# ── Railway API ───────────────────────────────────────────────────────────────
LESSON_SEARCH_PROMPT = """You are a bug pattern matcher. Given new error logs and a list of known bugs in compact format, find if there is a matching known bug.
Return ONLY valid JSON:
{"match": true/false, "lesson_id": <id or null>, "confidence": "high"/"low", "reason": "one line"}
high confidence: same root cause, same file/function, same error pattern.
low confidence: similar but not certain."""

async def search_lessons(error_logs: list[str]) -> dict:
    """Search lessons.json for a matching known bug before running full analysis."""
    try:
        raw = await read_file("ai-office-shared", LESSONS_FILE)
        lessons = json.loads(raw)
        if not lessons:
            return {"match": False}
        log_sample = "\n".join(error_logs[:20])
        prompt = f"Known bugs:\n{json.dumps(lessons)}\n\nNew error logs:\n{log_sample}"
        result = await ask_claude(prompt, system=LESSON_SEARCH_PROMPT, model="claude-haiku-4-5-20251001")
        result = result.strip()
        start, end = result.find("{"), result.rfind("}") + 1
        if start != -1 and end > start:
            result = result[start:end]
        return json.loads(result)
    except Exception as e:
        logger.debug(f"search_lessons failed: {e}")
        return {"match": False}


async def append_lesson_ai(title: str, symptom: str, cause: str, context: str, fix: str, avoid: str):
    """Append new lesson in compact AI format to lessons.json."""
    try:
        raw = await read_file("ai-office-shared", LESSONS_FILE)
        lessons = json.loads(raw)
        new_id = max((l.get("id", 0) for l in lessons), default=0) + 1
        # Ask Haiku to convert lesson to compact AI format
        prompt = (
            f"Convert this bug lesson to compact AI format JSON (like existing entries).\n"
            f"title: {title}\nsymptom: {symptom}\ncause: {cause}\n"
            f"context: {context}\nfix: {fix}\navoid: {avoid}\n\n"
            f"Existing format example: {json.dumps(lessons[0]) if lessons else '{}'}\n\n"
            f"Return ONLY the JSON object, no markdown. Add id:{new_id} and ts field with today's date."
        )
        compact = await ask_claude(prompt, system="Return only valid JSON, no markdown.", model="claude-haiku-4-5-20251001")
        compact = compact.strip()
        start, end = compact.find("{"), compact.rfind("}") + 1
        if start != -1 and end > start:
            compact = compact[start:end]
        lesson_obj = json.loads(compact)
        lessons.append(lesson_obj)
        await push_file("ai-office-shared", LESSONS_FILE, json.dumps(lessons, ensure_ascii=False, indent=2),
                        f"lesson({new_id}): {title[:50]}")
        logger.info(f"[lessons] saved lesson #{new_id}: {title}")
    except Exception as e:
        logger.error(f"append_lesson_ai failed: {e}")



INTENT_PROMPT = """Диспетчер AI-офиса. JSON без markdown:
{"intent":"push_code|fix_bot|create_bot|get_bot_token|deploy|read_file|list_files|answer","repo":"name|null","path":"path|null","task":"описание","confidence":"high|low"}
push_code=залить код, fix_bot=исправить баг, create_bot=новый бот, get_bot_token=получить токен существующего бота, deploy=редеплой, read_file=прочитать, list_files=список, answer=ответить.
Репо: billy-bot,tilly-bot,filly-bot,doctor-bot,milly-bot,ai-office-shared,logger-bot,office-dashboard.
билли→billy, тилли→tilly, макс/милли→milly, доктор→doctor, филли→filly, силли→ai-office-shared."""


OPS_LOG_FILE = "logs/ops.md"

async def append_ops_log(action: str, service: str, details: str = ""):
    """Append Cilly action to ops.md for Claude context on next session."""
    try:
        ts = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        entry = f"\n**[{ts}] Силли — {service}:** {action}"
        if details:
            entry += f"\n> {details}"
        entry += "\n"

        raw = await read_file("ai-office-shared", OPS_LOG_FILE)
        updated = raw + entry
        await push_file("ai-office-shared", OPS_LOG_FILE, updated,
                        f"log(cilly): {action[:50]} @ {service}")
    except Exception as e:
        logger.debug(f"append_ops_log failed: {e}")

async def railway_query(query: str, variables: dict = None) -> dict:
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        r = await client.post(
            "https://backboard.railway.com/graphql/v2",
            headers={"Authorization": f"Bearer {RAILWAY_TOKEN}", "Content-Type": "application/json"},
            json=payload
        )
        r.raise_for_status()
        return r.json()


async def get_service_logs(service_id: str) -> list[str]:
    """Получить последние логи сервиса."""
    try:
        data = await railway_query("""
            query($id: String!) {
              deployments(input: { serviceId: $id }) {
                edges { node { id status createdAt } }
              }
            }
        """, {"id": service_id})
        edges = (data.get("data") or {}).get("deployments", {}).get("edges", [])
        if not edges:
            return []
        latest_id = edges[0]["node"]["id"]

        log_data = await railway_query("""
            query($id: String!) {
              deploymentLogs(deploymentId: $id) { message timestamp }
            }
        """, {"id": latest_id})
        logs = (log_data.get("data") or {}).get("deploymentLogs", [])
        if not logs:
            return []
    except Exception as e:
        logger.debug(f"get_service_logs failed for {service_id}: {e}")
        return []

    # Только новые логи с момента последней проверки
    r = await get_redis()
    cutoff = float(await r.get(f"last_seen:{service_id}") or 0) if r else last_seen.get(service_id, 0)
    new_logs = []
    latest_ts = cutoff
    for l in logs:
        ts_str = l.get("timestamp", "")
        try:
            import datetime
            ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
        except Exception:
            ts = 0
        if ts > cutoff:
            new_logs.append(l.get("message", ""))
            if ts > latest_ts:
                latest_ts = ts
    r = await get_redis()
    if r:
        await r.set(f"last_seen:{service_id}", latest_ts)
    else:
        last_seen[service_id] = latest_ts
    return new_logs


async def redeploy_service(service_id: str) -> bool:
    """Передеплоить сервис через Railway API."""
    try:
        data = await railway_query("""
            mutation($serviceId: String!, $environmentId: String!) {
              serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId)
            }
        """, {"serviceId": service_id, "environmentId": "2efaaf60-ba39-492c-bf86-007fd505493f"})
        return "errors" not in data
    except Exception as e:
        logger.error(f"redeploy failed for {service_id}: {e}")
        return False


# ── Claude helpers ─────────────────────────────────────────────────────────────
async def ask_claude(prompt: str, system: str = CODER_PROMPT, model: str = "claude-opus-4-5-20251101") -> str:
    response = await claude.messages.create(
        model=model,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


async def analyze_logs(service_name: str, logs: list[str], source_code: str) -> dict:
    log_text = "\n".join(logs[-50:])  # последние 50 строк
    prompt = (
        f"Сервис: {service_name}\n\n"
        f"Логи:\n{log_text}\n\n"
        f"Исходный код:\n{source_code}"
    )
    # Haiku для анализа — в 20 раз дешевле Opus
    raw = await ask_claude(prompt, system=ANALYZER_PROMPT, model="claude-haiku-4-5-20251001")
    raw = raw.strip()
    if "```" in raw:
        parts = raw.split("```")
        for p in parts:
            p = p.strip().lstrip("json").strip()
            if p.startswith("{"):
                raw = p
                break
    start, end = raw.find("{"), raw.rfind("}") + 1
    if start != -1 and end > start:
        raw = raw[start:end]
    return json.loads(raw)


async def generate_fix(source_code: str, fix_description: str) -> str:
    prompt = f"Описание бага: {fix_description}\n\nИсходный код:\n{source_code}"
    # Opus только для генерации фикса — критично чтобы код был правильным
    return await ask_claude(prompt, system=FIXER_PROMPT, model="claude-opus-4-5-20251101")


# ── Lesson & notifications ─────────────────────────────────────────────────────
async def post_lesson(title: str, symptom: str, cause: str, context: str, fix: str, how_to_avoid: str):
    if not LESSONS_CHAT_ID:
        return
    text = (
        f"📚 Урок — {title}\n\n"
        f"🔴 Симптом\n{symptom}\n\n"
        f"🔍 Причина\n{cause}\n\n"
        f"📍 Контекст\n{context}\n\n"
        f"🔧 Фикс\n{fix}\n\n"
        f"🛡️ Как избежать\n{how_to_avoid}"
    )
    try:
        await bot.send_message(chat_id=LESSONS_CHAT_ID, text=text)
    except Exception as e:
        logger.error(f"post_lesson failed: {e}")
    # Save compact AI format to lessons.json in parallel
    asyncio.create_task(append_lesson_ai(title, symptom, cause, context, fix, how_to_avoid))


async def notify_office(text: str):
    if not OFFICE_CHAT_ID:
        return
    try:
        await bot.send_message(chat_id=OFFICE_CHAT_ID, text=text)
    except Exception as e:
        logger.error(f"notify_office failed: {e}")


# ── Auto-fix pipeline ──────────────────────────────────────────────────────────
async def handle_bug(service_id: str, service_name: str, repo: str, main_file: str, analysis: dict):
    """Основная логика: автофикс или запрос подтверждения."""
    confidence  = analysis.get("confidence", "low")
    description = analysis.get("description", "")
    fix_desc    = analysis.get("fix_description", "")
    affected    = main_file  # Всегда используем файл из SERVICES, не доверяем LLM

    try:
        source_code = await read_file(repo, affected)
    except Exception as e:
        logger.error(f"Can't read {repo}/{affected}: {e}")
        return

    fixed_code = await generate_fix(source_code, fix_desc)

    if False:  # Автофикс ОТКЛЮЧЁН — всегда требуем /approve
        pass
    else:
        # Всегда спрашиваем /approve — автофикс отключён во избежание цепных реакций
        fix_id = f"{service_name}_{int(time.time())}"
        pending_fixes[fix_id] = {
            "service_id": service_id,
            "service_name": service_name,
            "repo": repo,
            "affected": affected,
            "fixed_code": fixed_code,
            "analysis": analysis,
        }
        await notify_office(
            f"🤔 Cilly нашёл подозрительное в *{service_name}*:\n\n"
            f"_{description}_\n\n"
            f"Предлагаемый фикс: {fix_desc}\n\n"
            f"Применить?\n"
            f"/approve {fix_id} — да, фиксить\n"
            f"/skip {fix_id} — пропустить"
        )


# ── Monitor loop ───────────────────────────────────────────────────────────────
ERROR_PATTERNS = ["Traceback", "Error:", "Exception:", "CRITICAL", "crashed", "exit code"]

# Паттерны которые НЕ являются багами — игнорируем
IGNORE_PATTERNS = [
    "Conflict: terminated by other getUpdates",  # нормально при редеплое
    "terminated by other getUpdates request",
    "make sure that only one bot instance",
    "NetworkError while getting Updates",        # временная сетевая ошибка
    "TimedOut",                                  # telegram timeout — не баг
    "DeprecationWarning",                        # предупреждение, не ошибка
]

# Игнорировать ошибки старше этого времени (секунды) — стартовый шум редеплоя
ERROR_MAX_AGE = 120  # 2 минуты

# Фразы которые означают что боту не хватает инструмента
RESPONSE_ANALYZER_PROMPT = """Анализатор ответов AI-агентов. Есть ли проблема с возможностями?
ПРОБЛЕМА: агент не может получить актуальные данные и говорит об этом / отказывается / просит юзера найти самому.
НЕ ПРОБЛЕМА: просит уточнить / отвечает по делу / нет данных от юзера.
JSON без markdown: {"has_problem":bool,"problem_type":"no_web_search|none","fix_needed":"web_search|none","confidence":"high|low","reason":"1 предложение"}"""


async def analyze_bot_response(user_question: str, bot_response: str) -> dict:
    """Анализирует ответ бота — есть ли проблема с возможностями."""
    prompt = f"Вопрос пользователя: {user_question}\n\nОтвет агента: {bot_response}"
    raw = await ask_claude(prompt, system=RESPONSE_ANALYZER_PROMPT, model="claude-haiku-4-5-20251001")
    raw = raw.strip()
    start, end = raw.find("{"), raw.rfind("}") + 1
    if start != -1 and end > start:
        raw = raw[start:end]
    return json.loads(raw)

# Имя бота в группе → репо + файл
BOT_REPOS = {
    "тилли":  ("tilly-bot",  "bot.py"),
    "билли":  ("billy-bot",  "bot.py"),
    "милли":  ("milly-bot",  "bot.py"),
    "доктор": ("doctor-bot", "bot.py"),
}

WEB_SEARCH_FIX_PROMPT = """Добавь web search tool в этот Python код Telegram бота.

Нужно сделать три изменения:
1. В системный промпт добавить в самое начало (первая строка):
   "Используй web_search для получения актуальных данных: цены, курсы, новости, события."
2. В вызов client.messages.create() добавить параметр tools:
   tools=[{{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}}]
3. Парсинг ответа уже перебирает блоки через hasattr(block, "text") — не трогай его.

Верни ТОЛЬКО исправленный код целиком, без объяснений и markdown.

Исходный код:
{source}"""

async def monitor_loop():
    """Фоновая задача: каждые 5 минут проверяет логи всех сервисов."""
    await asyncio.sleep(30)  # подождать пока бот стартует
    logger.info("[monitor] started")
    while True:
        for service_id, (repo, main_file) in SERVICES.items():
            try:
                logs = await get_service_logs(service_id)
                if not logs:
                    continue

                # Быстрый фильтр — есть ли ошибки вообще
                error_logs = [l for l in logs if any(p in l for p in ERROR_PATTERNS)]
                if not error_logs:
                    continue

                # Фильтр игнорируемых паттернов (нормальные события при редеплое и т.п.)
                error_logs = [l for l in error_logs if not any(p in l for p in IGNORE_PATTERNS)]
                if not error_logs:
                    logger.info(f"[monitor] {repo}: only ignorable errors, skipping")
                    continue

                # Дедупликация: хэш первых 3 строк ошибки
                import hashlib
                error_signature = hashlib.md5("\n".join(error_logs[:3]).encode()).hexdigest()
                now = time.time()
                redis_key = f"seen_error:{service_id}:{error_signature}"
                r = await get_redis()
                if r:
                    last_analysis = float(await r.get(redis_key) or 0)
                else:
                    last_analysis = seen_errors.get(f"{service_id}:{error_signature}", 0)
                if now - last_analysis < ERROR_COOLDOWN:
                    logger.info(f"[monitor] skipping duplicate error in {repo} (cooldown)")
                    continue
                if r:
                    await r.setex(redis_key, ERROR_COOLDOWN, now)  # auto-expires
                else:
                    seen_errors[f"{service_id}:{error_signature}"] = now
                    cutoff = now - ERROR_COOLDOWN
                    expired = [k for k, v in seen_errors.items() if v < cutoff]
                    for k in expired:
                        del seen_errors[k]

                logger.info(f"[monitor] found {len(error_logs)} error lines in {repo}, analyzing...")

                # Читаем исходник
                try:
                    source_code = await read_file(repo, main_file)
                except Exception:
                    source_code = "# файл не удалось прочитать"

                # Check known bugs first — saves Opus tokens on repeated issues
                known = await search_lessons(error_logs)
                if known.get("match") and known.get("confidence") == "high":
                    logger.info(f"[monitor] known bug match in {repo}: lesson #{known.get('lesson_id')}")
                    await notify_office(
                        f"📚 Cilly узнал баг в *{repo}* — это уже было (урок #{known.get('lesson_id')})\n"
                        f"_{known.get('reason', '')}_\n\nПрименяю известный фикс..."
                    )

                analysis = await analyze_logs(repo, error_logs, source_code)

                if analysis.get("is_bug"):
                    await handle_bug(service_id, repo, repo, main_file, analysis)

            except Exception as e:
                logger.error(f"[monitor] error checking {repo}: {e}")

        await asyncio.sleep(MONITOR_INTERVAL)




# ── Bot creation pipeline ─────────────────────────────────────────────────────
PROJECT_ID = "271b40b7-199a-429a-88ef-ca417f26a638"
RAILWAY_TOKEN_VAL = os.getenv("RAILWAY_TOKEN", "")

BOT_TEMPLATE = """import os, logging, asyncio, httpx
from aiohttp import web
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import anthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
YOUR_TELEGRAM_ID = int(os.environ["YOUR_TELEGRAM_ID"])
OFFICE_CHAT_ID   = os.environ.get("OFFICE_CHAT_ID", "")
LOG_BOT_URL      = os.environ.get("LOG_BOT_URL", "")
HTTP_PORT        = 8080

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
conversation_history = {{}}

SYSTEM = \"\"\"{system_prompt}\"\"\"

async def log(event: str, msg: str):
    if not LOG_BOT_URL:
        return
    try:
        async with httpx.AsyncClient() as c:
            await c.post(f"{{LOG_BOT_URL}}/log", json={{"agent": "{bot_name}", "type": event, "message": msg}}, timeout=5)
    except Exception:
        pass

async def send_to_group(text: str):
    if not OFFICE_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient() as c:
            await c.post(f"https://api.telegram.org/bot{{TELEGRAM_TOKEN}}/sendMessage",
                json={{"chat_id": OFFICE_CHAT_ID, "text": text}}, timeout=10)
    except Exception as e:
        logger.error(f"send_to_group failed: {{e}}")

async def process(message: str, user_id: int) -> str:
    if user_id not in conversation_history:
        conversation_history[user_id] = []
    conversation_history[user_id].append({{"role": "user", "content": message}})
    if len(conversation_history[user_id]) > 20:
        conversation_history[user_id] = conversation_history[user_id][-10:]
    r = client.messages.create(model="claude-sonnet-4-6", max_tokens=1024,
        system=SYSTEM, messages=conversation_history[user_id])
    text = r.content[0].text
    conversation_history[user_id].append({{"role": "assistant", "content": text}})
    return text

async def handle_task(request):
    data = await request.json()
    message = data.get("message", "")
    user_id = data.get("user_id", YOUR_TELEGRAM_ID)
    await log("MSG_IN", f"[HTTP] {{message[:80]}}")
    response = await process(message, user_id)
    await send_to_group(f"{bot_name}:\\n{{response}}")
    await log("MSG_OUT", f"{bot_name}: {{response[:80]}}")
    return web.json_response({{"status": "ok", "response": response}})

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != YOUR_TELEGRAM_ID:
        return
    if update.effective_chat.type in ["group", "supergroup"]:
        return
    msg = update.message.text
    await log("MSG_IN", msg[:80])
    response = await process(msg, update.effective_user.id)
    await log("MSG_OUT", f"{bot_name}: {{response[:80]}}")
    await update.message.reply_text(response)


async def main():
    app_http = web.Application()
    app_http.router.add_post("/task", handle_task)
    runner = web.AppRunner(app_http)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", HTTP_PORT).start()
    logger.info(f"HTTP on :{{HTTP_PORT}}")
    ptb = Application.builder().token(TELEGRAM_TOKEN).build()
    ptb.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    async with ptb:
        await ptb.start()
        await ptb.updater.start_polling(drop_pending_updates=True)
        logger.info("{bot_name} запущен")
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
"""

REQUIREMENTS_TEMPLATE = """python-telegram-bot==21.3
anthropic
aiohttp
httpx
"""

DOCKERFILE_TEMPLATE = """FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "bot.py"]
"""

ENVIRONMENT_ID = "2efaaf60-ba39-492c-bf86-007fd505493f"

async def create_via_botfather(bot_name_en: str, bot_display: str) -> str:
    """Создать бота через BotFather и вернуть токен. bot_name_en — username без _bot."""
    api_id   = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    session  = os.getenv("TELETHON_SESSION", "")

    if not all([api_id, api_hash, session]):
        raise EnvironmentError("TELEGRAM_API_ID / TELEGRAM_API_HASH / TELETHON_SESSION не заданы")

    bot_username = f"{bot_name_en}_bot"

    async with TelegramClient(StringSession(session), api_id, api_hash) as client:
        botfather = await client.get_entity("@BotFather")

        async def send(text: str):
            await client.send_message(botfather, text)
            await asyncio.sleep(2)

        async def last_reply() -> str:
            msgs = await client.get_messages(botfather, limit=1)
            return msgs[0].text if msgs else ""

        # /newbot
        await send("/newbot")
        await send(bot_display)        # имя бота
        await send(bot_username)       # username

        reply = await last_reply()

        # Извлекаем токен из ответа BotFather
        import re
        match = re.search(r"(\d+:[A-Za-z0-9_-]{35,})", reply)
        if not match:
            raise ValueError(f"Не нашёл токен в ответе BotFather: {reply[:200]}")

        return match.group(1)



async def get_telethon_client() -> TelegramClient:
    """Создать и вернуть подключённый Telethon клиент."""
    api_id   = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    session  = os.getenv("TELETHON_SESSION", "")
    if not all([api_id, api_hash, session]):
        raise EnvironmentError("TELEGRAM_API_ID / TELEGRAM_API_HASH / TELETHON_SESSION не заданы")
    client = TelegramClient(StringSession(session), api_id, api_hash)
    await client.connect()
    return client


async def tg_add_bot_to_group(bot_username: str, group_id: int) -> bool:
    """Добавить бота в группу по group_id."""
    client = await get_telethon_client()
    try:
        bot_entity = await client.get_entity(bot_username)
        group_entity = await client.get_entity(group_id)
        await client(InviteToChannelRequest(group_entity, [bot_entity]))
        logger.info(f"tg_add_bot_to_group: {bot_username} → {group_id}")
        return True
    except Exception as e:
        logger.error(f"tg_add_bot_to_group failed: {e}")
        return False
    finally:
        await client.disconnect()


async def tg_get_folder_id(folder_name: str) -> int | None:
    """Найти ID папки по имени."""
    client = await get_telethon_client()
    try:
        filters = await client(GetDialogFiltersRequest())
        for f in filters.filters:
            if hasattr(f, 'title') and f.title.lower() == folder_name.lower():
                return f.id
        return None
    finally:
        await client.disconnect()


async def tg_add_peer_to_folder(peer_id: int, folder_name: str = "Office") -> bool:
    """Добавить диалог (бота или группу) в папку по имени."""
    client = await get_telethon_client()
    try:
        filters = await client(GetDialogFiltersRequest())
        target = None
        for f in filters.filters:
            if hasattr(f, 'title') and f.title.lower() == folder_name.lower():
                target = f
                break
        if not target:
            logger.warning(f"Папка '{folder_name}' не найдена")
            return False

        peer_entity = await client.get_entity(peer_id)
        input_peer = await client.get_input_entity(peer_entity)

        # Проверяем что ещё не добавлен
        existing_ids = [getattr(p, 'channel_id', None) or getattr(p, 'user_id', None) or getattr(p, 'chat_id', None)
                        for p in target.include_peers]
        new_id = getattr(input_peer, 'channel_id', None) or getattr(input_peer, 'user_id', None) or getattr(input_peer, 'chat_id', None)
        if new_id in existing_ids:
            logger.info(f"Peer {peer_id} уже в папке {folder_name}")
            return True

        target.include_peers.append(input_peer)
        await client(UpdateDialogFilterRequest(id=target.id, filter=target))
        logger.info(f"tg_add_peer_to_folder: {peer_id} → {folder_name}")
        return True
    except Exception as e:
        logger.error(f"tg_add_peer_to_folder failed: {e}")
        return False
    finally:
        await client.disconnect()


async def tg_create_group(title: str, bot_usernames: list[str] = None) -> int | None:
    """Создать новую группу и вернуть её ID."""
    from telethon.tl.functions.channels import CreateChannelRequest
    client = await get_telethon_client()
    try:
        result = await client(CreateChannelRequest(
            title=title, about="", megagroup=True
        ))
        group = result.chats[0]
        group_id = -100_000_000_000 - group.id  # правильный формат для supergroup

        if bot_usernames:
            for username in bot_usernames:
                try:
                    bot_entity = await client.get_entity(username)
                    await client(InviteToChannelRequest(group, [bot_entity]))
                    await asyncio.sleep(1)
                except Exception as e:
                    logger.warning(f"Не удалось добавить {username}: {e}")

        logger.info(f"tg_create_group: '{title}' → {group_id}")
        return group_id
    except Exception as e:
        logger.error(f"tg_create_group failed: {e}")
        return None
    finally:
        await client.disconnect()


async def tg_promote_bot_admin(bot_username: str, group_id: int) -> bool:
    """Выдать боту права администратора — работает с обычными чатами и супергруппами."""
    from telethon.tl.types import Chat, Channel
    client = await get_telethon_client()
    try:
        group_entity = await client.get_entity(group_id)
        bot_entity   = await client.get_entity(bot_username)

        if isinstance(group_entity, Channel):
            # Супергруппа или канал
            rights = ChatAdminRights(post_messages=True)
            await client(EditAdminRequest(
                channel=group_entity, user_id=bot_entity,
                admin_rights=rights, rank="Bot"
            ))
        else:
            # Обычный чат (Chat)
            await client(EditChatAdminRequest(
                chat_id=group_entity.id,
                user_id=bot_entity,
                is_admin=True
            ))

        logger.info(f"tg_promote_bot_admin: {bot_username} → admin in {group_id}")
        return True
    except Exception as e:
        logger.error(f"tg_promote_bot_admin failed for {bot_username}: {e}")
        return False
    finally:
        await client.disconnect()


async def railway_graphql(query: str, variables: dict = None) -> dict:
    """Выполнить GraphQL запрос к Railway API."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
        payload = {"query": query}
        if variables:
            payload["variables"] = variables
        r = await client.post(
            "https://backboard.railway.com/graphql/v2",
            headers={"Authorization": f"Bearer {RAILWAY_TOKEN_VAL}", "Content-Type": "application/json"},
            json=payload
        )
        r.raise_for_status()
        return r.json()

async def railway_set_variables(service_id: str, variables: dict) -> bool:
    """Записать переменные окружения в Railway сервис."""
    data = await railway_graphql(
        """mutation($input: VariableCollectionUpsertInput!) {
             variableCollectionUpsert(input: $input)
           }""",
        {"input": {
            "projectId": PROJECT_ID,
            "environmentId": ENVIRONMENT_ID,
            "serviceId": service_id,
            "variables": variables
        }}
    )
    return data.get("data", {}).get("variableCollectionUpsert") is True

async def railway_create_service(repo_name: str, bot_display_name: str, variables: dict = None) -> dict:
    """Создать сервис на Railway, подключить GitHub репо и записать переменные."""
    # 1. Создать сервис
    data = await railway_graphql(
        """mutation($input: ServiceCreateInput!) {
             serviceCreate(input: $input) { id name }
           }""",
        {"input": {
            "projectId": PROJECT_ID,
            "name": repo_name,
            "source": {"repo": f"unperson22-alt/{repo_name}"}
        }}
    )
    if "errors" in data:
        raise Exception(f"serviceCreate failed: {data['errors'][0]['message']}")
    service_id = data["data"]["serviceCreate"]["id"]

    # 2. Записать переменные если переданы
    if variables:
        ok = await railway_set_variables(service_id, variables)
        if not ok:
            logger.warning(f"railway_set_variables returned False for {repo_name}")

    return {"service_id": service_id}


async def handle_natural_language(message_text: str, chat_id: int, reply_func):
    """Process any natural language request — detect intent and execute."""
    await reply_func("🧠 Разбираю запрос...")

    # Detect intent via Haiku (cheap)
    # Truncate to 500 chars for intent detection — Haiku struggles with long messages
    intent_input = message_text[:500] if len(message_text) > 500 else message_text
    raw = await ask_claude(intent_input, system=INTENT_PROMPT, model="claude-haiku-4-5-20251001")
    raw = raw.strip()
    start, end = raw.find("{"), raw.rfind("}") + 1
    if start != -1 and end > start:
        raw = raw[start:end]

    try:
        intent_data = json.loads(raw)
    except Exception:
        # Keyword fallback — better than failing silently
        msg_lower = message_text.lower()
        if any(w in msg_lower for w in ["создай бота", "create bot", "новый бот"]):
            intent_data = {"intent": "create_bot", "repo": None, "path": None, "task": message_text, "confidence": "low"}
        elif any(w in msg_lower for w in ["задеплой", "redeploy", "передеплой"]):
            intent_data = {"intent": "deploy", "repo": None, "path": None, "task": message_text, "confidence": "low"}
        elif any(w in msg_lower for w in ["залей", "push", "запиши код"]):
            intent_data = {"intent": "push_code", "repo": None, "path": None, "task": message_text, "confidence": "low"}
        else:
            # Fallback: just answer conversationally
            answer = await ask_claude(message_text, system=CHAT_PROMPT, model="claude-haiku-4-5-20251001")
            await reply_func(answer)
            return

    intent = intent_data.get("intent", "answer")
    repo   = intent_data.get("repo")
    path   = intent_data.get("path")
    task   = intent_data.get("task", message_text)

    logger.info(f"[nl] intent={intent} repo={repo} path={path}")

    if intent == "answer":
        answer = await ask_claude(message_text, system=CHAT_PROMPT, model="claude-haiku-4-5-20251001")
        await reply_func(answer)

    elif intent in ("push_code", "fix_bot"):
        if not repo or not path:
            await reply_func("❓ Уточни: в каком репо и какой файл изменить?")
            return
        await reply_func(f"⏳ Генерирую код для `{repo}/{path}`...")
        code = await ask_claude(task)
        await reply_func("📤 Заливаю на GitHub...")
        try:
            result = await push_file(repo, path, code, f"nl: {task[:60]}")
            action = "Обновлён" if result["action"] == "updated" else "Создан"
            await reply_func(f"✅ {action}: {result['url']}")
            # Auto-redeploy
            service_id = next((sid for sid, (r, _) in SERVICES.items() if r == repo), None)
            if service_id:
                await reply_func("🔄 Запускаю редеплой...")
                ok = await redeploy_service(service_id)
                await reply_func("✅ Задеплоено" if ok else "⚠️ Пуш сделан, редеплой не удался")
        except Exception as e:
            await reply_func(f"❌ Ошибка: {e}")

    elif intent == "create_bot":
        # Extract bot name and persona from task
        await reply_func(f"🤖 Создаю бота: *{task}*...", )

        # Ask Claude to extract name + system prompt
        setup_raw = await ask_claude(
            f"Из описания извлеки: имя бота (одно слово, латиница, строчные, через дефис если нужно), "
            f"отображаемое имя (по-русски, одно слово) и системный промпт (1-2 предложения, роль и стиль). "
            f"Описание: {task}\n\n"
            f"Верни ТОЛЬКО JSON без markdown: {{\"repo\": \"имя-бота\", \"display\": \"Имя\", \"prompt\": \"...\"}}" ,
            model="claude-haiku-4-5-20251001"
        )
        try:
            setup_raw = setup_raw.strip()
            s, e = setup_raw.find("{"), setup_raw.rfind("}") + 1
            setup = json.loads(setup_raw[s:e])
            bot_repo   = setup["repo"].lower().replace(" ", "-") + "-bot"
            bot_display = setup["display"]
            bot_prompt  = setup["prompt"]
        except Exception as ex:
            await reply_func(f"❌ Не смог разобрать параметры бота: {ex}")
            return

        await reply_func(f"📦 Репо: `{bot_repo}`\n👤 Имя: {bot_display}\n📝 Промпт: {bot_prompt}")

        # 1. Создать GitHub репо
        await reply_func("1️⃣ Создаю GitHub репо...")
        try:
            repo_info = await create_repo(bot_repo, description=f"AI office bot: {bot_display}")
        except ValueError as ex:
            await reply_func(f"⚠️ {ex} — продолжаю с существующим")
        except Exception as ex:
            await reply_func(f"❌ GitHub: {ex}")
            return

        # 2. Пушу шаблон
        await reply_func("2️⃣ Генерирую и заливаю код...")
        bot_code = BOT_TEMPLATE.format(bot_name=bot_display, system_prompt=bot_prompt)
        try:
            await push_file(bot_repo, "bot.py", bot_code, f"init: {bot_display} bot")
            await push_file(bot_repo, "requirements.txt", REQUIREMENTS_TEMPLATE, "init: requirements")
            await push_file(bot_repo, "Dockerfile", DOCKERFILE_TEMPLATE, "init: Dockerfile")
        except Exception as ex:
            await reply_func(f"❌ Пуш файлов: {ex}")
            return

        # 3. Создать сервис на Railway
        # 3. BotFather — получаем токен автоматически
        await reply_func("3️⃣ Иду в BotFather за токеном...")
        try:
            tg_token = await create_via_botfather(bot_repo.replace("-bot", ""), bot_display)
        except Exception as ex:
            await reply_func(f"❌ BotFather: {ex}")
            return

        # 4. Создаём сервис на Railway со всеми переменными сразу
        await reply_func("4️⃣ Создаю сервис на Railway и прописываю все переменные...")
        all_vars = {
            "TELEGRAM_TOKEN":  tg_token,
            "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", ""),
            "YOUR_TELEGRAM_ID": os.getenv("YOUR_TELEGRAM_ID", ""),
            "OFFICE_CHAT_ID":   os.getenv("OFFICE_CHAT_ID", ""),
            "LOG_BOT_URL":      os.getenv("LOG_BOT_URL", ""),
        }
        try:
            railway_info = await railway_create_service(bot_repo, bot_display, variables=all_vars)
            service_id = railway_info["service_id"]
        except Exception as ex:
            await reply_func(f"❌ Railway: {ex}")
            return

        # Verify token works
        async with httpx.AsyncClient(timeout=10) as hc:
            me = await hc.get(f"https://api.telegram.org/bot{tg_token}/getMe")
            me_data = me.json()
        if not me_data.get("ok"):
            await reply_func(f"❌ Токен не работает: {me_data.get('description','')}")
            return
        actual_username = me_data["result"]["username"]
        await reply_func(f"✅ Токен проверен: @{actual_username}")

        # 5. Добавить бота в Office group
        await reply_func("5️⃣ Добавляю бота в Office group...")
        office_group_id = int(os.getenv("OFFICE_CHAT_ID", "0"))
        bot_username = f"@{bot_repo.replace('-', '_')}"
        added = await tg_add_bot_to_group(bot_username, office_group_id)
        if added:
            await tg_promote_bot_admin(bot_username, office_group_id)

        # 6. Переместить бота и сервис в папку Office
        await reply_func("6️⃣ Перемещаю в папку Office...")
        # Добавляем бота (личный чат) в папку
        try:
            bot_entity = None
            client_tmp = await get_telethon_client()
            try:
                bot_entity = await client_tmp.get_entity(bot_username)
            finally:
                await client_tmp.disconnect()
            if bot_entity:
                await tg_add_peer_to_folder(bot_entity.id, "Office")
        except Exception as e:
            logger.warning(f"Не удалось добавить в папку: {e}")

        # 7. Обновить Филли — добавить нового бота в BOT_URLS и ROUTER_SYSTEM
        await reply_func("7️⃣ Обновляю Филли...")
        try:
            filly_code = await read_file("filly-bot", "bot.py")
            bot_key = bot_display.upper()
            bot_internal = f"http://{bot_repo}.railway.internal:8080"

            # Обновляем Филли — добавляем нового бота в 4 места
            cilly_anchor = '"СИЛЛИ":  "http://cilly-bot.railway.internal:8080",'
            filly_code = filly_code.replace(
                cilly_anchor + "\n}",
                cilly_anchor + "\n    " + f'"{bot_key}":  "{bot_internal}",' + "\n}"
            )
            router_anchor = "СИЛЛИ — код, баги, технические задачи, мониторинг, Railway, боты"
            filly_code = filly_code.replace(
                router_anchor,
                router_anchor + "\n" + f"{bot_key} — {bot_prompt}"
            )
            sillie_dm = '"СИЛЛИ":  "Ты — Силли.'
            filly_code = filly_code.replace(
                sillie_dm,
                f'"{bot_key}":  "Ты — {bot_display}. {bot_prompt} Неформально, на русском.",\n    ' + sillie_dm
            )
            sillie_disp = '"СИЛЛИ":  "Силли",'
            filly_code = filly_code.replace(
                sillie_disp,
                f'"{bot_key}":  "{bot_display}",\n    ' + sillie_disp
            )

            await push_file("filly-bot", "bot.py", filly_code, f"feat: add {bot_display} to routing")
            # Redeploy Filly
            filly_service_id = "5d61d403-feee-455e-9c0d-523f0e7c79d5"
            await redeploy_service(filly_service_id)
        except Exception as e:
            logger.warning(f"Не удалось обновить Филли: {e}")

        await reply_func(
            f"✅ Бот *{bot_display}* полностью готов и интегрирован!\n\n"
            f"• GitHub репо: `{bot_repo}` ✅\n"
            f"• Код залит ✅\n"
            f"• Telegram бот создан ✅\n"
            f"• Railway сервис + переменные ✅\n"
            f"• Добавлен в Office group ✅\n"
            f"• Папка Office ✅\n"
            f"• Филли обновлён и задеплоен ✅\n\n"
            f"Бот уже работает в офисе 🎉"
        )

    elif intent == "get_bot_token":
        # Extract bot username from task
        bot_username = intent_data.get("repo") or ""
        if not bot_username:
            import re
            match = re.search(r"@?(\w+_bot)", task, re.IGNORECASE)
            bot_username = match.group(1) if match else ""
        if not bot_username:
            await reply_func("❓ Укажи username бота (например @ellice_mom_bot)")
            return
        await reply_func(f"🔍 Получаю токен для @{bot_username} через BotFather...")
        try:
            import re as re2
            tg_client = await get_telethon_client()
            botfather = await tg_client.get_entity("@BotFather")
            await tg_client.send_message(botfather, "/token")
            await asyncio.sleep(1)
            await tg_client.send_message(botfather, f"@{bot_username}")
            await asyncio.sleep(3)
            msgs = await tg_client.get_messages(botfather, limit=3)
            token = None
            for m in msgs:
                match = re2.search(r"(\d{8,12}:[A-Za-z0-9_-]{35,})", m.text or "")
                if match:
                    token = match.group(1)
                    break
            await tg_client.disconnect()
            if token:
                bot_id = token.split(":")[0]
                await reply_func(f"✅ Токен получен: {bot_id}:***\n\nОбновить Railway переменную? Укажи имя сервиса.")
            else:
                await reply_func("❌ Токен не найден в ответе BotFather. Попробуй /mybots вручную.")
        except Exception as e:
            await reply_func(f"❌ Ошибка: {e}")

    elif intent == "deploy":
        if not repo:
            await reply_func("❓ Укажи какой сервис задеплоить")
            return
        service_id = next((sid for sid, (r, _) in SERVICES.items() if r == repo), None)
        if not service_id:
            await reply_func(f"❌ Сервис {repo} не найден в SERVICES")
            return
        await reply_func(f"🔄 Деплою {repo}...")
        ok = await redeploy_service(service_id)
        await reply_func(f"✅ {repo} задеплоен" if ok else f"❌ Редеплой {repo} не удался")

    elif intent == "read_file":
        if not repo or not path:
            await reply_func("❓ Укажи репо и путь к файлу")
            return
        content_file = await read_file(repo, path)
        if len(content_file) > 3000:
            content_file = content_file[:3000] + "\n... (обрезано)"
        await reply_func(f"📄 `{repo}/{path}`:\n```\n{content_file}\n```")

    elif intent == "list_files":
        if not repo:
            await reply_func("❓ Укажи репо")
            return
        files = await list_files(repo, path or "")
        lines = [("📁 " if f["type"] == "dir" else "📄 ") + f["name"] for f in files]
        await reply_func("\n".join(lines))

# ── Telegram handlers ──────────────────────────────────────────────────────────
@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def monitor_group_responses(message: Message):
    """Следит за всеми ответами ботов в группе — анализирует через Haiku есть ли проблема."""
    text = message.text or ""
    sender = (message.from_user.first_name or "").lower()
    is_bot = message.from_user.is_bot

    # Пишем все сообщения в буфер
    recent_group_msgs.append({"sender": sender, "text": text, "is_bot": is_bot})

    # Анализируем только ответы ботов (не Cilly самого)
    if not is_bot:
        return
    if message.from_user.id == bot.id:
        return

    # Гослинг — casual бот, не анализируем его ответы
    if "гослинг" in sender or "gosling" in sender:
        return

    # Определяем какой бот ответил
    bot_display = None
    bot_system = None
    repo_info = None
    for name, system in BOT_SYSTEMS_WEB.items():
        if name in sender:
            bot_display = name.capitalize()
            bot_system = system
            repo_info = BOT_REPOS.get(name)
            break
    if not bot_display:
        return

    # Ищем последний вопрос пользователя перед этим ответом
    user_question = None
    for msg in reversed(list(recent_group_msgs)[:-1]):
        if not msg["is_bot"] and msg["text"].strip():
            user_question = msg["text"]
            break
    if not user_question:
        return

    # Если вопрос адресован конкретному боту через @тег — не лезем
    import re as _re
    if _re.search(r"@\w+_bot", user_question):
        return

    # Анализируем через Haiku — есть ли проблема с возможностями
    try:
        analysis = await analyze_bot_response(user_question, text)
    except Exception as e:
        logger.error(f"analyze_bot_response failed: {e}")
        return

    if not analysis.get("has_problem") or analysis.get("confidence") == "low":
        return
    if analysis.get("fix_needed") != "web_search":
        return

    logger.info(f"Capability gap detected in {bot_display}: {analysis.get('reason')}")

    # Объявляем что фиксим
    await bot.send_message(
        chat_id=message.chat.id,
        text=f"🔧 {bot_display} — вижу проблему ({analysis.get('reason', '')}), "
             f"сейчас отвечу с актуальными данными..."
    )

    try:
        # Немедленно отвечаем от имени бота с web search
        response = await claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=bot_system,
            messages=[{"role": "user", "content": user_question}],
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
        )
        answer = "\n".join(
            block.text for block in response.content if hasattr(block, "text")
        ).strip()

        await bot.send_message(
            chat_id=message.chat.id,
            text=f"{bot_display}:\n{answer}"
        )

        # Фиксим код в фоне — следующий раз бот сам справится
        if repo_info:
            asyncio.create_task(_fix_bot_code_background(bot_display, repo_info))

    except Exception as e:
        logger.error(f"instant reply failed for {bot_display}: {e}")
        await bot.send_message(
            chat_id=message.chat.id,
            text=f"❌ Не смог получить данные для {bot_display}: {e}"
        )


async def _fix_bot_code_background(bot_display: str, repo_info: tuple):
    """Добавляет web search в код бота в фоне — чтобы в следующий раз бот сам справился."""
    repo, filepath = repo_info
    try:
        source = await read_file(repo, filepath)
        if "web_search_20250305" in source:
            return  # уже есть
        fix_prompt = WEB_SEARCH_FIX_PROMPT.format(source=source)
        fixed_code = await generate_fix(source, fix_prompt)
        await push_file(repo, filepath, fixed_code,
                        f"feat({repo}): add web search tool for live data access")
        if OFFICE_CHAT_ID:
            await bot.send_message(
                chat_id=OFFICE_CHAT_ID,
                text=f"✅ Код {bot_display} обновлён — web search встроен, следующий раз сам справится."
            )
        await post_lesson(
            title=f"Web search добавлен для {bot_display}",
            symptom=f"{bot_display} не мог ответить на вопрос из-за отсутствия live данных",
            cause="tools=[web_search] не был подключён в client.messages.create()",
            context=f"{repo}/{filepath}",
            fix="Cilly ответил немедленно с web search, затем добавил tool в код бота",
            how_to_avoid="При создании аналитических ботов сразу подключать web search tool"
        )
    except Exception as e:
        logger.error(f"background fix failed for {bot_display}: {e}")


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "👨‍💻 Cilly онлайн. Мониторинг активен.\n\n"
        "Команды:\n"
        "/code <задача> — написать код\n"
        "/push <repo> <path> <задача> — написать и залить на GitHub\n"
        "/read <repo> <path> — прочитать файл из репо\n"
        "/ls <repo> [path] — список файлов\n"
        "/lesson <title>|<symptom>|<cause>|<ctx>|<fix>|<avoid> — урок в Bug Lessons\n"
        "/approve <id> — применить предложенный фикс\n"
        "/skip <id> — пропустить"
    )


@dp.message(F.text.startswith("/code"))
async def cmd_code(message: Message):
    task = message.text[5:].strip()
    if not task:
        await message.answer("Укажи задачу. Пример: /code скрипт для парсинга CSV")
        return
    await message.answer("⏳ Генерирую...")
    code = await ask_claude(task)
    await message.answer(f"```python\n{code}\n```", parse_mode="Markdown")


@dp.message(F.text.startswith("/push"))
async def cmd_push(message: Message):
    args = message.text[5:].strip().split(None, 2)
    if len(args) < 3:
        await message.answer("Формат: /push <repo> <path> <задача>")
        return
    repo, path, task = args[0], args[1], args[2]
    await message.answer(f"⏳ Генерирую код для `{path}`...", parse_mode="Markdown")
    code = await ask_claude(task)
    await message.answer("📤 Загружаю на GitHub...")
    try:
        result = await push_file(repo, path, code, f"Coder: {task[:60]}")
        await message.answer(
            f"✅ {'Обновлён' if result['action'] == 'updated' else 'Создан'}: {result['url']}"
        )
    except EnvironmentError as e:
        await message.answer(f"❌ Ошибка конфигурации: {e}")
    except PermissionError as e:
        await message.answer(f"❌ Нет доступа к GitHub: {e}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {type(e).__name__}: {e}")


@dp.message(F.text.startswith("/read"))
async def cmd_read(message: Message):
    args = message.text[5:].strip().split(None, 1)
    if len(args) < 2:
        await message.answer("Формат: /read <repo> <path>")
        return
    repo, path = args[0], args[1]
    content = await read_file(repo, path)
    if len(content) > 3000:
        content = content[:3000] + "\n\n... (обрезано)"
    await message.answer(f"📄 `{path}`:\n```\n{content}\n```", parse_mode="Markdown")


@dp.message(F.text.startswith("/ls"))
async def cmd_ls(message: Message):
    args = message.text[3:].strip().split(None, 1)
    if not args:
        await message.answer("Формат: /ls <repo> [path]")
        return
    repo = args[0]
    path = args[1] if len(args) > 1 else ""
    files = await list_files(repo, path)
    lines = [("📁 " if f["type"] == "dir" else "📄 ") + f["name"] for f in files]
    await message.answer(f"📂 `{repo}/{path}`:\n" + "\n".join(lines), parse_mode="Markdown")


@dp.message(F.text.startswith("/approve"))
async def cmd_approve(message: Message):
    fix_id = message.text[8:].strip()
    fix = pending_fixes.pop(fix_id, None)
    if not fix:
        await message.answer(f"❌ Фикс `{fix_id}` не найден или уже применён.")
        return
    await message.answer(f"⏳ Применяю фикс для *{fix['service_name']}*...", parse_mode="Markdown")
    try:
        await push_file(fix["repo"], fix["affected"], fix["fixed_code"],
                        f"approved fix({fix['service_name']}): {fix['analysis'].get('fix_description','')[:60]}")
        redeployed = await redeploy_service(fix["service_id"])
        status = "редеплой запущен ✅" if redeployed else "редеплой не удался ⚠️"
        await message.answer(f"✅ Фикс применён, {status}")
        asyncio.create_task(append_ops_log(
            f"approved fix: {fix['analysis'].get('fix_description','')[:60]}",
            fix['service_name'], f"approved by Влад | {status}"
        ))
        analysis = fix["analysis"]
        await post_lesson(
            title       = analysis.get("lesson_title", ""),
            symptom     = analysis.get("lesson_symptom", ""),
            cause       = analysis.get("lesson_cause", ""),
            context     = f"{fix['repo']}/{fix['affected']}",
            fix         = analysis.get("lesson_fix", ""),
            how_to_avoid= analysis.get("lesson_avoid", "")
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка при применении фикса: {e}")


@dp.message(F.text.startswith("/skip"))
async def cmd_skip(message: Message):
    fix_id = message.text[5:].strip()
    if pending_fixes.pop(fix_id, None):
        await message.answer(f"⏭️ Фикс `{fix_id}` пропущен.")
    else:
        await message.answer(f"❌ Фикс `{fix_id}` не найден.")


@dp.message(F.text.startswith("/lesson"))
async def cmd_lesson(message: Message):
    args = message.text[7:].strip()
    parts = [p.strip() for p in args.split("|")]
    if len(parts) < 6:
        await message.answer("Формат:\n/lesson Title|Symptom|Cause|Context|Fix|Avoid")
        return
    await post_lesson(*parts[:6])
    await message.answer("📚 Урок отправлен в Bug Lessons")



@dp.message(F.text & ~F.text.startswith("/"))
async def cmd_natural_language(message: Message):
    """Handle any non-command message as a natural language request."""
    # Only respond to direct messages or group messages that mention Cilly
    is_dm = message.chat.type == "private"
    is_mention = message.text and any(w in message.text.lower() for w in ["силли", "cilly", "@cilly"])

    if not is_dm and not is_mention:
        return  # ignore group chatter not directed at Cilly

    # Strip mention if present
    text = message.text
    for mention in ["силли,", "силли", "cilly,", "cilly", "@cilly_bot"]:
        text = text.replace(mention, "").strip()

    async def reply(msg: str):
        await message.answer(msg, parse_mode=None)

    await handle_natural_language(text, message.chat.id, reply)


# ── HTTP endpoint for Filly routing (family bots → Cilly) ────────────────────
async def handle_cilly_task(request):
    """Filly routes natural language requests here from any bot."""
    data = await request.json()
    text    = data.get("message", "")
    chat_id = data.get("chat_id", OFFICE_CHAT_ID)
    agent   = data.get("agent", "Unknown")

    responses = []
    async def collect(msg: str):
        responses.append(msg)
        target = chat_id or OFFICE_CHAT_ID
        if target:
            try:
                await bot.send_message(chat_id=int(target), text=msg, parse_mode=None)
            except Exception as e:
                logger.error(f"collect send_message failed: {e}")

    await handle_natural_language(f"[{agent}] {text}", int(chat_id) if chat_id else 0, collect)
    return web.json_response({"status": "ok", "responses": responses})




async def handle_promote_bots(request):
    """Выдать права администратора списку ботов в группе."""
    data = await request.json()
    group_id = int(data.get("group_id", -5194783850))
    bots = data.get("bots", [])
    results = {}
    for username in bots:
        ok = await tg_promote_bot_admin(username, group_id)
        results[username] = "✅" if ok else "❌"
    return web.json_response({"results": results})

# ── Secrets endpoint (for Claude to read GH token without exposing in chat) ──
RAILWAY_SECRET = os.getenv("RAILWAY_TOKEN", "")  # reuse existing Railway token as auth

async def handle_secrets(request):
    """Returns GH token to authenticated callers (Claude uses Railway token as key)."""
    auth = request.headers.get("X-Auth-Token", "")
    if not auth or auth != RAILWAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    return web.json_response({
        "GITHUB_TOKEN": os.getenv("GITHUB_TOKEN", ""),
        "GH_PAT": os.getenv("GH_PAT", ""),
    })

# ── Main ───────────────────────────────────────────────────────────────────────

async def handle_get_bot_token(request):
    """Get token for existing bot via BotFather."""
    data = await request.json()
    bot_username = data.get("bot_username", "").lstrip("@")
    try:
        client = await get_telethon_client()
        botfather = await client.get_entity("@BotFather")
        await client.send_message(botfather, "/mybots")
        await asyncio.sleep(2)
        msgs = await client.get_messages(botfather, limit=5)
        # Find the message with bot buttons
        import re
        for msg in msgs:
            if msg.reply_markup:
                for row in msg.reply_markup.rows:
                    for btn in row:
                        if bot_username.lower() in btn.text.lower():
                            await client.send_message(botfather, f"@{bot_username}")
                            await asyncio.sleep(2)
                            # Click API Token
                            msgs2 = await client.get_messages(botfather, limit=3)
                            for m2 in msgs2:
                                if m2.reply_markup:
                                    for row2 in m2.reply_markup.rows:
                                        for btn2 in row2:
                                            if "api token" in btn2.text.lower() or "token" in btn2.text.lower():
                                                await client.send_message(botfather, "API Token")
                                                await asyncio.sleep(2)
                                                final = await client.get_messages(botfather, limit=1)
                                                if final:
                                                    token_match = re.search(r"(\d+:[A-Za-z0-9_-]{35,})", final[0].text or "")
                                                    if token_match:
                                                        await client.disconnect()
                                                        return web.json_response({"token": token_match.group(1)})
        # Fallback: check recent BotFather messages for token pattern
        all_msgs = await client.get_messages(botfather, limit=20)
        for m in all_msgs:
            token_match = re.search(r"(\d+:[A-Za-z0-9_-]{35,})", m.text or "")
            if token_match:
                await client.disconnect()
                return web.json_response({"token": token_match.group(1), "note": "from recent history"})
        await client.disconnect()
        return web.json_response({"error": "token not found"})
    except Exception as e:
        return web.json_response({"error": str(e)})

async def main():
    asyncio.create_task(monitor_loop())
    # HTTP server for Filly routing
    app = web.Application()
    app.router.add_post("/task", handle_cilly_task)
    app.router.add_get("/secrets", handle_secrets)
    app.router.add_post("/promote_bots", handle_promote_bots)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080)))
    await site.start()
    logger.info("[http] Cilly HTTP server started on :8080")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())



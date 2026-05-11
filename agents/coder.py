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

from shared.github_tools import push_file, read_file, list_files

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
}

bot    = Bot(token=BOT_TOKEN)
dp     = Dispatcher()
claude = AsyncAnthropic(api_key=ANTHROPIC_KEY)

# Буфер последних сообщений группы — чтобы найти оригинальный вопрос
from collections import deque
recent_group_msgs: deque = deque(maxlen=30)  # (sender, text, is_bot)

# Системные промпты каждого бота — для мгновенного ответа с web search
BOT_SYSTEMS_WEB = {
    "тилли": (
        "Ты — Тилли. Аналитик по трейдингу и крипторынкам. "
        "Используй web_search для получения актуальных цен, данных и новостей. "
        "Холодная голова, цифры важнее эмоций. Говоришь чётко — уровни, объёмы, тренды. "
        "Не даёшь советов купи/продай — даёшь анализ и сценарии. Неформально, на русском."
    ),
    "макс": (
        "Ты — Макс. Бизнес-ассистент. Используй web_search для актуальных данных о рынке, "
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
last_seen: dict = {}

# Дедупликация: hash ошибки → timestamp последнего анализа
# Одна и та же ошибка не анализируется повторно в течение ERROR_COOLDOWN секунд
ERROR_COOLDOWN = 3600  # 1 час
seen_errors: dict = {}  # {error_hash: timestamp}

# ── Prompts ───────────────────────────────────────────────────────────────────
CODER_PROMPT = """Ты — Кодер, агент AI-офиса. Пишешь чистый, рабочий Python код.
- Возвращай ТОЛЬКО код, без объяснений и markdown-блоков
- Код должен быть готов к запуску
- Добавляй комментарии внутри кода где нужно
Когда тебя просят объяснить — отвечай кратко и по делу."""

ANALYZER_PROMPT = """Ты — анализатор багов Python-ботов на Telegram/Railway.
Тебе дают фрагмент логов сервиса и исходный код файла.

Ответь ТОЛЬКО JSON без markdown:
{
  "is_bug": true/false,
  "confidence": "high"/"low",
  "bug_type": "crash|logic|config|network|unknown",
  "description": "что именно сломалось (1-2 предложения)",
  "affected_file": "путь к файлу который надо исправить или null",
  "fix_description": "что нужно изменить в коде (конкретно)",
  "lesson_title": "короткое название урока",
  "lesson_symptom": "симптом",
  "lesson_cause": "причина",
  "lesson_fix": "что сделали",
  "lesson_avoid": "как избежать"
}

confidence=high: явный crash, NameError, ImportError, SyntaxError, KeyError на старте — фиксить автоматически.
confidence=low: логические баги, неожиданное поведение, сетевые ошибки — спросить у владельца."""

FIXER_PROMPT = """Ты — Кодер. Тебе дают исходный код файла и описание бага.
Верни ТОЛЬКО исправленный код целиком, без объяснений и markdown-блоков.
Минимальные изменения — только то что нужно для фикса."""


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



INTENT_PROMPT = """Ты — диспетчер AI-офиса. Пользователь написал запрос на естественном языке.
Определи намерение и верни ТОЛЬКО JSON без markdown:
{
  "intent": "push_code|fix_bot|create_bot|deploy|read_file|list_files|answer",
  "repo": "<repo name or null>",
  "path": "<file path or null>",
  "task": "<чёткое описание задачи для кодера или ответа>",
  "confidence": "high|low"
}

Намерения:
- push_code: написать/изменить код и залить в репо
- fix_bot: исправить баг или поведение бота
- create_bot: создать нового бота с нуля
- deploy: передеплоить сервис
- read_file: прочитать файл из репо
- list_files: список файлов репо
- answer: просто ответить на вопрос, ничего не деплоить

Известные репо: billy-bot, tilly-bot, filly-bot, doctor-bot, milly-bot, ai-office-shared, logger-bot, office-dashboard.
Если репо не указан явно — определи по контексту (билли=billy-bot, тилли=tilly-bot, макс/милли=milly-bot, доктор=doctor-bot, филли=filly-bot, силли=ai-office-shared)."""

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
    cutoff = last_seen.get(service_id, 0)
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
async def ask_claude(prompt: str, system: str = CODER_PROMPT, model: str = "claude-opus-4-5") -> str:
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
    return await ask_claude(prompt, system=FIXER_PROMPT, model="claude-opus-4-5")


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
    affected    = analysis.get("affected_file") or main_file

    try:
        source_code = await read_file(repo, affected)
    except Exception as e:
        logger.error(f"Can't read {repo}/{affected}: {e}")
        return

    fixed_code = await generate_fix(source_code, fix_desc)

    if confidence == "high":
        # Автофикс
        await notify_office(
            f"🔧 Cilly нашёл баг в *{service_name}* и фиксит автоматически...\n\n"
            f"_{description}_"
        )
        try:
            await push_file(repo, affected, fixed_code, f"autofix({service_name}): {fix_desc[:60]}")
            redeployed = await redeploy_service(service_id)
            status = "редеплой запущен ✅" if redeployed else "редеплой не удался, пуш сделан ⚠️"
            await notify_office(f"✅ *{service_name}* — фикс запушен, {status}")
            await post_lesson(
                title       = analysis.get("lesson_title", description),
                symptom     = analysis.get("lesson_symptom", description),
                cause       = analysis.get("lesson_cause", ""),
                context     = f"{repo}/{affected}",
                fix         = analysis.get("lesson_fix", fix_desc),
                how_to_avoid= analysis.get("lesson_avoid", "")
            )
        except Exception as e:
            await notify_office(f"❌ Cilly не смог запушить фикс для *{service_name}*: {e}")
    else:
        # Неоднозначный — спрашиваем
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

# Фразы которые означают что боту не хватает инструмента
RESPONSE_ANALYZER_PROMPT = """Ты — анализатор качества ответов AI-агентов в Telegram чате.

Тебе дают: вопрос пользователя и ответ AI-агента.

Определи: есть ли РЕАЛЬНАЯ ПРОБЛЕМА С ВОЗМОЖНОСТЯМИ агента?

ПРОБЛЕМА — агент:
- Не может получить актуальные данные (цены, курсы, новости) и говорит об этом
- Отказывается отвечать из-за отсутствия инструмента
- Просит пользователя самому найти данные которые агент мог бы найти через web search

НЕ ПРОБЛЕМА — агент:
- Просит уточнить вопрос или предоставить данные (скрин, цифры)
- Отвечает по делу в рамках своих возможностей
- Даёт общий анализ без конкретики потому что нет конкретных данных от пользователя

Ответь ТОЛЬКО валидным JSON без markdown:
{
  "has_problem": true или false,
  "problem_type": "no_web_search" или "none",
  "fix_needed": "web_search" или "none",
  "confidence": "high" или "low",
  "reason": "одно предложение почему"
}"""


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
    "макс":   ("milly-bot",  "bot.py"),
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

                # Дедупликация: хэш первых 3 строк ошибки
                import hashlib
                error_signature = hashlib.md5("\n".join(error_logs[:3]).encode()).hexdigest()
                now = time.time()
                last_analysis = seen_errors.get(f"{service_id}:{error_signature}", 0)
                if now - last_analysis < ERROR_COOLDOWN:
                    logger.info(f"[monitor] skipping duplicate error in {repo} (cooldown)")
                    continue
                seen_errors[f"{service_id}:{error_signature}"] = now

                # Чистим старые записи чтобы dict не рос бесконечно
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



async def handle_natural_language(message_text: str, chat_id: int, reply_func):
    """Process any natural language request — detect intent and execute."""
    await reply_func("🧠 Разбираю запрос...")

    # Detect intent via Haiku (cheap)
    raw = await ask_claude(message_text, system=INTENT_PROMPT, model="claude-haiku-4-5-20251001")
    raw = raw.strip()
    start, end = raw.find("{"), raw.rfind("}") + 1
    if start != -1 and end > start:
        raw = raw[start:end]

    try:
        intent_data = json.loads(raw)
    except Exception:
        await reply_func("❌ Не смог разобрать запрос, попробуй переформулировать")
        return

    intent = intent_data.get("intent", "answer")
    repo   = intent_data.get("repo")
    path   = intent_data.get("path")
    task   = intent_data.get("task", message_text)

    logger.info(f"[nl] intent={intent} repo={repo} path={path}")

    if intent == "answer":
        answer = await ask_claude(message_text)
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
        await reply_func(
            f"🤖 Создание нового бота: *{task}*\n\n"
            f"⚙️ Этап 2 (BotFather + Railway pipeline) ещё в разработке.\n"
            f"Пока используй: /push <repo> <path> <задача>"
        )

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
        if chat_id and str(chat_id) != str(OFFICE_CHAT_ID):
            try:
                await bot.send_message(chat_id=chat_id, text=msg, parse_mode=None)
            except Exception:
                pass

    await handle_natural_language(f"[{agent}] {text}", int(chat_id) if chat_id else 0, collect)
    return web.json_response({"status": "ok", "responses": responses})


# ── Main ───────────────────────────────────────────────────────────────────────
async def main():
    asyncio.create_task(monitor_loop())
    # HTTP server for Filly routing
    app = web.Application()
    app.router.add_post("/task", handle_cilly_task)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080)))
    await site.start()
    logger.info("[http] Cilly HTTP server started on :8080")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())



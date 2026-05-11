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

MONITOR_INTERVAL = 300  # секунд между проверками логов

# Railway service_id → (repo_name, main_file)
SERVICES = {
    "3319eabd-9f52-4f3f-8913-71d49de1afab": ("logger-bot",       "bot.py"),
    "367e25d7-2f7e-4b1e-b5e9-0e2c5e3b4a1d": ("tilly-bot",        "bot.py"),
    "5d61d403-d74a-4f73-b5b5-91ae35f7d3c8": ("filly-bot",        "bot.py"),
    "d949c4d2-8b3e-4f1a-b2c7-1e5d9f3a2b8c": ("doctor-bot",       "bot.py"),
    "db277aff-3c4e-4b2f-a1d8-2f6e8c5b9d3a": ("milly-bot",        "bot.py"),
    "3dfc7336-1a2b-4c3d-8e9f-5b6a7c8d9e0f": ("office-dashboard", "main.py"),
}

bot    = Bot(token=BOT_TOKEN)
dp     = Dispatcher()
claude = AsyncAnthropic(api_key=ANTHROPIC_KEY)

# Хранит pending-фиксы ожидающие /approve: {fix_id: fix_data}
pending_fixes: dict = {}

# Последние seen timestamps логов по сервису чтобы не дублировать
last_seen: dict = {}

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
    data = await railway_query("""
        query($id: String!) {
          deployments(input: { serviceId: $id }) {
            edges { node { id status createdAt } }
          }
        }
    """, {"id": service_id})
    edges = data.get("data", {}).get("deployments", {}).get("edges", [])
    if not edges:
        return []
    latest_id = edges[0]["node"]["id"]

    log_data = await railway_query("""
        query($id: String!) {
          deploymentLogs(deploymentId: $id) { message timestamp }
        }
    """, {"id": latest_id})
    logs = log_data.get("data", {}).get("deploymentLogs", [])

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
        """, {"serviceId": service_id, "environmentId": "production"})
        return "errors" not in data
    except Exception as e:
        logger.error(f"redeploy failed for {service_id}: {e}")
        return False


# ── Claude helpers ─────────────────────────────────────────────────────────────
async def ask_claude(prompt: str, system: str = CODER_PROMPT) -> str:
    response = await claude.messages.create(
        model="claude-opus-4-5",
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
    raw = await ask_claude(prompt, system=ANALYZER_PROMPT)
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
    return await ask_claude(prompt, system=FIXER_PROMPT)


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

                logger.info(f"[monitor] found {len(error_logs)} error lines in {repo}")

                # Читаем исходник
                try:
                    source_code = await read_file(repo, main_file)
                except Exception:
                    source_code = "# файл не удалось прочитать"

                analysis = await analyze_logs(repo, error_logs, source_code)

                if analysis.get("is_bug"):
                    await handle_bug(service_id, repo, repo, main_file, analysis)

            except Exception as e:
                logger.error(f"[monitor] error checking {repo}: {e}")

        await asyncio.sleep(MONITOR_INTERVAL)


# ── Telegram handlers ──────────────────────────────────────────────────────────
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


# ── Main ───────────────────────────────────────────────────────────────────────
async def main():
    asyncio.create_task(monitor_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

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
from agents.weekly_report import register_weekly_handlers

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, MessageReactionUpdated
from aiogram.filters import CommandStart
from anthropic import AsyncAnthropic
import redis.asyncio as aioredis
from ai_office_shared.shared.logging import log_event, read_logs

from shared.github_tools import (
    push_file, read_file, list_files, create_repo,
    create_branch, push_file_to_branch, create_pull_request, merge_pull_request, get_pr_by_url,
)
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
BOT_TOKEN       = os.environ.get("CODER_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN", "")
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_KEY") or ""
LESSONS_CHAT_ID = os.getenv("LESSONS_CHAT_ID")
OFFICE_CHAT_ID  = os.getenv("OFFICE_CHAT_ID")

# Ollama — локальная модель для лёгких задач (Haiku-tier classification)
OLLAMA_HOST     = os.getenv("OLLAMA_HOST", "").strip().rstrip("/\\")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "qwen3:8b")
OLLAMA_ENABLED  = os.getenv("OLLAMA_ENABLED", "").lower() in ("1", "true", "yes")
RAILWAY_TOKEN   = os.getenv("RAILWAY_TOKEN_VLAD") or os.getenv("RAILWAY_TOKEN")  # VLAD-token приоритет (audit fix)
RAILWAY_PROJECT = "271b40b7-199a-429a-88ef-ca417f26a638"
RAILWAY_ENV_ID  = "2efaaf60-ba39-492c-bf86-007fd505493f"  # BUILD:20260518-1803
GITHUB_USER     = "unperson22-alt"
LESSONS_FILE    = "lessons/lessons.json"

MONITOR_INTERVAL   = 300  # секунд между проверками логов
TEMPLATE_BOTS_FILE = "shared/template_bots.json"  # реестр ботов созданных по шаблону

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
    "5533bc5f-24aa-4079-903b-50bcde4cdd01": ("pilly-bot",         "bot.py"),
    "92f70bbb-70ea-474c-be0d-5cc1c9bd8f4e": ("kriss-bot",        "bot.py"),
    "a5e37cc4-0a9f-4700-b6d3-d39b958ce0cb": ("villy-bot",         "bot.py"),
    "ed03c9d3-e83f-4675-9f0a-a4d4fc622365": ("gosling-bot",       "bot.py"),  # был пропущен
}

bot    = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
_GLOBAL_BOT = bot  # глобальная ссылка для использования в handlers
dp     = Dispatcher()
# Lazy init — создаём при первом вызове чтобы не падать при старте без ключа
_claude_client = None
def get_claude():
    global _claude_client
    # Всегда читаем свежо — ключ мог появиться после старта
    key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_KEY") or ""
    if not key:
        raise ValueError("ANTHROPIC_API_KEY не задан. Добавь в Railway Variables.")
    # Пересоздаём клиент если ключ изменился
    if _claude_client is None:
        _claude_client = AsyncAnthropic(api_key=key)
    return _claude_client
claude = None  # инициализируется через get_claude()

# Буфер последних сообщений группы — чтобы найти оригинальный вопрос
from collections import deque
recent_group_msgs: deque = deque(maxlen=30)  # (sender, text, is_bot)

# Redis — персистентная дедупликация seen_errors и last_seen
REDIS_URL = os.getenv("REDIS_URL", "")
_redis: aioredis.Redis | None = None
_redis_last_attempt: float = 0
REDIS_RETRY_INTERVAL = 30  # секунд между попытками переподключения
_office_decisions: list = []  # правила office:decisions из Redis

async def get_redis() -> aioredis.Redis | None:
    global _redis, _redis_last_attempt
    if _redis is not None:
        # Проверяем что соединение живое
        try:
            await _redis.ping()
            return _redis
        except Exception:
            logger.warning("Redis connection lost, will retry")
            _redis = None

    if not REDIS_URL:
        return None

    now = time.time()
    if now - _redis_last_attempt < REDIS_RETRY_INTERVAL:
        return None  # cooldown — не спамим попытками
    _redis_last_attempt = now

    try:
        _redis = aioredis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
            retry_on_timeout=True,
        )
        await _redis.ping()
        logger.info("Redis connected successfully")
    except Exception as e:
        logger.warning(f"Redis unavailable: {e}")
        _redis = None
    return _redis


# ── FEEDBACK LOOP: msg owner mapping + reactions classification ─────────────
BOT_NAME_LOWER = "силли"  # Redis-ключ как в /metrics Фили
REACTION_UP    = {"👍", "❤️", "🔥", "🥰", "👏", "🎉", "🤩", "🙏"}
REACTION_DOWN  = {"👎", "💩", "🤬", "🤮", "😢"}

async def remember_my_message(msg):
    """Маркер 'это сообщение Силли' для последующего учёта реакций.
    Принимает aiogram Message или None (если send_message упал — silently no-op)."""
    if not msg or not getattr(msg, "message_id", None):
        return
    r = await get_redis()
    if r is None:
        return
    try:
        await r.setex(
            f"office:msg:{msg.chat.id}:{msg.message_id}",
            86400 * 14,
            BOT_NAME_LOWER,
        )
    except Exception as e:
        logger.warning(f"remember_my_message failed: {e}")

# ── Office Decisions (office:decisions Redis key) ────────────────────────────
DECISIONS_KEY = "office:decisions"

DEFAULT_DECISIONS = {
    "_meta": {"key": "office:decisions", "updated": "2026-05"},
    "rules": [
        {"id": "D001", "do_not": "Авто-фиксить TelegramConflictError при рестарте",
         "because": "Это норма при deployment. Не баг — не трогать."},
        {"id": "D002", "do_not": "Деплоить ≥2 ботов без паузы Силли через ollama_switch.py pause",
         "because": "Deployment-шум триггерит Силли на ложные тревоги."},
        {"id": "D003", "do_not": "Предлагать PixiJS для анимации дашборда",
         "because": "Отклонено — переход на Phaser.js. Phaser лучше для персонажной анимации."},
        {"id": "D004", "do_not": "Слать боту сообщения напрямую, минуя Филли",
         "because": "Филли — единственная точка входа. Прямые вызовы ломают роутинг."},
        {"id": "D005", "do_not": "Авто-фиксить баг если он уже фиксился 3+ раза с одним решением",
         "because": "Повторяющийся баг с одинаковым фиксом = системная проблема. Эскалировать Владу."},
        {"id": "D006", "do_not": "Создавать единую БД всех изменений для system prompt",
         "because": "Рост БД → токены → давление на контекст. Решение: SYSTEM_STATE.md + office:decisions."},
    ]
}

async def init_office_decisions():
    """Загружает office:decisions из Redis при старте.
    Если ключ не найден — создаёт из DEFAULT_DECISIONS."""
    global _office_decisions
    r = await get_redis()
    if not r:
        logger.warning("[decisions] Redis недоступен, office:decisions не загружен")
        _office_decisions = DEFAULT_DECISIONS["rules"]
        return
    try:
        raw = await r.get(DECISIONS_KEY)
        if raw:
            data = json.loads(raw)
            _office_decisions = data.get("rules", [])
            logger.info(f"[decisions] Загружено {len(_office_decisions)} правил из Redis")
        else:
            # Первый запуск — инициализируем дефолтными правилами
            await r.set(DECISIONS_KEY, json.dumps(DEFAULT_DECISIONS, ensure_ascii=False))
            _office_decisions = DEFAULT_DECISIONS["rules"]
            logger.info(f"[decisions] office:decisions создан в Redis ({len(_office_decisions)} правил)")
    except Exception as e:
        logger.error(f"[decisions] Ошибка при загрузке: {e}")
        _office_decisions = DEFAULT_DECISIONS["rules"]


def _check_decisions(context: str) -> dict | None:
    """Проверяет контекст против office:decisions.
    Возвращает первое совпавшее правило или None."""
    ctx_lower = context.lower()
    for rule in _office_decisions:
        keywords = rule.get("do_not", "").lower().split()
        matches = sum(1 for kw in keywords if len(kw) > 3 and kw in ctx_lower)
        if matches >= 2:
            return rule
    return None


async def add_office_decision(rule_id: str, do_not: str, because: str) -> bool:
    """Добавляет новое правило в office:decisions (Redis + in-memory)."""
    global _office_decisions
    import datetime
    new_rule = {"id": rule_id, "do_not": do_not, "because": because,
                "added": datetime.datetime.now().strftime("%Y-%m")}
    try:
        r = await get_redis()
        if r:
            raw = await r.get(DECISIONS_KEY)
            data = json.loads(raw) if raw else {"rules": []}
            data["rules"].append(new_rule)
            await r.set(DECISIONS_KEY, json.dumps(data, ensure_ascii=False))
        _office_decisions.append(new_rule)
        logger.info(f"[decisions] Добавлено правило {rule_id}: {do_not}")
        return True
    except Exception as e:
        logger.error(f"[decisions] Ошибка записи: {e}")
        return False


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

# ── CC-like subagent: многофайловый рефактор через Sonnet ──────────────────

# Хранит pending PR-approve: {pr_id: {repo, pr_number, branch}}
pending_prs: dict = {}

async def multi_file_refactor(
    task: str,
    file_specs: list[dict],   # [{repo, path}]
    branch_suffix: str = "",
) -> dict:
    """
    CC-like многофайловый рефактор через Sonnet API.

    Алгоритм:
    1. Скачивает все файлы через GitHub API
    2. Один вызов Sonnet: задача + полный контекст всех файлов
    3. Sonnet возвращает JSON {files: [{repo, path, content, reason}]}
    4. Создаёт ветку cc/{timestamp}-{suffix} в каждом затронутом репо
    5. Пушит все изменённые файлы в ветки
    6. Создаёт PR в каждом репо
    7. Возвращает список PR-ов для /approve_pr

    file_specs: [{repo: "billy-bot", path: "bot.py"}, ...]
    """
    import time as _time
    ts = int(_time.time())
    branch = f"cc/{ts}" + (f"-{branch_suffix[:20]}" if branch_suffix else "")

    # Шаг 1: скачиваем все файлы
    files_content = []
    for spec in file_specs:
        try:
            content = await read_file(spec["repo"], spec["path"])
            files_content.append({
                "repo": spec["repo"],
                "path": spec["path"],
                "content": content,
            })
        except Exception as e:
            logger.warning(f"[cc] read_file failed {spec['repo']}/{spec['path']}: {e}")

    if not files_content:
        return {"error": "Не удалось прочитать ни одного файла"}

    # Шаг 2: формируем контекст для Sonnet
    files_block = ""
    for f in files_content:
        files_block += f"\n\n### {f['repo']}/{f['path']}\n```python\n{f['content']}\n```"

    system_prompt = """Ты — инструмент рефакторинга кода. Твоя задача: внести конкретные изменения в предоставленные файлы согласно заданию.

Отвечай ТОЛЬКО валидным JSON без markdown-обёртки:
{
  "files": [
    {
      "repo": "имя-репо",
      "path": "path/to/file.py",
      "content": "полный новый контент файла",
      "reason": "что изменено и почему"
    }
  ],
  "summary": "краткое описание всех изменений"
}

ПРАВИЛА:
- Включай ТОЛЬКО файлы с реальными изменениями
- content — полный файл, не diff
- Сохраняй всю существующую логику, меняй только то что нужно
- Не меняй отступы, форматирование и стиль без необходимости"""

    user_msg = f"ЗАДАЧА: {task}\n\nФАЙЛЫ:{files_block}"

    # Шаг 3: вызов Sonnet
    try:
        response = await get_claude().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text.strip()
        # Очищаем от markdown если есть
        if "```" in raw:
            parts = raw.split("```")
            for p in parts:
                p = p.strip().lstrip("json").strip()
                if p.startswith("{"):
                    raw = p
                    break
        result = json.loads(raw)
    except Exception as e:
        return {"error": f"Sonnet call failed: {e}"}

    changed_files = result.get("files", [])
    if not changed_files:
        return {"error": "Sonnet не вернул изменений", "summary": result.get("summary", "")}

    # Шаг 4-6: создаём ветки и пушим файлы, создаём PR
    repos_touched = {}
    for cf in changed_files:
        repo = cf["repo"]
        if repo not in repos_touched:
            repos_touched[repo] = []
        repos_touched[repo].append(cf)

    created_prs = []
    errors = []

    for repo, repo_files in repos_touched.items():
        try:
            await create_branch(repo, branch)
            for cf in repo_files:
                await push_file_to_branch(
                    repo, cf["path"], cf["content"],
                    f"cc: {task[:60]}",
                    branch,
                )
            pr = await create_pull_request(
                repo,
                title=f"[CC] {task[:60]}",
                body=f"**Задача:** {task}\n\n**Изменения:**\n" +
                     "\n".join(f"- `{cf['path']}`: {cf.get('reason','')}" for cf in repo_files) +
                     f"\n\n**Summary:** {result.get('summary','')}\n\n_Создано Силли через CC-subagent_",
                head_branch=branch,
            )
            created_prs.append({"repo": repo, "pr": pr, "files": len(repo_files)})
            logger.info(f"[cc] PR created: {repo} #{pr['number']}")
        except Exception as e:
            errors.append(f"{repo}: {e}")
            logger.error(f"[cc] failed for {repo}: {e}")

    return {
        "branch": branch,
        "prs": created_prs,
        "errors": errors,
        "summary": result.get("summary", ""),
        "changed_files": len(changed_files),
    }



# Последние seen timestamps логов по сервису чтобы не дублировать
last_seen: dict = {}  # fallback in-memory (Redis preferred)

# Дедупликация: hash ошибки → timestamp последнего анализа
# Персистентно хранится в Redis; fallback на in-memory seen_errors при недоступности Redis
ERROR_COOLDOWN = 3600  # 1 час
seen_errors: dict = {}  # in-memory fallback

# История DM разговоров с Владом — чтобы Силли помнил контекст
dm_history: dict = {}   # {user_id: [{role, content}, ...]}
DM_HISTORY_MAX = 20     # последних сообщений

# ── Prompts ───────────────────────────────────────────────────────────────────
CODER_PROMPT = """Python-кодер AI-офиса. Пиши ТОЛЬКО готовый к запуску код без markdown-обёртки.\n\nСтандарты:\n— Используй async/await везде где возможно\n— Error handling: try/except с конкретными исключениями, не голый except\n— Логирование через logger, не print\n— Env переменные через os.getenv с fallback\n— Railway/aiogram/httpx — знаешь эти либы хорошо\n\nКомментарии внутри кода. Объяснения — только если спросят, кратко."""

CHAT_PROMPT = """Ты — Силли, технический мозг AI-офиса Влада. Построена на Claude, думаешь и отвечаешь как Claude — умно, прямо, без воды.

== КАК ТЫ ДУМАЕШЬ И ОТВЕЧАЕШЬ ==

СТИЛЬ:
— Коротко и конкретно. Никакой воды, никаких "конечно!", "отличный вопрос!", лишних предисловий.
— Неформально, по-русски. Как умный коллега, не как корпоративный чат-бот.
— Если задача понятна — делаешь, не рассуждаешь вслух про процесс.
— ЗОЛОТОЕ ПРАВИЛО: не можешь сделать — молчи. Никаких объяснений в группу почему не получилось, никаких "⚠️ ВАЖНО:", никаких запросов данных в группу. Ошибки — только в ответ через /task.
— ЗАПРЕЩЕНО писать в группу: запросы данных, объяснения ограничений, "не удалось найти репозиторий", просьбы уточнить. Если что-то не получается — верни ошибку ТОЛЬКО через ответ на /task. В группу — МОЛЧАТЬ.

== RAILWAY IDs (используй всегда) ==
RAILWAY_TOKEN: из env RAILWAY_TOKEN_VLAD
awake-happiness: projectId=271b40b7-199a-429a-88ef-ca417f26a638, envId=2efaaf60-3568-4462-8b77-f4a7e3c65b49
  filly:   5d61d403-feee-455e-9c0d-523f0e7c79d5
  cilly:   efa6bd21-91d8-467f-8250-60f8a3853791
  billy:   b441ce93-9736-49b3-9b5d-d0c82e715b28
  tilly:   367e25d7-896d-4b68-a85d-9db4108ef1b2
  milly:   db277aff-6638-4b4a-970e-b016bd753608
  villy:   a5e37cc4-0c92-4c87-b4d1-f3e2a1d9c8b7
  gosling: ed03c9d3-e035-4a66-b823-6badb57085c5
  prophet: 9db4108e-f7b7-4f89-b7ba-3c2d1e0f9a8b
  kriss:   92f70bbb-1234-5678-90ab-cdef01234567
  mama:    fa7c87cf-abcd-efgh-ijkl-mnopqrstuvwx
marketing-dept: projectId=ed4c408f-29f4-481a-aff1-b5bdbe0fc62e, envId=e987a790-e2e5-47a7-89cb-b0e8a6e8c9b3
  marty: 8fb51207, nelli: (nelli-bot service), ray: (ray-bot service)
vietnam-bot: projectId=d538d675-e29a-4b5a-a1ae-39d36be06c1d, envId=f2498bbf-c5e0-4cb3-b4a9-8f3d2a1e9c7b
ВАЖНО: variableCollectionUpsert требует специальные права — наш токен НЕ ИМЕЕТ их. Не пытаться.
— ЧИСТОТА: после выполнения задачи с промежуточными статусами в группе — подчищаешь свои служебные сообщения (⏸, ▶️, 🤖, прогресс-апдейты). В группе остаётся только финальный результат.
— Если что-то неясно — задаёшь ОДИН уточняющий вопрос, не несколько.
— Честна: если не знаешь — говоришь об этом прямо.

МЫШЛЕНИЕ:
— Сначала понимаешь задачу полностью, потом отвечаешь.
— Видишь несколько уровней проблемы: симптом → причина → системный контекст.
— Предлагаешь лучшее решение, а не просто выполняешь буквально.
— Замечаешь побочные эффекты и предупреждаешь о них.

ТЕХНИЧЕСКИЕ РЕШЕНИЯ:
— Пишешь рабочий код сразу. Без markdown обёрток, без "вот пример".
— Минимальные изменения для максимального эффекта. Не переписываешь то что работает.
— Сохраняешь стиль оригинального кода при правках.
— Error handling через конкретные исключения, не голый except.
— Async/await где нужно, логирование через logger.

== СТРУКТУРА ОФИСА (знаешь наизусть) ==

РЕПОЗИТОРИИ (GitHub: unperson22-alt):
• ai-office-shared — твой репо, agents/coder.py — твой код
• filly-bot/bot.py — РОУТЕР. Здесь регистрируются все боты:
  - BOT_URLS, ROUTER_SYSTEM, DM_AGENT_SYSTEMS, _name_map
• Остальные: billy-bot, tilly-bot, milly-bot, dilly-bot(doctor), mama-bot(эллис), pilly-bot, villy-bot, prophet-bot, gosling-bot, tilly-trader, kriss-bot

КАК ДОБАВИТЬ ВНЕШНЕГО БОТА В ОФИС:
1. filly-bot/bot.py → BOT_URLS + ROUTER_SYSTEM + DM_AGENT_SYSTEMS + _name_map
2. Telegram: добавить в офис-группу + папку Office
3. Создать Telegram-группу если нужна

TELEGRAM (Telethon функции в коде):
• tg_create_group, tg_add_peer_to_folder, tg_add_bot_to_group, tg_promote_bot_admin
• Офис-группа: -5194783850 | Bug Lessons: -5197140411

RAILWAY: проект 271b40b7, env 2efaaf60. Ключи в env.
GitHub: read_file/push_file из shared.github_tools.
SYSTEM_STATE.md в ai-office-shared — читай в начале сложных задач.
office:decisions в Redis — твои ограничения.

== ПРАВИЛО ==
НИКОГДА не спрашивай "какой репо" или "где конфиг" — ты знаешь структуру.
Для ВНЕШНЕГО бота — только: username + URL + описание одной фразой.
Отвечай кратко. Если задача — делай, не рассуждай долго.

---

ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА ВЫПОЛНЕНИЯ ЗАДАЧ:

1. ПЛАН → ДЕЙСТВИЕ БЕЗ ОСТАНОВКИ
   Перед любым действием составь план ВНУТРЕННЕ, но не пиши его в ответ как текст.
   Сразу выполняй: пуш в GitHub, отправка сообщения, изменение файла — без предварительного вывода плана.
   ЗАПРЕЩЕНО: писать блок кода в ответе вместо выполнения. Если можешь выполнить — выполни.

2. ВЕРИФИКАЦИЯ ПОСЛЕ ДЕЙСТВИЯ
   После каждого GitHub push — перечитай файл через GET и убедись что содержимое записалось верно. Никогда не пиши "Готово" без проверки результата.

3. КОНТЕНТ БЕРЁТСЯ ИЗ ДИАЛОГА, НЕ ПРИДУМЫВАЕТСЯ
   Если пользователь дал конкретный текст — используй ровно его. Не перефразируй, не заменяй своими словами. Если контент не найден в диалоге — спроси, не генерируй из головы.

4. НЕ СПРАШИВАЙ "ДЕЛАТЬ?" НА ЧЁТКУЮ ИНСТРУКЦИЮ
   Если задача однозначна — выполняй сразу. Уточняй только если инструкция реально неполная.

5. НЕ ПИШИ В ОФИС-ГРУППУ ИЗ /task ОБРАБОТЧИКОВ
   Если задача пришла через /task endpoint — статусы и ответы возвращаются ТОЛЬКО в JSON-ответе.
   
ПРАВИЛО ВЫВОДА: технические результаты (аудиты, таблицы, диагностика, списки) — отправляй ТОЛЬКО в личку user_id=391077101. В офисную группу (-5194783850) ТОЛЬКО: алерты о падениях, краткий ежедневный аудит, еженедельный отчёт."""

ANALYZER_PROMPT = """Анализатор багов Python/Telegram/Railway. JSON без markdown:
{"is_bug":bool,"confidence":"high|low","bug_type":"crash|logic|config|network|unknown","description":"1-2 предл","affected_file":"path|null","fix_description":"конкретно","lesson_title":"","lesson_symptom":"","lesson_cause":"","lesson_fix":"","lesson_avoid":""}
high=явный crash/NameError/ImportError/SyntaxError/KeyError→автофикс. low=логика/сеть→спросить."""

FIXER_PROMPT = """Фиксер Python кода. Верни ТОЛЬКО полный исправленный файл целиком. Минимум изменений — только то что нужно для фикса. Сохраняй стиль оригинала. Без markdown, без объяснений."""


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
{"intent":"push_code|fix_bot|create_bot|add_external_bot|get_bot_token|deploy|read_file|list_files|redis_query|dev_task|answer","repo":"repo_name_or_null","path":"file_path_or_null","task":"task_description","confidence":0.0-1.0}

ГЛАВНОЕ ПРАВИЛО — различай вопрос и команду:
- ВОПРОС о процессе ("как создать бота?", "что нужно для деплоя?", "какой стек?", "как задеплоить?", "с чего начать?") → intent=answer
- КОМАНДА к действию ("создай бота", "задеплой", "залей код", "исправь баг") → соответствующий intent
Сигналы вопроса: как, какой, какие, что такое, зачем, почему, расскажи, объясни, с чего начать, какие шаги
Сигналы команды: создай, сделай, залей, задеплой, исправь, добавь, зарегистрируй

push_code=залить/обновить код, fix_bot=исправить баг, create_bot=ЯВНАЯ команда создать нового бота (не вопрос!), add_external_bot=подключить внешнего бота, get_bot_token=зарегистрировать в BotFather, deploy=задеплоить, read_file=прочитать файл, list_files=список файлов, redis_query=запрос к Redis, post_lessons=прочитать lessons.json и отправить все уроки красиво в Bug Lessons группу (-5197140411), cleanup_group=удалить старые сообщения от ботов в группе через Telethon, cleanup_dm=удалить сообщения с ключами/секретами в личке (gsk_, GROQ, токен) через Telethon — ищет в диалоге с user_id=int(BOT_TOKEN.split(':')[0]) (сигналы: удали старые, почисти группу, удали сообщения до), send_group_message=отправить сообщение в Telegram-группу от имени бота (POST /post_raw {chat_id,text,bot_name} X-Auth-Token OFFICE_CHAT_ID=-5194783850 — выполнять ПРЯМО без генерации кода), edit_file=точечная замена строки в файле без чтения всего файла (сигналы: замени в файле, вставь после строки, patch, добавь в начало функции — когда указан repo+path+old+new), agentic_task=многошаговая задача из 2+ шагов: читай+делай, исправь+задеплой, залей+проверь, прочитай+перепиши. Сигналы: исправь и задеплой, залей код и задеплой, прочитай X и отправь, прочитай X и перепиши, пройдись по всем, для каждого, рефакторинг, аудит. ВАЖНО: если задача содержит И (исправить код И задеплоить) — это agentic_task. При чтении большого файла (bot.py 800+ строк) — не читать целиком в цикле, читать один раз и искать нужную функцию по имени, dev_task=делегировать задачу КОМАНДЕ разработки (Девви→Рикки→Тести→Секки→Скрибби). ТОЛЬКО когда речь о новой фиче/модуле/компоненте для продукта — НЕ о правке одного файла. Требует ВЫСОКОЙ уверенности (confidence>=0.85). Чёткие сигналы: "реализуй фичу", "разработай модуль", "напиши новый компонент", "сделай PR для", "задача для команды", "отдай команде", "dev-dept", "через цепочку". НЕЯСНЫЙ запрос ("сделай что-нибудь", "напиши функцию" без контекста) → confidence<0.85 → Силли переспрашивает. Если задача про правку существующего файла/бота — это push_code или agentic_task, НЕ dev_task. answer=ответить словами.
ВАЖНО redis_query: "прочитай Redis", "покажи quality", "health ботов", "office:*", "scan", "hgetall", "что в Redis" → redis_query.
ВАЖНО: "подключить бота", "добавить чужого бота" → add_external_bot, НЕ create_bot.
Репо: billy-bot,tilly-bot,filly-bot,dilly-bot,milly-bot,ai-office-shared,logger-bot,office-dashboard,mama-bot,gosling-bot,villy-bot,prophet-bot,kriss-bot,pilly-bot,doctor-bot,marketing-dept.
билли→billy, тилли→tilly, макс/милли→milly, доктор/дилли→dilly, филли→filly, силли→ai-office-shared."""


OPS_LOG_FILE = "logs/ops.md"

# ── Template bots registry ────────────────────────────────────────────────────

async def register_template_bot(repo: str, bot_name: str, system_prompt: str, service_id: str):
    """Регистрирует бота в реестре template_bots.json после создания."""
    try:
        raw = await read_file("ai-office-shared", TEMPLATE_BOTS_FILE)
        registry = json.loads(raw) if raw.strip() else []
        # Обновляем если уже есть, иначе добавляем
        existing = next((b for b in registry if b["repo"] == repo), None)
        if existing:
            existing.update({"bot_name": bot_name, "system_prompt": system_prompt, "service_id": service_id})
        else:
            registry.append({"repo": repo, "bot_name": bot_name, "system_prompt": system_prompt, "service_id": service_id})
        await push_file("ai-office-shared", TEMPLATE_BOTS_FILE,
                        json.dumps(registry, ensure_ascii=False, indent=2),
                        f"registry: add {repo}")
        logger.info(f"[template_registry] registered {repo}")
    except Exception as e:
        logger.error(f"register_template_bot failed: {e}")


async def update_all_template_bots(notify_func=None) -> str:
    """Перегенерирует bot.py для всех template-ботов по текущему BOT_TEMPLATE.
    Сохраняет их уникальный system_prompt и bot_name. Деплоит всех."""
    try:
        raw = await read_file("ai-office-shared", TEMPLATE_BOTS_FILE)
        registry = json.loads(raw) if raw.strip() else []
    except Exception as e:
        return f"❌ Не смог прочитать реестр: {e}"

    if not registry:
        return "ℹ️ Реестр пуст — нет ботов созданных по шаблону."

    results = []
    for bot in registry:
        repo         = bot["repo"]
        bot_name     = bot["bot_name"]
        system_prompt = bot["system_prompt"]
        service_id   = bot.get("service_id")

        try:
            new_code = BOT_TEMPLATE.format(bot_name=bot_name, system_prompt=system_prompt)
            await push_file(repo, "bot.py", new_code,
                            f"update(template): {bot_name} — batch template update")
            if service_id:
                await redeploy_service(service_id)
            results.append(f"✅ {bot_name} ({repo})")
            if notify_func:
                await notify_func(f"↻ {bot_name}...")
        except Exception as e:
            results.append(f"❌ {bot_name}: {e}")

    summary = f"🔄 Обновлено {len([r for r in results if r.startswith('✅')])}/{len(registry)} ботов:\n" + "\n".join(results)
    return summary


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
    """GraphQL-запрос к Railway API.
    Бросает RuntimeError если HTTP != 200 или в ответе есть errors.
    Это позволяет audit-коду отловить AUTH/PERMISSION ошибки явно
    вместо молчаливого "NO_DEPLOY" при data=null.
    """
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
        data = r.json()
        # GraphQL может вернуть HTTP 200 + {"data": null, "errors": [...]}
        # raise_for_status() это не поймает — проверяем явно
        if data.get("data") is None and data.get("errors"):
            msgs = "; ".join(e.get("message", "?") for e in data["errors"])
            raise RuntimeError(f"Railway GraphQL error: {msgs}")
        return data



async def _railway_is_available() -> bool:
    """Быстрая проверка доступности Railway API (timeout 8 сек)."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0)) as c:
            r = await c.post(
                "https://backboard.railway.com/graphql/v2",
                headers={"Authorization": f"Bearer {RAILWAY_TOKEN}", "Content-Type": "application/json"},
                json={"query": "{ me { id } }"},
            )
            return r.status_code == 200
    except Exception:
        return False


async def get_service_logs_via_redis(repo: str) -> list[str]:
    """
    Получить признаки проблем через Redis структурные логи (без Railway API).

    Возвращает список строк в формате совместимом с ERROR_PATTERNS — чтобы
    monitor_loop мог обработать их тем же путём что и Railway-логи.

    Логика детекта:
    - api_error за последние 2 часа → признак проблемы
    - message_received без response_sent в течение 5 мин → timeout/зависание
    - level=error любое событие → проблема
    """
    from ai_office_shared.shared.identity import canonical

    r = await get_redis()
    if not r:
        return []

    bot_name = repo.replace("-bot", "")
    bot_canon = canonical(bot_name)
    if not bot_canon:
        return []

    try:
        events = await read_logs(r, bot_canon, days=1, limit=100)
    except Exception as e:
        logger.warning(f"[redis-monitor] read_logs failed for {bot_canon}: {e}")
        return []

    if not events:
        return []

    import time as _time
    now = _time.time()
    TWO_HOURS = 7200
    FIVE_MIN  = 300

    synthetic_errors = []

    # Паттерн 1: явные api_error события
    api_errors = [e for e in events
                  if e.get("event") == "api_error" or e.get("level") == "error"]
    for ev in api_errors[:5]:
        ctx = ev.get("context", {})
        err_text = ctx.get("error", "") or ev.get("event", "error")
        synthetic_errors.append(f"ERROR {ev.get('ts','')} {bot_canon}: {err_text}")

    # Паттерн 2: message_received без парного response_sent (в окне 5 мин)
    received_ids = {}
    for ev in reversed(events):  # от старых к новым
        uid = ev.get("user_id")
        ts_str = ev.get("ts", "")
        if ev.get("event") == "message_received" and uid:
            received_ids[uid] = ts_str
        elif ev.get("event") == "response_sent" and uid in received_ids:
            del received_ids[uid]  # пара закрыта

    # Оставшиеся в received_ids — без ответа
    for uid, ts_str in list(received_ids.items())[:3]:
        synthetic_errors.append(
            f"ERROR {ts_str} {bot_canon}: message_received uid={uid} without response_sent — possible hang/crash"
        )

    if synthetic_errors:
        logger.info(f"[redis-monitor] {bot_canon}: {len(synthetic_errors)} synthetic errors from Redis")

    return synthetic_errors


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


# ── Ollama helper (silent fallback to Claude) ─────────────────────────────────
async def _try_ollama(prompt: str, system: str, timeout: float = 20.0) -> str | None:
    """Пробует локальную Ollama. Возвращает текст или None при любой ошибке."""
    if not (OLLAMA_ENABLED and OLLAMA_HOST):
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as cli:
            r = await cli.post(
                f"{OLLAMA_HOST}/api/chat",
                json={
                    "model": OLLAMA_MODEL,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    "stream": False,
                    "keep_alive": "30m",  # держим модель в RAM между циклами
                },
            )
            if r.status_code != 200:
                return None
            text = r.json().get("message", {}).get("content", "")
            return text or None
    except Exception as e:
        logger.info(f"Ollama unavailable, fallback to Claude: {e.__class__.__name__}: {e}")
        return None


# ── Claude helpers ─────────────────────────────────────────────────────────────
async def ask_claude(prompt: str, system: str = CODER_PROMPT, model: str = "claude-opus-4-6") -> str:
    # Haiku-tier (классификация/анализ) сначала пробует Ollama, fallback на Haiku
    if model == "claude-haiku-4-5-20251001":
        result = await _try_ollama(prompt, system)
        if result is not None:
            return result
    response = await get_claude().messages.create(
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
    return await ask_claude(prompt, system=FIXER_PROMPT, model="claude-opus-4-6")


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
        sent = await bot.send_message(chat_id=LESSONS_CHAT_ID, text=text)
        await remember_my_message(sent)
    except Exception as e:
        logger.error(f"post_lesson failed: {e}")
    # Save compact AI format to lessons.json in parallel
    asyncio.create_task(append_lesson_ai(title, symptom, cause, context, fix, how_to_avoid))
    r = await get_redis()
    if r:
        await log_event(r, BOT_NAME_LOWER, "lesson_saved",
                        title=title[:100])


async def notify_office(text: str):
    if not OFFICE_CHAT_ID:
        return
    try:
        sent = await bot.send_message(chat_id=OFFICE_CHAT_ID, text=text)
        await remember_my_message(sent)
    except Exception as e:
        logger.error(f"notify_office failed: {e}")


# ── Auto-fix pipeline ──────────────────────────────────────────────────────────
async def handle_bug(service_id: str, service_name: str, repo: str, main_file: str, analysis: dict):
    """Основная логика: автофикс или запрос подтверждения."""
    confidence  = analysis.get("confidence", "low")
    description = analysis.get("description", "")
    fix_desc    = analysis.get("fix_description", "")
    affected    = main_file  # Всегда используем файл из SERVICES, не доверяем LLM

    # Проверяем office:decisions — нет ли запрета на этот фикс
    if _office_decisions:
        combined = f"{description} {fix_desc} {service_name}"
        blocked = _check_decisions(combined)
        if blocked:
            await notify_office(
                f"⛔ Фикс заблокирован правилом {blocked['id']}:\n"
                f"Нельзя: {blocked['do_not']}\n"
                f"Причина: {blocked['because']}"
            )
            logger.info(f"[decisions] fix blocked by {blocked['id']} for {service_name}")
            return

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

# Kill-switch для аварийной остановки мониторинга (например, во время массовых деплоев)
# Поставь в Railway: CILLY_MONITOR_PAUSED=true → Cilly перестанет анализировать логи ботов
MONITOR_PAUSED = lambda: os.getenv("CILLY_MONITOR_PAUSED", "").lower() in ("1", "true", "yes")

# Паттерны которые НЕ являются багами — игнорируем
IGNORE_PATTERNS = [
    "Conflict: terminated by other getUpdates",  # нормально при редеплое
    "terminated by other getUpdates request",
    "make sure that only one bot instance",
    "NetworkError while getting Updates",        # временная сетевая ошибка
    "TimedOut",                                  # telegram timeout — не баг
    "DeprecationWarning",                        # предупреждение, не ошибка
    "httpx.ReadError",                           # сетевой сбой при polling — не баг
    "httpcore.ReadError",                        # то же
    "TelegramConflictError",                     # конфликт polling при рестарте
    "Failed to fetch updates",                   # временный сбой polling
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



# ── Daily audit ───────────────────────────────────────────────────────────────

HEALTH_URLS = {
    "pilly-bot":      "https://pilly-bot-production.up.railway.app/health",
    "logger-bot":     "https://logger-bot-production.up.railway.app/health",
    "office-dashboard": "https://office-dashboard-production-b571.up.railway.app/health",
}

async def run_daily_audit() -> str:
    """Полный аудит офиса: деплои, логи, health. Возвращает текст отчёта."""
    import datetime
    lines = []
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    lines.append(f"📋 Ежедневный аудит офиса — {ts}\n")

    # 1. Deployment status
    deploy_ok, deploy_fail = [], []
    for service_id, (repo, _) in SERVICES.items():
        try:
            data = await railway_query(
                """query($sid: String!) {
                     deployments(first:1, input:{serviceId:$sid}) {
                       edges { node { status } }
                     }
                   }""",
                {"sid": service_id}
            )
            deps = (data.get("data") or {}).get("deployments", {}).get("edges") or []
            status = deps[0]["node"]["status"] if deps else "NO_DEPLOY"
            name = repo
            if status == "SUCCESS":
                deploy_ok.append(name)
            else:
                deploy_fail.append(f"{name}:{status}")
        except RuntimeError as e:
            # GraphQL auth/permission error — критично, Railway API недоступен
            err_msg = str(e)
            if "Not Authorized" in err_msg or "Unauthorized" in err_msg:
                deploy_fail.append(f"{repo}:AUTH_ERROR")
            else:
                deploy_fail.append(f"{repo}:GQL_ERROR")
            logger.error(f"[audit] Railway API error for {repo}: {e}")
        except Exception as e:
            deploy_fail.append(f"{repo}:ERROR({type(e).__name__})")
            logger.error(f"[audit] deploy check failed for {repo}: {e}")

    if deploy_fail:
        lines.append(f"❌ Деплои упали: {', '.join(deploy_fail)}")
        # Auto-fix: пробуем передеплоить каждый упавший сервис
        for entry in deploy_fail:
            svc_name = entry.split(":")[0]  # repo name
            # Ищем service_id по имени в SERVICES
            svc_id = next(
                (sid for sid, (repo_n, _) in SERVICES.items() if repo_n == svc_name),
                None
            )
            if svc_id:
                logger.info(f"[audit] {svc_name} down — triggering redeploy")
                ok = await redeploy_service(svc_id)
                if ok:
                    lines.append(f"🔄 *{svc_name}* — редеплой запущен автоматически")
                    logger.info(f"[audit] auto-redeploy triggered for {svc_name}")
                else:
                    await notify_office(
                        f"⚠️ *{svc_name}* — редеплой не удался, нужен ручной разбор"
                    )
    else:
        lines.append(f"✅ Деплои ({len(deploy_ok)}): все SUCCESS")

    # 2. Health checks for HTTP services
    health_fail = []
    async with httpx.AsyncClient(timeout=10) as c:
        for name, url in HEALTH_URLS.items():
            try:
                r = await c.get(url)
                if r.status_code != 200:
                    health_fail.append(f"{name}:{r.status_code}")
            except Exception as e:
                health_fail.append(f"{name}:TIMEOUT")

    if health_fail:
        lines.append(f"❌ Health failed: {', '.join(health_fail)}")
    else:
        lines.append(f"✅ HTTP health ({len(HEALTH_URLS)}): все OK")

    # 3. Scan logs for new errors (last 2 hours)
    import time, hashlib
    cutoff_ts = time.time() - 7200  # 2 hours
    error_services = []
    IGNORE_LOG = [
        "Conflict: terminated by other getUpdates",
        "DeprecationWarning", "TimedOut", "NetworkError",
    ]
    for service_id, (repo, _) in SERVICES.items():
        try:
            logs = await get_service_logs(service_id)
            errs = [l for l in logs
                    if any(p in l for p in ["Error:", "Traceback", "CRITICAL", "KeyError"])
                    and not any(i in l for i in IGNORE_LOG)]
            if errs:
                error_services.append(f"{repo}({len(errs)})")
        except Exception:
            pass

    if error_services:
        lines.append(f"⚠️  Новые ошибки: {', '.join(error_services)}")
    else:
        lines.append("✅ Логи: ошибок за последние 2 часа нет")

    # 4. Bug lesson scan — ищем новые паттерны ошибок которых нет в lessons.json
    new_lesson_count = 0
    try:
        raw_lessons = await read_file("ai-office-shared", LESSONS_FILE)
        existing_lessons = json.loads(raw_lessons) if raw_lessons.strip() else []

        # Собираем все ошибки за сутки по всем сервисам
        all_errors: dict[str, list[str]] = {}
        for service_id, (repo, _) in SERVICES.items():
            try:
                logs = await get_service_logs(service_id)
                errs = [l for l in logs if any(p in l for p in ERROR_PATTERNS)
                        and not any(i in l for i in IGNORE_LOG)]
                if errs:
                    all_errors[repo] = errs
            except Exception:
                pass

        if all_errors:
            # Просим Haiku найти новые паттерны которых нет в known bugs
            errors_summary = "\n---\n".join(
                f"{repo}:\n" + "\n".join(errs[:10])
                for repo, errs in all_errors.items()
            )
            known_summary = json.dumps(
                [{"id": l.get("id"), "title": l.get("title"), "symptom": l.get("symptom","")} for l in existing_lessons],
                ensure_ascii=False
            )
            scan_prompt = (
                f"Known bug lessons:\n{known_summary}\n\n"
                f"Today's errors by service:\n{errors_summary}\n\n"
                f"Find errors that are NOT covered by known lessons. "
                f"For each new unique bug pattern return JSON array (max 3):\n"
                f'[{{"service":"...","title":"...","symptom":"...","cause":"...","fix":"...","avoid":"..."}}]\n'
                f"Return empty array [] if nothing new. JSON only, no markdown."
            )
            raw_new = await ask_claude(scan_prompt, system="Return only valid JSON array, no markdown.", model="claude-haiku-4-5-20251001")
            raw_new = raw_new.strip()
            s, e = raw_new.find("["), raw_new.rfind("]") + 1
            new_bugs = json.loads(raw_new[s:e]) if s != -1 and e > s else []

            for bug in new_bugs[:3]:
                await post_lesson(
                    title=bug.get("title", "Unknown bug"),
                    symptom=bug.get("symptom", ""),
                    cause=bug.get("cause", ""),
                    context=bug.get("service", ""),
                    fix=bug.get("fix", ""),
                    how_to_avoid=bug.get("avoid", "")
                )
                new_lesson_count += 1

    except Exception as e:
        logger.error(f"[daily_audit] bug scan failed: {e}")

    if new_lesson_count:
        lines.append(f"📚 Новых уроков записано: {new_lesson_count}")
    else:
        lines.append("📚 Новых паттернов багов не найдено")

    # 5. Итог
    lines.append("")
    status_icon = "🟢" if not deploy_fail and not health_fail and not error_services else "🟡"
    lines.append(f"{status_icon} Статус офиса: {'НОРМА' if status_icon == '🟢' else 'ТРЕБУЕТ ВНИМАНИЯ'}")

    return "\n".join(lines)


async def daily_audit_loop():
    """Запускать полный аудит дважды в сутки: 09:00 и 18:00 UTC."""
    import datetime
    logger.info("[daily_audit] loop started (09:00 + 18:00 UTC)")

    AUDIT_HOURS = [9, 18]  # утренний и вечерний аудит

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        # Ищем ближайший слот из AUDIT_HOURS
        target = None
        for hour in AUDIT_HOURS:
            candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if candidate > now:
                target = candidate
                break
        if target is None:
            # Все слоты сегодня прошли — берём первый завтра
            target = now.replace(hour=AUDIT_HOURS[0], minute=0, second=0, microsecond=0)
            target += datetime.timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        slot_label = "утренний" if target.hour == 9 else "вечерний"
        logger.info(f"[daily_audit] следующий аудит ({slot_label}) через {wait_seconds/3600:.1f}ч ({target.strftime('%d.%m %H:%M UTC')})")

        await asyncio.sleep(wait_seconds)

        try:
            report = await run_daily_audit()
            await notify_office(report)
            logger.info(f"[daily_audit] ✅ {slot_label} отчёт отправлен")
            await append_ops_log("daily_audit", "all_services", report[:300])
        except Exception as e:
            logger.error(f"[daily_audit] failed: {e}")
            await notify_office(f"⚠️ Аудит ({slot_label}) упал: {e}")

        await asyncio.sleep(60)  # небольшой отступ чтобы не запустить дважды



async def _deep_diagnose_and_escalate(
    repo: str,
    service_id: str,
    error_signature: str,
    error_logs: list[str],
    fix_count: int,
    redis_client,
):
    """
    Умная диагностика повторяющегося бага.
    Вместо немедленной эскалации — анализирует сам:
    1. Проверяет сколько других ботов имеют эту же сигнатуру
    2. Проверяет это деплой-шум или реальный баг
    3. Читает исходник + Redis логи + историю фиксов
    4. Просит Claude поставить диагноз
    5. Только если Claude не смог — эскалирует Владу с диагнозом
    """
    logger.info(f"[deep_diagnose] starting for {repo} sig={error_signature[:8]}")

    # ── 1. Проверяем сколько ботов имеют эту же сигнатуру ────────────────────
    affected_services = []
    if redis_client:
        async for key in redis_client.scan_iter(f"fix_count:*:{error_signature}"):
            svc = key.split(":")[1]
            count = int(await redis_client.get(key) or 0)
            affected_services.append((svc, count))

    systemic = len(affected_services) >= 3
    if systemic:
        # Та же ошибка в 3+ ботах = системный шум (деплой/сеть), не баг конкретного бота
        logger.info(f"[deep_diagnose] systemic noise: same sig in {len(affected_services)} services, skipping escalation")
        # Сбрасываем счётчики чтобы не эскалировать снова
        if redis_client:
            for svc, _ in affected_services:
                await redis_client.delete(f"fix_count:{svc}:{error_signature}")
        return  # Тихо, без эскалации

    # ── 2. Собираем контекст для глубокого анализа ───────────────────────────
    # 2a. Исходник бота
    source_code = "# не удалось прочитать"
    try:
        main_file = SERVICES.get(service_id, (None, "bot.py"))[1]
        source_code = await read_file(repo, main_file)
    except Exception:
        pass

    # 2b. Redis структурные логи
    redis_ctx = ""
    try:
        _r = await get_redis()
        if _r:
            from ai_office_shared.shared.identity import canonical
            bot_canon = canonical(repo.replace("-bot", ""))
            if bot_canon:
                events = await read_logs(_r, bot_canon, days=1, limit=30, level_filter=None)
                if events:
                    lines_out = []
                    for ev in events[:20]:
                        ts = ev.get("ts", "")[-8:]
                        lines_out.append(
                            f"[{ts}] {ev.get('level','?').upper()} "
                            f"{ev.get('event','?')} uid={ev.get('user_id','?')}"
                        )
                    redis_ctx = "\n--- Redis события (последние 20) ---\n" + "\n".join(lines_out)
    except Exception as _e:
        logger.warning(f"[deep_diagnose] redis ctx failed: {_e}")

    # 2c. Предыдущие попытки починки (ops.md)
    ops_ctx = ""
    try:
        raw_ops = await read_file("ai-office-shared", OPS_LOG_FILE)
        if raw_ops:
            # Ищем записи про этот репо
            relevant = [l for l in raw_ops.split("\n") if repo in l or error_signature[:8] in l]
            if relevant:
                ops_ctx = "\n--- История правок (ops.md) ---\n" + "\n".join(relevant[-10:])
    except Exception:
        pass

    full_context = (
        f"Ошибки из логов (последние {len(error_logs)}):\n"
        + "\n".join(error_logs[:10])
        + f"\n\nИсходник (первые 3000 символов):\n{source_code[:3000]}"
        + redis_ctx
        + ops_ctx
    )

    # ── 3. Глубокий анализ Claude (Sonnet — дороже, но для реальной диагностики) ──
    DEEP_ANALYSIS_PROMPT = """Ты — senior инженер AI-офиса. Этот баг уже встречался 3+ раза и стандартный фикс не помог.

Твоя задача — поставить ТОЧНЫЙ диагноз:
1. Что конкретно ломается (строка кода, функция, контракт)
2. Почему стандартный фикс не помог (симптом лечили, а не причину?)
3. Что нужно исправить РЕАЛЬНО (на уровне логики, не патч)
4. Можешь ли ты это исправить сам прямо сейчас?

Отвечай JSON без markdown:
{
  "root_cause": "точная причина в 1-2 предложениях",
  "why_fix_failed": "почему предыдущие попытки не помогли",
  "real_fix": "что нужно сделать на самом деле",
  "can_self_fix": true/false,
  "self_fix_action": "push_code|redeploy|config_change|null",
  "self_fix_details": "конкретные изменения если can_self_fix=true",
  "confidence": "high|medium|low",
  "escalate_reason": "null или причина почему нужен человек"
}"""

    try:
        raw = await ask_claude(
            f"Повторяющийся баг в {repo} (сигнатура {error_signature[:8]}, fix_count={fix_count}):\n\n{full_context}",
            system=DEEP_ANALYSIS_PROMPT,
            model="claude-sonnet-4-6",
        )
        raw = raw.strip()
        s, e = raw.find("{"), raw.rfind("}") + 1
        diagnosis = json.loads(raw[s:e]) if s != -1 else {}
    except Exception as ex:
        logger.error(f"[deep_diagnose] claude analysis failed: {ex}")
        diagnosis = {"can_self_fix": False, "confidence": "low", "escalate_reason": f"анализ упал: {ex}"}

    can_fix = diagnosis.get("can_self_fix", False)
    confidence = diagnosis.get("confidence", "low")
    root_cause = diagnosis.get("root_cause", "неизвестно")
    real_fix = diagnosis.get("real_fix", "")

    logger.info(f"[deep_diagnose] diagnosis: can_fix={can_fix} confidence={confidence} cause={root_cause[:60]}")

    # ── 4. Пробуем починить сам ───────────────────────────────────────────────
    if can_fix and confidence in ("high", "medium"):
        action = diagnosis.get("self_fix_action")
        details = diagnosis.get("self_fix_details", "")

        await notify_office(
            f"🔍 *{repo}* — нашла причину повторяющегося бага:\n"
            f"_{root_cause}_\n\n"
            f"Применяю фикс: {real_fix[:200]}..."
        )

        if action == "push_code" and details:
            # Пытаемся применить фикс через analyze_logs → handle_bug pipeline
            fix_analysis = {
                "is_bug": True,
                "root_cause": root_cause,
                "fix_description": real_fix,
                "fix_code_snippet": details,
                "confidence": confidence,
            }
            await handle_bug(service_id, repo, repo,
                             SERVICES.get(service_id, (None, "bot.py"))[1],
                             fix_analysis)
        elif action == "redeploy":
            ok = await redeploy_service(service_id)
            if ok:
                logger.info(f"[diagnose] auto-redeploy ok for {repo}")
            else:
                await notify_office(f"⚠️ *{repo}* — редеплой не удался")
        # Сбрасываем счётчик после применения фикса
        if redis_client:
            await redis_client.delete(f"fix_count:{service_id}:{error_signature}")
        return

    # ── 5. Не смогла — эскалируем с ДИАГНОЗОМ, не просто криком ─────────────
    escalate_reason = diagnosis.get("escalate_reason") or "не смогла подобрать фикс с высокой уверенностью"

    await notify_office(
        f"⚠️ *{repo}* — повторяющийся баг, нужна помощь\n\n"
        f"*Причина:* {root_cause}\n"
        f"*Почему предыдущий фикс не помог:* {diagnosis.get('why_fix_failed', 'неизвестно')}\n"
        f"*Что нужно сделать:* {real_fix}\n\n"
        f"*Почему сама не исправила:* {escalate_reason}\n"
        f"Сигнатура: `{error_signature[:16]}` | fix_count={fix_count}"
    )
    logger.warning(f"[deep_diagnose] escalated {repo}: {escalate_reason}")

async def monitor_loop():
    """Фоновая задача: каждые 5 минут проверяет логи всех сервисов.

    Автономный режим: если Railway API недоступен (outage) — переключается
    на детект через Redis структурные логи. Фикс (GitHub push) не требует
    Railway API — Railway автодеплоит из ветки сам.
    """
    await asyncio.sleep(30)  # подождать пока бот стартует
    logger.info("[monitor] started")
    _railway_down_notified = False  # чтобы не спамить уведомлениями об outage

    while True:
        if MONITOR_PAUSED():
            logger.info("[monitor] paused via CILLY_MONITOR_PAUSED env var, sleeping...")
            await asyncio.sleep(60)
            continue

        # Проверяем Railway API один раз в начале цикла
        railway_ok = await _railway_is_available()

        if not railway_ok:
            if not _railway_down_notified:
                await notify_office(
                    "⚠️ *Railway API недоступен* — переключаюсь на Redis-мониторинг.\n"
                    "Фиксы через GitHub работают, Railway автодеплоит сам."
                )
                _railway_down_notified = True
            logger.warning("[monitor] Railway API down — using Redis fallback for all services")
        else:
            if _railway_down_notified:
                await notify_office("✅ Railway API снова доступен — возвращаюсь к полному мониторингу.")
                _railway_down_notified = False

        for service_id, (repo, main_file) in SERVICES.items():
            try:
                # Основной путь: Railway logs. Fallback: Redis structural logs
                if railway_ok:
                    logs = await get_service_logs(service_id)
                else:
                    logs = await get_service_logs_via_redis(repo)

                if not logs:
                    continue

                # === Filter Layer 1: если в логе вообще присутствует deployment noise — пропускаем весь цикл
                # (Conflict/getUpdates ошибки порождают stack trace из строк, не содержащих ignore-паттернов;
                #  они проходили per-line filter и шли на анализ к Claude. Это была реальная дыра.)
                if any(any(p in l for p in IGNORE_PATTERNS) for l in logs):
                    logger.info(f"[monitor] {repo}: deployment-related noise in logs (Conflict/restart), skipping whole cycle")
                    continue

                # === Filter Layer 2: есть ли реальные ошибки помимо deployment-шума
                error_logs = [l for l in logs if any(p in l for p in ERROR_PATTERNS)]
                if not error_logs:
                    continue

                # Доп. per-line ignore (на случай других известных шумовых паттернов)
                filtered_errors = [
                    l for l in error_logs
                    if not any(p in l for p in IGNORE_PATTERNS)
                ]
                if not filtered_errors:
                    logger.info(f"[monitor] {repo}: only ignorable errors after per-line filter, skipping")
                    continue
                error_logs = filtered_errors

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

                # Счётчик повторений (правило D005)
                fix_count_key = f"fix_count:{service_id}:{error_signature}"
                r_count = await get_redis()
                fix_count = 0
                if r_count:
                    fix_count = int(await r_count.get(fix_count_key) or 0)
                    await r_count.incr(fix_count_key)
                    await r_count.expire(fix_count_key, 86400 * 7)

                if fix_count >= 3:
                    # Не просто кричать — сначала разобраться самой
                    logger.warning(f"[monitor] recurring error in {repo} fix_count={fix_count}, running deep analysis")
                    await _deep_diagnose_and_escalate(
                        repo, service_id, error_signature, error_logs, fix_count, r_count
                    )
                    continue

                logger.info(f"[monitor] found {len(error_logs)} error lines in {repo}, analyzing...")

                # Auto-pull структурных логов из Redis для обогащения контекста анализа
                redis_log_context = ""
                try:
                    _r_logs = await get_redis()
                    if _r_logs:
                        from ai_office_shared.shared.identity import canonical
                        bot_canon = canonical(repo.replace("-bot", ""))
                        if bot_canon:
                            recent_events = await read_logs(
                                _r_logs, bot_canon,
                                days=1, limit=30,
                                level_filter=None,
                            )
                            if recent_events:
                                lines = []
                                for ev in recent_events[:20]:
                                    ts = ev.get("ts", "")[-8:]  # HH:MM:SSZ
                                    lines.append(f"[{ts}] {ev.get('level','?').upper()} {ev.get('event','?')} {ev.get('context',{})}")
                                redis_log_context = "\n--- Redis структурные логи (последние 20 событий) ---\n" + "\n".join(lines)
                                logger.info(f"[monitor] pulled {len(recent_events)} Redis events for {bot_canon}")
                except Exception as _e:
                    logger.warning(f"[monitor] auto-pull Redis logs failed for {repo}: {_e}")

                # Читаем исходник
                try:
                    source_code = await read_file(repo, main_file)
                except Exception:
                    source_code = "# файл не удалось прочитать"

                # Если есть Redis-контекст — добавляем к source_code для анализа
                if redis_log_context:
                    source_code = source_code + "\n\n" + redis_log_context

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
RAILWAY_TOKEN_VAL = os.getenv("RAILWAY_TOKEN_VLAD", "") or os.getenv("RAILWAY_TOKEN", "")

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
    r = client.messages.create(model="claude-sonnet-4-6", max_tokens=4096,
        system=SYSTEM, messages=conversation_history[user_id])
    text = next((b.text for b in r.content if hasattr(b, "text")), "[нет текста]")
    conversation_history[user_id].append({{"role": "assistant", "content": text}})
    return text

async def handle_task(request):
    data = await request.json()
    message = data.get("message", "")
    user_id = data.get("user_id", YOUR_TELEGRAM_ID)
    await log("MSG_IN", f"[HTTP] {{message[:80]}}")
    try:
        response = await process(message, user_id)
    except Exception as e:
        logger.error(f"process() error: {e}")
        return web.json_response({"status": "error", "responses": [str(e)]}, status=500)
    # В группу ТОЛЬКО если явно передан notify=True
    # По умолчанию ответ идёт только в HTTP response (личка или вызывающий)
    if data.get("notify", False):
        await send_to_group(f"{bot_name}:\n{response}")
    await log("MSG_OUT", f"{bot_name}: {{response[:80]}}")
    return web.json_response({{"status": "ok", "response": response}})

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != YOUR_TELEGRAM_ID:
        return
    if update.effective_chat.type in ["group", "supergroup"]:
        return
    msg = update.message.text
    # Перехват GROQ API ключа
    if msg and msg.strip().startswith("gsk_") and len(msg.strip()) > 20:
        groq_key = msg.strip()
        if redis_client:
            await redis_client.set("office:secrets:groq_api_key", groq_key, ex=86400*365)
        await update.message.reply_text("✅ GROQ_API_KEY сохранён — удали это сообщение вручную 🗑")
        return
    await log("MSG_IN", msg[:80])
    response = await process(msg, update.effective_user.id)
    await log("MSG_OUT", f"{bot_name}: {{response[:80]}}")
    await update.message.reply_text(response)


async def _legacy_main_unused():  # дублировал main() — сломан
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

    # Кандидаты username — пробуем по очереди пока не создадим
    username_candidates = [
        f"{bot_name_en}_bot",
        f"{bot_name_en}ai_bot",
        f"{bot_name_en}2_bot",
        f"{bot_name_en}3_bot",
        f"{bot_name_en}_office_bot",
        f"ai{bot_name_en}_bot",
        f"{bot_name_en}_ru_bot",
    ]

    import re as _re

    async with TelegramClient(StringSession(session), api_id, api_hash) as client:
        botfather = await client.get_entity("@BotFather")

        async def send_msg(text: str) -> int:
            """Отправить и вернуть ID последнего сообщения BotFather ДО отправки."""
            msgs = await client.get_messages(botfather, limit=1)
            before_id = msgs[0].id if msgs else 0
            await client.send_message(botfather, text)
            return before_id

        async def wait_new_reply(after_id: int, timeout: float = 8.0) -> str:
            """Ждать новое сообщение BotFather с ID > after_id."""
            for _ in range(int(timeout / 0.5)):
                await asyncio.sleep(0.5)
                msgs = await client.get_messages(botfather, limit=1)
                if msgs and msgs[0].id > after_id:
                    return msgs[0].text or ""
            msgs = await client.get_messages(botfather, limit=1)
            return msgs[0].text if msgs else ""

        # Сбрасываем состояние
        before = await send_msg("/start")
        await wait_new_reply(before, timeout=3.0)  # дожидаемся приветствия, игнорируем
        await asyncio.sleep(1)

        for attempt, bot_username in enumerate(username_candidates):
            logger.info(f"[botfather] попытка {attempt+1}: @{bot_username}")

            if attempt > 0:
                before = await send_msg("/start")
                await wait_new_reply(before, timeout=3.0)
                await asyncio.sleep(1)

            # Шаг 1: /newbot → ждём "Give me a name"
            before1 = await send_msg("/newbot")
            reply1 = await wait_new_reply(before1, timeout=7.0)
            logger.info(f"[botfather] /newbot → {reply1[:80]}")

            # Шаг 2: имя бота → ждём "choose username"
            before2 = await send_msg(bot_display)
            reply2 = await wait_new_reply(before2, timeout=7.0)
            logger.info(f"[botfather] display → {reply2[:80]}")

            # Шаг 3: username → ждём токен или ошибку
            before3 = await send_msg(bot_username)
            reply3 = await wait_new_reply(before3, timeout=10.0)
            logger.info(f"[botfather] username reply → {reply3[:120]}")

            # Успех — есть токен
            token_match = _re.search(r"(\d+:[A-Za-z0-9_-]{35,})", reply3)
            if token_match:
                logger.info(f"[botfather] ✅ создан @{bot_username}")
                return token_match.group(1)

            if any(p in reply3.lower() for p in ["already taken", "taken", "sorry", "try something"]):
                logger.warning(f"[botfather] @{bot_username} занят, пробую следующий...")
                continue

            raise ValueError(f"BotFather ошибка (@{bot_username}): {reply3[:200]}")

        raise ValueError(f"Все {len(username_candidates)} вариантов username заняты для {bot_name_en}")



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
    """Добавить бота в группу по group_id. Поддерживает Chat и Channel/Supergroup."""
    from telethon.tl.types import Chat, Channel
    from telethon.tl.functions.messages import AddChatUserRequest
    client = await get_telethon_client()
    try:
        bot_entity   = await client.get_entity(bot_username)
        group_entity = await client.get_entity(group_id)
        if isinstance(group_entity, Channel):
            await client(InviteToChannelRequest(group_entity, [bot_entity]))
        else:
            # Обычный Chat
            await client(AddChatUserRequest(
                chat_id=group_entity.id,
                user_id=bot_entity,
                fwd_limit=0
            ))
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
            if hasattr(f, 'title') and (f.title.text if hasattr(f.title, 'text') else str(f.title)).strip().lower() == folder_name.strip().lower():
                return f.id
        # Логируем все найденные папки для диагностики
        names = [(f.title.text if hasattr(f.title, 'text') else str(f.title)) for f in filters.filters if hasattr(f, 'title')]
        logger.info(f"tg_get_folder_id: папки найдены: {names}, искали: '{folder_name}'")
        return None
    finally:
        await client.disconnect()


async def tg_add_peer_to_folder(peer_id: int, folder_name: str = "Office") -> bool:
    """Добавить диалог (бота или группу) в папку по имени."""
    client = await get_telethon_client()
    try:
        filters = await client(GetDialogFiltersRequest())
        target = None
        # Логируем все папки для диагностики
        all_names = [(f.title.text if hasattr(f.title, 'text') else str(f.title)) for f in filters.filters if hasattr(f, 'title')]
        logger.info(f"tg_add_peer_to_folder: все папки: {all_names}, ищем: '{folder_name}'")
        for f in filters.filters:
            if hasattr(f, 'title') and (f.title.text if hasattr(f.title, 'text') else str(f.title)).strip().lower() == folder_name.strip().lower():
                target = f
                break
        if not target:
            logger.warning(f"Папка '{folder_name}' не найдена. Доступны: {all_names}")
            return False

        peer_entity = await client.get_entity(peer_id)
        input_peer = await client.get_input_entity(peer_entity)

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
    """Выполнить GraphQL запрос к Railway API.
    Бросает RuntimeError при GraphQL-уровне ошибок (auth, permission и т.п.).
    """
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
        data = r.json()
        if data.get("data") is None and data.get("errors"):
            msgs = "; ".join(e.get("message", "?") for e in data["errors"])
            raise RuntimeError(f"Railway GraphQL error: {msgs}")
        return data

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

async def railway_get_service_id(repo_name: str) -> str | None:
    """Найти service_id по имени сервиса в проекте."""
    data = await railway_graphql(
        """query($id: String!) {
             project(id: $id) { services { edges { node { id name } } } }
           }""",
        {"id": PROJECT_ID}
    )
    for edge in ((data.get("data") or {}).get("project") or {}).get("services", {}).get("edges") or []:
        if edge["node"]["name"] == repo_name:
            return edge["node"]["id"]
    return None


async def railway_get_bot_url(name_hint: str) -> str:
    """Ищет сервис на Railway по имени, возвращает публичный URL."""
    try:
        data = await railway_graphql(
            """query($id: String!) {
                 project(id: $id) { services { edges { node { id name } } } }
               }""",
            {"id": PROJECT_ID}
        )
        services = ((data.get("data") or {}).get("project") or {}).get("services", {}).get("edges") or []
        # Нормализуем hint
        hint_clean = name_hint.replace("_bot", "").replace("_", "-").replace(" ", "-").lower()
        candidates = [
            hint_clean + "-bot",
            hint_clean,
            name_hint.replace("_", "-").lower(),
        ]
        for svc_edge in services:
            svc_name = svc_edge["node"]["name"].lower()
            for c in candidates:
                if svc_name == c or svc_name.startswith(c):
                    return f"https://{svc_edge['node']['name']}-production.up.railway.app"
    except Exception as e:
        logger.debug(f"railway_get_bot_url failed: {e}")
    # fallback: стандартный паттерн по hint
    clean = name_hint.replace("_", "-").lower()
    if not clean.endswith("-bot"):
        clean = clean.rstrip("-bot") + "-bot"
    return f"https://{clean}-production.up.railway.app"


async def railway_create_service(repo_name: str, bot_display_name: str, variables: dict = None) -> dict:
    """Создать сервис на Railway. Если уже существует — использовать его."""
    # Проверяем существует ли уже
    existing_id = await railway_get_service_id(repo_name)
    if existing_id:
        logger.info(f"[railway] сервис '{repo_name}' уже существует: {existing_id}")
        service_id = existing_id
    else:
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
        logger.info(f"[railway] создан сервис '{repo_name}': {service_id}")

    # Записать переменные если переданы
    if variables:
        ok = await railway_set_variables(service_id, variables)
        if not ok:
            logger.warning(f"railway_set_variables returned False for {repo_name}")

    return {"service_id": service_id}


async def handle_natural_language(message_text: str, chat_id: int, reply_func, history: list = None):
    """Process any natural language request — detect intent and execute."""
    # Читаем ops.md — лог последних действий Claude и Силли
    # Это даёт Силли контекст о том что уже было сделано
    ops_context = ""
    try:
        raw_ops = await read_file("ai-office-shared", OPS_LOG_FILE)
        if raw_ops:
            # Берём последние 3000 символов — самые свежие записи
            ops_context = raw_ops[-3000:]
    except Exception:
        pass  # ops.md может не существовать — не страшно

    # ops.md используется ТОЛЬКО для answer-контекста, не для intent detection
    # (иначе "pilly-bot создан" в ops.md сбивает intent с create_bot на get_bot_token)

    # Detect intent via Haiku (cheap) — без ops.md контекста
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
        # Только явные императивные команды — не вопросы о процессе
        question_signals = ["как ", "какой", "какие", "что нужно", "с чего", "как создать",
                            "как задеплоить", "как разверн", "подскажи", "расскажи", "объясни"]
        is_question = any(w in msg_lower for w in question_signals)
        if not is_question and any(w in msg_lower for w in ["создай бота", "create bot", "зарегистрируй бота",
                                           "зарегистрировать бота", "newbot", "зарегистрируй нового",
                                           "создать нового бота", "создай нового"]):
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

    intent     = intent_data.get("intent", "answer")
    repo       = intent_data.get("repo")
    path       = intent_data.get("path")
    task       = intent_data.get("task", message_text)
    _conf_raw = intent_data.get("confidence", 1.0)
    confidence = float(_conf_raw) if isinstance(_conf_raw, (int, float)) else {"high": 0.9, "medium": 0.6, "low": 0.3}.get(str(_conf_raw).lower(), 0.5)

    logger.info(f"[nl] intent={intent} confidence={confidence:.2f} repo={repo}")

    # Для деструктивных/долгих операций — требуем высокую уверенность
    DESTRUCTIVE = ("create_bot", "deploy", "push_code", "get_bot_token")
    if intent in DESTRUCTIVE and confidence < 0.75:
        await reply_func(
            f"🤔 Не уверен что правильно понял задачу (confidence={confidence:.0%}).\n"
            f"Уточни: ты хочешь чтобы я **{intent}** выполнил, или это вопрос?"
        )
        return

    if intent == "answer":
        # Для answer — используем ops.md как контекст и историю разговора
        answer_system = CHAT_PROMPT
        if ops_context:
            answer_system = (
                CHAT_PROMPT +
                f"\n\nПоследние действия в офисе (ops.md, последние записи):\n{ops_context}"
            )
        if history and len(history) > 1:
            answer_resp = await get_claude().messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                system=answer_system,
                messages=history[:-1] + [{"role": "user", "content": message_text}]
            )
            answer = answer_resp.content[0].text
        else:
            answer = await ask_claude(message_text, system=answer_system, model="claude-sonnet-4-6")
        await reply_func(answer)


    elif intent == "redis_query":
        """Выполняет реальные Redis операции — scan, get, hgetall, custom audit."""
        r = await get_redis()
        if not r:
            await reply_func("❌ Redis недоступен")
            return

        task_lower = task.lower()
        result: dict = {}

        # ── 1. quality audit ──────────────────────────────────────────────
        if any(w in task_lower for w in ["quality", "реакци", "голос", "👍", "👎", "up", "down", "аудит"]):
            async for key in r.scan_iter("office:quality:*"):
                data = await r.hgetall(key)
                bot_name = key.split(":")[-1]
                result[f"quality:{bot_name}"] = {
                    "up":   int(data.get("up",   0)),
                    "down": int(data.get("down", 0)),
                }

        # ── 2. health audit ───────────────────────────────────────────────
        if any(w in task_lower for w in ["health", "здоровь", "status", "up/down", "живой", "живые"]):
            async for key in r.scan_iter("office:health:*"):
                agent = key.split(":")[-1]
                result[f"health:{agent}"] = await r.get(key)

        # ── 3. logs ───────────────────────────────────────────────────────
        if any(w in task_lower for w in ["log", "лог", "событи", "ошибк"]):
            bot_hint = None
            for bot_key in ["билли","тилли","милли","доктор","крисс","эллис","вилли","гослинг","силли","фили"]:
                if bot_key in task_lower:
                    bot_hint = bot_key
                    break
            import datetime as _dt
            today = _dt.date.today().isoformat()
            pattern = f"office:logs:{bot_hint}:{today}" if bot_hint else f"office:logs:*:{today}"
            async for key in r.scan_iter(pattern):
                entries = await r.lrange(key, 0, 19)
                result[key] = [json.loads(e) for e in reversed(entries)]

        # ── 4. routing misses ─────────────────────────────────────────────
        if any(w in task_lower for w in ["miss", "промах", "маршрут", "routing"]):
            raw_misses = await r.lrange("office:routing:misses", 0, 19)
            result["routing_misses"] = [json.loads(m) for m in raw_misses]

        # ── 5. произвольный scan pattern ─────────────────────────────────
        import re as _re
        pattern_match = _re.search(r'(office:[a-z:*_]+)', task_lower)
        if pattern_match and not result:
            pattern_str = pattern_match.group(1)
            if not pattern_str.endswith("*"):
                # Точный ключ — пробуем get и hgetall
                val = await r.get(pattern_str)
                if val:
                    result[pattern_str] = val
                else:
                    hval = await r.hgetall(pattern_str)
                    if hval:
                        result[pattern_str] = hval
            else:
                async for key in r.scan_iter(pattern_str):
                    val = await r.get(key)
                    result[key] = val or await r.hgetall(key)

        # ── 6. если ничего не нашли — показываем ВСЁ ─────────────────────
        if not result:
            for ns in ["office:quality:*", "office:health:*", "office:routing:misses"]:
                if "*" in ns:
                    async for key in r.scan_iter(ns):
                        data = await r.hgetall(key)
                        if not data:
                            data = await r.get(key)
                        result[key] = data
                else:
                    raw = await r.lrange(ns, 0, 9)
                    result[ns] = [json.loads(e) for e in raw]

        # ── 7. сброс fix_count (reset / сброс) ───────────────────────
        if any(w in task_lower for w in ["сбро", "reset", "clear fix", "очист"]):
            deleted = []
            async for key in r.scan_iter("fix_count:*"):
                await r.delete(key)
                deleted.append(key.split(":")[-1][:8])
            async for key in r.scan_iter("seen_error:*"):
                await r.delete(key)
            result["reset"] = f"Сброшено {len(deleted)} fix_count ключей"

        out = json.dumps(result, ensure_ascii=False, indent=2)
        # Если много данных — режем
        if len(out) > 3000:
            out = out[:3000] + "\n... (обрезано)"
        await reply_func(f"```json\n{out}\n```")

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
        await reply_func(f"🤖 Создаю бота: *{task}*...")

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
            _raw_repo  = setup["repo"].lower().replace(" ", "-").replace("_", "-")
            bot_repo   = _raw_repo if _raw_repo.endswith("-bot") else _raw_repo + "-bot"
            bot_display = setup["display"]
            bot_prompt  = setup["prompt"]
        except Exception as ex:
            await reply_func(f"❌ Не смог разобрать параметры бота: {ex}")
            return

        await reply_func(f"📦 Репо: `{bot_repo}`\n👤 Имя: {bot_display}\n📝 Промпт: {bot_prompt}")

        # Если сервис уже существует — проверяем есть ли TELEGRAM_TOKEN
        # Если нет — resume: пропускаем создание репо/кода и сразу идём за токеном
        resume_mode = False
        existing_sid = next((sid for sid, (r, _) in SERVICES.items() if r == bot_repo), None)
        if not existing_sid:
            existing_sid = await railway_get_service_id(bot_repo)
        if existing_sid:
            try:
                vars_data = await railway_graphql(
                    """query($proj: String!, $svc: String!, $env: String!) {
                         variables(projectId: $proj, serviceId: $svc, environmentId: $env)
                       }""",
                    {"proj": PROJECT_ID, "svc": existing_sid, "env": ENVIRONMENT_ID}
                )
                existing_vars = (vars_data.get("data") or {}).get("variables") or {}
                if "TELEGRAM_TOKEN" in existing_vars:
                    await reply_func(f"✅ Бот `{bot_repo}` уже полностью настроен.")
                    return
                else:
                    await reply_func("⚠️ Сервис существует, но токена нет — получаю через BotFather...")
                    resume_mode = True
            except Exception:
                resume_mode = True

                # 1. Создать GitHub репо
        if not resume_mode:
            await reply_func("1️⃣ Создаю GitHub репо...")
        try:
            repo_info = await create_repo(bot_repo, description=f"AI office bot: {bot_display}")
        except ValueError as ex:
            await reply_func(f"⚠️ {ex} — продолжаю с существующим")
        except Exception as ex:
            await reply_func(f"❌ GitHub: {ex}")
            return

        # 2. Пушу шаблон
        if not resume_mode:
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

        # Регистрируем в реестре template_bots — для автообновлений
        asyncio.create_task(register_template_bot(bot_repo, bot_display, bot_prompt, service_id))

        await reply_func(
            f"✅ Бот *{bot_display}* полностью готов и интегрирован!\n\n"
            f"• GitHub репо: `{bot_repo}` ✅\n"
            f"• Код залит ✅\n"
            f"• Telegram бот создан ✅\n"
            f"• Railway сервис + переменные ✅\n"
            f"• Добавлен в Office group ✅\n"
            f"• Папка Office ✅\n"
            f"• Филли обновлён и задеплоен ✅\n"
            f"• Зарегистрирован для автообновлений ✅\n\n"
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

    elif intent == "add_external_bot":
        import re as _re

        # ── Шаг 0: вытащить всё из задачи через Haiku ────────────────────────
        extraction_raw = await ask_claude(
            f"Из запроса извлеки параметры внешнего бота.\n"
            f"Запрос: {task}\n\n"
            f"JSON без markdown:\n"
            f"{{\"name_ru\": \"имя по-русски одним словом\","
            f"\"name_en\": \"имя латиницей строчными без пробелов\","
            f"\"key\": \"ключ для роутера КАПСОМ\","
            f"\"url\": \"URL endpoint или null\","
            f"\"description\": \"роль и функции одной фразой на русском\","
            f"\"tg_folder\": \"название папки куда добавить или null\","
            f"\"tg_group\": \"название новой группы для создания или null\"}}",
            model="claude-haiku-4-5-20251001"
        )
        try:
            s, e = extraction_raw.find("{"), extraction_raw.rfind("}") + 1
            ext = json.loads(extraction_raw[s:e])
        except Exception:
            ext = {}

        bot_display   = ext.get("name_ru", "Крис").capitalize()
        name_en       = ext.get("name_en", bot_display.lower())
        bot_key       = ext.get("key", bot_display.upper())
        # URL: берём из запроса или вычисляем стандартный Railway-паттерн
        bot_url_raw = ext.get("url") or ""
        bot_url     = bot_url_raw.rstrip("/") if bot_url_raw else ""
        bot_description = ext.get("description", f"Внешний ассистент {bot_display}")
        tg_folder     = ext.get("tg_folder") or "Office"
        tg_new_group  = ext.get("tg_group")  # название новой группы если нужна

        # ── Шаг 1: найти username через Telegram API (автоподбор) ────────────
        await reply_func(f"🔍 Ищу @{name_en}_bot в Telegram...")

        candidates = [
            f"{name_en}_bot",
            f"{name_en}ai_bot",
            f"{name_en}_assistant_bot",
            f"ai{name_en}_bot",
            f"{name_en}2_bot",
            f"{name_en}_office_bot",
            f"{name_en}ru_bot",
            f"the{name_en}_bot",
        ]

        # Если в задаче явно указан @username — ставим его первым
        explicit = _re.search(r"@([A-Za-z][A-Za-z0-9_]{3,})", message_text)
        if explicit:
            candidates.insert(0, explicit.group(1))

        bot_username = None
        tg_token = os.getenv("CODER_BOT_TOKEN", "")
        async with httpx.AsyncClient(timeout=10) as hc:
            for candidate in candidates:
                try:
                    r = await hc.get(
                        f"https://api.telegram.org/bot{tg_token}/getChat",
                        params={"chat_id": f"@{candidate}"}
                    )
                    if r.json().get("ok"):
                        bot_username = candidate
                        logger.info(f"[add_external_bot] found @{candidate}")
                        break
                except Exception:
                    continue

        if not bot_username:
            tried = ", ".join(f"@{c}" for c in candidates[:5])
            await reply_func(
                f"Перебрал варианты ({tried}…) — ни один не найден в Telegram.\n"
                f"Скинь точный @username бота."
            )
            return

        # Если URL не указан — ищем сервис на Railway по имени бота
        if not bot_url:
            bot_url = await railway_get_bot_url(bot_username)

        await reply_func(
            f"✅ Нашёл: @{bot_username}\n"
            f"Имя: {bot_display} | Ключ: {bot_key}\n"
            f"URL: {bot_url}\n"
            f"Роль: {bot_description}"
        )

        # ── Шаг 2: Создать Telegram-группу если нужна ────────────────────────
        created_group_id = None
        if tg_new_group:
            await reply_func(f"2️⃣ Создаю группу «{tg_new_group}»...")
            created_group_id = await tg_create_group(tg_new_group, [f"@{bot_username}"])
            if created_group_id:
                await reply_func(f"✅ Группа создана: {created_group_id}")
                # Добавить группу в папку
                ok = await tg_add_peer_to_folder(created_group_id, tg_folder)
                await reply_func(f"✅ Группа добавлена в папку {tg_folder}" if ok else f"⚠️ Папка {tg_folder} не найдена")
            else:
                await reply_func("⚠️ Не удалось создать группу")

        # ── Шаг 3: Добавить бота в офис-группу ──────────────────────────────
        await reply_func("3️⃣ Добавляю в офис-группу...")
        office_id = int(os.getenv("OFFICE_CHAT_ID", "-5194783850"))
        added = await tg_add_bot_to_group(f"@{bot_username}", office_id)
        await reply_func("✅ Добавлен в офис-группу" if added else "⚠️ Не удалось (возможно уже там)")

        # ── Шаг 4: Добавить бота в папку Office ─────────────────────────────
        folder_ok = False
        await reply_func(f"4️⃣ Добавляю в папку {tg_folder}...")
        try:
            client_tmp = await get_telethon_client()
            try:
                entity = await client_tmp.get_entity(f"@{bot_username}")
                peer_id = entity.id
                from telethon.tl.functions.messages import GetDialogFiltersRequest as _GDF
                filters_resp = await client_tmp(_GDF())
                folder_names = [(f.title.text if hasattr(f.title, 'text') else str(f.title)) for f in filters_resp.filters if hasattr(f, 'title')]
            finally:
                await client_tmp.disconnect()
            folder_ok = await tg_add_peer_to_folder(peer_id, tg_folder)
            if folder_ok:
                await reply_func(f"✅ Добавлен в папку {tg_folder}")
            else:
                await reply_func(f"⚠️ Папка '{tg_folder}' не найдена.\nДоступные: {folder_names}\nСкажи точное название — добавлю.")
        except Exception as e:
            await reply_func(f"⚠️ Папка: {e}")

        # ── Шаг 5: Обновить Филли (routing) — всегда ────────────────────────
        await reply_func("5️⃣ Обновляю Филли (routing)...")
        try:
            filly_code = await read_file("filly-bot", "bot.py")

            # BOT_URLS
            urls_start = filly_code.find("BOT_URLS")
            urls_end   = filly_code.find("}", urls_start)
            last_comma = filly_code.rfind(",", urls_start, urls_end)
            filly_code = (filly_code[:last_comma+1]
                          + f'\n    "{bot_key}":  "{bot_url}",'
                          + filly_code[last_comma+1:])

            # ROUTER_SYSTEM
            anchor_router = "Только одно слово. Если непонятно — БИЛЛИ."
            filly_code = filly_code.replace(
                anchor_router,
                f'{bot_key} — {bot_description}\n{anchor_router}'
            )

            # DM_AGENT_SYSTEMS
            dm_start = filly_code.find("DM_AGENT_SYSTEMS")
            dm_end   = filly_code.find("}", dm_start)
            last_dm  = filly_code.rfind(",", dm_start, dm_end)
            filly_code = (filly_code[:last_dm+1]
                          + f'\n    "{bot_key}":  "Ты — {bot_display}. {bot_description} Неформально, на русском.",'
                          + filly_code[last_dm+1:])

            # _name_map
            nm_anchor = '"силли": "СИЛЛИ"'
            alias = bot_username.replace("_bot","").replace("_","")
            filly_code = filly_code.replace(
                nm_anchor,
                f'"{bot_display.lower()}": "{bot_key}", "{alias}": "{bot_key}",\n        {nm_anchor}'
            )

            await push_file("filly-bot", "bot.py", filly_code,
                            f"feat: add external bot {bot_display} to routing")
            await redeploy_service("5d61d403-feee-455e-9c0d-523f0e7c79d5")
            await reply_func("✅ Филли обновлён и задеплоен")
        except Exception as e:
            await reply_func(f"⚠️ Ошибка обновления Филли: {e}")

        await reply_func(
            f"✅ *{bot_display}* подключён!\n\n"
            f"• @{bot_username} найден автоматически ✅\n"
            + (f"• Группа «{tg_new_group}» создана ✅\n" if tg_new_group and created_group_id else "")
            + f"• Офис-группа {'✅' if added else '⚠️'}\n"
            f"• Папка {tg_folder} {'✅' if folder_ok else '⚠️ не найдена'}\n"
            f"• Роутинг Филли: {bot_key} → {bot_url} ✅"
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

    elif intent == "cleanup_dm":
        """Удалить сообщения с ключами/секретами в личке через Telethon userbot."""
        import asyncio as _asyncio
        SENSITIVE = ["gsk_", "groq", "token", "api_key", "secret", "✅ groq"]
        try:
            tg_cl = await get_telethon_client()
            from telethon.tl.types import PeerUser
            try:
                cilly = await tg_cl.get_input_entity(PeerUser(7779587562))
            except Exception:
                await tg_cl.disconnect()
                await reply_func('❌ Не могу найти диалог с Силли')
                return
            msgs = await tg_cl.get_messages(cilly, limit=50)
            to_delete = [
                m.id for m in msgs
                if m.text and any(s in m.text.lower() for s in SENSITIVE)
            ]
            if to_delete:
                await tg_cl.delete_messages(cilly, to_delete)
                await tg_cl.disconnect()
                await reply_func(f"✅ Удалено {len(to_delete)} сообщений с секретами из лички")
            else:
                await tg_cl.disconnect()
                await reply_func("✅ Секретных сообщений не найдено")
        except Exception as e:
            await reply_func(f"❌ {e}")


    elif intent == "cleanup_group":
        """Удаляет старые сообщения от ботов в указанной группе через Telethon."""
        import asyncio as _asyncio
        from datetime import datetime, timezone

        # Параметры из task
        # chat_id по умолчанию — Bug Lessons
        target_chat = -5197140411
        # Удаляем всё что старше сегодняшнего дня (до 13:30 UTC 28.05.2026)
        cutoff = datetime(2026, 5, 28, 13, 30, tzinfo=timezone.utc)

        await reply_func("🧹 Чищу старые сообщения от ботов...")

        # Паттерны служебных сообщений которые всегда удаляем
        SERVICE_PATTERNS = [
            "⏸", "▶️ Силли", "🤖 Запускаю agentic", "📚 Постю",
            "✅ Завершено за", "⚠️ Достигнут лимит шагов",
            "🧹 Чищу", "✅ Удалено", "✅ Все 28",
            "🔧 *", "упал — пробую", "редеплой запущен",
            "передеплоить", "редеплой", "Запускаю agentic",
            "agentic mode", "Постю уроков", "опубликованы в Bug",
        ]

        # Определяем chat из task
        if "-5194783850" in task or "офис" in task.lower() or "office" in task.lower():
            target_chat = -5194783850
            # Проверяем указана ли дата начала ("с 29 мая", "начиная с", "from_date")
            import re as _re
            date_match = _re.search(r"(\d{4}-\d{2}-\d{2}|29.?мая|29 мая)", task)
            if date_match or "29" in task or "начиная с" in task:
                from datetime import datetime, timezone
                cutoff_mode = "from_date"
                cutoff = datetime(2026, 5, 29, 0, 0, tzinfo=timezone.utc)
            else:
                cutoff_mode = "today_patterns"
        elif "-5197140411" in task or "баг" in task.lower() or "bug" in task.lower() or "logs" in task.lower():
            target_chat = -5197140411
            cutoff_mode = "old_bots"
        else:
            cutoff_mode = "today_patterns"  # дефолт — паттерны за сегодня

        try:
            tg_cl = await get_telethon_client()
            messages = await tg_cl.get_messages(target_chat, limit=3000)
            to_delete = []
            for msg in messages:
                if not msg or not msg.date:
                    continue
                if not msg.from_id:
                    continue

                if cutoff_mode == "today_patterns":
                    # Удаляем ВСЕ сообщения от ботов за сегодня из офисной группы
                    from datetime import date as _date, datetime as _dt, timezone as _tz
                    if msg.date.date() < _date.today():
                        continue
                    sender_id = getattr(msg.from_id, 'user_id', None)
                    if not sender_id:
                        continue
                    try:
                        user = await tg_cl.get_entity(sender_id)
                        if getattr(user, 'bot', False):
                            to_delete.append(msg.id)
                    except Exception:
                        # Если не можем получить entity — проверяем по паттернам
                        if msg.text and any(p in msg.text for p in SERVICE_PATTERNS):
                            to_delete.append(msg.id)
                elif cutoff_mode == "from_date":
                    # Удаляем всё от ботов начиная с cutoff даты
                    if msg.date < cutoff:
                        continue
                    sender_id = getattr(msg.from_id, 'user_id', None)
                    if not sender_id:
                        continue
                    try:
                        user = await tg_cl.get_entity(sender_id)
                        if getattr(user, 'bot', False):
                            to_delete.append(msg.id)
                    except Exception:
                        if msg.text and any(p in msg.text for p in SERVICE_PATTERNS):
                            to_delete.append(msg.id)
                else:
                    # Старый режим: удаляем старые сообщения от ботов
                    if msg.date >= cutoff:
                        continue
                    sender_id = getattr(msg.from_id, 'user_id', None)
                    if not sender_id:
                        continue
                    try:
                        user = await tg_cl.get_entity(sender_id)
                        if getattr(user, 'bot', False):
                            to_delete.append(msg.id)
                    except Exception:
                        continue

            if to_delete:
                for i in range(0, len(to_delete), 100):
                    await tg_cl.delete_messages(target_chat, to_delete[i:i+100])
                    await _asyncio.sleep(0.5)
                await tg_cl.disconnect()
                await reply_func(f"✅ Удалено {len(to_delete)} старых сообщений от ботов")
            else:
                await tg_cl.disconnect()
                await reply_func("✅ Старых сообщений от ботов не найдено")
        except Exception as e:
            await reply_func(f"❌ Ошибка: {e}")


    elif intent == "post_lessons":
        """Читает lessons.json и постит все уроки в Bug Lessons группу."""
        import asyncio as _asyncio
        _bot = bot  # избегаем конфликта с локальной переменной data
        BUG_GROUP = -5197140411
        try:
            raw = await read_file("ai-office-shared", "lessons/lessons.json")
            lessons_list = json.loads(raw)
        except Exception as e:
            await reply_func(f"❌ Не могу прочитать lessons.json: {e}")
            return

        await reply_func(f"📚 Постю {len(lessons_list)} уроков в Bug Lessons...")

        STATUS_EMOJI = {"fixed": "✅", "still_relevant": "⚠️", "outdated": "🗄"}

        for lesson in lessons_list:
            status_e = STATUS_EMOJI.get(lesson.get("status", ""), "❓")
            msg = (
                f"🐛 Урок #{lesson.get('id')} — {lesson.get('title', '?')}\n\n"
                f"📍 {lesson.get('bot', '?')} | {lesson.get('layer', '?')}\n\n"
                f"👁 Симптом:\n{lesson.get('symptom', '?')}\n\n"
                f"🔍 Причина:\n{lesson.get('root_cause', '?')}\n\n"
                f"🏗 Архитектура:\n{lesson.get('why_architecture', '?')}\n\n"
                f"✅ Фикс:\n{lesson.get('fix', '?')}\n\n"
                f"🛡 Профилактика:\n{lesson.get('prevention', '?')}\n\n"
                f"{status_e} Статус: {lesson.get('status', '?')}"
            )
            try:
                from aiogram.exceptions import TelegramAPIError
                await _GLOBAL_BOT.send_message(chat_id=BUG_GROUP, text=msg)
                await _asyncio.sleep(0.8)
            except Exception as e:
                try:
                    await _GLOBAL_BOT.send_message(chat_id=BUG_GROUP, text=f"⚠️ Урок #{lesson.get('id')} — ошибка: {e}")
                except Exception:
                    pass

        await reply_func(f"✅ Все {len(lessons_list)} уроков опубликованы в Bug Lessons")


    elif intent == "edit_file":
        """Точечное редактирование файла: old → new, с ast.parse для .py"""
        if not repo or not path:
            await reply_func("❌ Укажи repo и path")
            return
        old_text = intent_data.get("old", "")
        new_text = intent_data.get("new", "")
        if not old_text:
            await reply_func("❌ Укажи old (что заменить)")
            return
        try:
            file_content = await read_file(repo, path)
            if old_text not in file_content:
                await reply_func(f"❌ Строка не найдена в {repo}/{path}")
                return
            updated = file_content.replace(old_text, new_text, 1)
            if path.endswith(".py"):
                import ast as _ast
                try:
                    _ast.parse(updated)
                except SyntaxError as e:
                    await reply_func(f"❌ SyntaxError после замены: {e}")
                    return
            commit_msg = intent_data.get("message", f"edit: patch {path}")
            await push_file(repo, path, updated, commit_msg)
            await reply_func(f"✅ {repo}/{path} обновлён")
        except Exception as e:
            await reply_func(f"❌ Ошибка: {e}")


    elif intent == "agentic_task":
        """Agentic execution loop для многошаговых задач.
        ReAct pattern: think → act → observe → repeat.
        """
        AGENTIC_SYSTEM = """Ты — Силли, исполнитель задач AI-офиса.
Ты в agentic loop. На каждом шаге выбирай ОДНО действие и возвращай JSON.

Доступные действия:
- read_file: {"action":"read_file","repo":"...","path":"..."}
- push_file: {"action":"push_file","repo":"...","path":"...","content":"...","message":"..."}
- send_message: {"action":"send_message","chat_id":391077101,"text":"..."} — по умолчанию в личку Владу (391077101), НЕ в группу
- send_messages: {"action":"send_messages","chat_id":391077101,"texts":["msg1","msg2",...]} — батч, по умолчанию в личку Владу до 5 сообщений за раз
- done: {"action":"done","result":"итог для пользователя"}

Правила:
- Один JSON на шаг, без лишнего текста
- Если нужно прочитать несколько файлов — читай по одному
- done — когда задача полностью выполнена
- Максимум 15 шагов"""

        steps_log = []
        context = task
        max_steps = 30

        # agentic_task НЕ шлёт промежуточные шаги в чат — только финальный результат
        # silent_collect накапливает шаги в лог без отправки в группу
        agentic_log = []
        async def silent_collect(msg: str):
            agentic_log.append(msg)

        for step_num in range(max_steps):
            # Формируем prompt с историей шагов
            history_text = ""
            if steps_log:
                history_text = "\n\nУже выполнено:\n" + "\n".join(
                    f"  Шаг {i+1}: {s['action']} → {s['result'][:200]}"
                    for i, s in enumerate(steps_log)
                )

            step_prompt = f"Задача: {context}{history_text}\n\nСледующее действие:"

            raw_action = await ask_claude(step_prompt, system=AGENTIC_SYSTEM, model="claude-sonnet-4-6")
            raw_action = raw_action.strip()

            # Извлекаем JSON
            start_j = raw_action.find("{")
            end_j = raw_action.rfind("}") + 1
            if start_j == -1:
                await reply_func(f"❌ Шаг {step_num+1}: не получил JSON")
                break

            try:
                action_data = json.loads(raw_action[start_j:end_j])
            except Exception as e:
                await reply_func(f"❌ Шаг {step_num+1}: ошибка парсинга: {e}")
                break

            action = action_data.get("action", "")

            # Выполняем действие
            if action == "done":
                result_text = action_data.get("result", "✅ Готово")
                await reply_func(f"✅ {result_text}")  # только финал идёт в чат
                break

            elif action == "read_file":
                a_repo = action_data.get("repo", "")
                a_path = action_data.get("path", "")
                try:
                    file_content = await read_file(a_repo, a_path)
                    result = file_content[:4000]
                    steps_log.append({"action": f"read_file({a_repo}/{a_path})", "result": result})
                    # Добавляем содержимое в контекст
                    context += f"\n\n[Файл {a_repo}/{a_path}]:\n{result}"
                except Exception as e:
                    steps_log.append({"action": f"read_file({a_repo}/{a_path})", "result": f"ERROR: {e}"})

            elif action == "push_file":
                a_repo = action_data.get("repo", "")
                a_path = action_data.get("path", "")
                a_content = action_data.get("content", "")
                a_message = action_data.get("message", "agentic update")
                try:
                    await push_file(a_repo, a_path, a_content, a_message)
                    steps_log.append({"action": f"push_file({a_repo}/{a_path})", "result": "OK"})
                except Exception as e:
                    steps_log.append({"action": f"push_file({a_repo}/{a_path})", "result": f"ERROR: {e}"})

            elif action == "send_messages":
                a_chat = action_data.get("chat_id", -5194783850)
                texts = action_data.get("texts", [])
                sent = 0
                import asyncio as _asyncio
                for t in texts[:5]:
                    try:
                        await _GLOBAL_BOT.send_message(chat_id=int(a_chat), text=str(t))
                        sent += 1
                        await _asyncio.sleep(0.5)
                    except Exception:
                        pass
                steps_log.append({"action": f"send_messages({a_chat})", "result": f"sent {sent}/{len(texts)}"})

            elif action == "send_message":
                a_chat = action_data.get("chat_id", -5194783850)
                a_text = action_data.get("text", "")
                try:
                    await _GLOBAL_BOT.send_message(chat_id=int(a_chat), text=a_text)
                    steps_log.append({"action": f"send_message({a_chat})", "result": "OK"})
                except Exception as e:
                    steps_log.append({"action": f"send_message({a_chat})", "result": f"ERROR: {e}"})

            else:
                steps_log.append({"action": action, "result": "UNKNOWN ACTION"})
                await reply_func(f"⚠️ Неизвестное действие: {action}")
                break

        else:
            await reply_func(f"⚠️ Не смог завершить за {max_steps} шагов")


    elif intent == "dev_task":
        """Делегирование задачи команде dev-dept по цепочке:
        Силли → Девви → Рикки → Тести → Секки → Скрибби → итог."""
        import httpx as _httpx
        DEV_CHAIN = [
            (os.getenv("DEVVY_URL", ""), "девви", "напиши код"),
            (os.getenv("RICKY_URL", ""), "рикки", "сделай code review"),
            (os.getenv("TESTI_URL", ""), "тести", "протестируй"),
            (os.getenv("SEKKY_URL", ""), "секки", "проведи security audit"),
            (os.getenv("SCRIBBI_URL", ""), "скрибби", "задокументируй"),
        ]
        results = {}
        current_task = task
        await reply_func(f"🔁 Запускаю цепочку dev-dept...\n📋 Задача: {task[:200]}")
        async with _httpx.AsyncClient(timeout=60) as client:
            for url, name, prefix in DEV_CHAIN:
                if not url:
                    results[name] = "⚠️ URL не задан"
                    continue
                try:
                    payload = {
                        "message": f"{prefix}: {current_task}",
                        "user_id": int(os.getenv("YOUR_TELEGRAM_ID", "391077101")),
                        "source": "СИЛЛИ"
                    }
                    resp = await client.post(f"{url}/task", json=payload)
                    try:
                        result = resp.json().get("response", "нет ответа")
                    except Exception:
                        result = resp.text[:500] or "нет ответа"
                    results[name] = result[:500]
                    # Следующий получает результат предыдущего как контекст
                    current_task = f"{task}\n\n[{name}]: {result[:300]}"
                except Exception as e:
                    results[name] = f"❌ {e}"
                    current_task = task  # продолжаем без результата

        summary = f"✅ Цепочка dev-dept завершена:\n\n"
        for name, res in results.items():
            summary += f"**{name}:** {res[:200]}\n\n"
        await reply_func(summary)


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
    _r = await get_redis()
    if _r:
        await log_event(_r, BOT_NAME_LOWER, "capability_gap_detected",
                        bot=bot_display.lower(), reason=analysis.get("reason","")[:200])

    # Auto-pull Redis-логов бота — Силли видит что там происходило перед gap
    gap_log_context = ""
    try:
        if _r:
            from ai_office_shared.shared.identity import canonical
            bot_canon = canonical(bot_display)
            if bot_canon:
                gap_events = await read_logs(_r, bot_canon, days=1, limit=20)
                if gap_events:
                    gap_lines = []
                    for ev in gap_events[:15]:
                        ts = ev.get("ts","")[-8:]
                        gap_lines.append(f"[{ts}] {ev.get('event','?')} {ev.get('context',{})}")
                    gap_log_context = "\n\n[Последние события бота из Redis:]\n" + "\n".join(gap_lines)
                    logger.info(f"[gap] pulled {len(gap_events)} Redis events for {bot_canon}")
    except Exception as _ge:
        logger.warning(f"[gap] auto-pull failed for {bot_display}: {_ge}")

    # Объявляем что фиксим
    sent = await bot.send_message(
        chat_id=message.chat.id,
        text=f"🔧 {bot_display} — вижу проблему ({analysis.get('reason', '')}), "
             f"сейчас отвечу с актуальными данными..."
    )
    await remember_my_message(sent)

    try:
        # Немедленно отвечаем от имени бота с web search
        # Redis-контекст добавляем в system если есть — помогает понять причину gap
        enriched_system = bot_system + gap_log_context if gap_log_context else bot_system
        response = await get_claude().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=enriched_system,
            messages=[{"role": "user", "content": user_question}],
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
        )
        answer = "\n".join(
            block.text for block in response.content if hasattr(block, "text")
        ).strip()

        sent = await bot.send_message(
            chat_id=message.chat.id,
            text=f"{bot_display}:\n{answer}"
        )
        await remember_my_message(sent)

        # Фиксим код в фоне — следующий раз бот сам справится
        if repo_info:
            asyncio.create_task(_fix_bot_code_background(bot_display, repo_info))

    except Exception as e:
        logger.error(f"instant reply failed for {bot_display}: {e}")
        sent = await bot.send_message(
            chat_id=message.chat.id,
            text=f"❌ Не смог получить данные для {bot_display}: {e}"
        )
        await remember_my_message(sent)


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
            sent = await bot.send_message(
                chat_id=OFFICE_CHAT_ID,
                text=f"✅ Код {bot_display} обновлён — web search встроен, следующий раз сам справится."
            )
            await remember_my_message(sent)
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
        "/cc <задача> [@бот1 ...] — многофайловый рефактор через CC-subagent\n"
        "/approve_pr <id|all> — смержить PR из /cc\n"
        "/approve <id> — применить предложенный фикс\n"
        "/skip <id> — пропустить\n"
        "/update_all — обновить всех template-ботов по текущему шаблону"
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




@dp.message(F.text.startswith("/cc"))
async def cmd_cc(message: Message):
    """
    /cc <задача> [@бот1 @бот2 ...]
    Запускает CC-like многофайловый рефактор.

    Примеры:
      /cc добавь log_event в handle_message у всех ботов @билли @тилли
      /cc замени BOT_NAME на BOT_NAME_LOWER во всех bot.py @билли @крисс @доктор
      /cc обнови ai-office-shared до v0.1.2 в requirements.txt @билли @тилли @милли
    """
    from ai_office_shared.shared.identity import canonical, BOTS

    text = message.text[3:].strip()
    if not text:
        await message.answer(
            "Использование: `/cc <задача> [@бот1 @бот2 ...]`\n\n"
            "Примеры:\n"
            "• `/cc обнови shared до v0.1.2 @билли @тилли`\n"
            "• `/cc добавь log_event в handle_message @крисс @доктор`\n\n"
            "Если боты не указаны — спрошу список файлов явно.",
            parse_mode="Markdown"
        )
        return

    # Парсим упомянутых ботов из задачи
    import re as _re
    bot_mentions = _re.findall(r'@(\S+)', text)
    task_clean = _re.sub(r'@\S+', '', text).strip()

    file_specs = []
    for mention in bot_mentions:
        canon = canonical(mention.strip("@,.!?"))
        if canon and canon in BOTS:
            repo = BOTS[canon]["repo"]
            file_specs.append({"repo": repo, "path": "bot.py"})

    if not file_specs:
        await message.answer(
            f"⚠️ Не нашёл ботов в задаче. Укажи через @: `/cc {task_clean} @билли @тилли`",
            parse_mode="Markdown"
        )
        return

    repos_list = ", ".join(f"`{s['repo']}`" for s in file_specs)
    await message.answer(
        f"🤖 **CC-subagent запущен**\n\n"
        f"**Задача:** {task_clean}\n"
        f"**Файлы:** {repos_list}\n\n"
        f"⏳ Читаю файлы и генерирую изменения...",
        parse_mode="Markdown"
    )

    result = await multi_file_refactor(task_clean, file_specs,
                                        branch_suffix=bot_mentions[0] if bot_mentions else "")

    if "error" in result:
        await message.answer(f"❌ Ошибка: {result['error']}")
        return

    prs = result.get("prs", [])
    errors = result.get("errors", [])

    if not prs:
        await message.answer(f"⚠️ PR-ы не созданы.\nОшибки: {'; '.join(errors) if errors else 'нет изменений'}")
        return

    # Регистрируем PR-ы для /approve_pr
    pr_lines = []
    for item in prs:
        pr = item["pr"]
        pr_id = f"pr_{item['repo']}_{pr['number']}"
        pending_prs[pr_id] = {
            "repo": item["repo"],
            "pr_number": pr["number"],
            "branch": result["branch"],
            "html_url": pr["html_url"],
        }
        pr_lines.append(f"• [{item['repo']} #{pr['number']}]({pr['html_url']}) — {item['files']} файл(ов)")

    errors_text = f"\n\n⚠️ Ошибки: {'; '.join(errors)}" if errors else ""
    await message.answer(
        f"✅ **Готово!** {result['changed_files']} файл(ов) изменено\n\n"
        f"**PR-ы:**\n" + "\n".join(pr_lines) +
        f"\n\n**Summary:** {result.get('summary','')}\n\n"
        f"Для мержа: `/approve_pr {list(pending_prs.keys())[-1]}`" +
        errors_text,
        parse_mode="Markdown"
    )


@dp.message(F.text.startswith("/approve_pr"))
async def cmd_approve_pr(message: Message):
    """
    /approve_pr <id>  — мержит PR созданный через /cc
    /approve_pr all   — мержит все pending PR-ы
    """
    arg = message.text[11:].strip()

    if arg == "all":
        targets = list(pending_prs.items())
    elif arg in pending_prs:
        targets = [(arg, pending_prs[arg])]
    else:
        await message.answer(
            f"❌ PR `{arg}` не найден.\n"
            f"Pending PR-ы: {', '.join(pending_prs.keys()) or 'нет'}",
            parse_mode="Markdown"
        )
        return

    for pr_id, pr_data in targets:
        pending_prs.pop(pr_id, None)
        try:
            ok = await merge_pull_request(pr_data["repo"], pr_data["pr_number"],
                                           commit_msg=f"cc: approved by Влад")
            status = "✅ смержен" if ok else "⚠️ не смержен (проверь конфликты)"
            await message.answer(
                f"{status}: [{pr_data['repo']} #{pr_data['pr_number']}]({pr_data['html_url']})",
                parse_mode="Markdown"
            )
        except Exception as e:
            await message.answer(f"❌ Ошибка мержа {pr_data['repo']} #{pr_data['pr_number']}: {e}")


@dp.message(F.text.startswith("/skip"))
async def cmd_skip(message: Message):
    fix_id = message.text[5:].strip()
    if pending_fixes.pop(fix_id, None):
        await message.answer(f"⏭️ Фикс `{fix_id}` пропущен.")
    else:
        await message.answer(f"❌ Фикс `{fix_id}` не найден.")


@dp.message(F.text.startswith("/update_all"))
async def cmd_update_all(message: Message):
    """Обновить все template-боты по текущему BOT_TEMPLATE."""
    await message.answer("🔄 Запускаю обновление всех template-ботов...")
    async def progress(msg: str):
        await message.answer(msg)
    result = await update_all_template_bots(notify_func=progress)
    await message.answer(result)


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
    is_dm = message.chat.type == "private"
    # Перехват GROQ API ключа
    _msg_text = message.text or ""
    if _msg_text.strip().startswith("gsk_") and len(_msg_text.strip()) > 20:
        groq_key = _msg_text.strip()
        r = await get_redis()
        if r:
            await r.set("office:secrets:groq_api_key", groq_key, ex=86400*365)
        await message.reply("✅ GROQ_API_KEY сохранён")
        try:
            tg_cl = await get_telethon_client()
            # Userbot (аккаунт Влада) может удалять свои сообщения в любом диалоге
            # В личке с ботом — ищем диалог и удаляем сообщение с ключом
            bot_entity = await tg_cl.get_entity(f"@{bot_name}")
            msgs = await tg_cl.get_messages(bot_entity, limit=10)
            to_delete = [m.id for m in msgs if m.text and groq_key in m.text]
            if to_delete:
                await tg_cl.delete_messages(bot_entity, to_delete)
            # Также удаляем ответное сообщение бота "✅ GROQ_API_KEY сохранён"
            bot_msgs = await tg_cl.get_messages(bot_entity, limit=5, from_user="me")
            # from_user="me" не работает в личке — берём последние и фильтруем
            all_msgs = await tg_cl.get_messages(bot_entity, limit=5)
            bot_replies = [m.id for m in all_msgs if m.out and "GROQ" in (m.text or "")]
            if bot_replies:
                await tg_cl.delete_messages(bot_entity, bot_replies)
            await tg_cl.disconnect()
        except Exception:
            pass
        return

    # В группе — ТОЛЬКО если сообщение начинается с имени или явного тега
    # Игнорируем если просто упоминается в середине текста (чтобы не хватать чужие разговоры)
    if not is_dm:
        txt_lower = (message.text or "").lower().strip()
        is_direct = (
            txt_lower.startswith("силли") or
            txt_lower.startswith("cilly") or
            txt_lower.startswith("@cilly")
        )
        if not is_direct:
            return

    text = message.text
    for mention in ["силли,", "силли", "cilly,", "cilly", "@cilly_bot"]:
        text = text.replace(mention, "").strip()

    user_id = message.from_user.id

    # Сохраняем сообщение в историю
    if user_id not in dm_history:
        dm_history[user_id] = []
    dm_history[user_id].append({"role": "user", "content": text})
    if len(dm_history[user_id]) > DM_HISTORY_MAX:
        dm_history[user_id] = dm_history[user_id][-DM_HISTORY_MAX:]

    async def reply(msg: str):
        # Сохраняем ответ в историю
        dm_history[user_id].append({"role": "assistant", "content": msg})
        await message.answer(msg, parse_mode=None)

    await handle_natural_language(text, message.chat.id, reply, history=dm_history[user_id])


# ── HTTP endpoint for Filly routing (family bots → Cilly) ────────────────────
async def handle_cilly_task(request):
    """Filly routes natural language requests here from any bot."""
    try:
        data = await request.json()
    except Exception as parse_err:
        return web.json_response({"status": "error", "detail": f"json parse: {parse_err}"}, status=400)
    try:
        return await _handle_cilly_task_inner(data)
    except Exception as e:
        import traceback
        return web.json_response({"status": "error", "detail": str(e), "trace": traceback.format_exc()[-1000:]}, status=200)

async def _handle_cilly_task_inner(data):
    text    = data.get("message", "")
    chat_id = data.get("chat_id", "")   # нет дефолта — без chat_id шлём только JSON
    agent   = data.get("agent", "Unknown")
    silent  = data.get("silent", False)  # явный флаг тишины

    responses = []

    # /railway <gql> — ПЕРВЫЙ перехват, до LLM, не требует ANTHROPIC_API_KEY
    if text.strip().startswith("/railway"):
        gql_q = text.strip()[8:].strip()
        if not gql_q:
            return web.json_response({"status": "ok", "responses": ["Использование: /railway <graphql query>"]})
        try:
            rw_result = await railway_query(gql_q)
            out = json.dumps(rw_result.get("data") or rw_result, ensure_ascii=False, indent=2)
            if len(out) > 3000:
                out = out[:3000] + "\n...(обрезано)"
            return web.json_response({"status": "ok", "responses": [out]})
        except Exception as rw_e:
            return web.json_response({"status": "ok", "responses": [f"❌ Railway error: {rw_e}"]})
    async def collect(msg: str):
        responses.append(msg)
        # Шлём в чат ТОЛЬКО если chat_id явно передан И не silent
        if chat_id and not silent:
            try:
                await bot.send_message(chat_id=int(chat_id), text=msg, parse_mode=None)
            except Exception as e:
                logger.error(f"collect send_message failed: {e}")

    # Перехватываем GROQ API ключ — сохраняем в Redis
    if text.strip().startswith("gsk_") and len(text.strip()) > 20:
        groq_key = text.strip()
        # Сохраняем в Redis
        try:
            r_client = await get_redis()
            await r_client.set("office:config:GROQ_API_KEY", groq_key)
            redis_ok = True
        except Exception:
            redis_ok = False
        # Удаляем сообщение с ключом через Telethon
        deleted = False
        if chat_id:
            try:
                tg_cl = await get_telethon_client()
                msg_history = await tg_cl.get_messages(int(chat_id), limit=5)
                for msg in msg_history:
                    if msg.text and groq_key in msg.text:
                        await tg_cl.delete_messages(int(chat_id), [msg.id])
                        deleted = True
                        break
                await tg_cl.disconnect()
            except Exception:
                pass
        status = f"🔑 GROQ_API_KEY {'сохранён в Redis ✅' if redis_ok else '❌ Redis недоступен'}. Сообщение {'удалено 🗑' if deleted else 'не найдено'}."
        collect(status)
        responses.append(status)
        return web.json_response({"status": "ok", "responses": responses})

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
RAILWAY_SECRET = os.getenv("RAILWAY_TOKEN_VLAD", "") or os.getenv("RAILWAY_TOKEN", "")  # reuse existing Railway token as auth

async def handle_secrets(request):
    """Returns GH token to authenticated callers (Claude uses Railway token as key)."""
    auth = request.headers.get("X-Auth-Token", "")
    if not auth or auth != RAILWAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    return web.json_response({
        "GITHUB_TOKEN": os.getenv("GITHUB_TOKEN", ""),
        "GH_PAT": os.getenv("GH_PAT", ""),
        "RAILWAY_TOKEN_VLAD": os.getenv("RAILWAY_TOKEN_VLAD", ""),
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

async def handle_health(request):
    """Simple health check endpoint for external monitoring (Cloudflare Watchdog etc.)."""
    return web.json_response({"status": "ok", "service": "cilly-bot"})


# ── REACTIONS HANDLER: 👍/👎 на сообщения Силли → office:quality:силли ──────
@dp.message_reaction()
async def handle_reaction(reaction: MessageReactionUpdated):
    """Реакции на сообщения Силли — HASH up/down. Источник для feedback loop."""
    chat_id = reaction.chat.id
    msg_id  = reaction.message_id

    r = await get_redis()
    if r is None:
        return

    try:
        owner = await r.get(f"office:msg:{chat_id}:{msg_id}")
    except Exception as e:
        logger.warning(f"reaction owner lookup failed: {e}")
        return
    if owner != BOT_NAME_LOWER:
        return

    old_emojis = {x.emoji for x in (reaction.old_reaction or []) if getattr(x, "emoji", None)}
    new_emojis = {x.emoji for x in (reaction.new_reaction or []) if getattr(x, "emoji", None)}
    added   = new_emojis - old_emojis
    removed = old_emojis - new_emojis

    delta_up   = sum(1 for e in added if e in REACTION_UP)   - sum(1 for e in removed if e in REACTION_UP)
    delta_down = sum(1 for e in added if e in REACTION_DOWN) - sum(1 for e in removed if e in REACTION_DOWN)

    if delta_up == 0 and delta_down == 0:
        return

    try:
        key = f"office:quality:{BOT_NAME_LOWER}"
        if delta_up:
            await r.hincrby(key, "up", delta_up)
        if delta_down:
            await r.hincrby(key, "down", delta_down)
        logger.info(f"REACTION msg={msg_id} added={added} removed={removed} du={delta_up} dd={delta_down}")
    except Exception as e:
        logger.warning(f"quality hincrby failed: {e}")



async def handle_post_raw(request):
    """Send a raw message to any chat. Auth: X-Auth-Token = Railway token.
    Если передан bot_name — проксирует запрос на /send нужного бота.
    """
    auth = request.headers.get("X-Auth-Token", "")
    if not auth or auth != RAILWAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    data = await request.json()
    chat_id = data.get("chat_id")
    text = data.get("text", "")
    parse_mode = data.get("parse_mode", "HTML")
    bot_name = data.get("bot_name", "").upper()
    if not chat_id or not text:
        return web.json_response({"error": "chat_id and text required"}, status=400)
    if bot_name and bot_name in BOT_URLS:
        bot_url = BOT_URLS[bot_name].rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    f"{bot_url}/send",
                    json={"chat_id": int(chat_id), "text": text},
                    headers={"X-Secret-Token": HTTP_SECRET_BOTS},
                )
                return web.json_response(r.json())
        except Exception as e:
            return web.json_response({"error": f"proxy error: {e}"}, status=500)
    try:
        await bot.send_message(chat_id=int(chat_id), text=text, parse_mode=parse_mode)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)




async def handle_envcheck(request):
    """Диагностика: показывает какие env vars заданы (без значений)."""
    import os
    vars_set = []
    vars_missing = []
    for v in ["CODER_BOT_TOKEN","ANTHROPIC_API_KEY","REDIS_URL","OFFICE_CHAT_ID",
              "LESSONS_CHAT_ID","GH_PAT","RAILWAY_TOKEN_VLAD","YOUR_TELEGRAM_ID",
              "TELEGRAM_API_ID","TELEGRAM_API_HASH","TELETHON_SESSION","OLLAMA_ENABLED"]:
        if os.environ.get(v):
            vars_set.append(v)
        else:
            vars_missing.append(v)
    return web.json_response({"set": vars_set, "missing": vars_missing})

async def main():
    # Загружаем office:decisions из Redis при старте
    await init_office_decisions()
    asyncio.create_task(monitor_loop())
    asyncio.create_task(daily_audit_loop())
    # HTTP server for Filly routing
    app = web.Application()
    app.router.add_post("/task", handle_cilly_task)
    app.router.add_get("/secrets", handle_secrets)
    app.router.add_post("/post_raw", handle_post_raw)
    app.router.add_post("/promote_bots", handle_promote_bots)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/envcheck", handle_envcheck)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080)))
    await site.start()
    logger.info("[http] Cilly HTTP server started on :8080")
    # Weekly report handlers (/weekly, /approve, /skip)
    _redis_for_weekly = await get_redis()
    if _redis_for_weekly:
        register_weekly_handlers(dp, _redis_for_weekly, claude)
    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types()
    )


if __name__ == "__main__":
    asyncio.run(main())






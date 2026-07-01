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
from aiogram.types import (
    Message, MessageReactionUpdated, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.filters import CommandStart
from anthropic import AsyncAnthropic
import redis.asyncio as aioredis
from ai_office_shared.shared.logging import log_event, read_logs
from ai_office_shared.shared import taskboard as tb
from ai_office_shared.shared.auth import office_auth_middleware, office_headers

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

# Сервисы, живущие в ДРУГИХ Railway-проектах (после миграций) → их environment_id.
# Дефолт остаётся awake-happiness production (RAILWAY_ENV_ID).
SERVICE_ENV = {
    "1c08bbcc-32bb-4e91-9bc9-d196c937c1c4": "7ff2ff7a-b6d7-4c06-95c9-9958f0d3af7b",  # tilly-trader → trading-dept
}
def _env_for(service_id: str) -> str:
    """environment_id для редеплоя сервиса (дефолт — awake-happiness production)."""
    return SERVICE_ENV.get(service_id, RAILWAY_ENV_ID)

GITHUB_USER     = "unperson22-alt"
LESSONS_FILE    = "lessons/lessons.json"

MONITOR_INTERVAL   = 300  # секунд между проверками логов
TEMPLATE_BOTS_FILE = "shared/template_bots.json"  # реестр ботов созданных по шаблону

# Проактивная петля управления (management_loop): ревью доски задач + метрик
MANAGEMENT_INTERVAL = int(os.getenv("CILLY_MGMT_INTERVAL", "1800"))  # 30 мин
MGMT_STUCK_AFTER_SEC = int(os.getenv("CILLY_MGMT_STUCK_SEC", str(2 * 3600)))  # 2ч

# Railway service_id → (repo_name, main_file)
SERVICES = {
    "3319eabd-5bcb-4e59-839e-4813f1e7ef33": ("logger-bot",       "bot.py"),
    "367e25d7-8410-419d-896d-2cc86cd44efd": ("tilly-bot",        "bot.py"),
    "5d61d403-feee-455e-9c0d-523f0e7c79d5": ("filly-bot",        "bot.py"),
    "53551d10-478f-41e8-8d6c-a3102d6cbeb5": ("dilly-bot",        "bot.py"),  # doctor/Доктор — исправлен 2026-06-02
    "db277aff-6638-4b4a-970e-b016bd753608": ("milly-bot",        "bot.py"),
    "3dfc7336-2e91-4ade-950a-4f3d566baced": ("office-dashboard", "main.py"),
    "b441ce93-9736-49b3-9b5d-d0c82e715b28": ("billy-bot",        "bot.py"),
    "9db4108e-19f1-4c1f-a21c-3909442e137c": ("prophet-bot",      "bot.py"),
    "1c08bbcc-32bb-4e91-9bc9-d196c937c1c4": ("tilly-trader",     "bot.py"),  # trading-dept (миграция 2026-06)
    "2f647984-c08e-405c-aaa3-a2bffc7fdd14": ("mama-bot",         "bot.py"),  # Эллис — исправлен 2026-06-02
    "5533bc5f-24aa-4079-903b-50bcde4cdd01": ("pilly-bot",        "bot.py"),
    "92f70bbb-70ea-474c-be0d-5cc1c9bd8f4e": ("kriss-bot",        "bot.py"),
    "a5e37cc4-0a9f-4700-b6d3-d39b958ce0cb": ("villy-bot",        "bot.py"),
    "ed03c9d3-e83f-4675-9f0a-a4d4fc622365": ("gosling-bot",      "bot.py"),
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
# In-memory fallback — основной store теперь Redis (office:pending:{id}),
# чтобы pending-действия переживали рестарт Силли (см. stage_pending/pop_pending).
pending_fixes: dict = {}

# ── Redis-backed approval-гейт ──────────────────────────────────────────────
# Любое риск-действие (деплой кода, правка конфигов, update_instruction, delegate)
# стейджится сюда и ждёт /approve. Переживает рестарт. Привязано к задаче на доске.
import uuid as _uuid_mod

PENDING_TTL = 24 * 3600  # pending-действие живёт сутки

def _pending_key(action_id: str) -> str:
    return f"office:pending:{action_id}"


async def stage_pending(action_type: str, payload: dict, *, task_id: str = "",
                        title: str = "") -> str:
    """
    Кладёт риск-действие в очередь на подтверждение. Возвращает action_id.
    Пишет в Redis (переживает рестарт), при отсутствии Redis — in-memory fallback.
    """
    action_id = f"{action_type}_{int(time.time())}_{_uuid_mod.uuid4().hex[:4]}"
    entry = {
        "id": action_id,
        "type": action_type,
        "task_id": task_id,
        "title": title,
        "payload": payload,
        "created_at": int(time.time()),
    }
    r = await get_redis()
    if r:
        try:
            await r.setex(_pending_key(action_id), PENDING_TTL,
                          json.dumps(entry, ensure_ascii=False, default=str))
            return action_id
        except Exception as e:
            logger.warning(f"[pending] Redis stage failed, fallback to memory: {e}")
    pending_fixes[action_id] = entry
    return action_id


async def pop_pending(action_id: str) -> dict | None:
    """Извлекает и удаляет pending-действие. Ищет в Redis, затем в памяти."""
    r = await get_redis()
    if r:
        try:
            raw = await r.get(_pending_key(action_id))
            if raw:
                await r.delete(_pending_key(action_id))
                try:
                    return json.loads(raw)
                except Exception:
                    return None
        except Exception as e:
            logger.warning(f"[pending] Redis pop failed: {e}")
    return pending_fixes.pop(action_id, None)


# ── Inline-кнопки апрува (✅/⏭) ──────────────────────────────────────────────
# callback_data: "{domain}:{verb}:{ident}". domain: pg (office:pending) | pr (PR-мерж) |
# wk (weekly). verb: appr | decl. ident помещается в 64 байта (action_id/pr_id короткие).

def _approval_kb(domain: str, ident: str = "") -> InlineKeyboardMarkup:
    """Клавиатура ✅ Применить / ⏭ Отклонить для предложения."""
    suffix = f":{ident}" if ident else ""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Применить", callback_data=f"{domain}:appr{suffix}"),
        InlineKeyboardButton(text="⏭ Отклонить", callback_data=f"{domain}:decl{suffix}"),
    ]])


async def send_proposal(text: str, domain: str, ident: str = "", *, chat_id: int = 0) -> bool:
    """
    Шлёт предложение с inline-кнопками НАПРЯМУЮ (минуя буфер reply_func).
    chat_id=0 → в офис-группу (автономные предложения). Возвращает True при успехе.
    """
    target = chat_id or OFFICE_CHAT_ID
    if not target:
        return False
    if await outbound_paused():
        logger.info("send_proposal: подавлено (пауза)")
        return False
    try:
        sent = await bot.send_message(chat_id=target, text=text,
                                      reply_markup=_approval_kb(domain, ident))
        await remember_my_message(sent)
        return True
    except Exception as e:
        logger.error(f"send_proposal failed (domain={domain}): {e}")
        return False


async def _finish_cb(cb: CallbackQuery, status_line: str):
    """Дописывает результат к сообщению предложения и убирает кнопки."""
    try:
        base = cb.message.text or ""
        await cb.message.edit_text(f"{base}\n\n{status_line}", reply_markup=None)
    except Exception:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        try:
            await bot.send_message(cb.message.chat.id, status_line)
        except Exception:
            pass


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
            max_tokens=32000,   # было 8000 — полный файл в JSON усекался → битый код у Девви
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        # Усечение по лимиту → честная ошибка, а НЕ молчаливый обрезанный файл
        if getattr(response, "stop_reason", None) == "max_tokens":
            return {"error": "Вывод обрезан по лимиту токенов: файл слишком большой "
                             "для полной перезаписи. Нужна точечная правка, не перезапись целиком."}
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
— АНТИ-ГАЛЛЮЦИНАЦИЯ (КРИТИЧНО): никогда не утверждай, что создала файл, запушила/задеплоила код, отправила сообщение или выполнила любое действие, если ты ФАКТИЧЕСКИ не выполнила его в этом ответе. Не выдумывай коммиты и статусы «закинул/задеплоил/готово». Если действие не выполнено — скажи об этом прямо и коротко. Реальные деплои подтверждаются деплой-шагом (commit), а не твоим текстом.

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
• ai-office-shared — ТВОЙ репо. Твой код: agents/coder.py. Уроки: lessons/lessons.json

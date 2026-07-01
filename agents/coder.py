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
• filly-bot/bot.py — РОУТЕР. Здесь регистрируются все боты:
  - BOT_URLS, ROUTER_SYSTEM, DM_AGENT_SYSTEMS, _name_map

МАППИНГ БОТ → РЕПО (знай наизусть, никогда не угадывай):
  billy → billy-bot/bot.py
  kriss → kriss-bot/bot.py
  milly → milly-bot/bot.py
  villy → villy-bot/bot.py
  gosling → gosling-bot/bot.py
  эллис/мама/mama → mama-bot/bot.py
  doctor/dilly → dilly-bot/bot.py
  pilly → pilly-bot/bot.py
  tilly → tilly-bot/bot.py
  filly → filly-bot/bot.py
  prophet → prophet-bot/bot.py
  силли/cilly/ты сама → ai-office-shared/agents/coder.py
  ray → marketing-dept/ray/bot.py
  nelli → marketing-dept/nelli/bot.py
  marty → marketing-dept/marty/bot.py
  тилли-трейдер → tilly-trader/bot.py

ПРАВИЛО ПОИСКА РЕПО: если бот не нашёлся как отдельное репо → ищи в монорепо marketing-dept/, trading-dept/, family-dept/. НИКОГДА не ищи vlad-tg-bot, sillycms, tg-bot или другие несуществующие репо.

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

ТИХИЙ РЕЖИМ (обязательно):
— Когда выполняешь задачу от Влада — никаких промежуточных статусов. Только финальный результат.
— Когда задача пришла через HTTP /task (от Клода или автоматики) — вообще не пиши Владу ничего. Ни статусов, ни результатов. Молчи.
— В группу пишешь только финальные результаты которые реально нужны команде. Не «🧹 Чищу...», не «📚 Постю...».

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
{"is_bug":bool,"confidence":"high|low","bug_type":"crash|logic|config|network|external|unknown","description":"1-2 предл","affected_file":"path|null","fix_description":"конкретно","lesson_title":"","lesson_symptom":"","lesson_cause":"","lesson_fix":"","lesson_avoid":""}
high=явный crash/NameError/ImportError/SyntaxError/KeyError→автофикс. low=логика→спросить.
ВНЕШНЕЕ (НЕ наш баг): если корневая причина — недоступность СТОРОННЕГО сервиса (Telegram/Railway API, DNS, сеть: NetworkError, ConnectError, RemoteProtocolError, Bad Gateway, 502/503/504), а наш код её просто пробрасывает → is_bug=false, bug_type="external". Баг — ТОЛЬКО если НАШ код не обрабатывает сбой и крашится в цикле (CrashLoop).
Поля lesson_* (lesson_title/lesson_symptom/lesson_cause/lesson_fix/lesson_avoid) — ВСЕГДА на английском (English), даже если логи/контекст на русском."""

FIXER_PROMPT = """Фиксер Python кода. Верни ТОЛЬКО полный исправленный файл целиком. Минимум изменений — только то что нужно для фикса. Сохраняй стиль оригинала. Без markdown, без объяснений.

ЖЁСТКИЕ ПРАВИЛА (урок #5 — иначе бот крашится на старте):
- НИКАКИХ side-effects на уровне модуля. Любое чтение env (os.environ[...] / os.getenv) и любые сетевые/Redis-соединения — ТОЛЬКО внутри функций или main(), не на верхнем уровне файла.
- НЕ вводи новые обязательные переменные окружения, которых не было в оригинале. Не выдумывай имена переменных.
- Не превращай файл бота в скрипт/утилиту — сохраняй его исходное назначение и точку входа."""


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
            f"Write ALL text fields (title/symptom/root_cause/why_architecture/fix/prevention/cause) "
            f"in ENGLISH — translate if the input is in Russian. Keep code/identifiers/commit hashes as-is.\n"
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
{"intent":"push_code|fix_bot|create_bot|create_cron|add_external_bot|get_bot_token|deploy|read_file|list_files|redis_query|trader_winrate|dev_task|delegate|update_bot_instruction|answer","repo":"repo_name_or_null","path":"file_path_or_null","task":"task_description","bot":"имя_бота_или_null","instruction":"текст_инструкции_или_null","mode":"append|set|clear","confidence":0.0-1.0}

ГЛАВНОЕ ПРАВИЛО — различай вопрос и команду:
- ВОПРОС о процессе ("как создать бота?", "что нужно для деплоя?", "какой стек?", "как задеплоить?", "с чего начать?") → intent=answer
- КОМАНДА к действию ("создай бота", "задеплой", "залей код", "исправь баг") → соответствующий intent
Сигналы вопроса: как, какой, какие, что такое, зачем, почему, расскажи, объясни, с чего начать, какие шаги
Сигналы команды: создай, сделай, залей, задеплой, исправь, добавь, зарегистрируй

push_code=залить/обновить код, fix_bot=исправить баг, create_bot=ЯВНАЯ команда создать нового бота (не расписание!), create_cron=создать расписание/напоминание/cron для пользователя ("напоминай каждый день", "отправляй каждое утро", "напоминалка в X время") — создаёт Railway cron-сервис, add_external_bot=подключить внешнего бота, get_bot_token=зарегистрировать в BotFather, deploy=задеплоить, read_file=прочитать файл, list_files=список файлов, redis_query=запрос к Redis, post_lessons=прочитать lessons.json и отправить все уроки красиво в Bug Lessons группу (-5197140411), cleanup_group=удалить старые сообщения от ботов в группе через Telethon, cleanup_dm=удалить сообщения с ключами/секретами в личке (gsk_, GROQ, токен) через Telethon — ищет в диалоге с user_id=int(BOT_TOKEN.split(':')[0]) (сигналы: удали старые, почисти группу, удали сообщения до), send_group_message=отправить сообщение в Telegram-группу от имени бота (POST /post_raw {chat_id,text,bot_name} X-Auth-Token OFFICE_CHAT_ID=-5194783850 — выполнять ПРЯМО без генерации кода), edit_file=точечная замена строки в файле без чтения всего файла (сигналы: замени в файле, вставь после строки, patch, добавь в начало функции — когда указан repo+path+old+new), agentic_task=многошаговая задача из 2+ шагов: читай+делай, исправь+задеплой, залей+проверь, прочитай+перепиши. Сигналы: исправь и задеплой, залей код и задеплой, прочитай X и отправь, прочитай X и перепиши, пройдись по всем, для каждого, рефакторинг, аудит. ВАЖНО: если задача содержит И (исправить код И задеплоить) — это agentic_task. При чтении большого файла (bot.py 800+ строк) — не читать целиком в цикле, читать один раз и искать нужную функцию по имени, dev_task=делегировать задачу КОМАНДЕ разработки (Девви→Рикки→Тести→Секки→Скрибби). ТОЛЬКО когда речь о новой фиче/модуле/компоненте для продукта — НЕ о правке одного файла. Требует ВЫСОКОЙ уверенности (confidence>=0.85). Чёткие сигналы: "реализуй фичу", "разработай модуль", "напиши новый компонент", "сделай PR для", "задача для команды", "отдай команде", "dev-dept", "через цепочку". НЕЯСНЫЙ запрос ("сделай что-нибудь", "напиши функцию" без контекста) → confidence<0.85 → Силли переспрашивает. Если задача про правку существующего файла/бота — это push_code или agentic_task, НЕ dev_task. delegate=поручить задачу ГЛАВЕ ОТДЕЛА и проверить результат (НЕ написание кода). Сигналы: "спроси у Тилли", "пусть Милли посчитает", "делегируй Доктору", "поручи отделу", "узнай у <бот>". Заполни "bot" именем отдела. confidence>=0.85, иначе Силли переспросит. update_bot_instruction=изменить поведение бота на лету через инструкцию в системном промпте (БЕЗ редеплоя). Сигналы: "научи <бота>", "пусть <бот> всегда/больше не", "добавь <боту> правило", "обнови инструкцию <бота>", "запомни для <бота>". Заполни "bot" (кого учим), "instruction" (что добавить), "mode" (append по умолчанию; set=заменить; clear=сбросить). answer=ответить словами.
ВАЖНО redis_query: "прочитай Redis", "покажи quality", "health ботов", "office:*", "scan", "hgetall", "что в Redis" → redis_query.
ВАЖНО trader_winrate: "винрейт трейдера", "посчитай winrate", "проверь винрейт сигналов", "какой winrate у трейдера", "винрейт по сигналам", "статистика трейдера WR" → trader_winrate (читает signals:list/signal:* трейдера, считает WR по свечам, отдаёт за 7 дней и за всё время).
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

        # Пишем в Redis (не GitHub) — каждый push в GitHub = деплой Силли = 90 сек даунтайм
        r_ops = await get_redis()
        if r_ops:
            await r_ops.lpush("office:ops_log", entry)
            await r_ops.ltrim("office:ops_log", 0, 499)  # хранить последние 500 записей
        else:
            logger.warning("[ops_log] Redis недоступен, лог потерян")
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
        """, {"serviceId": service_id, "environmentId": _env_for(service_id)})
        return "errors" not in data
    except Exception as e:
        logger.error(f"redeploy failed for {service_id}: {e}")
        return False


async def connect_repo(service_id: str, repo: str, branch: str = "main") -> bool:
    """Привязать GitHub-репо к сервису и ВКЛЮЧИТЬ авто-деплой (serviceConnect).

    Проверено 2026-06-14 на tilly-trader: чинит выключенный авто-деплой —
    после этого push в branch снова автоматически катит деплой.
    repo в формате 'owner/name'. (serviceInstanceRedeploy для нового кода НЕ годится —
    пересобирает СТАРЫЙ коммит; выкат конкретного коммита — serviceInstanceDeployV2 + commitSha.)
    """
    try:
        data = await railway_query("""
            mutation($id: String!, $input: ServiceConnectInput!) {
              serviceConnect(id: $id, input: $input) { id }
            }
        """, {"id": service_id, "input": {"repo": repo, "branch": branch}})
        ok = "errors" not in data
        if ok:
            logger.info(f"connect_repo: auto-deploy enabled for {service_id} ({repo}@{branch})")
        return ok
    except Exception as e:
        logger.error(f"connect_repo failed for {service_id}: {e}")
        return False


async def deploy_commit(service_id: str, commit_sha: str) -> str | None:
    """Выкатить КОНКРЕТНЫЙ коммит (serviceInstanceDeployV2). Возвращает deploymentId или None.

    Нужен когда авто-деплой выключен/недоступен, а код уже в GitHub.
    """
    try:
        data = await railway_query("""
            mutation($s: String!, $e: String!, $c: String!) {
              serviceInstanceDeployV2(serviceId: $s, environmentId: $e, commitSha: $c)
            }
        """, {"s": service_id, "e": _env_for(service_id), "c": commit_sha})
        return data.get("data", {}).get("serviceInstanceDeployV2") if "errors" not in data else None
    except Exception as e:
        logger.error(f"deploy_commit failed for {service_id}: {e}")
        return None


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
BUG_LESSONS_CHAT = -5197140411  # Telegram-группа Bug Lessons — единая точка публикации уроков

def _format_lesson(l: dict) -> str:
    """Единый формат сообщения урока для Bug Lessons."""
    status_emoji = {"fixed": "✅", "still_relevant": "⚠️", "outdated": "🗄", "documented": "📝"}
    se = status_emoji.get(l.get("status", ""), "❓")
    return (
        f"🐛 Lesson #{l.get('id')} — {l.get('title', '?')}\n\n"
        f"📍 {l.get('bot', '?')} | {l.get('layer', '?')}\n\n"
        f"👁 Symptom:\n{l.get('symptom', '?')}\n\n"
        f"🔍 Root cause:\n{l.get('root_cause', l.get('cause', '?'))}\n\n"
        f"✅ Fix:\n{l.get('fix', '?')}\n\n"
        f"🛡 Prevention:\n{l.get('prevention', '?')}\n\n"
        f"{se} Status: {l.get('status', '?')}"
    )


async def publish_pending_lessons(reply_func=None, limit: int = 100) -> int:
    """Постит в Bug Lessons ТОЛЬКО уроки без флага posted_to_group, ставит флаг и
    коммитит lessons.json. Единый источник правды — сам файл (durable): переживает
    сброс Redis и НЕ может зафлудить (уже опубликованное помечено в git).

    Вызывается из аудита (Силли сама подтягивает новые уроки), из post_lesson и add_lessons.
    """
    from datetime import datetime as _dt, timezone as _tz
    try:
        raw = await read_file("ai-office-shared", LESSONS_FILE)
        lessons = json.loads(raw)
    except Exception as e:
        logger.error(f"publish_pending_lessons read failed: {e}")
        if reply_func:
            await reply_func(f"❌ Не могу прочитать lessons.json: {e}")
        return 0

    pending = [l for l in lessons if not l.get("posted_to_group")]
    if not pending:
        if reply_func:
            await reply_func(f"✅ Новых уроков нет — все {len(lessons)} уже в Bug Lessons")
        return 0

    capped = pending[:limit]
    posted = 0
    now_iso = _dt.now(_tz.utc).isoformat()
    for lesson in capped:
        try:
            await _GLOBAL_BOT.send_message(chat_id=BUG_LESSONS_CHAT, text=_format_lesson(lesson))
            lesson["posted_to_group"] = True
            lesson["posted_at"] = now_iso
            posted += 1
            await asyncio.sleep(0.8)
        except Exception as e:
            logger.error(f"publish_pending_lessons #{lesson.get('id')} failed: {e}")
            break  # непосланное НЕ помечаем; коммитим только то, что успели

    if posted:
        try:
            await push_file("ai-office-shared", LESSONS_FILE,
                            json.dumps(lessons, ensure_ascii=False, indent=2),
                            f"chore(lessons): mark {posted} posted_to_group")
        except Exception as e:
            logger.error(f"publish_pending_lessons commit failed: {e}")
    if reply_func:
        extra = f" (ещё {len(pending) - posted} в очереди)" if len(pending) > posted else ""
        await reply_func(f"✅ Опубликовано {posted} новых уроков в Bug Lessons{extra}")
    return posted


async def post_lesson(title: str, symptom: str, cause: str, context: str, fix: str, how_to_avoid: str):
    """Записывает урок в durable-историю (lessons.json) и публикует НОВЫЕ уроки.

    Публикация в Bug Lessons идёт ТОЛЬКО через publish_pending_lessons (идемпотентно по
    флагу posted_to_group), поэтому повторный вызов и аудит не задваивают сообщения.
    """
    # 1) durable-история: ждём, урок должен лечь в файл до публикации
    await append_lesson_ai(title, symptom, cause, context, fix, how_to_avoid)
    r = await get_redis()
    if r:
        await log_event(r, BOT_NAME_LOWER, "lesson_saved", title=title[:100])
    # 2) опубликовать новые (включая только что записанный) — идемпотентно по флагу
    try:
        await publish_pending_lessons()
    except Exception as e:
        logger.error(f"post_lesson publish failed: {e}")


async def publish_pending_on_startup():
    """Старт-задача: опубликовать pending-уроки (НИЧЕГО не удаляет).

    Идемпотентно — постит только уроки без posted_to_group. Нужна, чтобы при редеплое
    Силли сама дозалила в Bug Lessons новые/восстановленные уроки без ручных команд.
    Никакой авто-чистки/wipe здесь нет — чистка только дедупом по явной команде.
    """
    try:
        await asyncio.sleep(25)  # дать боту и сети подняться
        await publish_pending_lessons()
    except Exception as e:
        logger.error(f"[publish_startup] failed: {e}")


async def outbound_paused() -> bool:
    """Глобальный mute исходящих в офис-группу.
    True если env CILLY_PAUSED ∈ {1,true,yes} ИЛИ выставлен Redis-флаг cilly:paused."""
    if os.getenv("CILLY_PAUSED", "").lower() in ("1", "true", "yes"):
        return True
    try:
        r = await get_redis()
        if r and await r.get("cilly:paused"):
            return True
    except Exception:
        pass
    return False


async def notify_office(text: str):
    if not OFFICE_CHAT_ID:
        return
    if await outbound_paused():
        logger.info("notify_office: подавлено (CILLY_PAUSED/cilly:paused)")
        return
    try:
        sent = await bot.send_message(chat_id=OFFICE_CHAT_ID, text=text)
        await remember_my_message(sent)
    except Exception as e:
        logger.error(f"notify_office failed: {e}")

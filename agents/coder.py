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
from ai_office_shared.shared.json_extract import first_json_object
from ai_office_shared.shared.verify import extract_code
from ai_office_shared.shared.target_repo import repo_looks_valid, target_repo
from ai_office_shared.shared.telegram_text import split_for_telegram
from ai_office_shared.shared.fault_evidence import dispatch_refusal, failure_evidence
from ai_office_shared.shared.log_redaction import (
    install_secret_redaction, quiet_http_client_logs, redact,
)
from ai_office_shared.shared import dev_queue
from ai_office_shared.shared.file_window import (
    find_regions, summary as file_summary,
)
from ai_office_shared.shared.auth import office_auth_middleware, office_headers

from shared.github_tools import (
    push_file, read_file, list_files, create_repo,
    create_branch, push_file_to_branch, create_pull_request, merge_pull_request, get_pr_by_url,
    commit_exists, get_checks_status, get_default_branch,
)
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetDialogFiltersRequest, UpdateDialogFilterRequest
from telethon.tl.functions.channels import InviteToChannelRequest, EditAdminRequest
from telethon.tl.functions.messages import EditChatAdminRequest
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import DialogFilter, InputPeerUser, InputPeerChannel, ChatAdminRights

logging.basicConfig(level=logging.INFO)
# Порядок обязателен: basicConfig создаёт обработчики, а эти два вызова снимают
# с логов секреты. httpx печатает URL запроса на INFO — то есть токен бота в
# каждой строке про getUpdates (урок #118). Регистрация значений идёт по именам
# из SECRET_ENV_NAMES, поэтому вызов стоит ДО чтения конфига ниже.
quiet_http_client_logs()
install_secret_redaction()
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
# Сколько раз подряд агентная петля прощает модели неразобранный ответ.
# Ноль означал бы прежнее поведение: одна кривая реплика — и вся задача
# мертва. Больше двух смысла нет: если модель трижды не смогла вернуть
# один объект, дело не в случайности.
MAX_PARSE_RETRIES  = 2
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
    # trading-dept, бумажный режим. В HEALTH_URLS НЕ добавлен намеренно: публичного
    # домена у сервиса нет (serviceDomains пуст), HTTP-проверка дала бы ложный сбой.
    # Мониторинг логов идёт через Railway API и домена не требует.
    "86dc28a1-b283-4038-9b64-b91741583da7": ("molly-trader",     "bot.py"),
}

# Свой собственный сервис — ОТДЕЛЬНО от SERVICES, и это не оплошность.
#
# SERVICES — список того, что чинится АВТОМАТИЧЕСКИ (аудит, автохил). Меня там
# нет намеренно: иначе аудит принялся бы править мой же coder.py, что запрещено
# после 01.07 (урок #70 — гейт пропустил заглушку на 8 строк вместо файла на
# 5766, офис лёг целиком). Запрет остаётся.
#
# Но «не правлю себя автоматически» и «меня нельзя передеплоить по прямой
# просьбе» — разные вещи, а до 16.08 это было одно и то же: интент deploy искал
# service_id только в SERVICES и отвечал «Сервис ai-office-shared не найден».
# Поиск по имени (railway_get_service_id) тоже не помогает: он ходит в
# project(PROJECT_ID), а моего сервиса в этом проекте нет — там 13 ботов, и ни
# один из них не отдаёт домен ai-office-shared-production.up.railway.app.
#
# Значение по умолчанию здесь НЕ ЗАШИТО, и это принципиально. 16.08 сюда был
# записан efa6bd21-91d8-467f-8250-60f8a3853791 — id, взятый из переменной
# SILLI_SERVICE_ID вачдога. Проверка показала: у этого сервиса
# deletedAt = 2026-05-30, serviceInstances пуст, деплоев ноль. Правдоподобный
# мёртвый id хуже пустого: редеплой уходит в никуда, а вызывающий получает
# «ок». Пусто → честный отказ с указанием, что именно доставить.
#
# Кто поднимает меня, когда я лежу, — watchdog-bot: он опрашивает /health
# каждые 120 с и не зависит от того, отвечаю ли я на HTTP (урок #99). Но его
# SILLI_SERVICE_ID — тот самый мёртвый id, так что до его замены этот путь
# тоже не работает. Настоящий id знает только Railway UI: проект, в котором
# живёт этот сервис, моему токену не виден.
SELF_REPO       = "ai-office-shared"
SELF_SERVICE_ID = os.getenv("SELF_SERVICE_ID", "").strip()


def resolve_service_id(repo: str) -> str | None:
    """repo → Railway service_id для ЯВНОЙ операции (деплой по просьбе).

    Не для аудита и не для автохила: те ходят по SERVICES напрямую, и своего
    сервиса там по-прежнему нет. Разделение намеренное — «что чинить самому»
    и «что вообще существует» это два разных вопроса, и 16.08 они были
    склеены в один.
    """
    if repo == SELF_REPO:
        return SELF_SERVICE_ID or None
    return next((sid for sid, (r, _) in SERVICES.items() if r == repo), None)


def _render_railway_ids_block() -> str:
    """Блок RAILWAY IDs для системного промпта — генерируется из SERVICES.

    Единый источник истины: список всегда сходится с рабочим dict SERVICES,
    по которому аудит успешно ходит в Railway API. Раньше ID дублировались в
    промпте вручную и галлюцинировались (верные первые 8 hex, выдуманные хвосты)
    → Силли слала неверный serviceId → «Not Authorized». Больше руками не пишем.
    """
    lines = [
        "== RAILWAY IDs ==",
        "RAILWAY_TOKEN: из env RAILWAY_TOKEN_VLAD",
        "serviceId бери ТОЛЬКО из списка ниже или через deployments-запрос — "
        "НИКОГДА не вспоминай и не достраивай ID по памяти.",
        "Список сервисов (repo → serviceId), сгенерирован из SERVICES:",
    ]
    for sid, (repo, _main) in SERVICES.items():
        lines.append(f"  {repo}: {sid}")
    lines.append(
        "ВАЖНО: variableCollectionUpsert требует специальные права — "
        "наш токен НЕ ИМЕЕТ их. Не пытаться."
    )
    return "\n".join(lines)

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


# ── Совпадение по СЛОВУ, а не по подстроке ──────────────────────────────────
# 23.08.2026 просьба «выполни HSET … updated_at …» ушла в аудит качества ботов
# и вернула {"quality:билли": {"up": 9, "down": 1}}. Ветку выбрало условие
# `"up" in task_lower` — а "up" сидит внутри "updated_at". Тот же капкан ловит
# "group", "support", "дубль". Ровно от этого в target_repo.bots_named_in стоит
# граница слова, после того как «били» внутри «побили» дало Билли.
import re as _re_words


def _mentions(text: str, words) -> bool:
    """True, если в тексте есть хоть одно слово из списка — по границе слова.

    Не-буквенные метки (эмодзи, "up/down") границ слова не имеют, поэтому для
    них остаётся обычное вхождение: `\b` рядом с 👍 не работает вовсе.
    """
    low = (text or "").lower()
    if not low:
        return False
    for w in words:
        w = (w or "").strip().lower()
        if not w:
            continue
        if w[0].isalnum() and w[-1].isalnum():
            if _re_words.search(rf"(?<!\w){_re_words.escape(w)}(?!\w)", low):
                return True
        elif w in low:
            return True
    return False


# ── Гейт против схлопывания файла (инцидент coder.py: 5766 → 8 строк) ───────
# compile() + отсутствие NEEDS_FIX пропускают валидный, но катастрофически
# неполный файл. Перед стейджем и перед пушем сравниваем размер нового кода
# со старым: усадка сверх порога на непустом файле — блок + эскалация.

SHRINK_GUARD_RATIO = 0.7      # новый файл обязан сохранить ≥70% строк старого
SHRINK_GUARD_MIN_LINES = 50   # маленькие файлы не гейтим — там усадка легальна


def file_shrink_guard(old_src: str, new_src: str) -> str:
    """Возвращает описание проблемы, если новый код подозрительно меньше
    старого (признак обрезанного огрызка вместо полного файла), иначе ''."""
    old_lines = (old_src or "").count("\n") + 1 if (old_src or "").strip() else 0
    new_lines = (new_src or "").count("\n") + 1 if (new_src or "").strip() else 0
    if old_lines >= SHRINK_GUARD_MIN_LINES and new_lines < old_lines * SHRINK_GUARD_RATIO:
        return (f"новый файл схлопнулся: {old_lines} → {new_lines} строк "
                f"(порог {int(SHRINK_GUARD_RATIO * 100)}% от старого) — похоже на обрезанный код")
    return ""


# ── Гейт против битого пина зависимости (инцидент filly-bot: ai-office-shared@af0bd71) ──
# compile()/shrink НЕ ловят семантическую порчу requirements.txt: несуществующий SHA
# (например HEAD ЧУЖОГО репо) валиден синтаксически, но роняет `pip install` → build FAILED.
# Перед пушем резолвим каждый git+…@<ref> в НУЖНОМ репозитории через GitHub API.
import re as _re_pins

# git+https://github.com/<owner>/<name>(.git)@<ref>  (egg/подкаталог/пробелы — хвост игнорим)
_PIN_RE = _re_pins.compile(
    r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?@([^\s#&]+)"
)


async def validate_requirements_pins(content: str) -> str:
    """Возвращает описание проблемы, если хотя бы один git-пин в requirements
    ссылается на несуществующий коммит/ref в СВОЁМ репозитории, иначе ''.

    Закрывает Дефект 1 (порча зависимости) и Дефект 2 (SHA взят из чужого репо:
    HEAD filly-bot не резолвится в ai-office-shared → блок). Проверяем только репо
    владельца (unperson22-alt) — сторонние пины (PyPI, чужие GitHub) не трогаем.
    """
    problems = []
    for line in (content or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = _PIN_RE.search(s)
        if not m:
            continue
        owner, name, ref = m.group(1), m.group(2), m.group(3).rstrip("/")
        # Пин на репо-владельца сверяем через API текущего токена; чужих владельцев пропускаем.
        if owner != "unperson22-alt":
            continue
        ref_clean = ref.split("#", 1)[0].split("&", 1)[0]
        try:
            exists = await commit_exists(name, ref_clean)
        except Exception as e:
            problems.append(f"{name}@{ref_clean}: не удалось проверить ({e})")
            continue
        if not exists:
            problems.append(
                f"{name}@{ref_clean}: коммит/ref НЕ найден в репозитории {owner}/{name} "
                f"(битый пин → pip install упадёт, build FAILED)"
            )
    if problems:
        return "битый пин зависимости: " + "; ".join(problems)
    return ""


# ── Inline-кнопки апрува (✅/🕓/⏭) ────────────────────────────────────────────
# callback_data: "{domain}:{verb}:{ident}". domain: pg (office:pending) | pr (PR-мерж) |
# wk (weekly). verb: appr | defer | decl. ident помещается в 64 байта (action_id/pr_id короткие).

def _approval_kb(domain: str, ident: str = "", *, defer: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура ✅ Применить / (🕓 Отложить) / ⏭ Отклонить для предложения.

    Третья кнопка появляется ТОЛЬКО там, где «потом» — осмысленный ответ, то
    есть у заявок из входящей очереди отдела. «Отклонить» там означало бы «эта
    доработка не нужна вообще», и подсовывать его вместо «не сейчас» — значит
    терять заявки, которые владелец просто отложил.
    """
    suffix = f":{ident}" if ident else ""
    row = [InlineKeyboardButton(text="✅ Применить", callback_data=f"{domain}:appr{suffix}")]
    if defer:
        row.append(InlineKeyboardButton(text="🕓 Отложить", callback_data=f"{domain}:defer{suffix}"))
    row.append(InlineKeyboardButton(text="⏭ Отклонить", callback_data=f"{domain}:decl{suffix}"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


async def send_proposal(text: str, domain: str, ident: str = "", *, chat_id: int = 0,
                        defer: bool = False) -> bool:
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
                                      reply_markup=_approval_kb(domain, ident, defer=defer))
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

""" + _render_railway_ids_block() + """
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
БАГ — ЭТО ТО, ЧТО ПОКАЗАЛИ ЛОГИ. Исходник дан, чтобы ОБЪЯСНИТЬ наблюдавшийся отказ, а не чтобы искать в нём недостатки. Нет трейсбека/ошибки в логах → is_bug=false, bug_type="unknown", description="в логах нет отказа". Не пиши «в логах пусто, НО в коде видно…» — это ревью кода, а не разбор инцидента, и оно стоит отделу трёх попыток (23.08.2026: villy-bot был здоров, «await log(event,msg) вызвана всего с 2 аргументами» — функция объявлена с двумя параметрами, файл компилируется, pyflakes чист).
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
        return first_json_object(result) or {"match": False}
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
        lesson_obj = first_json_object(compact)
        if lesson_obj is None:
            # Урок, который не разобрался, дописывать в lessons.json нельзя:
            # файл читает Силли на каждом аудите, мусор в нём дороже пропуска.
            raise ValueError(f"урок не разобрался как JSON: {compact[:120]!r}")
        lessons.append(lesson_obj)
        await push_file("ai-office-shared", LESSONS_FILE, json.dumps(lessons, ensure_ascii=False, indent=2),
                        f"lesson({new_id}): {title[:50]}")
        logger.info(f"[lessons] saved lesson #{new_id}: {title}")
    except Exception as e:
        logger.error(f"append_lesson_ai failed: {e}")



INTENT_PROMPT = """Диспетчер AI-офиса. JSON без markdown:
{"intent":"push_code|fix_bot|create_bot|create_repo|create_cron|add_external_bot|get_bot_token|deploy|read_file|list_files|redis_query|board_task|trader_winrate|dev_task|delegate|update_bot_instruction|office_scan|answer","repo":"repo_name_or_null","path":"file_path_or_null","task":"task_description","bot":"имя_бота_или_null","instruction":"текст_инструкции_или_null","mode":"append|set|clear","confidence":0.0-1.0}

ГЛАВНОЕ ПРАВИЛО — различай вопрос и команду:
- ВОПРОС о процессе ("как создать бота?", "что нужно для деплоя?", "какой стек?", "как задеплоить?", "с чего начать?") → intent=answer
- КОМАНДА к действию ("создай бота", "задеплой", "залей код", "исправь баг") → соответствующий intent
Сигналы вопроса: как, какой, какие, что такое, зачем, почему, расскажи, объясни, с чего начать, какие шаги
Сигналы команды: создай, сделай, залей, задеплой, исправь, добавь, зарегистрируй

push_code=залить/обновить код, fix_bot=исправить баг, create_bot=ЯВНАЯ команда создать нового бота (не расписание!), create_repo=создать ПУСТОЙ GitHub-репозиторий и больше ничего. Сигналы: "создай репозиторий X", "заведи репо X", "нужен новый репозиторий X", "создай приватный репозиторий X". Имя репо клади в "repo". Это ОДИН вызов готового create_repo — НЕ создавай бота, НЕ ходи в BotFather, НЕ пушь шаблонный код и НЕ зови команду разработки. Если для репо нужен ещё и код, он придёт ОТДЕЛЬНОЙ задачей или его зальют снаружи. Отличие от create_bot: create_bot просят словом «бот» и он тянет за собой токен и Railway-сервис; здесь просят словом «репозиторий» и нужен только он, create_cron=создать расписание/напоминание/cron для пользователя ("напоминай каждый день", "отправляй каждое утро", "напоминалка в X время") — создаёт Railway cron-сервис, add_external_bot=подключить внешнего бота, get_bot_token=зарегистрировать в BotFather, deploy=задеплоить, read_file=прочитать файл, list_files=список файлов, redis_query=запрос к Redis, post_lessons=прочитать lessons.json и отправить все уроки красиво в Bug Lessons группу (-5197140411), cleanup_group=удалить старые сообщения от ботов в группе через Telethon, cleanup_dm=удалить сообщения с ключами/секретами в личке (gsk_, GROQ, токен) через Telethon — ищет в диалоге с user_id=int(BOT_TOKEN.split(':')[0]) (сигналы: удали старые, почисти группу, удали сообщения до), send_group_message=отправить сообщение в Telegram-группу от имени бота (POST /post_raw {chat_id,text,bot_name} X-Auth-Token OFFICE_CHAT_ID=-5194783850 — выполнять ПРЯМО без генерации кода), edit_file=точечная замена строки в файле без чтения всего файла (сигналы: замени в файле, вставь после строки, patch, добавь в начало функции — когда указан repo+path+old+new), agentic_task=многошаговая задача из 2+ шагов: читай+делай, исправь+задеплой, залей+проверь, прочитай+перепиши. Сигналы: исправь и задеплой, залей код и задеплой, прочитай X и отправь, прочитай X и перепиши, пройдись по всем, для каждого, рефакторинг, аудит. ВАЖНО: если задача содержит И (исправить код И задеплоить) — это agentic_task. При чтении большого файла (bot.py 800+ строк) — не читать целиком в цикле, читать один раз и искать нужную функцию по имени, dev_task=делегировать задачу КОМАНДЕ разработки (Девви→Рикки→Тести→Секки→Скрибби). ТОЛЬКО когда речь о новой фиче/модуле/компоненте для продукта — НЕ о правке одного файла. Требует ВЫСОКОЙ уверенности (confidence>=0.85). Чёткие сигналы: "реализуй фичу", "разработай модуль", "напиши новый компонент", "сделай PR для", "задача для команды", "отдай команде", "dev-dept", "через цепочку". НЕЯСНЫЙ запрос ("сделай что-нибудь", "напиши функцию" без контекста) → confidence<0.85 → Силли переспрашивает. Если задача про правку существующего файла/бота — это push_code или agentic_task, НЕ dev_task. Просьба СОЗДАТЬ РЕПОЗИТОРИЙ — это create_repo, а НЕ dev_task: писать код для этого не надо, у меня есть готовый вызов (31.07.2026 «создай репозиторий molly-trader» ушло в dev_task с confidence 0.92, команда три попытки писала скрипт с выдуманной организацией, репозиторий так и не появился). delegate=поручить задачу ГЛАВЕ ОТДЕЛА и проверить результат (НЕ написание кода). Сигналы: "спроси у Тилли", "пусть Милли посчитает", "делегируй Доктору", "поручи отделу", "узнай у <бот>". Заполни "bot" именем отдела. confidence>=0.85, иначе Силли переспросит. update_bot_instruction=изменить поведение бота на лету через инструкцию в системном промпте (БЕЗ редеплоя). Сигналы: "научи <бота>", "пусть <бот> всегда/больше не", "добавь <боту> правило", "обнови инструкцию <бота>", "запомни для <бота>". Заполни "bot" (кого учим), "instruction" (что добавить), "mode" (append по умолчанию; set=заменить; clear=сбросить). office_scan=самопроверка офиса по команде владельца: просканировать боты на ошибки и предложить фиксы (каждый уходит на /approve). Сигналы: "проверь офис", "просканируй ботов", "что сломалось", "самопроверка", "проверь всех ботов", "почини <бот>" (тогда заполни "bot"). Свип НЕ применяет фиксы сам — только предлагает. answer=ответить словами.
ВАЖНО redis_query: "прочитай Redis", "покажи quality", "health ботов", "office:*", "scan", "hgetall", "что в Redis" → redis_query. redis_query ТОЛЬКО ЧИТАЕТ. Любая просьба ИЗМЕНИТЬ задачу на доске — это board_task, а не redis_query.
board_task=операция над задачей доски по её id. Сигналы: "верни задачу X в работу", "переоткрой задачу X", "задача X снова в очередь", "reopen X". Id — 12 hex-символов, клади его в "task". Заполни "mode": reopen (вернуть в очередь, сбросив счётчик попыток) — единственный поддерживаемый режим. Это НЕ произвольная запись в Redis: доступна ровно одна названная операция.
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



# Пробник доступности Railway. НЕ спрашивать `me` — это АККАУНТНЫЙ уровень, и
# токен с областью «Projects» отвечает на него Not Authorized, будучи при этом
# полностью рабочим для всего, что офису нужно (projects, deployments,
# variables, редеплой). 02.08.2026 из-за этого аудит два дня подряд объявлял
# живой токен истёкшим и требовал его перевыпустить. Спрашиваем то, что офис
# реально использует: список проектов.
RAILWAY_PROBE = "{ projects { edges { node { id } } } }"


async def _railway_is_available() -> bool:
    """Быстрая проверка доступности Railway API (timeout 8 сек).

    Railway отдаёт HTTP 200 и с ошибкой в теле, поэтому одного кода мало —
    проверяем, что в ответе нет `errors`."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0)) as c:
            r = await c.post(
                "https://backboard.railway.com/graphql/v2",
                headers={"Authorization": f"Bearer {RAILWAY_TOKEN}", "Content-Type": "application/json"},
                json={"query": RAILWAY_PROBE},
            )
            if r.status_code != 200:
                return False
            return not (r.json() or {}).get("errors")
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


async def get_service_logs(service_id: str, window_s: float | None = None) -> list[str]:
    """Логи сервиса. Два режима отбора — выбирается наличием window_s.

    window_s=None — ВОДЯНОЙ ЗНАК (по умолчанию, режим monitor_loop): отдаёт только
      то, что появилось с прошлого чтения, и сдвигает отметку last_seen. Монитору
      нужен поток без повторов, иначе один баг детектился бы каждые 5 минут.

    window_s=N — ОКНО: отдаёт логи за последние N секунд и отметку НЕ трогает.

    🔴 Зачем понадобился второй режим. Отметка last_seen ОБЩАЯ для всех, кто зовёт
    эту функцию, а вычитывание её сдвигает. Монитор ходит раз в 5 минут, аудит —
    дважды в сутки, поэтому монитор почти всегда успевал вычерпать логи первым, и
    аудит видел пустоту. Он сообщал «ошибок нет» не потому, что их не было, а
    потому что их уже съел другой потребитель. Проверялось это ровно наоборот:
    molly-trader показывал ошибки в каждом отчёте — единственный сервис, которого
    НЕ было в SERVICES, то есть чью отметку никто не двигал.

    Окно ничего не потребляет, поэтому аудит и монитор больше не мешают друг другу.
    """
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

    if window_s is not None:
        cutoff = time.time() - window_s
        r = None                      # окно ничего не потребляет — Redis не нужен
    else:
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
            # Чужие логи чистим на входе: боты печатают токен в строках httpx, а
            # отсюда строка уезжает в промпт анализатора, в отчёт и в чат (#118).
            new_logs.append(redact(l.get("message", "")))
            if ts > latest_ts:
                latest_ts = ts
    if window_s is not None:
        return new_logs               # 🔴 отметку НЕ двигаем: иначе окно съело бы
                                      # логи у монитора и он ослеп бы вместо аудита
    r = await get_redis()
    if r:
        await r.set(f"last_seen:{service_id}", latest_ts)
    else:
        last_seen[service_id] = latest_ts
    return new_logs


_ENV_CACHE: dict[str, str] = {}


async def resolve_env_id(service_id: str) -> str:
    """
    environmentId сервиса — со СПРАВКОЙ у Railway, а не по таблице.

    Зачем не хватает `_env_for`: он для незнакомого сервиса молча отдаёт
    production awake-happiness. Пока чинились только сервисы из SERVICES, это
    работало. Как только появился редеплой чужих отделов (family-dept,
    marketing-dept, medical-dept — у каждого СВОЙ проект и своё окружение),
    дефолт стал тихо неверным ответом: мутация ушла бы в другой проект.
    Молча неправильный ID хуже отсутствующего — ошибки нет, эффекта тоже.

    Порядок: статическая таблица (быстро, для своих) → запрос → дефолт.
    """
    known = SERVICE_ENV.get(service_id)
    if known:
        return known
    if service_id in _ENV_CACHE:
        return _ENV_CACHE[service_id]
    try:
        data = await railway_query(
            "query($sid:String!){ service(id:$sid){ project{ environments{ edges{ node{ id name } } } } } }",
            {"sid": service_id})
        edges = (((data.get("data") or {}).get("service") or {})
                 .get("project") or {}).get("environments", {}).get("edges") or []
        envs = {e["node"]["name"]: e["node"]["id"] for e in edges}
        env_id = envs.get("production") or (next(iter(envs.values()), "") if envs else "")
        if env_id:
            _ENV_CACHE[service_id] = env_id
            return env_id
        logger.warning("[railway] у сервиса %s не нашлось окружений", service_id)
    except Exception as e:
        logger.warning("[railway] не смог определить environment для %s: %s", service_id, e)
    return _env_for(service_id)


async def redeploy_service(service_id: str) -> bool:
    """Передеплоить сервис через Railway API."""
    try:
        data = await railway_query("""
            mutation($serviceId: String!, $environmentId: String!) {
              serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId)
            }
        """, {"serviceId": service_id, "environmentId": await resolve_env_id(service_id)})
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


async def _escalate_vlad(text: str) -> None:
    """Прямое сообщение владельцу (Влад) в личку — для алертов/эскалаций.
    Никогда не бросает и не идёт в офис-группу. Работает даже при source=CLAUDE:
    это не спам в группу, а адресный алерт владельцу."""
    try:
        uid = int(os.getenv("YOUR_TELEGRAM_ID", "391077101") or "391077101")
        await bot.send_message(chat_id=uid, text=text, parse_mode=None)
    except Exception as e:
        logger.error(f"_escalate_vlad failed: {e}")


# ── Дефект 4: проверка статуса сборки после деплоя ────────────────────────────
_DEPLOY_TERMINAL_OK   = {"SUCCESS"}
_DEPLOY_TERMINAL_FAIL = {"FAILED", "CRASHED", "REMOVED", "SKIPPED"}


async def wait_for_deploy(service_id: str, *, timeout: float = 180.0,
                          poll: float = 6.0) -> tuple[str, list[str]]:
    """Опрашивает статус последнего деплоя сервиса до терминального.

    Переиспользует существующий GraphQL-запрос deployments{edges{node{id status}}}
    (ср. get_service_logs). Возвращает (status, build_log_tail). status ∈
    {SUCCESS, FAILED, CRASHED, REMOVED, SKIPPED, TIMEOUT, UNKNOWN}. На не-SUCCESS
    подтягивает хвост deploymentLogs для диагностики. Никогда не бросает.
    """
    deadline = time.monotonic() + timeout
    last_status = "UNKNOWN"
    dep_id = None
    while time.monotonic() < deadline:
        try:
            data = await railway_query("""
                query($id: String!) {
                  deployments(input: { serviceId: $id }) {
                    edges { node { id status createdAt } }
                  }
                }
            """, {"id": service_id})
            edges = (data.get("data") or {}).get("deployments", {}).get("edges", [])
            if edges:
                node = edges[0]["node"]
                dep_id = node.get("id")
                last_status = (node.get("status") or "UNKNOWN").upper()
                if last_status in _DEPLOY_TERMINAL_OK:
                    return last_status, []
                if last_status in _DEPLOY_TERMINAL_FAIL:
                    break
        except Exception as e:
            logger.warning(f"wait_for_deploy poll error ({service_id}): {e}")
        await asyncio.sleep(poll)
    else:
        last_status = last_status if last_status in _DEPLOY_TERMINAL_FAIL else "TIMEOUT"

    # Не-SUCCESS → тянем хвост логов сборки для диагностики.
    log_tail: list[str] = []
    if dep_id:
        try:
            log_data = await railway_query("""
                query($id: String!) {
                  deploymentLogs(deploymentId: $id) { message timestamp }
                }
            """, {"id": dep_id})
            logs = (log_data.get("data") or {}).get("deploymentLogs", []) or []
            log_tail = [redact(l.get("message", "")) for l in logs][-20:]
        except Exception as e:
            logger.warning(f"wait_for_deploy logs error ({service_id}): {e}")
    return last_status, log_tail


async def redeploy_and_verify(service_id: str, service_name: str = "") -> bool:
    """redeploy_service + проверка статуса сборки. FAILED/timeout → эскалация Владу.

    Возвращает True только при подтверждённом SUCCESS. Заменяет «выстрелил и забыл»
    там, где деплой автономный (Силли не видела бы упавшую сборку — Дефект 4).
    """
    name = service_name or service_id
    ok = await redeploy_service(service_id)
    if not ok:
        await _escalate_vlad(f"⚠️ {name}: мутация редеплоя не принята Railway. Сборка не запущена.")
        return False
    status, tail = await wait_for_deploy(service_id)
    if status == "SUCCESS":
        return True
    tail_txt = ("\n".join(tail))[-1500:] if tail else "(логи недоступны)"
    await _escalate_vlad(
        f"🔴 {name}: деплой завершился статусом {status}.\n"
        f"Автоматический откат не делаю — нужно решение.\n\n"
        f"Хвост build-логов:\n{tail_txt}"
    )
    return False


# ── Дефект 1+2: безопасный автономный пуш правок бот-репозиториев через PR ──────
async def safe_autonomous_push(repo: str, path: str, content: str, message: str,
                               *, service_id: str = "", reply_func=None,
                               silent: bool = False) -> str:
    """Единая безопасная точка автономной правки чужого бот-репозитория.

    Вместо прямого push_file→redeploy в main:
      1. ai-office-shared → отказ (свой код правится ТОЛЬКО вручную, правило #70).
      2. Валидация: shrink-гейт (код) + пин-гейт (requirements) → провал = НЕ пушим,
         эскалация Владу, возврат ошибки.
      3. Пуш в новую ветку → PR → регистрация в pending_prs + кнопки апрува Владу.
    Возвращает человекочитаемый статус (никогда не бросает).
    """
    async def _say(msg: str):
        if reply_func and not silent:
            try:
                await reply_func(msg)
            except Exception:
                pass

    if repo in ("ai-office-shared", "ai_office_shared"):
        note = ("🚫 Правки ai-office-shared (код Силли) — только вручную: "
                "ветка → PR → ревью Влада (правило #70). Автономно не пушу.")
        await _say(note)
        return note

    # ── Валидация перед любым коммитом ───────────────────────────────────────
    if path.endswith("requirements.txt") or path.endswith("requirements"):
        pin_problem = await validate_requirements_pins(content)
        if pin_problem:
            note = f"⛔ Блок пуша {repo}/{path}: {pin_problem}"
            await _escalate_vlad(
                f"⛔ Силли заблокировала автономный пуш в {repo}/{path}.\n{pin_problem}\n"
                f"Коммит НЕ сделан. Нужна корректная ревизия."
            )
            await _say(note)
            return note

    if path.endswith(".py"):
        try:
            old_src = await read_file(repo, path)
        except Exception:
            old_src = ""
        shrink = file_shrink_guard(old_src, content)
        if shrink:
            note = f"⛔ Блок пуша {repo}/{path}: {shrink}"
            await _escalate_vlad(
                f"⛔ Силли заблокировала автономный пуш в {repo}/{path}.\n{shrink}\nКоммит НЕ сделан."
            )
            await _say(note)
            return note

    # ── Ветка → PR (без прямого пуша в main, без авто-деплоя) ────────────────
    try:
        branch = f"cilly/auto-{_uuid_mod.uuid4().hex[:8]}"
        await create_branch(repo, branch, "main")
        await push_file_to_branch(repo, path, content, message, branch)
        pr = await create_pull_request(
            repo,
            title=message[:70] or f"cilly: правка {path}",
            body=(f"Автономная правка Силли, поставлена на ревью (правило #0).\n\n"
                  f"Файл: `{path}`\nЗадача: {message}\n\n"
                  f"Пуш в main и деплой — только после мержа Владом."),
            head_branch=branch,
        )
    except Exception as e:
        note = f"❌ Не удалось создать PR для {repo}/{path}: {e}"
        await _escalate_vlad(note)
        await _say(note)
        return note

    pr_id = f"pr_{repo}_{pr['number']}"
    pending_prs[pr_id] = {
        "repo": repo,
        "pr_number": pr["number"],
        "branch": branch,
        "html_url": pr["html_url"],
    }
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Мержить {repo} #{pr['number']}",
                             callback_data=f"pr:appr:{pr_id}"),
        InlineKeyboardButton(text="⏭ Отклонить", callback_data=f"pr:decl:{pr_id}"),
    ]])
    try:
        uid = int(os.getenv("YOUR_TELEGRAM_ID", "391077101") or "391077101")
        await bot.send_message(
            chat_id=uid,
            text=(f"🔎 Силли подготовила автономную правку и ждёт ревью:\n"
                  f"{repo}/{path}\nЗадача: {message}\n{pr['html_url']}"),
            reply_markup=kb, parse_mode=None,
        )
    except Exception as e:
        logger.error(f"safe_autonomous_push notify failed: {e}")

    note = (f"✅ PR на ревью: {repo} #{pr['number']} — {pr['html_url']} "
            f"(пуш в main и деплой после апрува Влада)")
    await _say(note)
    return note


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
    obj = first_json_object(raw)
    if obj is None:
        raise ValueError(f"анализатор вернул не JSON: {raw[:120]!r}")
    return obj


async def generate_fix(source_code: str, fix_description: str) -> str:
    prompt = f"Описание бага: {fix_description}\n\nИсходный код:\n{source_code}"
    # Opus только для генерации фикса — критично чтобы код был правильным
    return await ask_claude(prompt, system=FIXER_PROMPT, model="claude-opus-4-6")


# ── Генератор точной задачи для dev-команды ─────────────────────────────────
# Причина, почему «команда тупит»: пайплайн Девви→Рикки работает ровно настолько
# хорошо, насколько точна постановка. Свободная формулировка «исправь баг» даёт
# размытый результат и лишние NEEDS_FIX-циклы. Этот хелпер превращает диагноз в
# claude-grade атомарную спеку (по CLAUDE_WORK_PROTOCOL: один файл, точное место,
# симптом→причина→что изменить→ожидаемый результат→критерий приёмки).
PRECISE_TASK_PROMPT = """Ты — техлид, ставишь задачу джуну-кодеру для параллельной dev-команды.
На входе диагноз бага и исходный код одного файла. Верни ОДНУ атомарную задачу текстом (без markdown-заголовков),
строго по структуре и по-русски:

ФАЙЛ: <repo>/<path> (ровно один файл)
СИМПТОМ: <что наблюдается, 1 строка>
КОРНЕВАЯ ПРИЧИНА: <точное место — функция/строка/условие — и почему ломается>
ЧТО ИЗМЕНИТЬ: <минимальная правка: какую функцию/блок и как; одно логическое изменение>
ОЖИДАЕМЫЙ РЕЗУЛЬТАТ: <как ведёт себя код после фикса>
КРИТЕРИЙ ПРИЁМКИ: <проверяемое условие: компилируется, симптом исчез, легаси не сломано>
ВЕРНИ: полный исправленный файл целиком, минимум изменений, ничего не выбрасывая.

Правила: не выдумывай места, которых нет в коде; если причина не локализуется однозначно —
укажи наиболее вероятную функцию по симптому. Никаких TODO/заглушек. Только сам текст задачи."""


async def build_precise_dev_task(analysis: dict, source_code: str, repo: str, affected: str) -> str:
    """Строит точную атомарную постановку задачи для dev-команды из диагноза.
    Fail-silent: при любой ошибке возвращает базовую формулировку (без регресса)."""
    service = analysis.get("affected_file") or f"{repo}/{affected}"
    description = analysis.get("description", "")
    fix_desc = analysis.get("fix_description", "")
    fallback = (
        f"Исправь баг в {repo}/{affected}.\n"
        f"Симптом: {description}\n"
        f"Что нужно сделать: {fix_desc}\n"
        f"Верни ПОЛНЫЙ исправленный файл целиком, минимум изменений."
    )
    try:
        prompt = (
            f"repo/path: {repo}/{affected}\n"
            f"Диагноз: {json.dumps(analysis, ensure_ascii=False)}\n\n"
            f"Исходный код файла:\n{source_code[:16000]}"
        )
        spec = await ask_claude(prompt, system=PRECISE_TASK_PROMPT,
                                model="claude-sonnet-4-6")
        spec = (spec or "").strip()
        # Санити: спека должна быть содержательной и упоминать файл — иначе фолбэк.
        if len(spec) >= 60 and ("ФАЙЛ" in spec or affected in spec):
            return spec
        return fallback
    except Exception as e:
        logger.warning(f"build_precise_dev_task fallback: {e}")
        return fallback


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
    failed: list = []
    consecutive = 0
    now_iso = _dt.now(_tz.utc).isoformat()
    for lesson in capped:
        try:
            # Урок #91 форматируется в 5035 символов при лимите Telegram 4096:
            # 12.08 он встал первым в очереди и запер за собой всё остальное на
            # одиннадцать дней. Режем по абзацам, а не обрезаем: потерянный хвост
            # выглядел бы как целый урок.
            for part in split_for_telegram(_format_lesson(lesson)):
                await _GLOBAL_BOT.send_message(chat_id=BUG_LESSONS_CHAT, text=part)
                await asyncio.sleep(0.4)
            lesson["posted_to_group"] = True
            lesson["posted_at"] = now_iso
            posted += 1
            consecutive = 0
            await asyncio.sleep(0.8)
        except Exception as e:
            # Раньше здесь стоял break: непосланное не помечалось (это верно), но
            # отказ ОДНОЙ записи становился отказом всех последующих. Пропускаем
            # её — флаг всё равно ставится только после успешной отправки, — а
            # останавливаемся лишь когда сыпется подряд: это уже не кривая запись,
            # а недоступный Telegram, и долбиться в него бессмысленно.
            logger.error(f"publish_pending_lessons #{lesson.get('id')} failed: {e}")
            failed.append((lesson.get("id"), str(e)[:120]))
            consecutive += 1
            if consecutive >= 3:
                logger.error("publish_pending_lessons: 3 отказа подряд, останавливаюсь")
                break

    if posted:
        try:
            await push_file("ai-office-shared", LESSONS_FILE,
                            json.dumps(lessons, ensure_ascii=False, indent=2),
                            f"chore(lessons): mark {posted} posted_to_group")
        except Exception as e:
            logger.error(f"publish_pending_lessons commit failed: {e}")
    if failed:
        # Тихая остановка неотличима от «новых уроков нет» — ровно поэтому
        # публикация стояла одиннадцать дней при единственном logger.error.
        names = ", ".join(f"#{lid}: {why}" for lid, why in failed[:5])
        await _escalate_vlad(
            f"⚠️ Уроки не ушли в Bug Lessons ({len(failed)} из {len(capped)}):\n{names}\n"
            f"Опубликовано за этот заход: {posted}."
        )
    if reply_func:
        extra = f" (ещё {len(pending) - posted} в очереди)" if len(pending) > posted else ""
        fail_note = f", не ушло {len(failed)}" if failed else ""
        await reply_func(f"✅ Опубликовано {posted} новых уроков в Bug Lessons{extra}{fail_note}")
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


# ── Приёмка dev-задачи: гейт пайплайна превращается в улики на доске ─────────
# Пайплайн и так считает три вещи: компилируется ли финальный код, нет ли у
# Рикки вердикта NEEDS_FIX и не схлопнулся ли файл. До сих пор эти результаты
# жили только в локальных переменных: гейт срабатывал, но на доске оставалось
# голое «awaiting_approval», а потом «done» — слово, за которым нечего
# перепроверить. Теперь то же самое пишется критериями ДО работы и уликами
# после, поэтому tb.update_status(..., "done") может отказать, если улик нет.
# Один источник правды с dev_queue.GATE_ACCEPTANCE: две копии этих строк
# разъехались бы молча — улика ищется по тексту критерия, и задача просто
# перестала бы закрываться, не сказав почему.
DEV_ACCEPTANCE = list(dev_queue.GATE_ACCEPTANCE)


def extract_ricky_code(ricky_result: str) -> str:
    """Финальный код из ответа Рикки.

    Раньше здесь стоял срез от «```python» до ближайшего «```». Он промахивался
    трижды подряд, а пайплайн объявлял «Рикки не вернул финальный код» — хотя
    код был:

      • «```py» и голый «```» не совпадали с литералом вовсе;
      • забор ``` не вложенный, поэтому файл, который сам содержит тройные
        кавычки, обрезался на внутреннем заборе и отдавал обрубок.

    Ровно это 01.08.2026 дало три попытки «unterminated triple-quoted string» —
    и тогда же в shared/verify.py появился extract_code: он берёт несколько
    кандидатов (внешний срез и самый длинный блок) и предпочитает тот, что
    компилируется. Сюда его просто не принесли, и наивный срез остался в двух
    местах на 16.08, когда команда шесть раз подряд «не давала код» по
    villy-bot.

    Отдельно — ответ вида «ERROR: …»: воркер так сообщает, что не смог довести
    код до проверки. Кода в нём нет намеренно, и говорить ему в ответ «ты забыл
    забор» бессмысленно — retry_feedback ниже отдаёт настоящую причину.
    """
    if not ricky_result:
        return ""
    if ricky_result.lstrip().startswith("ERROR:"):
        return ""
    return extract_code(ricky_result)


def ricky_failure_reason(ricky_result: str) -> str:
    """Почему кода нет — словами воркера, а не догадкой вызывающего."""
    text = (ricky_result or "").strip()
    if not text:
        return "Рикки не ответил вообще (пустой ответ от воркера)."
    if text.startswith("ERROR:"):
        # Воркер уже назвал причину и приложил последнюю ошибку компилятора.
        return f"Рикки вернул отказ: {text[:400]}"
    return ("Рикки не вернул финальный код: в ответе нет распознаваемого блока "
            "кода. Верни ПОЛНЫЙ файл одним ```python-блоком.")


def dev_failure_reason(pipe: dict) -> str:
    """Почему кода нет — по ответам ВСЕЙ цепочки, а не одного Рикки.

    23.08.2026 заявка 62ffa25b5e30 упала с диагнозом «Рикки не ответил вообще»,
    хотя все пять воркеров отвечали на /health. Причина диагноза, а не сбоя:
    при провале Девви `dev_pipeline` обрывает фан-аут, кладёт
    `final_code_artifact=""` и ключа `ricky` в ответе не оставляет вовсе —
    а вызывающий звал `ricky_failure_reason("")`, которая по пустой строке
    всегда отвечает «Рикки не ответил». Отчёт указывал на воркера, которого
    в этом прогоне даже не вызывали.

    Урок #100 ровно про это: если дальняя сторона сказала, почему упала —
    цитируй её; если промолчала — так и скажи, но не выдумывай виноватого.
    """
    devvy = (pipe.get("devvy") or "").strip() if isinstance(pipe, dict) else ""
    if not devvy:
        return ("Девви не ответил вообще (пустой ответ от воркера) — "
                "остальные воркеры не вызывались.")
    if devvy.startswith("ERROR:"):
        return (f"Девви вернул отказ: {devvy[:400]} — фан-аут не запускался, "
                f"Рикки/Тести/Секки в этом прогоне не участвовали.")
    return ricky_failure_reason(pipe.get("final_code_artifact") or pipe.get("ricky") or "")


async def record_dev_gate_evidence(
    redis_client, board_id: str, *,
    final_code: str, compile_ok: bool, se_info: str,
    review_ok: bool, ricky_result: str, shrink_info: str,
) -> None:
    """
    Переложить уже посчитанные результаты гейта в улики на доске.

    Проверяющий — `tb.VERIFIER_GATE`, а не dev-dept: все три проверки
    детерминированы (compile(), поиск подстроки, счёт строк), у них нет
    интереса в исходе. Улика — наблюдённое значение, а не «проверено».

    Fail-silent: доска вторична по отношению к самой работе.
    """
    if not redis_client or not board_id:
        return

    # 🔴 Кода нет — значит НИ ОДНА из трёх проверок не исполнялась, и ни одна
    # не имеет права отчитаться «пройдено». Иначе выходит ровно то, что 23.08
    # увидел владелец по заявке 62ffa25b5e30: «compile() ок, 0 строк», «NEEDS_FIX
    # не встречается» и «размер в пределах гейта» — три галочки за пустоту.
    # Механика была такая: compile_ok инициализируется True и сбрасывается лишь
    # внутри `if final_code`, пустая строка не содержит NEEDS_FIX, а гейт размера
    # при пустом коде не вызывается вовсе. Проверка, которой не на чем было
    # исполниться, — провал, а не пропуск (урок #93).
    has_code = bool((final_code or "").strip())
    nothing_to_check = "финального кода нет — проверять было нечего"
    proofs = [
        (DEV_ACCEPTANCE[0], compile_ok and has_code,
         f"compile() ок, {len(final_code.splitlines())} строк" if (compile_ok and has_code)
         else f"SyntaxError: {se_info}" if se_info else nothing_to_check),
        (DEV_ACCEPTANCE[1], review_ok and has_code,
         "NEEDS_FIX в вердикте не встречается" if (review_ok and has_code)
         else f"вердикт Рикки: {(ricky_result or '')[:150]}" if has_code
         else nothing_to_check),
        (DEV_ACCEPTANCE[2], (not shrink_info) and has_code,
         "размер файла в пределах гейта" if ((not shrink_info) and has_code)
         else shrink_info or nothing_to_check),
    ]
    for criterion, passed, proof in proofs:
        try:
            await tb.add_evidence(redis_client, board_id, criterion,
                                  passed=passed, proof=proof,
                                  checked_by=tb.VERIFIER_GATE)
        except Exception as e:
            logger.warning("[board] улика %r не записалась: %s", criterion[:40], e)


async def close_task_reported(redis_client, task_id: str, result: str = "") -> bool:
    """
    Закрыть задачу — и, если приёмка не пропустила, сказать это вслух.

    `tb.update_status(..., "done")` умеет ОТКАЗАТЬ: критерии заморожены, а улик
    по ним нет. Молчаливый отказ — худший исход из возможных: задача остаётся
    висеть в awaiting_approval, а офис уверен, что она закрыта, потому что
    деплой-то прошёл. Отказ обязан быть виден там же, где случился.
    """
    ok = await tb.update_status(redis_client, task_id, "done", result=result)
    if not ok:
        t = await tb.get_task(redis_client, task_id)
        await notify_office(
            f"⚠️ Задача [{task_id}] не закрывается — приёмка не пройдена:\n"
            + (tb.format_evidence_report(t) if t else "задача не найдена на доске")
        )
    return ok


MAX_DEV_ATTEMPTS = 3


async def run_dev_chain(
    task_text: str, *, repo: str, file_path: str, context: str,
    board_id: str, task_id: str, redis_client,
    max_attempts: int = MAX_DEV_ATTEMPTS, stage_label: str = "попытка",
    seed_feedback: str = "", on_attempt_fail=None,
) -> dict:
    """
    Один прогон отдела до прохождения гейта: Девви → [Рикки‖Тести‖Секки] → Скрибби,
    с ретраями и конкретным фидбеком, и с уликами на доске в конце.

    Зачем функция: этот цикл был скопирован ДВАЖДЫ — в автофиксе краша и в
    intent'е dev_task, — а очередь заявок стала бы третьей копией. Ровно на этом
    16.08.2026 сгорел день: hardened-парсер жил в shared, а наивный срез остался
    у двух вызывающих, и «Рикки не вернул код» повторялось часами, пока Рикки
    возвращал код (урок #100). Дублирующаяся копия — это та, которую забудут
    починить.

    Args:
        seed_feedback: причина, с которой заходим на ПЕРВУЮ попытку (например
            имена упавших джоб CI прошлого раунда). Пусто — обычный первый заход.
        on_attempt_fail: async (attempt, max_attempts, feedback) -> None —
            вызывается после каждой отклонённой попытки. Провал колбэка не
            останавливает работу.

    Returns:
        dict: ok, infra_error, final_code, commit_msg, pipe, attempts, reason,
              compile_ok, review_ok, shrink_info, ricky_result, se_info

        infra_error непустой — работа не начиналась: сломан доступ к LLM, а не
        код. Это не провал отдела, и раунд на него не тратится.
    """
    from ai_office_shared.shared.dev_pipeline import (
        run_dev_pipeline, unrecoverable_provider_error,
    )
    from ai_office_shared.shared.dev_activity import publish_activity

    uid = int(os.getenv("YOUR_TELEGRAM_ID", "391077101"))

    final_code = ""
    review_ok = compile_ok = False
    shrink_info = ""
    commit_msg = ""
    retry_feedback = seed_feedback or ""
    ricky_result = ""
    se_info = ""
    infra_error = ""
    pipe: dict = {}
    attempt = 0

    while attempt < max_attempts:
        attempt += 1
        cur_task = task_text if not retry_feedback else (
            f"{task_text}\n\n[ПОВТОР #{attempt}] Предыдущая попытка отклонена:\n{retry_feedback}"
        )
        await publish_activity(redis_client, task_id, "силли", "plan",
                               f"{stage_label} {attempt}/{max_attempts}: {repo or '—'}/{file_path}")

        pipe = await run_dev_pipeline(
            cur_task, repo=repo, file_path=file_path,
            context=context, user_id=uid,
            redis_client=redis_client, task_id=task_id,
        )
        ricky_result = pipe.get("final_code_artifact", "") or pipe.get("ricky", "")
        commit_msg = pipe.get("commit_msg", "")
        final_code = extract_ricky_code(ricky_result)

        # Гейт качества: вердикт Рикки + компиляция + размер файла
        review_ok = "NEEDS_FIX" not in (ricky_result or "").upper()
        compile_ok = True
        se_info = ""
        if final_code and file_path.endswith(".py"):
            try:
                compile(final_code, file_path, "exec")
            except SyntaxError as _se:
                compile_ok = False
                se_info = f"{_se.msg}, строка {_se.lineno}"
        # Валидный, но схлопнувшийся файл (инцидент 5766 → 8 строк) не проходит
        shrink_info = file_shrink_guard(context, final_code) if final_code else ""

        if final_code and review_ok and compile_ok and not shrink_info:
            retry_feedback = ""
            break

        # Сбой доступа к LLM повтором не лечится: у аккаунта не появятся деньги
        # оттого, что мы спросим ещё раз. 23.08 две заявки потратили на это ~70
        # минут и ~19 вызовов Девви, а владелец прочитал «команда не дала код» —
        # то есть обвинение отдела вместо счёта за электричество. Обрываем сразу
        # и НЕ тратим раунд: попытка, которой не дали случиться, не попытка.
        infra_error = unrecoverable_provider_error(
            pipe.get("devvy"), pipe.get("ricky"), pipe.get("testi"),
            pipe.get("sekky"), pipe.get("scribbi"),
        )
        if infra_error:
            retry_feedback = (
                f"⚙️ Это не код, а доступ к LLM: {infra_error}. "
                f"Повторы бессмысленны, пока доступ не восстановлен. "
                f"Ответ воркера: {(pipe.get('devvy') or '')[:200]}"
            )
            await publish_activity(redis_client, task_id, "силли", "error",
                                   f"сбой провайдера: {infra_error}", level="error")
            break

        # Провал гейта: считаем попытку, формируем фидбек на следующий заход
        await tb.incr_attempts(redis_client, board_id)
        if not final_code:
            retry_feedback = dev_failure_reason(pipe)
        elif not review_ok:
            retry_feedback = "Рикки вернул NEEDS_FIX — код требует доработки. " + ricky_result[:600]
        elif not compile_ok:
            retry_feedback = f"Финальный код не компилируется: {se_info}. Похоже воркер обрезал код."
        else:
            retry_feedback = (f"Гейт размера: {shrink_info}. "
                              f"Верни ПОЛНЫЙ файл целиком, ничего не выбрасывая.")
        if on_attempt_fail is not None:
            try:
                await on_attempt_fail(attempt, max_attempts, retry_feedback)
            except Exception as e:
                logger.warning(f"[dev_chain] колбэк провала упал: {e}")

    ok = bool(final_code and review_ok and compile_ok and not shrink_info)

    # Результаты гейта → улики на доске. Пишем ВСЕГДА, а не только на успехе:
    # проваленная проверка — тоже факт, и без неё на доске не видно, почему
    # задача не закрывается.
    await record_dev_gate_evidence(
        redis_client, board_id, final_code=final_code, compile_ok=compile_ok,
        se_info=se_info, review_ok=review_ok, ricky_result=ricky_result,
        shrink_info=shrink_info,
    )

    return {
        "ok": ok,
        "infra_error": infra_error,
        "final_code": final_code,
        "commit_msg": commit_msg,
        "pipe": pipe,
        "attempts": attempt,
        "reason": retry_feedback,
        "compile_ok": compile_ok,
        "review_ok": review_ok,
        "shrink_info": shrink_info,
        "ricky_result": ricky_result,
        "se_info": se_info,
    }


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


# ── Auto-fix pipeline ──────────────────────────────────────────────────────────
async def handle_bug(service_id: str, service_name: str, repo: str, main_file: str,
                     analysis: dict, *, proposal_chat_id: int = 0) -> dict:
    """Основная логика: автофикс или запрос подтверждения.

    proposal_chat_id: куда слать предложение с кнопками. 0 → офис-группа (автономный
    monitor_loop/аудит, поведение по умолчанию). При явной команде владельца
    (/office scan|fix) сюда передаётся его чат, чтобы находка и /approve пришли туда.

    Возвращает dict-итог для консолидированного отчёта свипа:
    {"bot", "status": proposed|blocked|blocked_decision|error, "pending_id"?, "symptom", "fix_desc"?}.
    Существующие вызыватели (monitor_loop, аудит) значение игнорируют.
    """
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
            return {"bot": service_name, "status": "blocked_decision",
                    "symptom": blocked.get("do_not", "")}

    try:
        source_code = await read_file(repo, affected)
    except Exception as e:
        logger.error(f"Can't read {repo}/{affected}: {e}")
        return {"bot": service_name, "status": "error",
                "symptom": f"не смог прочитать {repo}/{affected}: {e}"}

    # ── Фикс генерит НЕ соло-Opus, а параллельная команда dev-dept с ревью ──
    # Девви → [Рикки ‖ Тести ‖ Секки] → Скрибби. Предложение в офис уходит
    # ТОЛЬКО после прохождения гейта (вердикт Рикки ≠ NEEDS_FIX + compile()).
    # Так аудит становится командным, а в чат не попадает неотревьюенный код.
    import uuid as _uuid

    r_tb     = await get_redis()
    _task_id = _uuid.uuid4().hex[:12]
    board_id = await tb.create_task(
        r_tb, f"Фикс {service_name}: {fix_desc[:80]}",
        created_by="силли", assignee="dev-dept",
        status="in_progress", task_id=_task_id,
        acceptance=DEV_ACCEPTANCE,   # заморожены ДО работы, см. record_dev_gate_evidence
    ) or _task_id

    # Точная спека для команды (claude-grade): атомарная, один файл, симптом→причина→
    # что изменить→ожидаемый результат→критерий приёмки. Fail-silent → базовая формулировка.
    devvy_task = await build_precise_dev_task(analysis, source_code, repo, affected)

    chain = await run_dev_chain(
        devvy_task, repo=repo, file_path=affected, context=source_code,
        board_id=board_id, task_id=_task_id, redis_client=r_tb,
        stage_label="аудит-фикс попытка",
    )
    final_code     = chain["final_code"]
    retry_feedback = chain["reason"]
    pipe           = chain["pipe"]

    if chain["ok"]:
        # Гейт пройден — стейджим деплой на /approve (контракт deploy_fix без изменений).
        fix_id = await stage_pending("deploy_fix", {
            "service_id": service_id,
            "service_name": service_name,
            "repo": repo,
            "affected": affected,
            "fixed_code": final_code,
            "analysis": analysis,
        }, task_id=board_id, title=f"Фикс {service_name}")
        await tb.update_status(r_tb, board_id, "awaiting_approval")
        _testi = (pipe.get("testi") or "")[:120].replace("\n", " ")
        _sekky = (pipe.get("sekky") or "")[:120].replace("\n", " ")
        await send_proposal(
            f"🤔 Cilly нашёл баг в {service_name}:\n\n"
            f"{description}\n\n"
            f"Предлагаемый фикс: {fix_desc}\n\n"
            f"✅ Прошёл ревью команды (Рикки OK + compile).\n"
            f"🧪 Тести: {_testi or '—'}\n"
            f"🔐 Секки: {_sekky or '—'}\n\n"
            f"Применить? (или текстом: /approve {fix_id})",
            "pg", fix_id, chat_id=proposal_chat_id,  # 0 → офис-группа; иначе чат владельца
        )
        return {"bot": service_name, "status": "proposed", "pending_id": fix_id,
                "symptom": description, "fix_desc": fix_desc}
    else:
        # Гейт НЕ пройден — НЕ предлагаем неотревьюенный код, эскалируем владельцу.
        await tb.update_status(r_tb, board_id, "blocked",
                               result=retry_feedback or "рабочий код не получен", escalated=True)
        if chain.get("infra_error"):
            await notify_office(
                f"⚙️ Cilly: фикс для *{service_name}* не начинался — "
                f"{chain['infra_error']}. Это не код и не команда: отдел не смог "
                f"обратиться к модели.\nСимптом: {description[:160]}"
            )
        else:
            await notify_office(
                f"⛔ Cilly: баг в *{service_name}* — команда за {MAX_DEV_ATTEMPTS} попыток "
                f"не дала код, прошедший ревью. Нужен твой разбор.\n"
                f"Симптом: {description[:160]}\nПричина провала: {retry_feedback[:200]}"
            )
        return {"bot": service_name, "status": "blocked",
                "symptom": description, "fix_desc": fix_desc}


# ── Monitor loop ───────────────────────────────────────────────────────────────
ERROR_PATTERNS = ["Traceback", "Error:", "Exception:", "CRITICAL", "crashed", "exit code"]

# Окна аудита. Аудит НЕ должен потреблять логи водяным знаком: он ходит дважды в
# сутки, а монитор — раз в 5 минут, и монитор всегда успевал бы первым, оставляя
# аудиту пустоту (см. get_service_logs).
AUDIT_LOG_WINDOW_S    = float(os.getenv("AUDIT_LOG_WINDOW_S", 2 * 3600))   # ошибки в отчёт
AUDIT_LESSON_WINDOW_S = float(os.getenv("AUDIT_LESSON_WINDOW_S", 24 * 3600))  # поиск паттернов и диагностика

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


def strip_ignored_tracebacks(logs: list[str]) -> list[str]:
    """Выбрасывает шумовые трейсбеки ЦЕЛИКОМ, оставляя всё остальное.

    Зачем: ERROR_PATTERNS начинается с "Traceback", а строка
    `Traceback (most recent call last):` не содержит ни одного IGNORE_PATTERN.
    Поэтому построчный фильтр пропускал стек-трейсы от Conflict/TimedOut дальше,
    и они уходили на анализ. Прежнее лечение было грубым — при любой шумовой
    строке пропускался ВЕСЬ сервис на цикл; так как телеграм-бот почти всегда
    имеет в логе TimedOut или Conflict после рестарта, монитор переставал
    смотреть на ботов вовсе (03.08.2026: 20 пропусков за один деплой у 4 ботов).

    Здесь шум удаляется блоком: трейсбек выкидывается вместе с телом, если его
    строка ИСКЛЮЧЕНИЯ попадает в IGNORE_PATTERNS. Настоящие ошибки, стоящие в том
    же логе рядом, остаются и доходят до анализа.
    """
    out: list[str] = []
    i, n = 0, len(logs)
    while i < n:
        line = logs[i]
        if "Traceback (most recent call last)" in line:
            # Тело трейсбека — строки с отступом; исключение — первая строка без него.
            j = i + 1
            while j < n and (not logs[j].strip() or logs[j][:1] in (" ", "\t")):
                j += 1
            exc_line = logs[j] if j < n else ""
            end = j + 1 if j < n else n
            if any(p in exc_line for p in IGNORE_PATTERNS):
                i = end                      # весь блок — шум
                continue
            out.extend(logs[i:end])          # настоящая ошибка — сохраняем целиком
            i = end
            continue
        if not any(p in line for p in IGNORE_PATTERNS):
            out.append(line)
        i += 1
    return out

# Внешние/транзиентные сбои — НЕ наш баг. Если корневая причина в недоступности
# стороннего сервиса (Telegram/Railway API, DNS, сеть), а бот жив — Силли МОЛЧИТ
# (по требованию владельца), а не предлагает фикс. Список шире IGNORE_PATTERNS:
# ловит сбои не только на polling/getUpdates, но и при отправке/любых POST.
EXTERNAL_FAULT_PATTERNS = [
    "telegram.error.NetworkError",
    "NetworkError",
    "httpx.ConnectError",
    "httpx.ConnectTimeout",
    "httpx.ReadTimeout",
    "httpx.RemoteProtocolError",
    "httpcore.RemoteProtocolError",
    "ConnectTimeout",
    "ReadTimeout",
    "RemoteProtocolError",
    "Server disconnected",
    "Bad Gateway",
    " 502",
    " 503",
    " 504",
    "getaddrinfo failed",
    "Temporary failure in name resolution",
    "Connection reset by peer",
    "Connection aborted",
]


def classify_fault(error_logs: list[str]) -> str:
    """Внешний транзиентный сбой vs наш баг.

    Возвращает "external" если корневая причина — недоступность стороннего
    сервиса (Telegram/Railway API, DNS, сеть) И в логах НЕТ признаков нашего
    структурного бага (NameError/ImportError/SyntaxError/KeyError/AttributeError).
    Иначе "internal".
    """
    text = "\n".join(error_logs)
    OUR_BUG_MARKERS = (
        "NameError", "ImportError", "ModuleNotFoundError", "SyntaxError",
        "IndentationError", "KeyError", "AttributeError", "TypeError",
        "ValueError", "IndexError", "UnboundLocalError",
    )
    if any(m in text for m in OUR_BUG_MARKERS):
        return "internal"
    if any(p in text for p in EXTERNAL_FAULT_PATTERNS):
        return "external"
    return "internal"


# Cooldown для внешних/известных-урочных сбоев — не дёргаемся по кругу (секунды).
EXTERNAL_FAULT_COOLDOWN = 6 * 3600  # 6 часов тишины по сигнатуре

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
    obj = first_json_object(raw)
    if obj is None:
        raise ValueError(f"анализатор ответа вернул не JSON: {raw[:120]!r}")
    return obj

# Имя бота в группе → репо + файл
BOT_REPOS = {
    "тилли":  ("tilly-bot",  "bot.py"),
    "билли":  ("billy-bot",  "bot.py"),
    "милли":  ("milly-bot",  "bot.py"),
    "доктор": ("dilly-bot",  "bot.py"),  # репо dilly-bot, не doctor-bot
    "эллис":  ("mama-bot",   "bot.py"),  # репо mama-bot (ellice-bot в Railway)
    "мама":   ("mama-bot",   "bot.py"),
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
    "pilly-bot":        "https://pilly-bot-production.up.railway.app/health",
    "logger-bot":       "https://logger-bot-production.up.railway.app/health",
    "office-dashboard": "https://office-dashboard-production-b571.up.railway.app/health",
    "mama-bot":         "https://ellice-bot-production.up.railway.app/health",    # Эллис
    "dilly-bot":        "https://dilly-bot-production-4a9b.up.railway.app/health", # Доктор
    "kriss-bot":        "https://kriss-bot-production.up.railway.app/health",
    "filly-bot":        "https://filly-bot-production.up.railway.app/health",
    "gosling-bot":      "https://gosling-bot-production.up.railway.app/health",
    "villy-bot":        "https://villy-bot-production.up.railway.app/health",
    "milly-bot":        "https://milly-bot-production.up.railway.app/health",
    "tilly-bot":        "https://tilly-bot-production.up.railway.app/health",
    "tilly-trader":     "https://tilly-trader-production.up.railway.app/health",
}

async def _all_office_services() -> list:
    """[(service_id, name)] по ВСЕМ проектам-отделам (для аудита/скана логов на баги).
    Фолбэк на статический SERVICES, если Railway API недоступен."""
    try:
        d = await railway_query("{ projects { edges { node { services { edges { node { id name } } } } } } }")
        out = []
        for pe in ((d.get("data") or {}).get("projects") or {}).get("edges") or []:
            for se in (pe["node"].get("services") or {}).get("edges") or []:
                out.append((se["node"]["id"], se["node"]["name"]))
        return out or [(sid, repo) for sid, (repo, _) in SERVICES.items()]
    except Exception as e:
        logger.error(f"_all_office_services: {e}")
        return [(sid, repo) for sid, (repo, _) in SERVICES.items()]


async def _handle_health_failures(health_fail: list[str], lines: list[str]) -> list[str]:
    """Реакция аудита на упавшие health-чеки — тот же конвейер, что для деплоев.

    1. Retry (2 раунда по 45с) — транзиентные сбои (рестарт/сеть в момент
       проверки) не эскалируем: сервис ожил → тихая пометка в отчёт.
    2. classify_fault по логам — внешний сбой (Telegram/Railway/DNS) = не наш
       баг, молчим без делегирования.
    3. Наш баг → анализ Haiku → редеплой (can_autofix) или делегирование
       команде через fix_bot; действия попадают в отчёт аудита.
    4. Redis-cooldown по сигнатуре — вечерний аудит не дублирует эскалацию
       утреннего.

    Возвращает список стойких сбоев: только они окрашивают статус офиса.
    """
    import hashlib

    # ── 1. Retry: отсеиваем транзиентные сбои ────────────────────────────────
    still_failing = {e.partition(":")[0]: e for e in health_fail}
    for _round in range(2):
        if not still_failing:
            break
        await asyncio.sleep(45)
        async with httpx.AsyncClient(timeout=10) as c:
            for name in list(still_failing):
                url = HEALTH_URLS.get(name)
                if not url:
                    continue
                try:
                    r = await c.get(url)
                    if r.status_code == 200:
                        detail = still_failing.pop(name).partition(":")[2]
                        lines.append(
                            f"🌐 *{name}* — был недоступен в момент проверки ({detail}), "
                            f"восстановился сам (transient), не эскалирую"
                        )
                except Exception:
                    pass

    persistent: list[str] = []
    repo_to_sid = {repo: sid for sid, (repo, _) in SERVICES.items()}
    try:
        r_cd = await get_redis()
    except Exception:
        r_cd = None

    for name, entry in still_failing.items():
        detail = entry.partition(":")[2]
        persistent.append(entry)

        # ── 2. Cooldown: этот сбой уже эскалирован (утро → вечер) ────────────
        sig = hashlib.sha256(f"{name}:{detail.split('(')[0]}".encode()).hexdigest()[:12]
        cd_key = f"audit_health_fix:{name}:{sig}"
        try:
            if r_cd and await r_cd.get(cd_key):
                lines.append(
                    f"⏳ *{name}* — health всё ещё {detail}, "
                    f"уже эскалировано в прошлом аудите, жду фикс"
                )
                continue
        except Exception:
            pass

        # ── 3. Диагностика: логи + классификация наш/не наш ──────────────────
        svc_id = repo_to_sid.get(name)
        svc_logs: list[str] = []
        if svc_id:
            try:
                # Окно: иначе диагностика упавшего бота может получить пустой
                # список, если монитор вычитал логи прямо перед этим.
                svc_logs = (await get_service_logs(svc_id, window_s=AUDIT_LESSON_WINDOW_S))[:30]
            except Exception as ex:
                logger.warning(f"[audit_health] logs failed for {name}: {ex}")
        crash_text = "\n".join(svc_logs[:20])

        if svc_logs and classify_fault(svc_logs) == "external":
            lines.append(
                f"🌐 *{name}* — health {detail}, в логах внешний/сетевой сбой "
                f"(не наш баг), молчу"
            )
            continue

        # ── 4. Анализ причины и действие ─────────────────────────────────────
        crash_reason, fix_description, prevention = "неизвестна", "", ""
        can_autofix = False
        try:
            analysis_raw = await ask_claude(
                f"Бот {name} деплоится со статусом SUCCESS, но HTTP health-check "
                f"вернул {detail}. Логи:\n{crash_text}\n\n"
                f"Определи: 1) точную причину, 2) можно ли починить автоматически "
                f"редеплоем (да/нет), 3) что именно исправить. Ответь JSON без markdown:\n"
                f'{{"reason": "...", "can_autofix": true/false, "fix": "...", "prevention": "..."}}',
                system="Ты senior DevOps. Анализируй логи и давай конкретный диагноз. JSON только.",
                model="claude-haiku-4-5-20251001",
            )
            analysis = first_json_object(analysis_raw) or {}
            crash_reason = analysis.get("reason", "неизвестна")
            can_autofix = analysis.get("can_autofix", False)
            fix_description = analysis.get("fix", "")
            prevention = analysis.get("prevention", "")
        except Exception as ex:
            logger.warning(f"[audit_health] analysis failed for {name}: {ex}")

        if can_autofix and svc_id:
            ok = await redeploy_service(svc_id)
            lines.append(
                f"🔄 *{name}* — health {detail}, редеплой запущен автоматически\n"
                f"   📍 Причина: {crash_reason[:120]}\n"
                f"   🛠 Действие: {'редеплой запущен' if ok else 'редеплой не удался'}"
                + (f"\n   🛡 Предотвращение: {prevention[:120]}" if prevention else "")
            )
            if not ok:
                await notify_office(f"⚠️ *{name}* — health {detail}, редеплой не удался, нужен ручной разбор")
        else:
            code_keywords = ["import", "syntax", "error", "exception", "attribute", "module", "port", "route", "handler"]
            is_code_issue = any(kw in crash_reason.lower() for kw in code_keywords)
            if is_code_issue:
                try:
                    await handle_natural_language(
                        f"[audit_autofix] fix_bot {name}: health check {detail}. "
                        f"Причина: {crash_reason}. Logs: {crash_text[:500]}. "
                        f"Fix needed: {fix_description}",
                        0, lambda x: None
                    )
                    lines.append(
                        f"🤖 *{name}* — health {detail}, делегировала фикс команде\n"
                        f"   📍 Причина: {crash_reason[:120]}\n"
                        f"   🛠 Задача: {fix_description[:120]}"
                    )
                except Exception as ex:
                    lines.append(f"⚠️ *{name}* — health {detail}, не смогла делегировать: {ex}")
            else:
                lines.append(
                    f"⚠️ *{name}* — health {detail}, требует ручного вмешательства\n"
                    f"   📍 Причина: {crash_reason[:120]}"
                    + (f"\n   🛠 Рекомендация: {fix_description[:120]}" if fix_description else "")
                )

        # Cooldown после принятого действия — не эскалировать повторно
        try:
            if r_cd:
                await r_cd.set(cd_key, "1", ex=EXTERNAL_FAULT_COOLDOWN)
        except Exception:
            pass

    return persistent


async def run_daily_audit() -> str:
    """Полный аудит офиса: деплои, логи, health. Возвращает текст отчёта."""
    import datetime
    lines = []
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    lines.append(f"📋 Ежедневный аудит офиса — {ts}\n")

    # 0. Early Railway API auth check — prevents 14 fake AUTH_ERRORs on expired token
    _railway_auth_ok = True
    try:
        await railway_query(RAILWAY_PROBE)
    except RuntimeError as e:
        if "Not Authorized" in str(e) or "Unauthorized" in str(e):
            _railway_auth_ok = False
            alert = (
                "🔴 RAILWAY_TOKEN истёк или отозван — Railway API недоступен.\n"
                "❌ Деплои: проверить невозможно (AUTH_ERROR на все запросы).\n"
                "🛠 Нужно: сгенерировать новый токен на railway.app → Settings → Tokens\n"
                "   и обновить RAILWAY_TOKEN_VLAD в переменных Силли на Railway."
            )
            lines.append(alert)
            logger.error(f"[audit] Railway API auth failed: {e}")
            await notify_office(alert)
    except Exception:
        pass  # network/timeout — не прерываем аудит

    # 1. Deployment status
    # deploy_fail — РЕАЛЬНЫЙ статус деплоя от Railway (FAILED/CRASHED/...).
    # check_fail — сбой самой ПРОВЕРКИ (HTTPStatusError/таймаут/GraphQL от
    # Railway API): бот при этом может быть полностью здоров, поэтому такие
    # записи не попадают в конвейер авто-фикса и не красят статус офиса.
    deploy_ok, deploy_fail, check_fail = [], [], []
    for service_id, (repo, _) in (SERVICES.items() if _railway_auth_ok else []):
        status, check_err = None, None
        for attempt in range(3):
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
                check_err = None
                break
            except RuntimeError as e:
                # GraphQL auth/permission error — не транзиентно, ретрай бесполезен
                err_msg = str(e)
                logger.error(f"[audit] Railway API error for {repo}: {e}")
                if "Not Authorized" in err_msg or "Unauthorized" in err_msg:
                    check_err = "AUTH_ERROR"
                    break
                check_err = "GQL_ERROR"
            except Exception as e:
                check_err = f"ERROR({type(e).__name__})"
                logger.error(f"[audit] deploy check failed for {repo} (attempt {attempt + 1}): {e}")
            if attempt < 2:
                await asyncio.sleep(15)
        if status == "SUCCESS":
            deploy_ok.append(repo)
        elif status is not None:
            deploy_fail.append(f"{repo}:{status}")
        else:
            check_fail.append(f"{repo}:{check_err}")

    if check_fail:
        lines.append(
            f"🌐 Статус деплоя не проверить (сбой Railway API при опросе, не баг ботов, "
            f"авто-фикс пропущен): {', '.join(check_fail)}"
        )

    if deploy_fail:
        lines.append(f"❌ Деплои упали: {', '.join(deploy_fail)}")
        # Auto-fix: читаем логи → анализируем причину → редеплоим или чиним
        for entry in deploy_fail:
            svc_name = entry.split(":")[0]
            svc_status = entry.split(":", 1)[1] if ":" in entry else "UNKNOWN"
            svc_id = next(
                (sid for sid, (repo_n, _) in SERVICES.items() if repo_n == svc_name),
                None
            )
            if not svc_id:
                continue

            # 1. Читаем логи упавшего деплоя
            crash_logs = []
            crash_reason = "неизвестна"
            fix_action = "redeploy"
            fix_description = "редеплой"
            crash_reason = "неизвестна"
            can_autofix = False
            prevention = ""
            fix_action = "escalate"
            try:
                # Окно, а не водяной знак: логи падения нужны целиком, даже
                # если монитор уже прочитал их минуту назад.
                crash_logs = (await get_service_logs(svc_id, window_s=AUDIT_LESSON_WINDOW_S))[:30]
                crash_text = "\n".join(crash_logs[:20])

                # Классификация ДО анализа/редеплоя (как в _handle_health_failures):
                # внешний/сетевой сбой (Telegram/Railway, DNS) — не наш баг, молчим.
                # Иначе can_autofix от Haiku редеплоит бот по чужому сбою.
                if crash_logs and classify_fault(crash_logs) == "external":
                    lines.append(
                        f"🌐 *{svc_name}* — {svc_status}, в логах внешний/сетевой сбой "
                        f"(не наш баг), молчу"
                    )
                    continue

                # 2. Анализируем причину через Claude Haiku
                analysis_raw = await ask_claude(
                    f"Бот {svc_name} упал со статусом {svc_status}. Логи:\n{crash_text}\n\n"
                    f"Определи: 1) точную причину падения, 2) можно ли починить автоматически (да/нет), "
                    f"3) что именно исправить. Ответь JSON без markdown:\n"
                    f'{{"reason": "...", "can_autofix": true/false, "fix": "...", "prevention": "..."}}',
                    system="Ты senior DevOps. Анализируй логи и давай конкретный диагноз. JSON только.",
                    model="claude-haiku-4-5-20251001"
                )
                try:
                    analysis = first_json_object(analysis_raw) or {}
                    crash_reason = analysis.get("reason", "неизвестна")
                    can_autofix = analysis.get("can_autofix", False)
                    fix_description = analysis.get("fix", "редеплой")
                    prevention = analysis.get("prevention", "")

                    if can_autofix and "import" in crash_reason.lower():
                        fix_action = "fix_import"
                    elif can_autofix:
                        fix_action = "redeploy"
                    else:
                        fix_action = "escalate"
                except Exception:
                    pass
            except Exception as ex:
                logger.warning(f"[audit] log analysis failed for {svc_name}: {ex}")

            # 3. Применяем фикс
            if fix_action == "fix_import" or fix_action == "redeploy":
                logger.info(f"[audit] {svc_name} — {fix_action}, reason: {crash_reason[:60]}")
                ok = await redeploy_service(svc_id)
                action_taken = f"редеплой запущен ({fix_description[:80]})" if ok else "редеплой не удался"
                lines.append(
                    f"🔄 *{svc_name}* — редеплой запущен автоматически\n"
                    f"   📍 Причина: {crash_reason[:120]}\n"
                    f"   🛠 Действие: {action_taken}\n"
                    f"   🛡 Предотвращение: {prevention[:120]}" if prevention else
                    f"🔄 *{svc_name}* — редеплой запущен\n"
                    f"   📍 Причина: {crash_reason[:120]}"
                )
                if not ok:
                    await notify_office(f"⚠️ *{svc_name}* — редеплой не удался, нужен ручной разбор")
            elif classify_fault(crash_logs or [crash_reason]) == "external":
                # Внешний/сетевой сбой (Telegram/Railway API, DNS) — НЕ наш баг,
                # команду не дёргаем. Тихая строка в отчёт, без делегирования.
                lines.append(
                    f"🌐 *{svc_name}* — внешний/сетевой сбой (не наш баг), молчу\n"
                    f"   📍 Причина: {crash_reason[:120]}"
                )
            else:
                # Пробуем делегировать команде если это код-проблема
                code_keywords = ["import", "syntax", "error", "exception", "attribute", "module"]
                is_code_issue = any(kw in crash_reason.lower() for kw in code_keywords)
                if is_code_issue:
                    try:
                        await handle_natural_language(
                            f"[audit_autofix] fix_bot {svc_name}: {crash_reason}. "
                            f"Logs: {crash_text[:500]}. Fix needed: {fix_description}",
                            0, lambda x: None
                        )
                        lines.append(
                            f"🤖 *{svc_name}* — делегировала фикс команде\n"
                            f"   📍 Причина: {crash_reason[:120]}\n"
                            f"   🛠 Задача: {fix_description[:120]}"
                        )
                    except Exception as ex:
                        lines.append(f"⚠️ *{svc_name}* — не смогла делегировать: {ex}")
                else:
                    lines.append(
                        f"⚠️ *{svc_name}* — требует ручного вмешательства\n"
                        f"   📍 Причина: {crash_reason[:120]}\n"
                        f"   🛠 Рекомендация: {fix_description[:120]}"
                    )
    else:
        lines.append(f"✅ Деплои ({len(deploy_ok)}): все SUCCESS")

    # 1b. Сквозной аудит ВСЕХ проектов-отделов (а не только awake-happiness).
    #     Только видимость/алерт; авто-фикс остаётся лишь для известных репо из
    #     SERVICES — чужие отделы не чиним вслепую.
    # Инициализация ДО try: other_fail читается ниже в итоговом статусе, и если
    # блок упадёт на первой же строке, там будет NameError — падение аудита
    # целиком вместо пропущенной секции.
    other_fail, other_total = [], 0
    try:
        known_sids = set(SERVICES.keys())
        all_data = await railway_query(
            "{ projects { edges { node { name services { edges { node { id name } } } } } } }"
        )
        for pe in ((all_data.get("data") or {}).get("projects") or {}).get("edges") or []:
            pname = pe["node"]["name"]
            for se in (pe["node"].get("services") or {}).get("edges") or []:
                sid = se["node"]["id"]
                if sid in known_sids:
                    continue
                other_total += 1
                try:
                    dd = await railway_query(
                        "query($sid:String!){deployments(first:1,input:{serviceId:$sid}){edges{node{status}}}}",
                        {"sid": sid})
                    deps2 = (dd.get("data") or {}).get("deployments", {}).get("edges") or []
                    st = deps2[0]["node"]["status"] if deps2 else "NO_DEPLOY"
                    # NO_DEPLOY — это кроны/не настроенные сервисы, не инцидент → не алертим
                    if st not in ("SUCCESS", "NO_DEPLOY"):
                        other_fail.append(f"{pname}/{se['node']['name']}:{st}")
                except Exception:
                    pass
        if other_fail:
            lines.append(f"❌ Другие отделы упали: {', '.join(other_fail)}")
            # «Не чиним вслепую» — верно, но раньше это означало НЕ ДЕЛАТЬ
            # НИЧЕГО: ни задачи, ни эскалации. Падение чужого отдела молча
            # переезжало из отчёта в отчёт (family-dept/nelli-bot пролежал
            # CRASHED сутки). Чинить вслепую по-прежнему не будем — но заведём
            # задачу на доске, чтобы у падения был владелец и срок.
            for _fail in other_fail:
                _title = f"Упал сервис вне SERVICES: {_fail}"
                try:
                    _r_x = await get_redis()
                    _open = await tb.list_tasks(_r_x, status={"open", "in_progress",
                                                              "needs_fix", "blocked"},
                                                limit=200)
                    if any((t.get("title") or "") == _title for t in _open):
                        continue      # уже заведена — второй раз не плодим
                    await tb.create_task(_r_x, _title, created_by="силли",
                                         assignee="влад", status="blocked")
                    await notify_office(
                        f"🚨 {_fail} — сервис вне списка автоматического ремонта.\n"
                        f"Автофикс не применяю (чужой репо), задача на доске, нужен разбор."
                    )
                except Exception as _e:
                    logger.error(f"[audit] задача по {_fail} не завелась: {_e}")
        elif other_total:
            lines.append(f"✅ Другие отделы ({other_total}): все деплои SUCCESS")
    except Exception as e:
        logger.error(f"[audit] cross-project sweep failed: {e}")

    # 2. Health checks for HTTP services
    health_fail = []
    async with httpx.AsyncClient(timeout=10) as c:
        for name, url in HEALTH_URLS.items():
            try:
                r = await c.get(url)
                if r.status_code != 200:
                    # Богатый /health (как у tilly-trader) кладёт в тело причину
                    # деградации — вытаскиваем, чтобы отчёт говорил ПОЧЕМУ, а не
                    # просто ":503". Стоп сканера трейдера теперь виден офису.
                    detail = str(r.status_code)
                    try:
                        body = r.json()
                        reason = body.get("reason") or body.get("status")
                        if reason:
                            detail = f"{r.status_code}/{reason}"
                        age = body.get("last_scan_age_s")
                        if age is not None:
                            detail += f"(scan {age // 60}m)"
                    except Exception:
                        pass
                    health_fail.append(f"{name}:{detail}")
            except Exception as e:
                health_fail.append(f"{name}:TIMEOUT")

    if health_fail:
        lines.append(f"❌ Health failed: {', '.join(health_fail)}")
        # Auto-fix: retry (transient) → classify (external = молчим) →
        # редеплой/делегирование команде. Статус офиса окрашивают только
        # стойкие сбои — их возвращает обработчик.
        try:
            health_fail = await _handle_health_failures(health_fail, lines)
        except Exception as e:
            logger.error(f"[audit_health] handler failed: {e}")
    else:
        lines.append(f"✅ HTTP health ({len(HEALTH_URLS)}): все OK")

    # 3. Ошибки за последние AUDIT_LOG_WINDOW_S секунд.
    # Окно, а не водяной знак: иначе монитор (раз в 5 минут) вычёрпывает логи
    # первым и аудит рапортует «ошибок нет», ничего на самом деле не проверив.
    error_services = []
    office_services = await _all_office_services()
    for service_id, repo in office_services:
        try:
            logs = await get_service_logs(service_id, window_s=AUDIT_LOG_WINDOW_S)
            # Блочный фильтр вместо построчного. Построчный не работал: строка
            # `Traceback (most recent call last):` совпадает с шаблоном ошибки и не
            # содержит ни одного игнор-паттерна, поэтому трейсбек от Conflict —
            # который стоит в списке игнорируемых — всё равно считался ошибкой.
            # Так molly-trader показывал «(2)» в каждом отчёте из-за двух строк
            # трейсбека, порождённых одним заведомо безобидным рестартом.
            errs = [l for l in strip_ignored_tracebacks(logs)
                    if any(p in l for p in ["Error:", "Traceback", "CRITICAL", "KeyError"])]
            if errs:
                error_services.append(f"{repo}({len(errs)})")
        except Exception:
            pass

    _win_h = AUDIT_LOG_WINDOW_S / 3600
    if error_services:
        lines.append(f"⚠️  Ошибки за последние {_win_h:g}ч: {', '.join(error_services)}")
    else:
        lines.append(f"✅ Логи: ошибок за последние {_win_h:g}ч нет")

    # 4. Bug lesson scan — ищем новые паттерны ошибок которых нет в lessons.json
    new_lesson_count = 0
    try:
        raw_lessons = await read_file("ai-office-shared", LESSONS_FILE)
        existing_lessons = json.loads(raw_lessons) if raw_lessons.strip() else []

        # Собираем все ошибки за сутки по всем сервисам
        all_errors: dict[str, list[str]] = {}
        for service_id, repo in office_services:
            try:
                logs = await get_service_logs(service_id, window_s=AUDIT_LESSON_WINDOW_S)
                errs = [l for l in strip_ignored_tracebacks(logs)
                        if any(p in l for p in ERROR_PATTERNS)]
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

    # 4b. Публикация новых уроков в Bug Lessons — Силли подтягивает их сама на аудите
    #     (durable: постятся только уроки без posted_to_group, повтор/флуд исключён)
    try:
        published = await publish_pending_lessons()
        if published:
            lines.append(f"📤 Опубликовано новых уроков в Bug Lessons: {published}")
    except Exception as e:
        logger.error(f"[daily_audit] publish lessons failed: {e}")

    # 5. Итог
    lines.append("")
    # other_fail входит в условие с 2026-08-13. До этого отчёт мог в одном
    # сообщении сказать «❌ Другие отделы упали: family-dept/nelli-bot:CRASHED»
    # и тут же «🟢 Статус офиса: НОРМА». Противоречивый отчёт хуже отсутствующего:
    # читатель верит итоговой строке и прокручивает детали.
    status_icon = ("🟢" if not deploy_fail and not health_fail
                   and not error_services and not other_fail else "🟡")
    lines.append(f"{status_icon} Статус офиса: {'НОРМА' if status_icon == '🟢' else 'ТРЕБУЕТ ВНИМАНИЯ'}")

    return "\n".join(lines)


# ── Самообслуживание: инбокс находок + свип по команде владельца ─────────────
# Поверх ежедневного аудита: владелец в ЛЮБОЙ момент говорит «проверь офис» /
# «почини <бот>», получает единый отчёт, где каждый наш баг уже застейджен как
# pending-фикс (через тот же командный гейт handle_bug) и ждёт /approve. Свип
# НИЧЕГО не применяет сам — только детект + предложение. Это прямой ответ на
# «команда тупит»: детект теперь on-demand, а не только раз в 5 мин / в аудит.
FINDINGS_KEY = "office:findings"
FINDINGS_MAX = 100
FINDINGS_TTL = 7 * 24 * 3600


async def add_finding(finding: dict) -> None:
    """Пишет находку свипа в инбокс (LPUSH+LTRIM+EXPIRE, контракт как у routing:misses)."""
    r = await get_redis()
    if not r:
        return
    try:
        entry = {
            "id": _uuid_mod.uuid4().hex[:8],
            "bot": finding.get("bot", "?"),
            "symptom": (finding.get("symptom", "") or "")[:300],
            "pending_id": finding.get("pending_id", ""),
            "status": finding.get("status", "open"),
            "ts": int(time.time()),
        }
        await r.lpush(FINDINGS_KEY, json.dumps(entry, ensure_ascii=False))
        await r.ltrim(FINDINGS_KEY, 0, FINDINGS_MAX - 1)
        await r.expire(FINDINGS_KEY, FINDINGS_TTL)
    except Exception as e:
        logger.debug(f"add_finding failed: {e}")


async def read_findings(limit: int = 20) -> list[dict]:
    r = await get_redis()
    if not r:
        return []
    try:
        raw = await r.lrange(FINDINGS_KEY, 0, limit - 1)
        out = []
        for x in raw:
            try:
                out.append(json.loads(x))
            except Exception:
                continue
        return out
    except Exception:
        return []


async def mark_finding(pending_id: str, status: str) -> None:
    """Помечает открытую находку с данным pending_id (applied/skipped).
    Переписывает список, сохраняя порядок (newest-first). Вызывается из /approve, /skip, кнопок."""
    if not pending_id:
        return
    r = await get_redis()
    if not r:
        return
    try:
        raw = await r.lrange(FINDINGS_KEY, 0, -1)
        out, changed = [], False
        for x in raw:
            try:
                d = json.loads(x)
            except Exception:
                out.append(x)
                continue
            if d.get("pending_id") == pending_id and d.get("status") == "open":
                d["status"] = status
                changed = True
                out.append(json.dumps(d, ensure_ascii=False))
            else:
                out.append(x)
        if changed:
            pipe = r.pipeline()
            pipe.delete(FINDINGS_KEY)
            if out:
                pipe.rpush(FINDINGS_KEY, *out)
                pipe.expire(FINDINGS_KEY, FINDINGS_TTL)
            await pipe.execute()
    except Exception as e:
        logger.debug(f"mark_finding failed: {e}")


def _resolve_scan_target(raw: str) -> str:
    """Имя/алиас бота → repo из SERVICES. '' если не распознан."""
    raw = (raw or "").strip().lower()
    repos = {r for _, (r, _) in SERVICES.items()}
    if raw in repos:
        return raw
    if f"{raw}-bot" in repos:
        return f"{raw}-bot"
    try:
        from ai_office_shared.shared.identity import canonical, BOTS
        canon = canonical(raw)
        if canon and canon in BOTS:
            repo = BOTS[canon].get("repo", "")
            if repo in repos:
                return repo
    except Exception:
        pass
    for r in repos:
        if raw and (raw in r or r.split("-")[0] == raw):
            return r
    return ""


async def _run_office_scan(target: str = "", *, proposal_chat_id: int = 0) -> dict:
    """On-demand свип офиса: детект → классификация → (наш баг) предложение фикса.

    target — repo одного бота ('gosling-bot') или '' для всех сервисов из SERVICES.
    proposal_chat_id — куда слать предложение с кнопками (0 → офис-группа).
    Переиспользует существующий конвейер (IGNORE/ERROR_PATTERNS, classify_fault,
    search_lessons, analyze_logs, handle_bug). Ничего не применяет сам.
    Каждый бот в отдельном try/except: сбой ОДНОЙ проверки ≠ падение всего свипа
    (урок #1 — «не смог проверить» ≠ «сломано»)."""
    railway_ok = await _railway_is_available()
    result = {"healthy": [], "external": [], "known": [],
              "proposed": [], "blocked": [], "errors": []}
    for sid, (repo, main_file) in SERVICES.items():
        if target and repo != target:
            continue
        try:
            logs = await get_service_logs(sid) if railway_ok else await get_service_logs_via_redis(repo)
            if not logs:
                result["healthy"].append(repo)
                continue
            error_lines = [ln for ln in logs
                           if any(ep in ln for ep in ERROR_PATTERNS)
                           and not any(ip in ln for ip in IGNORE_PATTERNS)]
            if not error_lines:
                result["healthy"].append(repo)
                continue
            if classify_fault(error_lines) == "external":
                result["external"].append(repo)
                continue
            known = await search_lessons(error_lines)
            if known.get("match") and str(known.get("confidence", "")).lower() == "high":
                result["known"].append(f"{repo} (lesson #{known.get('lesson_id')})")
                continue
            source_code = await read_file(repo, main_file)
            analysis = await analyze_logs(repo, error_lines, source_code)
            if not analysis.get("is_bug") or analysis.get("bug_type") == "external":
                (result["external"] if analysis.get("bug_type") == "external"
                 else result["healthy"]).append(repo)
                continue
            _no_proof = dispatch_refusal(analysis, error_lines)
            if _no_proof:
                logger.info(f"[office-scan] {repo}: находку не отдаю отделу — {_no_proof}")
                result["healthy"].append(repo)
                continue
            outcome = await handle_bug(sid, repo, repo, main_file, analysis,
                                       proposal_chat_id=proposal_chat_id) or {}
            st = outcome.get("status")
            if st == "proposed":
                await add_finding(outcome)
                result["proposed"].append(outcome)
            elif st in ("blocked", "blocked_decision"):
                result["blocked"].append(outcome)
            else:
                result["errors"].append({"bot": repo, "symptom": outcome.get("symptom", "?")})
        except Exception as e:
            logger.error(f"[office_scan] {repo}: {e}")
            result["errors"].append({"bot": repo, "symptom": f"сбой проверки: {e}"})
    return result


def _format_scan_report(result: dict, *, target: str = "") -> str:
    import datetime as _dt
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    scope = f"бот {target}" if target else "весь офис"
    lines = [f"🔎 Самопроверка офиса ({scope}) — {ts}\n"]
    prop = result.get("proposed", [])
    blocked = result.get("blocked", [])
    if prop:
        lines.append(f"🛠 Наши баги — предложены фиксы ({len(prop)}), подтверди /approve:")
        for f in prop:
            lines.append(f"  • {f['bot']}: {(f.get('symptom') or '')[:120]}")
            lines.append(f"     ↳ /approve {f.get('pending_id')}   (или ⏭ /skip {f.get('pending_id')})")
    if blocked:
        lines.append(f"\n⛔ Наши баги — команда не дала чистый код за 3 попытки ({len(blocked)}), нужен разбор:")
        for f in blocked:
            lines.append(f"  • {f['bot']}: {(f.get('symptom') or '')[:120]}")
    if result.get("known"):
        lines.append(f"\n📚 Известные (в уроках, не чиним заново): {', '.join(result['known'])}")
    if result.get("external"):
        lines.append(f"\n🌐 Внешние/сетевые сбои (не наш баг, пропущены): {', '.join(result['external'])}")
    if result.get("errors"):
        lines.append("\n⚠️ Проверка не удалась: " + ", ".join(e["bot"] for e in result["errors"]))
    healthy = result.get("healthy", [])
    if healthy:
        lines.append(f"\n🟢 Здоровы ({len(healthy)}): {', '.join(healthy)}")
    if not prop and not blocked:
        lines.append("\n✅ Наших багов, требующих фикса, не найдено.")
    return "\n".join(lines)


async def _office_status_snapshot() -> str:
    r = await get_redis()
    if not r:
        return "❌ Redis недоступен"
    lines = ["📊 Статус офиса\n"]
    try:
        up = down = 0
        async for key in r.scan_iter("office:health:*"):
            v = await r.get(key)
            if v == "up":
                up += 1
            elif v == "down":
                down += 1
        lines.append(f"❤️ Health: {up} up / {down} down")
    except Exception:
        pass
    try:
        bad = []
        async for key in r.scan_iter("office:quality:*"):
            d = await r.hgetall(key)
            u, dn = int(d.get("up", 0)), int(d.get("down", 0))
            if dn >= 5 and dn > u:
                bad.append(f"{key.split(':')[-1]} 👎{dn}/👍{u}")
        lines.append("👎 Качество (проблемные): " + (", ".join(bad) if bad else "нет"))
    except Exception:
        pass
    try:
        from collections import Counter
        raw = await r.lrange("office:routing:misses", 0, 99)
        c = Counter()
        for m in raw:
            try:
                c[json.loads(m).get("agent", "?")] += 1
            except Exception:
                pass
        top = ", ".join(f"{a}:{n}" for a, n in c.most_common(3)) if c else "нет"
        lines.append(f"🧭 Routing-миссы (топ): {top}")
    except Exception:
        pass
    try:
        all_tasks = await tb.list_tasks(r, limit=200)
        active = [t for t in all_tasks
                  if t.get("status") in ("in_progress", "needs_fix", "blocked", "awaiting_approval")]
        lines.append(f"📋 Активных задач: {len(active)}")
    except Exception:
        pass
    try:
        fs = await read_findings(50)
        openf = [f for f in fs if f.get("status") == "open"]
        lines.append(f"🔎 Открытых находок: {len(openf)} (см. /office inbox)")
    except Exception:
        pass
    return "\n".join(lines)


async def _office_inbox_text() -> str:
    fs = await read_findings(30)
    openf = [f for f in fs if f.get("status") == "open" and f.get("pending_id")]
    if not openf:
        return "🔎 Инбокс пуст — открытых находок с ожидающим фиксом нет."
    lines = ["🔎 Открытые находки (подтверди /approve id или отклони /skip id):\n"]
    for f in openf:
        lines.append(f"• {f['bot']}: {(f.get('symptom') or '')[:120]}")
        lines.append(f"   ↳ /approve {f['pending_id']}   /skip {f['pending_id']}")
    done = [f for f in fs if f.get("status") in ("applied", "skipped")]
    if done:
        lines.append(f"\n(закрыто недавно: {len(done)})")
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
        r_ops = await get_redis()
        raw_ops = ""
        if r_ops:
            ops_entries = await r_ops.lrange("office:ops_log", 0, 19)
            raw_ops = "\n".join(reversed(ops_entries)) if ops_entries else ""
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
        diagnosis = first_json_object(raw) or {}
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
    # Дребезг. Один неудачный probe — не outage: 15.08 офис получил четыре
    # сообщения за час («недоступен» → «снова доступен» → и снова), потому что
    # флаг сбрасывался на первом же успехе и следующий сбой снова алертил.
    # Это то же правило, что записано про «Not Authorized» от Railway: один
    # провалившийся запрос не диагноз. Цена шума высока — если алерты Силли
    # начать пролистывать, пролистают и настоящий.
    _railway_fails = 0
    _RAILWAY_FAIL_THRESHOLD = 3   # подряд, при MONITOR_INTERVAL=300 это ~15 минут

    while True:
        if MONITOR_PAUSED():
            logger.info("[monitor] paused via CILLY_MONITOR_PAUSED env var, sleeping...")
            await asyncio.sleep(60)
            continue

        # Проверяем Railway API один раз в начале цикла
        railway_ok = await _railway_is_available()

        if not railway_ok:
            _railway_fails += 1
            # На Redis-фолбэк переходим сразу (это дёшево и безопасно), а вот
            # ОБЪЯВЛЯЕМ outage только после нескольких неудач подряд.
            if _railway_fails >= _RAILWAY_FAIL_THRESHOLD and not _railway_down_notified:
                await notify_office(
                    f"⚠️ *Railway API недоступен* ({_railway_fails} проверки подряд) — "
                    "переключаюсь на Redis-мониторинг.\n"
                    "Фиксы через GitHub работают, Railway автодеплоит сам."
                )
                _railway_down_notified = True
            logger.warning("[monitor] Railway API down (%s/%s) — Redis fallback",
                           _railway_fails, _RAILWAY_FAIL_THRESHOLD)
        else:
            if _railway_down_notified:
                await notify_office("✅ Railway API снова доступен — возвращаюсь к полному мониторингу.")
                _railway_down_notified = False
            elif _railway_fails:
                # Моргнуло и прошло — в группу не пишем, но след оставляем.
                logger.info("[monitor] Railway API восстановился после %s неудач — "
                            "порог не достигнут, офис не тревожим", _railway_fails)
            _railway_fails = 0

        for service_id, (repo, main_file) in SERVICES.items():
            try:
                # Основной путь: Railway logs. Fallback: Redis structural logs
                if railway_ok:
                    logs = await get_service_logs(service_id)
                else:
                    logs = await get_service_logs_via_redis(repo)

                if not logs:
                    continue

                # === Filter Layer 1: вырезаем шумовые трейсбеки ЦЕЛИКОМ и работаем с остатком.
                # Раньше здесь стоял пропуск ВСЕГО сервиса при любой шумовой строке. Это
                # закрывало дыру со стек-трейсами Conflict, но ценой слепоты: телеграм-бот
                # почти всегда имеет в логе TimedOut или Conflict после рестарта, поэтому
                # монитор переставал смотреть на ботов вовсе и не чинил ничего.
                noisy = len(logs)
                logs = strip_ignored_tracebacks(logs)
                if not logs:
                    logger.info(f"[monitor] {repo}: в логе только deployment-шум ({noisy} строк) — анализировать нечего")
                    continue
                if noisy != len(logs):
                    logger.info(f"[monitor] {repo}: отброшено {noisy - len(logs)} шумовых строк, осталось {len(logs)}")

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

                # === Filter Layer 3: внешний/транзиентный сбой — НЕ наш баг → МОЛЧИМ.
                # Telegram/Railway API, DNS, сеть (NetworkError/ConnectError/
                # RemoteProtocolError/Bad Gateway/5xx) при живом боте: предлагать фикс
                # нельзя — это не наша вина. Тихо логируем раз в EXTERNAL_FAULT_COOLDOWN,
                # в офис ничего не шлём. Watchdog поднимет бота, если он реально лёг.
                if classify_fault(error_logs) == "external":
                    import hashlib as _h3
                    _ext_sig = _h3.md5(
                        "\n".join(error_logs)[:500].encode()
                    ).hexdigest()[:12]
                    _ext_key = f"external_fault_seen:{service_id}:{_ext_sig}"
                    _r_ext = await get_redis()
                    _seen = bool(await _r_ext.get(_ext_key)) if _r_ext else False
                    if not _seen:
                        if _r_ext:
                            await _r_ext.setex(_ext_key, EXTERNAL_FAULT_COOLDOWN, "1")
                        try:
                            await log_event(
                                _r_ext, BOT_NAME_LOWER, "external_fault_ignored",
                                service=repo, sample="\n".join(error_logs[:3])[:300],
                            )
                        except Exception:
                            pass
                        logger.info(
                            f"[monitor] {repo}: внешний/сетевой сбой (не наш баг) — молчу, "
                            f"cooldown {EXTERNAL_FAULT_COOLDOWN//3600}ч"
                        )
                    continue

                # Дедупликация: УСТОЙЧИВАЯ сигнатура (тип ошибки + файл + сообщение
                # без чисел), чтобы один и тот же баг не пере-детектился из-за разных
                # номеров строк/динамики и не обнулял fix_count по кругу.
                import hashlib, re as _re
                _err_text = "\n".join(error_logs)
                _exc = _re.findall(r"\b([A-Za-z_]+(?:Error|Exception))\b", _err_text)
                _files = _re.findall(r'File "[^"]*?([^"/\\]+\.py)"', _err_text)
                _msg = ""
                for _line in reversed(error_logs):
                    if _exc and _exc[-1] in _line:
                        _msg = _line
                        break
                _msg_norm = _re.sub(r"0x[0-9a-fA-F]+|\d+", "", _msg).strip()
                _sig_basis = "|".join([
                    _exc[-1] if _exc else "",
                    _files[-1] if _files else "",
                    _msg_norm,
                ]).strip("|")
                if not _sig_basis:  # фолбэк: нормализованный текст без чисел
                    _sig_basis = _re.sub(r"0x[0-9a-fA-F]+|\d+", "", _err_text)[:500]
                error_signature = hashlib.md5(_sig_basis.encode()).hexdigest()
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

                # Check known bugs first — урок РЕАЛЬНО гейтит, а не украшает текст.
                # Раньше high-confidence матч лишь менял уведомление, но дальше всё
                # равно шёл analyze_logs → handle_bug → новое предложение. Это и был
                # «бред»: «применяю известный фикс» и тут же «нашёл подозрительное».
                known = await search_lessons(error_logs)
                if known.get("match") and known.get("confidence") == "high":
                    lesson_id = known.get("lesson_id")
                    logger.info(f"[monitor] known bug match in {repo}: lesson #{lesson_id}")
                    # Дедуп: один разбор известного урока на сервис за cooldown —
                    # не открываем то же предложение каждый цикл.
                    _r_les = await get_redis()
                    _les_key = f"lesson_applied:{service_id}:{lesson_id}"
                    _already = bool(await _r_les.get(_les_key)) if _r_les else False
                    if _already:
                        logger.info(f"[monitor] {repo}: урок #{lesson_id} уже разобран недавно — молчу")
                        continue
                    if _r_les:
                        await _r_les.setex(_les_key, EXTERNAL_FAULT_COOLDOWN, "1")
                    # Известный урок = разбор уже есть. Не пере-генерируем фикс Opus-ом
                    # и НЕ открываем повторное предложение. Тихая заметка один раз.
                    await notify_office(
                        f"📚 Cilly: повтор известной проблемы в *{repo}* — урок #{lesson_id}.\n"
                        f"_{known.get('reason', '')}_\n"
                        f"Новых действий не требуется (фикс уже задокументирован)."
                    )
                    continue

                analysis = await analyze_logs(repo, error_logs, source_code)

                # Второй слой: анализатор сам мог распознать внешний сбой → молчим.
                if analysis.get("bug_type") == "external" or not analysis.get("is_bug"):
                    logger.info(f"[monitor] {repo}: анализатор — не наш баг ({analysis.get('bug_type')}), молчу")
                    continue

                # Третий слой, детерминированный: находка без улики отдел не занимает.
                # is_bug — суждение той же модели, которой показали ВЕСЬ исходник, и на
                # пустых логах она начинает ревьюить код вместо разбора инцидента.
                _no_proof = dispatch_refusal(analysis, error_logs)
                if _no_proof:
                    logger.info(f"[monitor] {repo}: находку не отдаю отделу — {_no_proof}. "
                                f"Заявление: {str(analysis.get('description',''))[:200]}")
                    continue

                # Что именно посчитали отказом — в лог, рядом с решением занять
                # отдел. Иначе разбор «почему Силли туда пошла» снова сведётся
                # к «модель что-то решила».
                logger.info(f"[monitor] {repo}: отдаю отделу, улика: "
                            f"{failure_evidence(error_logs)[:160]!r}")
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
# httpx печатает URL запроса на INFO, а в URL Telegram лежит токен бота: без
# этой строки токен уходит в логи Railway на каждый getUpdates. Здесь голый
# logging, а не хелпер офиса: у бота по шаблону нет пина ai_office_shared.
logging.getLogger("httpx").setLevel(logging.WARNING)
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
    await log("MSG_IN", f"[HTTP] {{message}}")
    try:
        response = await process(message, user_id)
    except Exception as e:
        logger.error(f"process() error: {e}")
        return web.json_response({"status": "error", "responses": [str(e)]}, status=500)
    # В группу ТОЛЬКО если явно передан notify=True
    # По умолчанию ответ идёт только в HTTP response (личка или вызывающий)
    if data.get("notify", False):
        await send_to_group(f"{bot_name}:\n{response}")
    await log("MSG_OUT", f"{bot_name}: {{response}}")
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
    await log("MSG_IN", msg)
    response = await process(msg, update.effective_user.id)
    await log("MSG_OUT", f"{bot_name}: {{response}}")
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

from ai_office_shared.shared.railway_vars import set_var_allowed


async def railway_set_variable(service_id: str, name: str, value: str) -> bool:
    """Записать ОДНУ переменную окружения.

    Сознательно НЕ variableCollectionUpsert (как в railway_set_variables): та
    мутация заменяет НАБОР целиком — промах модели снёс бы остальные переменные
    сервиса. Плюс прав на неё у токена нет, а на одиночный variableUpsert есть
    (проверено 2026-08-12 при добавлении нового аккаунта Яны в ALLOWED_USERS).
    """
    data = await railway_graphql(
        """mutation($input: VariableUpsertInput!) { variableUpsert(input: $input) }""",
        {"input": {
            "projectId": PROJECT_ID,
            "environmentId": ENVIRONMENT_ID,
            "serviceId": service_id,
            "name": name,
            "value": value,
        }}
    )
    return data.get("data", {}).get("variableUpsert") is True


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

async def railway_get_variables(service_id: str) -> dict:
    """Прочитать переменные окружения сервиса Railway (для check_var)."""
    data = await railway_graphql(
        """query($proj: String!, $svc: String!, $env: String!) {
             variables(projectId: $proj, serviceId: $svc, environmentId: $env)
           }""",
        {"proj": PROJECT_ID, "svc": service_id, "env": ENVIRONMENT_ID}
    )
    return (data.get("data") or {}).get("variables") or {}


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


import unicodedata as _ud


def _norm_for_match(s: str) -> str:
    """Нормализация ТОЛЬКО для поиска (не для записи): NFC, срез variation
    selectors, NBSP→пробел, схлопывание пробелов внутри строк, strip концов."""
    s = _ud.normalize("NFC", s)
    s = s.replace("️", "").replace("︎", "")    # эмодзи VS16 / VS15
    s = s.replace(" ", " ").replace(" ", " ")  # NBSP, narrow NBSP
    s = "\n".join(" ".join(line.split()) for line in s.split("\n"))
    return s.strip()


def resilient_replace(content: str, old: str, new: str):
    """(updated, method) при успехе или (None, reason) при неудаче.
    reason ∈ {"empty","notfound","ambiguous"}. Поиск — по нормализованной форме
    (устойчив к эмодзи-VS/NBSP/пробелам), замена — по ТОЧНОМУ исходному срезу файла,
    поэтому запись всегда побайтово-корректна.
    """
    if old in content:
        return content.replace(old, new, 1), "exact"
    n_old = _norm_for_match(old)
    if not n_old:
        return None, "empty"
    lines = content.split("\n")
    span = n_old.count("\n") + 1
    hits = [i for i in range(len(lines) - span + 1)
            if _norm_for_match("\n".join(lines[i:i + span])) == n_old]
    if len(hits) != 1:
        return None, ("ambiguous" if hits else "notfound")
    i = hits[0]
    orig = "\n".join(lines[i:i + span])            # точный исходный срез файла
    indent = orig[:len(orig) - len(orig.lstrip())]
    repl = new
    if span == 1 and repl[:1] and not repl[:1].isspace():
        repl = indent + repl                       # сохранить отступ однострочной замены
    return content.replace(orig, repl, 1), "normalized"


async def handle_natural_language(message_text: str, chat_id: int, reply_func, history: list = None, silent: bool = False, repo_override: str = "", file_path_override: str = "", proposal_chat_id: int = 0, sender: str = ""):
    """Process any natural language request — detect intent and execute."""
    # Читаем ops.md — лог последних действий Claude и Силли
    # Это даёт Силли контекст о том что уже было сделано
    ops_context = ""
    try:
        r_ops = await get_redis()
        raw_ops = ""
        if r_ops:
            ops_entries = await r_ops.lrange("office:ops_log", 0, 19)
            raw_ops = "\n".join(reversed(ops_entries)) if ops_entries else ""
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
    intent_data = first_json_object(raw)
    if intent_data is None:
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
    # Явный repo из payload (HTTP /task) приоритетнее догадки интента-LLM —
    # иначе планировщик путал репо (tilly-trader ↔ tilly-bot).
    #
    # А если явного нет — репозиторий выбирается ПО ФАКТАМ, а не догадкой.
    # 15.08: заявка «убрать кнопки из /menu» пришла через Крисса, модель с
    # уверенностью 0.92 отправила Силли в billy-bot (кнопки — у Крисса), она
    # ничего не нашла и упёрлась в стоп-гард. Уверенность 0.92 тут особенно
    # вредна: выглядит как знание, а модель просто не может знать, у кого какие
    # кнопки. Названный в тексте бот и бот-отправитель — факты, они и решают.
    repo = repo_override
    _repo_why = "явный repo из payload"
    if not repo:
        repo, _repo_why = target_repo(
            message_text, sender=str(sender or ""),
            llm_guess=intent_data.get("repo") or "",
        )
    path       = intent_data.get("path")
    task       = intent_data.get("task", message_text)
    _conf_raw = intent_data.get("confidence", 1.0)
    confidence = float(_conf_raw) if isinstance(_conf_raw, (int, float)) else {"high": 0.9, "medium": 0.6, "low": 0.3}.get(str(_conf_raw).lower(), 0.5)

    logger.info(f"[nl] intent={intent} confidence={confidence:.2f} "
                f"repo={repo} ({_repo_why})")

    # Для деструктивных/долгих операций — требуем высокую уверенность
    DESTRUCTIVE = ("create_bot", "create_repo", "deploy", "push_code", "get_bot_token")
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


    elif intent == "office_scan":
        # Самопроверка офиса по явной команде. Предложения с кнопками уходят в чат
        # инициатора (proposal_chat_id из HTTP /task, иначе текущий chat_id).
        bot_arg = intent_data.get("bot") or ""
        target = _resolve_scan_target(bot_arg) if bot_arg else ""
        pcid = proposal_chat_id or chat_id or 0
        if bot_arg and not target:
            known = ", ".join(sorted({r for _, (r, _) in SERVICES.items()}))
            await reply_func(f"Не знаю бота «{bot_arg}». Доступные репо: {known}")
            return
        await reply_func(
            f"🔎 Запускаю самопроверку {'бота ' + target if target else 'офиса'}… (1–2 мин)"
        )
        result = await _run_office_scan(target=target, proposal_chat_id=pcid)
        await reply_func(_format_scan_report(result, target=target))

    elif intent == "board_task":
        """Операция над задачей доски по id. Названная и узкая — намеренно.

        23.08 понадобилось вернуть в очередь две заявки dev-dept, легшие в
        blocked из-за пустого счёта Anthropic. Сделать это было нечем: redis_query
        только читает, `/redis` требует токен, которого у сессии Клода нет, и
        просьба «выполни HSET …» ушла в аудит качества. Даём ровно одну операцию,
        а не право писать в Redis что угодно через LLM-интент.
        """
        import re as _re_board

        raw_id = (intent_data.get("task") or "").strip()
        m_id = _re_board.search(r"\b([0-9a-f]{12})\b", raw_id or task)
        board_task_id = m_id.group(1) if m_id else ""
        if not board_task_id:
            await reply_func(
                "⚠️ Не вижу id задачи. Он выглядит как 12 hex-символов, например "
                "`62ffa25b5e30` — покажи его в сообщении."
            )
            return

        mode = (intent_data.get("mode") or "reopen").strip().lower()
        if mode != "reopen":
            await reply_func(f"⚠️ Для доски поддерживается только reopen, не {mode!r}.")
            return

        r_board = await get_redis()
        ok_reopen, why_reopen = await tb.reopen_task(
            r_board, board_task_id, reason=f"возвращена в очередь: {task[:150]}"
        )
        if not ok_reopen:
            await reply_func(f"⚠️ Не вернул задачу [{board_task_id}]: {why_reopen}")
            return
        fresh = await tb.get_task(r_board, board_task_id)
        await reply_func(
            f"✅ Задача [{board_task_id}] снова в очереди.\n"
            f"«{(fresh or {}).get('title', '')[:120]}»\n"
            f"status={(fresh or {}).get('status')} attempts={(fresh or {}).get('attempts')} "
            f"escalated={(fresh or {}).get('escalated')}\n"
            f"Отдел подхватит её на ближайшем тике, если слот свободен."
        )

    elif intent == "redis_query":
        """Выполняет реальные Redis операции — scan, get, hgetall, custom audit.

        ЧИТАЕТ, но не пишет: это фиксированное меню аудитов, а не проксирование
        команд. Просьбы изменить задачу доски обслуживает интент board_task.
        """
        r = await get_redis()
        if not r:
            await reply_func("❌ Redis недоступен")
            return

        task_lower = task.lower()
        result: dict = {}

        # ── 0. DEL — обрабатывается первым и возвращает сразу ────────────
        # Ключ ищем в оригинальном task (регистр важен) и без хардкода 'office:' —
        # любой namespace вида prefix:..., кроме URL-схем (https://...)
        if _mentions(task_lower, ["del", "delete", "удали ключ"]):
            import re as _re_del
            del_match = _re_del.search(
                r'\b(?!https?:)([A-Za-z][A-Za-z0-9_.\-]*:(?!//)[A-Za-zа-яА-ЯёЁ0-9:_.\-]+)', task)
            if del_match:
                del_key = del_match.group(1)
                deleted = await r.delete(del_key)
                msg = f"✅ DEL {del_key}: {'удалён' if deleted else 'не найден'}"
                await reply_func(msg)
                return

        # ── 1. quality audit ──────────────────────────────────────────────
        if _mentions(task_lower, ["quality", "реакци", "голос", "👍", "👎", "up", "down", "аудит"]):
            async for key in r.scan_iter("office:quality:*"):
                data = await r.hgetall(key)
                bot_name = key.split(":")[-1]
                result[f"quality:{bot_name}"] = {
                    "up":   int(data.get("up",   0)),
                    "down": int(data.get("down", 0)),
                }

        # ── 2. health audit ───────────────────────────────────────────────
        if _mentions(task_lower, ["health", "здоровь", "status", "up/down", "живой", "живые"]):
            async for key in r.scan_iter("office:health:*"):
                agent = key.split(":")[-1]
                result[f"health:{agent}"] = await r.get(key)

        # ── 3. logs ───────────────────────────────────────────────────────
        if _mentions(task_lower, ["log", "лог", "событи", "ошибк"]):
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
        # Без хардкода 'office:' — любой namespace (fix_count:*, board:*, …),
        # регистр сохраняем, URL-схемы (https://...) отсекаем
        import re as _re
        pattern_match = _re.search(
            r'\b(?!https?:)([A-Za-z][A-Za-z0-9_.\-]*:(?!//)[A-Za-zа-яА-ЯёЁ0-9:*_.\-]+)', task)
        if pattern_match and not result:
            pattern_str = pattern_match.group(1)
            if not pattern_str.endswith("*"):
                # Точный ключ — сначала проверяем тип чтобы не получить WRONGTYPE
                try:
                    key_type = await r.type(pattern_str)
                    key_type = key_type.decode() if isinstance(key_type, bytes) else str(key_type)
                    if key_type == "string":
                        val = await r.get(pattern_str)
                        result[pattern_str] = val
                    elif key_type == "hash":
                        result[pattern_str] = await r.hgetall(pattern_str)
                    elif key_type == "set":
                        members = await r.smembers(pattern_str)
                        result[pattern_str] = sorted([m.decode() if isinstance(m, bytes) else m for m in members])
                    elif key_type == "list":
                        result[pattern_str] = await r.lrange(pattern_str, 0, 19)
                    elif key_type == "zset":
                        result[pattern_str] = await r.zrange(pattern_str, 0, 19, withscores=True)
                    else:
                        result[pattern_str] = f"key_type={key_type}"
                except Exception as _e:
                    result[pattern_str] = f"error: {_e}"
            else:
                async for key in r.scan_iter(pattern_str):
                    try:
                        key_type = await r.type(key)
                        key_type = key_type.decode() if isinstance(key_type, bytes) else str(key_type)
                        if key_type == "string":
                            result[key] = await r.get(key)
                        elif key_type == "hash":
                            result[key] = await r.hgetall(key)
                        elif key_type == "set":
                            members = await r.smembers(key)
                            result[key] = sorted([m.decode() if isinstance(m, bytes) else m for m in members])
                        elif key_type == "list":
                            result[key] = await r.lrange(key, 0, 9)
                    except Exception:
                        pass

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

    elif intent == "trader_winrate":
        """Винрейт трейдера: читает signals:list/signal:* и считает TP/SL по 1h-свечам.

        Свечи берём фолбэк-цепочкой binance→okx (BingX пропускаем — гео-бан IP).
        Закрытие консервативное: если в одной свече задеты и TP, и SL — считаем SL.
        Отдаёт WR за 7 дней и за всё время + краткую разбивку.
        """
        r = await get_redis()
        if not r:
            await reply_func("❌ Redis недоступен")
            return
        SIGNAL_TTL = 259200  # 72h — как в tilly-trader
        now_ts = int(time.time())
        _cache: dict = {}

        async def _tw_fetch(symbol: str) -> list:
            if symbol in _cache:
                return _cache[symbol]
            if "-" in symbol:
                base, quote = symbol.split("-", 1)
            else:
                quote = "USDT"; base = symbol[:-len(quote)] if symbol.endswith(quote) else symbol
            base, quote = base.upper(), quote.upper()
            concat = f"{base}{quote}"
            rows: list = []
            async with httpx.AsyncClient(timeout=10) as c:
                try:
                    resp = await c.get("https://fapi.binance.com/fapi/v1/klines",
                                       params={"symbol": concat, "interval": "1h", "limit": 1000})
                    if resp.status_code == 200:
                        rows = [(int(k[0]), float(k[2]), float(k[3])) for k in (resp.json() or [])]
                except Exception:
                    rows = []
                if len(rows) < 20:
                    try:
                        resp = await c.get("https://www.okx.com/api/v5/market/candles",
                                           params={"instId": f"{base}-{quote}-SWAP", "bar": "1H", "limit": 300})
                        if resp.status_code == 200:
                            lst = resp.json().get("data") or []
                            rows = [(int(k[0]), float(k[2]), float(k[3])) for k in lst]
                    except Exception:
                        pass
            rows.sort(key=lambda x: x[0])
            _cache[symbol] = rows
            return rows

        def _tw_outcome(direction: str, sl: float, tp: float, window: list):
            for (_tms, high, low) in window:
                if direction == "LONG":
                    sl_hit = low <= sl; tp_hit = high >= tp
                else:
                    sl_hit = high >= sl; tp_hit = low <= tp
                if sl_hit:      # покрывает и (sl_hit and tp_hit) → sl
                    return "sl"
                if tp_hit:
                    return "tp"
            return None

        sids = await r.lrange("signals:list", 0, -1)
        raw_signals = []
        for sid in sids:
            sid = sid if isinstance(sid, str) else sid.decode()
            rawj = await r.get("signal:" + sid)
            if rawj:
                try:
                    raw_signals.append(json.loads(rawj))
                except Exception:
                    pass

        async def _tw_calc(days: int):
            cutoff = now_ts - days * 86400 if days > 0 else 0
            tot = op = w = l = ex = nodata = 0
            lines = []
            for s in raw_signals:
                if s.get("ts", 0) < cutoff:
                    continue
                tot += 1
                ts = s["ts"]; end_ts = min(now_ts, ts + SIGNAL_TTL)
                sl = float(s.get("sl") or 0)
                tp = float(s.get("tp1") or s.get("tp") or 0)
                direction = s.get("direction", "LONG")
                candles = await _tw_fetch(s.get("symbol", ""))
                window = [c for c in candles if ts * 1000 <= c[0] <= end_ts * 1000]
                if not candles:
                    nodata += 1; mark = "⚠️"
                elif tp and window and (hit := _tw_outcome(direction, sl, tp, window)):
                    if hit == "tp":
                        w += 1; mark = "✅"
                    else:
                        l += 1; mark = "❌"
                elif now_ts >= ts + SIGNAL_TTL:
                    ex += 1; mark = "⌛"
                else:
                    op += 1; mark = "⏳"
                lines.append(f"{mark} {s.get('symbol','?')} {direction}")
            closed = w + l
            wr = f"{round(w / closed * 100, 1)}%" if closed else "нет закрытых"
            return {"tot": tot, "open": op, "win": w, "loss": l, "exp": ex,
                    "nodata": nodata, "wr": wr, "lines": lines}

        d7 = await _tw_calc(7)
        da = await _tw_calc(0)

        def _tw_fmt(tag, d):
            extra = f", ⚠️нет свечей {d['nodata']}" if d["nodata"] else ""
            return (f"{tag}: всего {d['tot']}, закрыто {d['win'] + d['loss']} "
                    f"(✅{d['win']}/❌{d['loss']}), ⌛{d['exp']}, ⏳{d['open']}{extra} → WR {d['wr']}")

        out_lines = [
            "📊 Винрейт трейдера",
            _tw_fmt("7 дней", d7),
            _tw_fmt("Всё время", da),
            "",
            "Разбивка (всё время):",
            *da["lines"][:40],
        ]
        await reply_func("\n".join(out_lines))

    elif intent in ("push_code", "fix_bot"):
        if not repo or not path:
            await reply_func("❓ Уточни: в каком репо и какой файл изменить?")
            return
        await reply_func(f"⏳ Генерирую код для `{repo}/{path}`...")
        code = await ask_claude(task)
        await reply_func("🔎 Валидирую и ставлю правку на ревью...")
        # Дефект 1+2: не пушим напрямую в main + не деплоим без ревью.
        # safe_autonomous_push валидирует (пин/усадка) и открывает PR с кнопками апрува.
        service_id = next((sid for sid, (r, _) in SERVICES.items() if r == repo), None)
        await safe_autonomous_push(
            repo, path, code, f"nl: {task[:60]}",
            service_id=service_id or "", reply_func=reply_func, silent=silent,
        )

    elif intent == "create_repo":
        # Создать ПУСТОЙ репозиторий — и ничего больше.
        #
        # Отдельный интент появился 31.07.2026. До него готовый create_repo был
        # вызываем ТОЛЬКО изнутри create_bot, и просьба «создай репозиторий
        # molly-trader» классифицировалась как dev_task (confidence 0.92):
        # команда разработки три попытки писала скрипт, который создаёт
        # репозиторий через GitHub API, с выдуманной организацией "ai-office" в
        # GITHUB_ORG, задача ушла в blocked, а репозиторий так и не появился.
        # Инструмент лежал рядом — не хватало только способа его позвать.
        if not repo:
            await reply_func("❓ Как назвать репозиторий?")
            return
        name = str(repo).strip().strip("/")
        if name.startswith(GITHUB_USER + "/"):
            name = name[len(GITHUB_USER) + 1:]
        if not name or len(name) > 100 or not all(c.isalnum() or c in "._-" for c in name):
            await reply_func(f"❌ Недопустимое имя репозитория: `{name}`")
            return
        try:
            info = await create_repo(name, description=(task or "")[:200])
            await reply_func(f"✅ Репозиторий создан: {info['url']} (приватный, пустой).")
        except ValueError as ex:
            # 422 от GitHub — репо уже есть либо имя занято/недопустимо.
            await reply_func(f"⚠️ {ex}")
        except Exception as ex:
            await reply_func(f"❌ GitHub: {ex}")

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
            setup = first_json_object(setup_raw)
            if setup is None:
                raise ValueError(f"параметры бота не разобрались: {setup_raw[:120]!r}")
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
            # Redeploy Filly (Дефект 4: проверяем статус сборки, FAILED → алерт Владу)
            filly_service_id = "5d61d403-feee-455e-9c0d-523f0e7c79d5"
            await redeploy_and_verify(filly_service_id, "filly-bot")
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
            ext = first_json_object(extraction_raw) or {}
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
            _dep_ok = await redeploy_and_verify("5d61d403-feee-455e-9c0d-523f0e7c79d5", "filly-bot")
            await reply_func("✅ Филли обновлён и задеплоен" if _dep_ok
                             else "⚠️ Филли обновлён, но деплой не подтверждён (алерт в личке)")
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
        service_id = resolve_service_id(repo)
        if not service_id:
            # Имя на Railway может не совпадать с именем репозитория — тогда
            # спрашиваем Railway. Тот же фолбэк стоит в create_bot и в четырёх
            # ветках аудита.
            service_id = await railway_get_service_id(repo)
        if not service_id:
            if repo == SELF_REPO:
                # Свой случай отдельный: дело не в опечатке, а в незаданной
                # переменной, и без неё я не угадаю id — мой Railway-токен
                # проекта с этим сервисом не видит.
                await reply_func(
                    "❌ Не могу передеплоить себя: не задан SELF_SERVICE_ID.\n"
                    "Мой сервис лежит в проекте, которого мой токен не видит, "
                    "а прежний id (efa6bd21…) удалён 30.05 — угадывать не буду.\n"
                    "Влад: возьми service id из Railway UI и положи в "
                    "SELF_SERVICE_ID у меня и в SILLI_SERVICE_ID у watchdog-bot "
                    "— у него сейчас тот же мёртвый id.")
                return
            await reply_func(
                f"❌ Сервис {repo} не найден ни в SERVICES, ни в Railway. "
                f"Проверь название репозитория.")
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


    elif intent == "create_cron":
        extract_prompt = f"""Из запроса извлеки параметры cron. JSON без markdown:
{{"bot":"kriss","chat_id":391077101,"schedule":"0 1 * * *","message":"текст","generate":false,"name":"kriss-daily-reminder"}}
schedule — UTC (Дананг UTC+7). Запрос: {message_text}"""
        raw = await ask_claude(extract_prompt, system="Верни только валидный JSON без markdown.", model="claude-haiku-4-5-20251001")
        try:
            raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            params = json.loads(raw)
        except Exception as e:
            await reply_func(f"❌ Не смог разобрать параметры: {e}")
            return

        bot_name_cron = params.get("bot", "kriss")
        chat_id_cron  = int(params.get("chat_id", 391077101))
        schedule      = params.get("schedule", "0 9 * * *")
        msg_cron      = params.get("message", "Напоминание")
        generate      = params.get("generate", False)
        _ts           = int(__import__('time').time()) % 100000
        cron_name     = params.get("name", f"{bot_name_cron}-cron-{_ts}")

        bot_url = f"https://{bot_name_cron}-bot-production.up.railway.app/send_scheduled"
        payload_data  = json.dumps({"chat_id": chat_id_cron, "message": msg_cron, "generate": generate})

        await reply_func(f"⏰ Создаю cron *{cron_name}*...\nРасписание: `{schedule}`\nСообщение: {msg_cron}")

        try:
            import urllib.request as _ur

            RAILWAY_TOKEN = os.getenv("RAILWAY_TOKEN_VLAD") or os.getenv("RAILWAY_TOKEN") or ""
            if not RAILWAY_TOKEN:
                await reply_func("❌ RAILWAY_TOKEN не задан в окружении.")
                return
            PROJECT_ID    = "271b40b7-199a-429a-88ef-ca417f26a638"
            ENV_ID        = "2efaaf60-ba39-492c-bf86-007fd505493f"

            def _rql(q, variables=None):
                body = {"query": q}
                if variables:
                    body["variables"] = variables
                req = _ur.Request(
                    "https://backboard.railway.app/graphql/v2",
                    data=json.dumps(body).encode(),
                    method="POST",
                    headers={"Authorization": f"Bearer {RAILWAY_TOKEN}",
                             "Content-Type": "application/json",
                             "User-Agent": "Mozilla/5.0 (compatible; railway-cli/3.0)"}
                )
                with _ur.urlopen(req) as r:
                    return json.loads(r.read())

            # 1. Создаём сервис
            d1 = _rql(f'mutation {{ serviceCreate(input: {{ projectId: "{PROJECT_ID}", name: "{cron_name}" }}) {{ id name }} }}')
            if not d1.get("data") or not d1["data"].get("serviceCreate"):
                err = d1.get("errors", [{}])[0].get("message", str(d1))
                await reply_func(f"❌ Railway serviceCreate failed: {err}")
                return
            svc_id = d1["data"]["serviceCreate"]["id"]

            # 2. Image
            _rql(f'mutation {{ serviceInstanceUpdate(serviceId: "{svc_id}", environmentId: "{ENV_ID}", input: {{ source: {{ image: "curlimages/curl:latest" }} }}) }}')

            # 3. Cron schedule
            _rql(f'mutation {{ serviceInstanceUpdate(serviceId: "{svc_id}", environmentId: "{ENV_ID}", input: {{ cronSchedule: "{schedule}" }}) }}')

            # 4. startCommand без кавычек внутри — payload через env var $P,
            #    office-токен через env var $T (иначе при OFFICE_RPC_STRICT=1 cron получит 401)
            start_cmd = f"curl -sf -X POST {bot_url} -H Content-Type:application/json -H X-Office-Token:$T -d $P"
            _rql(f'mutation {{ serviceInstanceUpdate(serviceId: "{svc_id}", environmentId: "{ENV_ID}", input: {{ startCommand: "{start_cmd}" }}) }}')

            # 5. Env var P = payload JSON
            _rql(
                'mutation Upsert($input: VariableUpsertInput!) { variableUpsert(input: $input) }',
                variables={"input": {
                    "projectId": PROJECT_ID,
                    "environmentId": ENV_ID,
                    "serviceId": svc_id,
                    "name": "P",
                    "value": payload_data
                }}
            )

            # 5b. Env var T = OFFICE_RPC_TOKEN (значение из окружения Силли на момент создания;
            #     пустое допустимо в warn-режиме, при ротации токена — обновить var у cron-сервисов)
            _rql(
                'mutation Upsert($input: VariableUpsertInput!) { variableUpsert(input: $input) }',
                variables={"input": {
                    "projectId": PROJECT_ID,
                    "environmentId": ENV_ID,
                    "serviceId": svc_id,
                    "name": "T",
                    "value": os.getenv("OFFICE_RPC_TOKEN", "")
                }}
            )

            await reply_func(f"✅ Cron создан!\n*{cron_name}*\n`{svc_id}`\nРасписание: `{schedule}` UTC\n{bot_name_cron} → {chat_id_cron}")
        except Exception as e:
            await reply_func(f"❌ Ошибка Railway: {e}")


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
            if any(w in task.lower() for w in ["дубл", "dedup", "дублир", "повтор"]):
                cutoff_mode = "dedup_lessons"
            else:
                cutoff_mode = "old_bots"
        else:
            cutoff_mode = "today_patterns"  # дефолт — паттерны за сегодня

        try:
            tg_cl = await get_telethon_client()
            messages = await tg_cl.get_messages(target_chat, limit=3000)
            to_delete = []
            _lesson_map = {}  # для dedup_lessons: lesson_key -> [msg_ids]
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
                elif cutoff_mode == "dedup_lessons":
                    import re as _re
                    if msg.text:
                        _m = _re.search(r'(?:Урок|Lesson) #(\S+)', msg.text)
                        if _m:
                            lesson_key = _m.group(1)
                            if lesson_key not in _lesson_map:
                                _lesson_map[lesson_key] = []
                            _lesson_map[lesson_key].append(msg.id)
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

            if cutoff_mode == "dedup_lessons":
                for lesson_key, msg_ids in _lesson_map.items():
                    if len(msg_ids) > 1:
                        msg_ids.sort()
                        to_delete.extend(msg_ids[:-1])
                if to_delete:
                    for i in range(0, len(to_delete), 100):
                        await tg_cl.delete_messages(target_chat, to_delete[i:i+100])
                        await _asyncio.sleep(0.5)
                    await tg_cl.disconnect()
                    await reply_func(f"✅ Удалено {len(to_delete)} дублей уроков")
                else:
                    await tg_cl.disconnect()
                    await reply_func("✅ Дублей уроков не найдено")
            elif to_delete:
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
        """Публикует в Bug Lessons только НОВЫЕ уроки через единый durable-механизм.
        Состояние «опубликован» хранится флагом posted_to_group в lessons.json (git) —
        переживает сброс Redis и НЕ может зафлудить. Чтобы перепостить конкретный урок,
        снимите ему posted_to_group в lessons.json."""
        await publish_pending_lessons(reply_func)


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
            # Дефект: old формирует LLM-классификатор из текста задачи, а не из файла,
            # поэтому побайтовое old_text not in file_content ломалось на variation
            # selector'ах эмодзи (U+FE0F), NBSP и отличии пробелов/отступа. Ищем
            # нормализованно, но заменяем ИСХОДНЫЙ срез файла (запись байт-в-байт).
            updated, how = resilient_replace(file_content, old_text, new_text)
            if updated is None:
                if how == "ambiguous":
                    await reply_func(f"❌ Строка встречается в нескольких местах в {repo}/{path} — уточни old (добавь контекст вокруг)")
                else:
                    await reply_func(f"❌ Строка не найдена в {repo}/{path}")
                return
            if path.endswith(".py"):
                import ast as _ast
                try:
                    _ast.parse(updated)
                except SyntaxError as e:
                    await reply_func(f"❌ SyntaxError после замены: {e}")
                    return
            commit_msg = intent_data.get("message", f"edit: patch {path}")
            await push_file(repo, path, updated, commit_msg)
            await reply_func(f"✅ {repo}/{path} обновлён" + ("" if how == "exact" else f" (совпадение: {how})"))
        except Exception as e:
            await reply_func(f"❌ Ошибка: {e}")


    elif intent == "agentic_task":
        """Agentic execution loop для многошаговых задач.
        ReAct pattern: think → act → observe → repeat.
        """
        AGENTIC_SYSTEM = """Ты — Силли, исполнитель задач AI-офиса.
Ты в agentic loop. На каждом шаге выбирай ОДНО действие и возвращай JSON.

Доступные действия:
- read_file: {"action":"read_file","repo":"...","path":"...","find":"опц. подстрока"} — без find возвращает начало файла (обрезается на 24000 симв.); с find возвращает строки ВОКРУГ совпадения с номерами. Для больших файлов (bot.py у ботов — 50К+ символов) сразу используй find с именем функции или куском целевой строки: иначе цель может лежать за окном и ты её не увидишь.
- push_file: {"action":"push_file","repo":"...","path":"...","content":"...","message":"..."} — правка НЕ идёт в main напрямую: она валидируется (пины/усадка) и открывается PR на ревью Владу; деплой будет только после мержа. Не жди мгновенного деплоя и не повторяй push.
- check_var: {"action":"check_var","service":"billy-bot","name":"REDIS_PROXY_TOKEN","expected":"опц. ожидаемая строка"} — читает переменную окружения сервиса в Railway (значение вернётся ЗАМАСКИРОВАННЫМ)
- railway_logs: {"action":"railway_logs","service":"billy-bot","lines":30} — последние строки логов последнего деплоя сервиса в Railway (lines ≤ 100)
- set_var: {"action":"set_var","service":"kriss-bot","name":"ALLOWED_USERS","value":"полное новое значение"} — записывает ОДНУ переменную окружения. value подставляется ЦЕЛИКОМ, дозаписи нет: сначала прочитай текущее через check_var, собери новое значение сам и передай его полностью. Секреты (TOKEN/KEY/SECRET/PASSWORD/URL подключения) записывать запрещено — их меняет человек.
- redeploy: {"action":"redeploy","service":"nelli-bot"} — передеплоить сервис в Railway и ДОЖДАТЬСЯ терминального статуса. Работает для ЛЮБОГО отдела (family-dept, marketing-dept, medical-dept, trading-dept), не только для основного проекта: serviceId и environmentId резолвятся запросом. Не пиши код для редеплоя и не заявляй, что нет доступа — доступ есть, используй это действие.
- send_message: {"action":"send_message","chat_id":-5194783850,"text":"..."} — в ОФИС ГРУППУ (-5194783850)
- send_messages: {"action":"send_messages","chat_id":-5194783850,"texts":["msg1","msg2",...]} — батч до 5
- done: {"action":"done","result":"итог для пользователя"}

Правила:
- Один JSON на шаг, без лишнего текста
- НИКОГДА не проси у людей токены/секреты/пароли/ключи в чат. Нужно значение переменной сервиса — используй check_var. Не хватает данных — заверши done с кратким запросом и НЕ повторяй одно и то же действие.
- Не больше 2 сообщений в группу за всю задачу.
- Если нужно прочитать несколько файлов — читай по одному
- done — когда задача выполнена. Максимум 12 шагов."""

        steps_log = []
        # Петля обязана ЗНАТЬ целевой репозиторий. Раньше здесь было
        # `context = task`: резолвленный repo (см. target_repo) до модели не
        # доходил, и она выбирала его в каждом действии сама. 15.08 из текста
        # «убрать кнопки в /menu» она вывела репозиторий "/menu" и доложила
        # «404 по всем попыткам» — то есть выдумала цель и отчиталась о ней
        # как о проблеме доступа.
        context = task
        if repo:
            context = (f"[Целевой репозиторий: {repo}. Используй ИМЕННО его в "
                       f"read_file/push_file. Не выводи название репозитория из "
                       f"текста задачи — оно уже определено.]\n\n{task}")
        max_steps = 12
        consecutive_failures = 0
        last_error = None
        group_sends = 0           # анти-спам: сообщений в группу за задачу
        sent_texts = set()        # дедуп одинаковых сообщений
        _last_action_sig = None   # стоп-гард против зацикливания
        parse_retries = 0         # подряд идущие неразобранные ответы модели
        _action_repeat = 0

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

            # Извлекаем JSON — ПЕРВЫЙ полный объект, а не срез «от первой
            # скобки до последней». Старый срез захватывал два объекта разом,
            # если модель их вернула подряд, и падал с «Extra data» (15.08:
            # баг-репорт Влада через Крисса умер на шаге 10).
            action_data = first_json_object(raw_action)

            if action_data is None:
                # Кривая выдача модели — это НЕ провал задачи. Раньше здесь был
                # break: одна неаккуратная реплика убивала всю работу, и человек
                # видел «ошибка парсинга» вместо результата. Даём поправку и
                # ещё попытку; если модель не понимает и её — тогда сдаёмся.
                parse_retries += 1
                snippet = raw_action[:200].replace("\n", " ")
                logger.warning("[agentic] шаг %s: не разобрал ответ (%s попытка): %r",
                               step_num + 1, parse_retries, snippet)
                if parse_retries > MAX_PARSE_RETRIES:
                    await reply_func(
                        f"❌ Шаг {step_num+1}: {MAX_PARSE_RETRIES + 1} раза подряд "
                        f"не смог разобрать собственный ответ. Последнее: {snippet[:120]}")
                    break
                context += ("\n\n[система] Предыдущий ответ не разобрался как JSON. "
                            "Верни РОВНО ОДИН объект действия, без текста вокруг, "
                            "без второго объекта, без markdown-ограждения.")
                continue

            parse_retries = 0

            action = action_data.get("action", "")

            # Стоп-гард: одно и то же действие 3 раза подряд → зацикливание
            _sig = json.dumps(action_data, sort_keys=True, ensure_ascii=False)[:300]
            if _sig == _last_action_sig:
                _action_repeat += 1
            else:
                _action_repeat = 0
                _last_action_sig = _sig
            # На первом повторе — корректирующий толчок (даём шанс самоисправиться),
            # и только при следующем повторе рвём цикл. Транзиентный повтор не роняет задачу.
            if _action_repeat == 1:
                context += ("\n\n[СИСТЕМА] Ты повторяешь то же действие — оно не приблизило к цели. "
                            "Показанного фрагмента достаточно: смени подход или заверши done с текущим итогом.")
            if _action_repeat >= 2:
                await reply_func("⚠️ Остановлено: повторяющееся действие (зацикливание).")
                break

            # Выполняем действие
            if action == "done":
                result_text = action_data.get("result", "✅ Готово")
                await reply_func(f"✅ {result_text}")  # только финал идёт в чат
                break

            elif action == "read_file":
                a_repo = action_data.get("repo", "")
                a_path = action_data.get("path", "")
                a_find = str(action_data.get("find", "") or "").strip()
                if not repo_looks_valid(a_repo):
                    # Отличаем «репо недоступен» от «репо выдуман». Четыре 404
                    # подряд выглядят как проблема доступа и уводят разбор в
                    # сторону токенов — так и случилось 15.08.
                    _hint = repo if repo else "уточни у постановщика"
                    result = (f"Репозитория {a_repo!r} нет среди репозиториев офиса. "
                              f"Целевой репозиторий этой задачи: {_hint}. "
                              f"Повтори read_file с ним.")
                    logger.warning("[agentic] выдуманный репозиторий %r (цель: %s)",
                                   a_repo, repo or "—")
                    steps_log.append({"action": f"read_file({a_repo}/{a_path})",
                                      "result": result})
                    context += f"\n\n[read_file] {result}"
                    continue
                try:
                    file_content = await read_file(a_repo, a_path)
                    # Окно чтения. Раньше срез прятал цель за границей видимости:
                    # 15.08 kriss-bot/bot.py вырос до 51 340 символов, целевая
                    # строка оказалась примерно на 42 000-м — за окном 24 000, и
                    # Силли не увидела того, что просили править. В июле окно уже
                    # поднимали (4000 → 24000) по этой же причине. Порог, привязанный
                    # к сегодняшнему размеру файлов, истекает молча вместе с их ростом,
                    # поэтому лечим не порогом, а возможностью СПРОСИТЬ нужное место.
                    _CAP = 24000
                    if a_find:
                        region = find_regions(file_content, a_find)
                        if region:
                            result = (f"[{a_path}: {file_summary(file_content)}; "
                                      f"фрагменты вокруг {a_find!r}]\n{region}")
                        else:
                            # Честное «не найдено» вместо начала файла: увидев
                            # валидный код без цели, модель заключает, что цели нет.
                            result = (f"[{a_path}: {file_summary(file_content)}] "
                                      f"строка {a_find!r} в файле НЕ найдена — "
                                      f"попробуй другую подстроку.")
                    elif len(file_content) > _CAP:
                        result = file_content[:_CAP] + (
                            f"\n\n[...файл обрезан: показано {_CAP} из {len(file_content)} символов. "
                            "Если нужной функции/строки здесь нет — НЕ перечитывай файл целиком, "
                            "а повтори read_file с параметром find: {\"action\":\"read_file\","
                            "\"repo\":\"...\",\"path\":\"...\",\"find\":\"имя_функции\"} — "
                            "вернутся строки вокруг совпадения...]")
                    else:
                        result = file_content
                    steps_log.append({"action": f"read_file({a_repo}/{a_path})", "result": result})
                    # Добавляем содержимое в контекст
                    context += f"\n\n[Файл {a_repo}/{a_path}]:\n{result}"
                    consecutive_failures = 0
                    last_error = None
                except Exception as e:
                    err_str = str(e)
                    steps_log.append({"action": f"read_file({a_repo}/{a_path})", "result": f"ERROR: {err_str}"})
                    if err_str == last_error:
                        consecutive_failures += 1
                    else:
                        consecutive_failures = 1
                        last_error = err_str
                    if consecutive_failures >= 3:
                        await reply_func(f"❌ Задача остановлена: повторяющаяся ошибка — {err_str}")
                        break

            elif action == "push_file":
                a_repo = action_data.get("repo", "")
                a_path = action_data.get("path", "")
                a_content = action_data.get("content", "")
                a_message = action_data.get("message", "agentic update")
                # Дефект 1+2: автономный пуш идёт через валидацию + ветку/PR, не в main напрямую.
                try:
                    a_status = await safe_autonomous_push(
                        a_repo, a_path, a_content, a_message,
                        reply_func=None, silent=True,
                    )
                    steps_log.append({"action": f"push_file({a_repo}/{a_path})", "result": a_status})
                except Exception as e:
                    steps_log.append({"action": f"push_file({a_repo}/{a_path})", "result": f"ERROR: {e}"})

            elif action == "send_messages":
                # silent (source=CLAUDE) / пауза / лимит 2 за задачу / дедуп
                if silent or await outbound_paused():
                    steps_log.append({"action": "send_messages", "result": "SKIPPED (silent/paused)"})
                elif group_sends >= 2:
                    steps_log.append({"action": "send_messages", "result": "SUPPRESSED (лимит сообщений в группу)"})
                else:
                    a_chat = action_data.get("chat_id", -5194783850)
                    texts = action_data.get("texts", [])
                    sent = 0
                    import asyncio as _asyncio
                    for t in texts[:5]:
                        if group_sends >= 2 or str(t) in sent_texts:
                            continue
                        try:
                            await _GLOBAL_BOT.send_message(chat_id=int(a_chat), text=str(t))
                            sent += 1
                            group_sends += 1
                            sent_texts.add(str(t))
                            await _asyncio.sleep(0.5)
                        except Exception:
                            pass
                    steps_log.append({"action": f"send_messages({a_chat})", "result": f"sent {sent}/{len(texts)}"})

            elif action == "send_message":
                # silent (source=CLAUDE) / пауза / лимит 2 за задачу / дедуп
                if silent or await outbound_paused():
                    steps_log.append({"action": "send_message", "result": "SKIPPED (silent/paused)"})
                elif group_sends >= 2 or action_data.get("text", "") in sent_texts:
                    steps_log.append({"action": "send_message", "result": "SUPPRESSED (дубль/лимит)"})
                else:
                    a_chat = action_data.get("chat_id", -5194783850)
                    a_text = action_data.get("text", "")
                    try:
                        await _GLOBAL_BOT.send_message(chat_id=int(a_chat), text=a_text)
                        group_sends += 1
                        sent_texts.add(a_text)
                        steps_log.append({"action": f"send_message({a_chat})", "result": "OK"})
                    except Exception as e:
                        steps_log.append({"action": f"send_message({a_chat})", "result": f"ERROR: {e}"})

            elif action == "railway_logs":
                a_service = action_data.get("service", "")
                try:
                    a_lines = max(1, min(int(action_data.get("lines", 30) or 30), 100))
                except (TypeError, ValueError):
                    a_lines = 30
                try:
                    svc_id = next((sid for sid, (r, _) in SERVICES.items() if r == a_service), None)
                    if not svc_id:
                        svc_id = await railway_get_service_id(a_service)
                    if not svc_id:
                        raise Exception(f"сервис '{a_service}' не найден")
                    dep_data = await railway_query("""
                        query($id: String!) {
                          deployments(input: { serviceId: $id }) {
                            edges { node { id status createdAt } }
                          }
                        }
                    """, {"id": svc_id})
                    edges = (dep_data.get("data") or {}).get("deployments", {}).get("edges", [])
                    if not edges:
                        raise Exception(f"у '{a_service}' нет деплоев")
                    dep_node = edges[0]["node"]
                    log_data = await railway_query("""
                        query($id: String!) {
                          deploymentLogs(deploymentId: $id) { message timestamp }
                        }
                    """, {"id": dep_node["id"]})
                    logs = (log_data.get("data") or {}).get("deploymentLogs", []) or []
                    tail = [redact(f"{l.get('timestamp', '')} {l.get('message', '')}")
                            for l in logs[-a_lines:]]
                    res = (f"деплой {dep_node['id'][:8]} ({dep_node.get('status', '?')}): "
                           + (f"{len(tail)} строк логов" if tail else "логи пусты"))
                    steps_log.append({"action": f"railway_logs({a_service})", "result": res})
                    if tail:
                        context += (f"\n\n[Логи {a_service}, последние {len(tail)} строк]:\n"
                                    + "\n".join(tail)[-4000:])
                    consecutive_failures = 0
                    last_error = None
                except Exception as e:
                    err_str = str(e)
                    steps_log.append({"action": f"railway_logs({a_service})", "result": f"ERROR: {err_str}"})
                    if err_str == last_error:
                        consecutive_failures += 1
                    else:
                        consecutive_failures = 1
                        last_error = err_str
                    if consecutive_failures >= 3:
                        await reply_func(f"❌ Задача остановлена: повторяющаяся ошибка — {err_str}")
                        break

            elif action == "check_var":
                a_service  = action_data.get("service", "")
                a_name     = action_data.get("name", "")
                a_expected = action_data.get("expected")
                try:
                    svc_id = next((sid for sid, (r, _) in SERVICES.items() if r == a_service), None)
                    if not svc_id:
                        svc_id = await railway_get_service_id(a_service)
                    if not svc_id:
                        raise Exception(f"сервис '{a_service}' не найден")
                    vars_map = await railway_get_variables(svc_id)
                    val = vars_map.get(a_name)
                    if val is None:
                        res = f"{a_name} НЕ задан на {a_service}"
                    else:
                        masked = (val[:4] + "…" + val[-4:]) if len(str(val)) > 8 else "***"
                        res = f"{a_name} на {a_service} = {masked} (len={len(str(val))})"
                        if a_expected is not None:
                            res += f"; equals_expected={str(val) == str(a_expected)}"
                    steps_log.append({"action": f"check_var({a_service}/{a_name})", "result": res})
                    context += f"\n\n[check_var] {res}"
                    consecutive_failures = 0
                    last_error = None
                except Exception as e:
                    err_str = str(e)
                    steps_log.append({"action": f"check_var({a_service}/{a_name})", "result": f"ERROR: {err_str}"})
                    if err_str == last_error:
                        consecutive_failures += 1
                    else:
                        consecutive_failures = 1
                        last_error = err_str
                    if consecutive_failures >= 3:
                        await reply_func(f"❌ Задача остановлена: повторяющаяся ошибка — {err_str}")
                        break

            elif action == "set_var":
                a_service = action_data.get("service", "")
                a_name    = action_data.get("name", "")
                a_value   = action_data.get("value")
                try:
                    allowed, why = set_var_allowed(a_name)
                    if not allowed:
                        raise Exception(why)
                    if a_value is None:
                        raise Exception("value обязателен и подставляется целиком")
                    svc_id = next((sid for sid, (r, _) in SERVICES.items() if r == a_service), None)
                    if not svc_id:
                        svc_id = await railway_get_service_id(a_service)
                    if not svc_id:
                        raise Exception(f"сервис '{a_service}' не найден")

                    before = (await railway_get_variables(svc_id)).get(a_name)
                    ok_set = await railway_set_variable(svc_id, a_name, str(a_value))
                    if not ok_set:
                        raise Exception("Railway вернул variableUpsert=false")
                    # Читаем ФАКТ, а не верим ответу мутации.
                    after = (await railway_get_variables(svc_id)).get(a_name)
                    if str(after) != str(a_value):
                        raise Exception(
                            f"записали, но чтение вернуло другое: {str(after)[:80]!r}")

                    res = (f"{a_name} на {a_service}: было {str(before)[:60]!r} → "
                           f"стало {str(after)[:60]!r} (Railway передеплоит сервис)")
                    logger.warning("[set_var] %s/%s изменена", a_service, a_name)
                    steps_log.append({"action": f"set_var({a_service}/{a_name})", "result": res})
                    context += f"\n\n[set_var] {res}"
                    consecutive_failures = 0
                    last_error = None
                except Exception as e:
                    err_str = str(e)
                    steps_log.append({"action": f"set_var({a_service}/{a_name})",
                                      "result": f"ERROR: {err_str}"})
                    if err_str == last_error:
                        consecutive_failures += 1
                    else:
                        consecutive_failures = 1
                        last_error = err_str
                    if consecutive_failures >= 3:
                        await reply_func(f"❌ Задача остановлена: повторяющаяся ошибка — {err_str}")
                        break

            elif action == "redeploy":
                # ЗАЧЕМ ЭТО ДЕЙСТВИЕ (инцидент 2026-08-13):
                #     Владу пришлось руками просить поднять упавший nelli-bot.
                #     На прямой приказ «найди serviceId, передеплой, проверь
                #     логи» петля выдала СГЕНЕРИРОВАННЫЙ python-скрипт и вывод
                #     «нет RAILWAY_TOKEN для family-dept, доступа к проекту не
                #     имею». Оба утверждения ложны: тем же токеном её же аудит
                #     часом раньше перечислил сервисы family-dept и сообщил
                #     nelli-bot:CRASHED. Причина не в доступе — в петле не было
                #     действия «передеплоить», а модель без инструмента делает
                #     то же, что и в июльском инциденте с set_var: пишет код и
                #     правдоподобно объясняет отсутствие результата нехваткой
                #     креденшелов. Дыру закрывает инструмент, не промпт.
                a_service = action_data.get("service", "")
                try:
                    svc_id = next((sid for sid, (r, _) in SERVICES.items() if r == a_service), None)
                    if not svc_id:
                        svc_id = await railway_get_service_id(a_service)
                    if not svc_id:
                        raise Exception(f"сервис '{a_service}' не найден в Railway")

                    if not await redeploy_service(svc_id):
                        raise Exception("Railway отклонил serviceInstanceRedeploy")

                    # Не «запустила и молодец»: ждём терминальный статус.
                    # Запущенный деплой и удавшийся — разные события, и рапорт
                    # о первом вместо второго уже приводил к «починили» поверх
                    # сервиса, который так и не поднялся.
                    st = await wait_for_deploy(svc_id)
                    res = f"{a_service}: редеплой завершён со статусом {st}"
                    if st != "SUCCESS":
                        res += " — подними логи через railway_logs и разберись, что упало"
                    steps_log.append({"action": f"redeploy({a_service})", "result": res})
                    context += f"\n\n[redeploy] {res}"
                    consecutive_failures = 0
                    last_error = None
                except Exception as e:
                    err_str = str(e)
                    steps_log.append({"action": f"redeploy({a_service})",
                                      "result": f"ERROR: {err_str}"})
                    if err_str == last_error:
                        consecutive_failures += 1
                    else:
                        consecutive_failures = 1
                        last_error = err_str
                    if consecutive_failures >= 3:
                        await reply_func(f"❌ Задача остановлена: повторяющаяся ошибка — {err_str}")
                        break

            else:
                steps_log.append({"action": action, "result": "UNKNOWN ACTION"})
                await reply_func(f"⚠️ Неизвестное действие: {action}")
                break

        else:
            await reply_func(f"⚠️ Не смог завершить за {max_steps} шагов")


    elif intent == "dev_task":
        """Делегирование задачи команде dev-dept по цепочке:
        Силли составляет план → Девви пишет код → Рикки review →
        Тести QA → Секки security → Скрибби docs → Силли деплоит.
        Каждый бот читает GitHub сам, получает артефакт предыдущего."""
        # ── 1. Силли составляет план ───────────────────────────────────────
        # Канонический список репо из SERVICES
        known_repos = sorted(set(repo_name for _, (repo_name, _) in SERVICES.items()))
        known_repos_str = ", ".join(known_repos)

        plan_prompt = (
            f"Задача: {task}\n\n"
            "Составь краткий план реализации для команды разработки.\n"
            f"Доступные репозитории (выбирай ТОЛЬКО из этого списка): {known_repos_str}\n\n"
            "Определи:\n"
            "1. repo — ТОЧНОЕ имя репо из списка выше (только имя, без org), или null\n"
            "2. file_path — путь к файлу (обычно bot.py)\n"
            "3. devvy_task — конкретное ТЗ для разработчика (что именно написать/изменить)\n\n"
            "Ответь ТОЛЬКО JSON без пояснений: {\"repo\": \"...\", \"file_path\": \"bot.py\", \"devvy_task\": \"...\"}"
        )
        plan_raw = await ask_claude(plan_prompt, system=CODER_PROMPT, model="claude-haiku-4-5-20251001")
        try:
            plan = first_json_object(plan_raw) or {}
        except Exception:
            plan = {}

        dev_repo      = repo or plan.get("repo") or ""
        dev_file_path = file_path_override or plan.get("file_path") or "bot.py"
        # ТЗ для Девви = ОРИГИНАЛ задачи (авторитетно) + уточнение плана.
        # Не даём перефразу планировщика (Haiku/Ollama) затереть детали запроса —
        # из-за этого ТЗ искажалось ("подними порог до 9" вместо реальных правок).
        _hint = (plan.get("devvy_task") or "").strip()
        devvy_task    = task if not _hint else f"{task}\n\nУточнение плана: {_hint}"

        # Силли сама читает файл и передаёт контекст команде — надёжнее чем доверять Девви
        file_context = ""
        if dev_repo:
            try:
                gh_pat = os.getenv("GH_PAT", "")
                _url = f"https://api.github.com/repos/{GITHUB_USER}/{dev_repo}/contents/{dev_file_path}"
                _req = __import__("urllib.request", fromlist=["Request"]).Request(
                    _url, headers={"Authorization": f"token {gh_pat}", "User-Agent": "cilly-planner"}
                )
                import urllib.request as _ur
                with _ur.urlopen(_req, timeout=15) as _r:
                    _d = json.load(_r)
                file_context = __import__("base64").b64decode(_d["content"]).decode()
                logger.info(f"[dev_task] read {dev_repo}/{dev_file_path}: {len(file_context)} chars")
            except Exception as e:
                logger.warning(f"[dev_task] failed to read {dev_repo}/{dev_file_path}: {e}")

        await reply_func(
            f"🧠 План готов\n"
            f"📦 Репо: {dev_repo or 'не указано'}\n"
            f"📄 Файл: {dev_file_path}\n"
            f"📋 ТЗ: {devvy_task[:200]}"
        )

        # ── 2. Доска задач + параллельный пайплайн с ретраями ──────────────
        # Девви → [Рикки ‖ Тести ‖ Секки] → Скрибби. При провале гейта
        # (NEEDS_FIX / не компилируется / нет кода) — авто-повтор с фидбеком,
        # до MAX_DEV_ATTEMPTS. Исчерпали — blocked + эскалация (как fix_count>=3).
        from ai_office_shared.shared.dev_activity import publish_activity
        import uuid as _uuid

        _r_act   = await get_redis()
        _task_id = _uuid.uuid4().hex[:12]

        # Заводим задачу на доске тем же id, что у эфира — связка board ↔ activity
        board_id = await tb.create_task(
            _r_act, f"dev_task: {devvy_task[:80]}",
            created_by="силли", assignee="dev-dept",
            status="in_progress", task_id=_task_id,
            acceptance=DEV_ACCEPTANCE,   # заморожены ДО работы
        ) or _task_id

        async def _attempt_failed(attempt_no: int, total: int, feedback: str):
            await tb.update_status(_r_act, board_id, "needs_fix", result=feedback)
            await reply_func(f"🛠 Попытка {attempt_no}/{total} отклонена: {feedback[:160]}")

        chain = await run_dev_chain(
            devvy_task, repo=dev_repo, file_path=dev_file_path, context=file_context,
            board_id=board_id, task_id=_task_id, redis_client=_r_act,
            on_attempt_fail=_attempt_failed,
        )
        final_code     = chain["final_code"]
        commit_msg     = chain["commit_msg"]
        retry_feedback = chain["reason"]
        attempt        = chain["attempts"]
        pipe           = chain["pipe"]
        results = {
            "Девви":   pipe.get("devvy", "")   or "⚠️ нет ответа",
            "Рикки":   pipe.get("ricky", "")   or "⚠️ нет ответа",
            "Тести":   pipe.get("testi", "")   or "⚠️ нет ответа",
            "Секки":   pipe.get("sekky", "")   or "⚠️ нет ответа",
            "Скрибби": pipe.get("scribbi", "") or "⚠️ нет ответа",
        }

        # Подзадачи-срез по воркерам — прозрачность доски
        for _name, _res in results.items():
            _st = "done" if "⚠️" not in _res else "blocked"
            await tb.add_subtask(_r_act, board_id,
                                 f"{_name}: {_res[:60].replace(chr(10), ' ')}",
                                 assignee=_name.lower(), status=_st)

        # ── 3. Гейт: успех → стейджим деплой на /approve; провал → эскалация ─
        # (улики по гейту записал run_dev_chain — см. record_dev_gate_evidence)
        if chain["ok"] and dev_repo and dev_file_path:
            action_id = await stage_pending("deploy_devtask", {
                "repo": dev_repo, "path": dev_file_path, "code": final_code,
                "commit_msg": commit_msg or f"feat: {devvy_task[:60]}",
                "title": f"dev_task {dev_repo}/{dev_file_path}",
            }, task_id=board_id, title=f"deploy {dev_repo}/{dev_file_path}")
            await tb.update_status(_r_act, board_id, "awaiting_approval")
            await publish_activity(_r_act, _task_id, "силли", "done", "код готов, ждёт апрува")
            await send_proposal(
                f"✅ Код готов и прошёл гейт (review + compile).\n"
                f"📦 Деплой в {dev_repo}/{dev_file_path}\n"
                f"⏳ Approval-гейт — подтверди кнопкой ниже.\n"
                f"(или текстом: /approve {action_id})",
                "pg", action_id, chat_id=proposal_chat_id,
            )
            deploy_status = "✅ Код готов — отправил предложение на деплой с кнопками ✅/⏭"
        else:
            await tb.update_status(_r_act, board_id, "blocked",
                                   result=retry_feedback or "не удалось получить рабочий код",
                                   escalated=True)
            await publish_activity(_r_act, _task_id, "силли", "error",
                                   "исчерпаны попытки, эскалация")
            deploy_status = (
                f"⛔ После {MAX_DEV_ATTEMPTS} попыток рабочий код не получен — задача [{board_id}] "
                f"в статусе blocked, нужен твой разбор.\nПричина: {retry_feedback[:200]}"
            )

        await publish_activity(_r_act, _task_id, "силли", "deploy", deploy_status[:160])

        # ── 4. Итоговый отчёт ─────────────────────────────────────────────
        summary_parts = [f"🏁 Цепочка dev-dept завершена (задача [{board_id}], попыток: {attempt})\n"]
        for name, res in results.items():
            short = res[:150].replace("\n", " ")
            summary_parts.append(f"• {name}: {short}")
        summary_parts.append(f"\n{deploy_status}")
        if commit_msg:
            summary_parts.append(f"📝 {commit_msg}")

        await reply_func("\n".join(summary_parts))

    elif intent == "update_bot_instruction":
        """Рантайм-обучение бота: добавить/заменить инструкцию в системном промпте
        через Redis (office:instructions:{canon}) — бот учтёт без редеплоя.
        Approval-гейт: применяется только после /approve."""
        from ai_office_shared.shared.identity import canonical, display, BOTS

        target = intent_data.get("bot") or repo or ""
        canon = canonical(target) if target else None
        if not canon:
            for word in task.replace(",", " ").replace(":", " ").split():
                c = canonical(word)
                if c:
                    canon = c
                    break
        if not canon:
            await reply_func(
                "⚠️ Не понял, какому боту менять инструкцию. Укажи имя бота явно "
                f"(доступны: {', '.join(display(b) for b in BOTS)})."
            )
            return

        instruction = (intent_data.get("instruction") or "").strip()
        if not instruction:
            # эвристика: убираем имя бота из текста, остальное — инструкция
            words = [w for w in task.split() if canonical(w.strip(",:")) != canon]
            instruction = " ".join(words).strip(" :,-—") or task
        mode = intent_data.get("mode", "append")
        if mode not in ("append", "set", "clear"):
            mode = "append"
        disp = display(canon) or canon

        r_tb = await get_redis()
        board_id = await tb.create_task(
            r_tb, f"Инструкция {disp}: {instruction[:60]}",
            created_by="силли", assignee=canon, status="awaiting_approval",
        ) or ""
        action_id = await stage_pending("update_instruction", {
            "canon": canon, "display": disp, "instruction": instruction, "mode": mode,
        }, task_id=board_id, title=f"инструкция {disp}")
        await send_proposal(
            f"📝 Обновить инструкцию для {disp} (mode={mode}):\n"
            f"«{instruction[:300]}»\n\n"
            f"Бот учтёт это в следующем ответе БЕЗ редеплоя.\n"
            f"(или текстом: /approve {action_id})",
            "pg", action_id, chat_id=proposal_chat_id,
        )
        await reply_func(f"📨 Предложение по инструкции для {disp} — подтверди кнопкой ✅/⏭")

    elif intent == "delegate":
        """Делегирование задачи главе отдела (через офисный роутинг) + верификация
        результата. Approval-гейт: вызов отдела идёт после /approve."""
        from ai_office_shared.shared.office import OFFICE_AGENTS
        from ai_office_shared.shared.identity import canonical, display

        if confidence < 0.85:
            await reply_func(
                "🤔 Не уверен, кому и что делегировать. Уточни: какому отделу "
                "(Тилли/Милли/Доктор/Билли/Крисс/Вилли) и какую задачу?"
            )
            return

        target = intent_data.get("bot") or repo or ""
        canon = canonical(target) if target else None
        if not canon:
            for word in task.replace(",", " ").replace(":", " ").split():
                c = canonical(word)
                if c:
                    canon = c
                    break
        assignee_display = (canon.upper() if canon else (target.upper() if target else ""))
        if assignee_display not in OFFICE_AGENTS:
            await reply_func(
                f"⚠️ Не знаю отдела для делегирования: {assignee_display or target or '—'}.\n"
                f"Доступны: {', '.join(OFFICE_AGENTS)}"
            )
            return

        r_tb = await get_redis()
        board_id = await tb.create_task(
            r_tb, f"delegate {assignee_display}: {task[:60]}",
            created_by="силли", assignee=(canon or assignee_display.lower()),
            status="awaiting_approval",
        ) or ""
        action_id = await stage_pending("delegate", {
            "assignee_display": assignee_display,
            "task_text": task,
            "user_id": int(os.getenv("YOUR_TELEGRAM_ID", "391077101")),
        }, task_id=board_id, title=f"delegate {assignee_display}")
        await send_proposal(
            f"🤝 Делегировать {assignee_display}:\n«{task[:300]}»\n\n"
            f"После подтверждения дёрну отдел и проверю результат (верификация).\n"
            f"(или текстом: /approve {action_id})",
            "pg", action_id, chat_id=proposal_chat_id,
        )
        await reply_func(f"📨 Предложение делегировать {assignee_display} — подтверди кнопкой ✅/⏭")


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


async def set_bot_instruction(name: str, instruction: str, mode: str = "append") -> bool:
    """
    Рантайм-обучение бота: пишет office:instructions:{canon}, который бот дочитывает
    в build_system() и аппендит к системному промпту БЕЗ редеплоя.

    mode: "append" (добавить строку), "set" (заменить целиком), "clear" (удалить).
    """
    from ai_office_shared.shared.identity import canonical
    canon = canonical(name) or name
    r = await get_redis()
    if not r:
        return False
    key = f"office:instructions:{canon}"
    instruction = (instruction or "").strip()
    try:
        if mode == "clear" or not instruction:
            await r.delete(key)
        elif mode == "set":
            await r.set(key, instruction[:4000])
        else:  # append
            existing = await r.get(key) or ""
            combined = (existing + "\n" + instruction).strip() if existing else instruction
            await r.set(key, combined[:4000])
        return True
    except Exception as e:
        logger.warning(f"[instruction] set failed for {canon}: {e}")
        return False


async def _verify_delegation(task_text: str, response: str) -> dict:
    """Верификация ответа отдела: отвечает ли он на задачу. {ok:bool, reason:str}."""
    if not response.strip():
        return {"ok": False, "reason": "пустой ответ"}
    prompt = (
        f"Задача, которую делегировали отделу:\n{task_text}\n\n"
        f"Ответ отдела:\n{response[:2000]}\n\n"
        "Ответ закрывает задачу по существу (не отписка, не отказ, не запрос уточнений)?"
    )
    sys = ('Верификатор делегированных задач. JSON без markdown: '
           '{"ok": true|false, "reason": "1 короткое предложение"}')
    try:
        raw = await ask_claude(prompt, system=sys, model="claude-haiku-4-5-20251001")
        data = first_json_object(raw) or {}
        return {"ok": bool(data.get("ok")), "reason": str(data.get("reason", ""))[:200]}
    except Exception as e:
        # Верификатор упал — не блокируем, но честно помечаем
        return {"ok": True, "reason": f"верификатор недоступен ({e}), принято без проверки"}


async def run_delegation(assignee_display: str, task_text: str, user_id: int,
                         *, task_id: str = "") -> dict:
    """
    Делегирует задачу главе отдела через call_office (source=ФИЛЛИ → отдел вернёт JSON,
    не дублируя в группу), затем верифицирует ответ. Обновляет доску.
    Возвращает {ok, response, verdict}.
    """
    from ai_office_shared.shared.office import call_office
    r = await get_redis()
    if task_id:
        await tb.update_status(r, task_id, "in_progress")
    resp = await call_office(assignee_display, task_text, user_id, source="ФИЛЛИ")
    if not resp:
        if task_id:
            await tb.update_status(r, task_id, "blocked", result="нет ответа от отдела")
        return {"ok": False, "response": "", "verdict": "нет ответа от отдела"}
    verdict = await _verify_delegation(task_text, resp)
    if task_id:
        await tb.update_status(
            r, task_id, "done" if verdict["ok"] else "needs_fix",
            result=f"[{'OK' if verdict['ok'] else 'NEEDS_FIX'}] {verdict['reason']}\n\n{resp[:3000]}",
        )
    return {"ok": verdict["ok"], "response": resp, "verdict": verdict["reason"]}


async def _apply_pending_action(entry: dict) -> str:
    """
    Применяет подтверждённое (/approve) риск-действие. Диспетчит по type.
    Поддерживает legacy in-memory фикс-дикты (без поля type → deploy_fix).
    Возвращает человекочитаемый статус.
    """
    atype = entry.get("type")
    if atype is None:                 # legacy: сам entry и есть payload фикса
        atype = "deploy_fix"
        payload = entry
    else:
        payload = entry.get("payload", {}) or {}
    task_id = entry.get("task_id", "")
    r = await get_redis()
    try:
        if atype == "deploy_fix":
            analysis = payload.get("analysis", {})
            await push_file(
                payload["repo"], payload["affected"], payload["fixed_code"],
                f"approved fix({payload['service_name']}): {analysis.get('fix_description','')[:60]}",
            )
            redeployed = await redeploy_and_verify(payload["service_id"], payload.get("service_name", ""))
            status = "деплой SUCCESS ✅" if redeployed else "деплой не подтверждён ⚠️ (см. алерт в личке)"
            asyncio.create_task(append_ops_log(
                f"approved fix: {analysis.get('fix_description','')[:60]}",
                payload["service_name"], f"approved by Влад | {status}",
            ))
            await post_lesson(
                title        = analysis.get("lesson_title", ""),
                symptom      = analysis.get("lesson_symptom", ""),
                cause        = analysis.get("lesson_cause", ""),
                context      = f"{payload['repo']}/{payload['affected']}",
                fix          = analysis.get("lesson_fix", ""),
                how_to_avoid = analysis.get("lesson_avoid", ""),
            )
            if task_id:
                await close_task_reported(r, task_id, status)
            return f"✅ Фикс применён ({payload['service_name']}), {status}"

        if atype == "deploy_devtask":
            # Правило #70 стояло только в safe_autonomous_push, то есть закрывало
            # push_code/fix_bot/agentic_task. Сюда его не принесли, и dev_task на
            # ai-office-shared/agents/coder.py доходил бы до пуша в main — ровно
            # тот путь, которым 01.07.2026 лёг весь офис. Запрет должен стоять у
            # КАЖДОГО пишущего пути, а не у того, где о нём вспомнили.
            _self_edit = dev_queue.self_edit_refusal(payload.get("repo", ""),
                                                     payload.get("path", ""))
            if _self_edit:
                if task_id:
                    await tb.update_status(r, task_id, "blocked",
                                           result=_self_edit, escalated=True)
                asyncio.create_task(append_ops_log(
                    "dev_task push BLOCKED (self-edit)", payload.get("repo", ""),
                    payload.get("path", ""),
                ))
                return _self_edit
            # Последний рубеж: даже заапрувленный код не пушим, если он
            # схлопывает существующий файл (см. инцидент coder.py 5766 → 8 строк).
            try:
                _old_src = await read_file(payload["repo"], payload["path"])
            except Exception:
                _old_src = ""
            _shrink = file_shrink_guard(_old_src, payload.get("code", ""))
            if _shrink:
                if task_id:
                    await tb.update_status(r, task_id, "blocked",
                                           result=f"пуш заблокирован: {_shrink}", escalated=True)
                asyncio.create_task(append_ops_log(
                    "dev_task push BLOCKED (shrink guard)", payload["repo"],
                    f"{payload['path']}: {_shrink}",
                ))
                return (f"⛔ Пуш в {payload['repo']}/{payload['path']} ЗАБЛОКИРОВАН: {_shrink}.\n"
                        f"Нужна ручная проверка кода (ветка+PR, без автодеплоя).")
            res = await push_file(
                payload["repo"], payload["path"], payload["code"],
                payload.get("commit_msg") or f"feat: {payload.get('title', 'dev task')[:60]}",
            )
            action = res.get("action", "pushed") if isinstance(res, dict) else "pushed"
            url = res.get("url", "") if isinstance(res, dict) else ""
            asyncio.create_task(append_ops_log(
                "approved dev_task push", payload["repo"], f"{action} {payload['path']}",
            ))
            if task_id:
                await close_task_reported(r, task_id, f"{action}: {url}")
            return (f"✅ Код запушен в {payload['repo']}/{payload['path']} ({action}) — "
                    f"Railway задеплоит автоматически.\n{url}")

        if atype == "dev_queue_run":
            # Цепочка отдела идёт минутами — в callback'е её держать нельзя.
            # Фон докладывает о каждом исходе сам (см. _run_queued_dev_task).
            asyncio.create_task(_run_queued_dev_task(payload, task_id))
            return (f"🚀 Отдел взял заявку в работу: "
                    f"{payload.get('repo','?')}/{payload.get('file_path','?')}.\n"
                    f"Напишут код, откроют PR, дождутся CI — доложу по готовности.")

        if atype == "update_instruction":
            ok = await set_bot_instruction(
                payload["canon"], payload.get("instruction", ""),
                mode=payload.get("mode", "append"),
            )
            if task_id:
                await (close_task_reported(r, task_id) if ok
                       else tb.update_status(r, task_id, "blocked"))
            who = payload.get("display", payload.get("canon", "?"))
            return (f"✅ Инструкция для {who} обновлена (mode={payload.get('mode','append')})"
                    if ok else f"⚠️ Не удалось обновить инструкцию для {who} (Redis?)")

        if atype == "delegate":
            uid = payload.get("user_id") or int(os.getenv("YOUR_TELEGRAM_ID", "391077101"))
            result = await run_delegation(
                payload["assignee_display"], payload["task_text"], uid, task_id=task_id,
            )
            mark = "✅" if result["ok"] else "⚠️"
            return (f"{mark} Делегировано {payload['assignee_display']}: {result['verdict']}\n\n"
                    f"{result['response'][:1500]}")

        return f"❌ Неизвестный тип pending-действия: {atype}"
    except Exception as e:
        if task_id:
            await tb.update_status(r, task_id, "blocked", result=f"ошибка применения: {e}")
        return f"❌ Ошибка при применении ({atype}): {e}"


@dp.message(F.text.startswith("/approve"))
async def cmd_approve(message: Message):
    # /approve_pr — отдельный handler ниже; не перехватываем его здесь
    if message.text.startswith("/approve_pr"):
        return
    action_id = message.text[8:].strip()
    entry = await pop_pending(action_id)
    if not entry:
        await message.answer(f"❌ Действие `{action_id}` не найдено или уже применено.")
        return
    label = entry.get("title") or entry.get("type", "действие")
    await message.answer(f"⏳ Применяю: {label}...")
    status = await _apply_pending_action(entry)
    await mark_finding(action_id, "applied")
    await message.answer(status)


@dp.message(F.text.startswith("/office"))
async def cmd_office(message: Message):
    """Консоль самообслуживания офиса (только владелец).
      /office | /office scan  — полный свип, предложить фиксы (ждут /approve)
      /office fix <bot>       — точечно проверить и предложить фикс
      /office status          — снапшот health/quality/routing/задачи/находки
      /office inbox           — открытые находки с /approve id
    Ничего не применяет сам — только детект и предложение через гейт /approve."""
    owner = int(os.getenv("YOUR_TELEGRAM_ID", "0") or "0")
    if owner and message.from_user and message.from_user.id != owner:
        await message.answer("Только Влад может запускать самопроверку офиса.")
        return
    parts = message.text[len("/office"):].strip().split()
    sub = parts[0].lower() if parts else "scan"
    chat_id = message.chat.id

    if sub in ("scan", "проверь", "check", "свип"):
        await message.answer("🔎 Запускаю самопроверку офиса… (1–2 мин)")
        result = await _run_office_scan(proposal_chat_id=chat_id)
        await message.answer(_format_scan_report(result))
        return

    if sub in ("fix", "почини"):
        raw_bot = " ".join(parts[1:]).strip()
        if not raw_bot:
            await message.answer("Укажи бота: /office fix gosling-bot (или имя: гослинг)")
            return
        repo = _resolve_scan_target(raw_bot)
        if not repo:
            known = ", ".join(sorted({r for _, (r, _) in SERVICES.items()}))
            await message.answer(f"Не знаю бота «{raw_bot}». Доступные репо: {known}")
            return
        await message.answer(f"🔎 Проверяю {repo}…")
        result = await _run_office_scan(target=repo, proposal_chat_id=chat_id)
        await message.answer(_format_scan_report(result, target=repo))
        return

    if sub in ("status", "статус"):
        await message.answer(await _office_status_snapshot())
        return

    if sub in ("inbox", "инбокс", "находки"):
        await message.answer(await _office_inbox_text())
        return

    await message.answer(
        "Команды: /office scan | /office fix <bot> | /office status | /office inbox"
    )




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

    # Регистрируем PR-ы для /approve_pr + строим кнопки (по строке на PR)
    pr_lines = []
    kb_rows = []
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
        kb_rows.append([
            InlineKeyboardButton(text=f"✅ Мержить {item['repo']} #{pr['number']}",
                                 callback_data=f"pr:appr:{pr_id}"),
            InlineKeyboardButton(text="⏭", callback_data=f"pr:decl:{pr_id}"),
        ])

    errors_text = f"\n\n⚠️ Ошибки: {'; '.join(errors)}" if errors else ""
    await message.answer(
        f"✅ **Готово!** {result['changed_files']} файл(ов) изменено\n\n"
        f"**PR-ы:**\n" + "\n".join(pr_lines) +
        f"\n\n**Summary:** {result.get('summary','')}\n\n"
        f"Мержи кнопкой ниже (или текстом `/approve_pr {list(pending_prs.keys())[-1]}`)" +
        errors_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None,
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
    action_id = message.text[5:].strip()
    entry = await pop_pending(action_id)
    if entry:
        task_id = entry.get("task_id", "") if isinstance(entry, dict) else ""
        if task_id:
            r = await get_redis()
            await tb.update_status(r, task_id, "rejected", result="пропущено Владом")
        await mark_finding(action_id, "skipped")
        await message.answer(f"⏭️ Действие `{action_id}` пропущено.")
    else:
        await message.answer(f"❌ Действие `{action_id}` не найдено.")


@dp.callback_query(F.data.startswith("pg:") | F.data.startswith("pr:") | F.data.startswith("wk:"))
async def cb_approval(cb: CallbackQuery):
    """Единый обработчик кнопок ✅/⏭ для всех предложений Силли. Только Влад."""
    owner = int(os.getenv("YOUR_TELEGRAM_ID", "0") or "0")
    if owner and cb.from_user and cb.from_user.id != owner:
        await cb.answer("Только Влад может подтверждать", show_alert=True)
        return

    parts = (cb.data or "").split(":")
    domain = parts[0] if parts else ""
    verb = parts[1] if len(parts) > 1 else ""
    ident = parts[2] if len(parts) > 2 else ""

    # ── office:pending (deploy_fix / deploy_devtask / update_instruction / delegate) ──
    if domain == "pg":
        entry = await pop_pending(ident)
        if not entry:
            await cb.answer("Уже применено или истекло", show_alert=True)
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            return
        task_id = entry.get("task_id", "") if isinstance(entry, dict) else ""
        if verb == "defer":
            # «Не сейчас», а не «не надо»: задача остаётся open и вернётся в
            # очередь сама. Спрашивать заново раньше, чем через сутки, значит
            # превращать вопрос в спам, а спам — в кнопку, которую жмут не глядя.
            r_defer = await get_redis()
            await dev_queue.snooze(r_defer, task_id)
            await dev_queue.clear_asked(r_defer, task_id)
            await dev_queue.release_claim(r_defer, task_id)
            await mark_finding(ident, "skipped")
            await cb.answer("Отложено на сутки")
            await _finish_cb(cb, "🕓 Отложено на сутки — вернусь с этим вопросом позже")
            return
        if verb == "decl":
            if task_id:
                await tb.update_status(await get_redis(), task_id, "rejected",
                                       result="отклонено кнопкой")
                await dev_queue.clear_asked(await get_redis(), task_id)
            await mark_finding(ident, "skipped")
            await cb.answer("Отклонено")
            await _finish_cb(cb, "⏭ Отклонено Владом")
            return
        await cb.answer("Применяю…")
        status = await _apply_pending_action(entry)
        await mark_finding(ident, "applied")
        await _finish_cb(cb, status)
        return

    # ── PR-мерж из /cc (pending_prs) ──
    if domain == "pr":
        pr_data = pending_prs.pop(ident, None)
        if not pr_data:
            await cb.answer("PR не найден или уже обработан", show_alert=True)
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            return
        if verb == "decl":
            await cb.answer("Отклонено")
            await _finish_cb(cb, f"⏭ PR {pr_data['repo']} #{pr_data['pr_number']} отклонён")
            return
        await cb.answer("Мержу…")
        try:
            ok = await merge_pull_request(pr_data["repo"], pr_data["pr_number"],
                                          commit_msg="cc: approved by Влад")
            line = (f"✅ Смержен: {pr_data['repo']} #{pr_data['pr_number']}" if ok
                    else f"⚠️ Не смержен (конфликты?): {pr_data['repo']} #{pr_data['pr_number']}")
        except Exception as e:
            line = f"❌ Ошибка мержа {pr_data['repo']} #{pr_data['pr_number']}: {e}"
        await _finish_cb(cb, line)
        return

    # ── weekly proposal (single, PENDING_KEY) ──
    if domain == "wk":
        from agents.weekly_report import apply_proposal, PENDING_KEY
        r = await get_redis()
        if verb == "decl":
            if r:
                await r.delete(PENDING_KEY)
            await cb.answer("Пропущено")
            await _finish_cb(cb, "↩️ Предложение пропущено")
            return
        await cb.answer("Применяю…")
        result = await apply_proposal(r) if r else "⚠️ Redis недоступен"
        await _finish_cb(cb, result)
        return


@dp.message(F.text.startswith("/pause"))
async def cmd_pause(message: Message):
    """Мгновенно заглушить исходящие сообщения Силли в группу (kill-switch)."""
    r = await get_redis()
    if r:
        await r.set("cilly:paused", "1")
        await message.answer("⏸ Силли поставлена на паузу: исходящие в группу подавлены. /resume — снять.")
    else:
        await message.answer("⚠️ Redis недоступен. Поставь env CILLY_PAUSED=1 в Railway для остановки.")


@dp.message(F.text.startswith("/resume"))
async def cmd_resume(message: Message):
    """Снять паузу."""
    r = await get_redis()
    if r:
        await r.delete("cilly:paused")
        await message.answer("▶️ Пауза снята. (Если стоит env CILLY_PAUSED — убери его в Railway.)")
    else:
        await message.answer("⚠️ Redis недоступен.")


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


async def migrate_lessons_to_english(reply_func, confirm: bool) -> None:
    """Контролируемый перепост Bug Lessons на английском.

    dry-run (confirm=False): только считает существующие сообщения-уроки в группе,
    НИЧЕГО не трогает. Реальный прогон (confirm=True): удаляет старые сообщения-уроки
    (русские «Урок #» и английские «Lesson #»), сбрасывает posted_to_group у всех
    уроков в lessons.json и перепубликовывает английские версии через
    publish_pending_lessons. Идемпотентно, лимит на партию — анти-флуд (урок #54).
    НЕ автозапуск: только по явной команде владельца.
    """
    import re as _re
    try:
        tg_cl = await get_telethon_client()
        messages = await tg_cl.get_messages(BUG_LESSONS_CHAT, limit=3000)
        lesson_msg_ids = [
            m.id for m in messages
            if m and m.text and _re.search(r'(?:Урок|Lesson) #\S+', m.text)
        ]
        if not confirm:
            await tg_cl.disconnect()
            await reply_func(
                f"🔎 Dry-run: в Bug Lessons найдено {len(lesson_msg_ids)} сообщений-уроков.\n"
                f"Будут удалены и перепощены на английском. Подтверди: "
                f"`/migrate_lessons_en confirm`"
            )
            return

        # 1. Удаляем существующие сообщения-уроки (батчами по 100)
        deleted = 0
        for i in range(0, len(lesson_msg_ids), 100):
            await tg_cl.delete_messages(BUG_LESSONS_CHAT, lesson_msg_ids[i:i + 100])
            deleted += len(lesson_msg_ids[i:i + 100])
            await asyncio.sleep(0.5)
        await tg_cl.disconnect()
        await reply_func(f"🧹 Удалено {deleted} старых сообщений-уроков. Перепубликую на английском...")

        # 2. Сбрасываем posted_to_group у всех уроков (durable, в git) + коммит
        raw = await read_file("ai-office-shared", LESSONS_FILE)
        lessons = json.loads(raw)
        for l in lessons:
            l["posted_to_group"] = False
            l["posted_at"] = None
        await push_file(
            "ai-office-shared", LESSONS_FILE,
            json.dumps(lessons, ensure_ascii=False, indent=2),
            "chore(lessons): reset posted flags for English re-post",
        )

        # 3. Перепост английских версий (publish_pending_lessons постит непомеченные)
        posted = await publish_pending_lessons(reply_func=reply_func)
        await reply_func(f"✅ Миграция завершена: перепощено {posted} уроков на английском.")
    except Exception as e:
        await reply_func(f"❌ Ошибка миграции уроков: {e}")


async def _lessons_en_migration_once():
    """Одноразовый авто-перепост Bug Lessons на английском (явная авторизация владельца).

    At-most-once: ставит Redis-флаг `cilly:lessons_en_migrated` ДО удаления, поэтому
    деструктивный шаг НЕ повторится при рестартах/редеплоях (защита от флуда, урок #54).
    При частичном сбое владелец дозапустит `/migrate_lessons_en confirm` (idempotent).
    """
    try:
        await asyncio.sleep(40)  # дать боту, HTTP и Telethon подняться
        r = await get_redis()
        if not r:
            logger.warning("[lessons_en_migration] Redis недоступен — пропускаю one-shot")
            return
        if await r.get("cilly:lessons_en_migrated"):
            return  # уже выполнено ранее
        await r.set("cilly:lessons_en_migrated", "1")  # фиксируем ДО удаления (at-most-once)
        logger.info("[lessons_en_migration] одноразовый перепост уроков на английском")

        async def _log(msg: str):
            try:
                await notify_office(msg)
            except Exception:
                pass

        await migrate_lessons_to_english(_log, confirm=True)
    except Exception as e:
        logger.error(f"[lessons_en_migration] failed: {e}")


@dp.message(F.text.startswith("/migrate_lessons_en"))
async def cmd_migrate_lessons_en(message: Message):
    """Перепост Bug Lessons на английском. По умолчанию dry-run; `confirm` — выполнить.
    Только владелец (YOUR_TELEGRAM_ID)."""
    owner = int(os.getenv("YOUR_TELEGRAM_ID", "0") or "0")
    if owner and message.from_user and message.from_user.id != owner:
        await message.answer("⛔ Только владелец может запускать миграцию уроков.")
        return
    confirm = "confirm" in (message.text or "").lower()
    await migrate_lessons_to_english(message.answer, confirm)



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

    _reply_buffer = []

    async def reply(msg: str):
        # Буферизуем — шлём только финальный ответ, не промежуточные статусы
        _reply_buffer.append(msg)

    await handle_natural_language(text, message.chat.id, reply, history=dm_history[user_id],
                                  proposal_chat_id=message.chat.id)

    # Шлём только последний (финальный) ответ
    if _reply_buffer:
        final = _reply_buffer[-1]
        dm_history[user_id].append({"role": "assistant", "content": final})
        await message.answer(final, parse_mode=None)


# ── HTTP endpoint for Filly routing (family bots → Cilly) ────────────────────
async def handle_cilly_task(request):
    """Filly routes natural language requests here from any bot."""
    try:
        data = await request.json()
    except Exception as parse_err:
        return web.json_response({"status": "error", "detail": f"json parse: {parse_err}"}, status=400)
    try:
        # Привилегия определяется ЗАГОЛОВКОМ запроса, а не полем в теле:
        # тело шлёт вызывающий, и user_id в нём подделывается тривиально.
        privileged = request.headers.get("X-Auth-Token", "") == RAILWAY_SECRET and bool(RAILWAY_SECRET)
        return await _handle_cilly_task_inner(data, privileged=privileged)
    except Exception as e:
        import traceback
        return web.json_response({"status": "error", "detail": str(e), "trace": traceback.format_exc()[-1000:]}, status=200)

async def _handle_cilly_task_inner(data, *, privileged: bool = False):
    text    = data.get("message", "")
    agent   = data.get("agent", "Unknown")
    source  = data.get("source", "")
    # source=CLAUDE → полная тишина: не пишем промежуточные шаги ни в группу ни в личку
    silent  = data.get("silent", False) or source.upper() == "CLAUDE"
    # chat_id: куда слать промежуточные reply_func ответы (только если НЕ silent)
    # target_chat: явный параметр для операций (cleanup_group, post_lessons) — не зависит от silent
    chat_id     = data.get("chat_id", "") if not silent else ""
    target_chat = data.get("target_chat", "") or data.get("chat_id", "")
    # Явные repo/file_path из payload — прокидываем в планировщик (приоритетнее догадки интента)
    payload_repo = data.get("repo", "")
    payload_file = data.get("file_path", "")

    responses = []

    # /railway <gql> — ПЕРВЫЙ перехват, до LLM, не требует ANTHROPIC_API_KEY
    if text.strip().startswith("/railway"):
        # 🔴 Сырой проброс в Railway GraphQL от имени рабочего токена офиса:
        # чтение переменных ВСЕХ сервисов (то есть всех секретов) и мутации —
        # правка переменных, редеплой, удаление сервисов.
        #
        # До 23.08.2026 он был доступен без единого секрета. office_auth_middleware
        # спроектирован как поэтапная раскатка: check_office_token возвращает True,
        # пока OFFICE_RPC_TOKEN не выставлен, — а он не выставлен ни на одном
        # сервисе. То есть middleware был no-op, POST /task открыт всему интернету,
        # и через него любой, кто знает адрес, читал секреты воркспейса. Проверено
        # в этот день: сессия Клода без единого токена вытащила все 44 переменные
        # сервиса, включая TELETHON_SESSION и ключи ботов.
        #
        # Поэтапность годится для обычных эндпоинтов, но не для канала в облачный
        # API: привилегия не имеет права наследовать позу «пока пускаем всех».
        # Гейтим тем секретом, который УЖЕ существует и уже защищает /redis и
        # /secrets, — не дожидаясь раскатки OFFICE_RPC_TOKEN.
        if not privileged:
            logger.warning("[railway] отказ: запрос без X-Auth-Token")
            return web.json_response({"status": "error", "responses": [
                "🚫 /railway требует X-Auth-Token. Сырой доступ к Railway GraphQL "
                "не отдаётся по открытому запросу."
            ]}, status=401)
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

    # Дефект 3: задача выполняется В ФОНЕ, ответ отдаём сразу (202 accepted).
    # Раньше весь агентный цикл шёл ДО web.json_response → длинные задачи (>30-40с)
    # рвали вызывающего по таймауту, хотя задача принята и делается. Теперь:
    # ACK + task_id, а финал кладём в Redis office:task:<id> (опрос GET /task/{id})
    # и при silent (source=CLAUDE) шлём финал в личку Владу.
    task_id = _uuid_mod.uuid4().hex[:12]

    async def _run_task_bg():
        result_status = "done"
        try:
            await handle_natural_language(
                f"[{agent}] {text}", int(chat_id) if chat_id else 0, collect,
                silent=silent, repo_override=payload_repo, file_path_override=payload_file,
                # Кто принёс заявку — факт, а не догадка: он решает, чей репо
                # чинить, когда в тексте никто не назван (см. target_repo).
                sender=str(data.get("sender") or agent or ""),
            )
        except Exception as bg_e:
            result_status = "error"
            responses.append(f"❌ Ошибка выполнения: {bg_e}")
            logger.error(f"/task bg {task_id} failed: {bg_e}")
        final = {"status": result_status, "task_id": task_id,
                 "responses": responses, "ts": time.time()}
        try:
            r_cl = await get_redis()
            if r_cl:
                await r_cl.set(f"office:task:{task_id}",
                               json.dumps(final, ensure_ascii=False), ex=3600)
        except Exception as store_e:
            logger.error(f"/task bg {task_id} redis store failed: {store_e}")
        # source=CLAUDE (silent) → в чат ничего не шло; финал адресно в личку Владу.
        if silent and responses:
            tail = "\n".join(str(x) for x in responses)[-3000:]
            await _escalate_vlad(f"✅ Задача Силли завершена ({task_id}):\n{tail}")

    asyncio.create_task(_run_task_bg())
    return web.json_response(
        {"status": "accepted", "task_id": task_id,
         "poll": f"/task/{task_id}"},
        status=202,
    )


async def handle_task_status(request):
    """GET /task/{id} — статус/результат фоновой задачи из Redis office:task:<id>."""
    task_id = request.match_info.get("id", "")
    try:
        r_cl = await get_redis()
        raw = await r_cl.get(f"office:task:{task_id}") if r_cl else None
    except Exception as e:
        return web.json_response({"status": "error", "detail": str(e)}, status=200)
    if not raw:
        # Ещё выполняется (или истёк TTL / неизвестен) — 202, чтобы клиент опрашивал.
        return web.json_response({"status": "pending", "task_id": task_id}, status=202)
    try:
        return web.json_response(json.loads(raw), status=200)
    except Exception:
        return web.json_response({"status": "done", "task_id": task_id, "raw": raw}, status=200)




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
                    headers=office_headers({"X-Secret-Token": HTTP_SECRET_BOTS}),
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


async def handle_logs(request):
    """GET /logs — спросить логи и получить ЦИФРЫ, а не дамп.

    16.08 разбор цикла в dev-отделе упёрся не в сложность, а в то, что спросить
    «как часто Девви получал ту же задачу» было негде: четыре попытки через
    /task вернули сырой список, обрезанный на 3000 символах. Ни счёта, ни
    первой метки, ни последней, ни интервала.

    Параметры (все необязательные):
        bot    — каноническое имя; пусто → все, у кого есть логи за период.
                 Имя берётся из ключей Redis, а не из реестра identity.BOTS:
                 воркеров dev-отдела в реестре нет, а нужны были именно они.
        days   — сколько последних суток (UTC), по умолчанию 1, потолок 30
        event  — фильтр по имени события
        level  — фильтр по уровню (info/warn/error)
        limit  — сколько записей приложить к сводке; 0 → только сводка
        format — text (по умолчанию) или json

    Сводка есть ВСЕГДА, записи — опционально. Ответ на «что происходит» почти
    никогда не требует всех строк, а обрезанный дамп не отвечает ни на что.
    """
    from ai_office_shared.shared.log_query import query as _log_query

    r = await get_redis()
    if not r:
        return web.json_response({"error": "redis unavailable"}, status=503)

    q = request.rel_url.query
    try:
        days  = int(q.get("days", 1))
        limit = int(q.get("limit", 0))
    except ValueError:
        return web.json_response(
            {"error": "days и limit должны быть числами"}, status=400)

    try:
        data = await _log_query(
            r,
            bot=q.get("bot", "").strip().lower(),
            days=days,
            event=q.get("event", "").strip(),
            level=q.get("level", "").strip(),
            limit=max(0, min(limit, 200)),
        )
    except Exception as e:
        logger.error("[/logs] failed: %s", e)
        return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    if q.get("format", "text") == "json":
        return web.json_response(data)
    body = "\n".join(data["lines"]) or "записей нет"
    return web.Response(text=body + "\n", content_type="text/plain",
                        charset="utf-8")


async def handle_redis(request):
    """Proxy Redis commands for Claude diagnostics. Auth required."""
    auth = request.headers.get("X-Auth-Token", "")
    if not auth or auth != RAILWAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
        cmd = body.get("cmd", "")
        args = body.get("args", [])
        if not cmd:
            return web.json_response({"error": "cmd required"})
        r = await get_redis()
        if not r:
            return web.json_response({"error": "redis unavailable"})
        result = await r.execute_command(cmd, *args)
        if isinstance(result, list):
            result = [v.decode() if isinstance(v, bytes) else v for v in result]
        elif isinstance(result, bytes):
            result = result.decode()
        return web.json_response({"result": result})
    except Exception as e:
        return web.json_response({"error": str(e)})


async def handle_web_search(request):
    """Shared web search endpoint for all office bots.
    POST /web_search {"query": "...", "n": 5}
    Auth: X-Auth-Token = Railway token.
    Returns: {"results": [{"title": ..., "url": ..., "snippet": ...}]}
    """
    auth = request.headers.get("X-Auth-Token", "")
    if not auth or auth != RAILWAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
        query = body.get("query", "").strip()
        n = int(body.get("n", 5))
        if not query:
            return web.json_response({"error": "query required"}, status=400)
        from ai_office_shared.shared.web_search import web_search
        results = await web_search(query, n)
        return web.json_response({"results": results, "count": len(results)})
    except Exception as e:
        logger.error(f"[web_search] endpoint error: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def vietnam_cron_loop():
    """Триггерит vietnam-bot каждый день в 01:00 UTC."""
    import datetime
    logger.info("[vietnam_cron] loop started (01:00 UTC daily)")
    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        target = now.replace(hour=1, minute=0, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        wait = (target - now).total_seconds()
        logger.info(f"[vietnam_cron] следующий запуск через {wait/3600:.1f}ч ({target.strftime('%d.%m %H:%M UTC')})")
        await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(
                    "https://vietnam-bot-production.up.railway.app/generate",
                    json={"send": True},
                    headers=office_headers(),
                )
            result = r.json()
            count = result.get("count", 0)
            logger.info(f"[vietnam_cron] ✅ отправлено {count} идей")
        except Exception as e:
            logger.error(f"[vietnam_cron] ❌ ошибка: {e}")
            await notify_office(f"⚠️ Vietnam-bot cron упал: {e}")
        await asyncio.sleep(60)




async def handle_add_lessons(request):
    """Добавить готовые уроки в lessons.json (GitHub) + выложить в Bug Lessons + дедуп.

    Используется Клодом в конце сессии: POST готовых уроков → Силли их персистит и постит.
    Auth: X-Auth-Token = RAILWAY_SECRET.
    Body: {"lessons":[{title,symptom,cause|root_cause,fix,prevention,bot?,layer?,status?,
           why_architecture?,context?}], "post":true, "dry_run":false}
    Идемпотентно по title (дубли по названию пропускаются)."""
    auth = request.headers.get("X-Auth-Token", "")
    if not auth or auth != RAILWAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
        incoming = body.get("lessons", [])
        do_post = body.get("post", True)
        dry_run = body.get("dry_run", False)
        if not isinstance(incoming, list) or not incoming:
            return web.json_response({"error": "lessons (non-empty list) required"}, status=400)
        import datetime as _dt
        raw = await read_file("ai-office-shared", LESSONS_FILE)
        lessons = json.loads(raw)
        existing_titles = {str(l.get("title", "")).strip().lower() for l in lessons}
        next_id = max((l.get("id", 0) for l in lessons), default=0) + 1
        added = []
        for item in incoming:
            title = str(item.get("title", "")).strip()
            if not title or title.lower() in existing_titles:
                continue
            entry = {
                "id": next_id,
                "date": item.get("date") or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"),
                "bot": item.get("bot", "cilly"),
                "layer": item.get("layer", "system"),
                "title": title,
                "symptom": item.get("symptom", ""),
                "root_cause": item.get("root_cause") or item.get("cause", ""),
                "fix": item.get("fix", ""),
                "prevention": item.get("prevention", ""),
                "why_architecture": item.get("why_architecture", ""),
                "cause": item.get("cause") or item.get("root_cause", ""),
                "status": item.get("status", "fixed"),
                "tag": item.get("tag", "system"),
            }
            if item.get("context"):
                entry["context"] = item["context"]
            lessons.append(entry)
            existing_titles.add(title.lower())
            added.append(entry)
            next_id += 1
        if not added:
            return web.json_response({"added": [], "note": "все уроки уже есть (дедуп по title)"})
        if dry_run:
            return web.json_response({"dry_run": True, "would_add": [l["id"] for l in added],
                                      "titles": [l["title"] for l in added]})
        # 1) персист в GitHub одним коммитом
        await push_file("ai-office-shared", LESSONS_FILE,
                        json.dumps(lessons, ensure_ascii=False, indent=2),
                        f"lessons: +{len(added)} (#{added[0]['id']}-#{added[-1]['id']})")
        # 2) опубликовать новые уроки через единый durable-механизм.
        #    Добавленные на шаге 1 записи ещё без posted_to_group → publish их подхватит,
        #    пометит и закоммитит. Без ручного цикла и без Redis-дедупа (single source of truth).
        posted = await publish_pending_lessons() if do_post else 0
        return web.json_response({"added": [l["id"] for l in added], "posted": posted})
    except Exception as e:
        logger.error(f"handle_add_lessons: {e}")
        return web.json_response({"error": str(e)}, status=500)


def _age_seconds(iso_ts: str) -> float:
    """Возраст ISO8601-таймстампа (UTC, формат taskboard) в секундах. 0 при ошибке."""
    from datetime import datetime, timezone
    try:
        dt = datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return 0.0


async def _review_quality_and_routing(r):
    """B3: проактивно заводит задачи по падению качества и роутинг-промахам."""
    from ai_office_shared.shared.identity import BOTS, display, redis_key

    open_tasks = await tb.list_tasks(r, status="open", limit=200)
    open_titles = {t.get("title", "") for t in open_tasks}

    # 1. Качество: много 👎 относительно 👍
    for canon in BOTS:
        try:
            h = await r.hgetall(redis_key(canon, "quality"))
        except Exception:
            continue
        if not h:
            continue
        up = int(h.get("up", 0) or 0)
        down = int(h.get("down", 0) or 0)
        if down >= 5 and down > up:
            title = f"Качество {display(canon)}: {up}👍/{down}👎 — разобраться"
            if title not in open_titles:
                await tb.create_task(r, title, created_by="силли", assignee=canon, status="open")
                await notify_office(f"📉 {title}. Завёл задачу на доске.")

    # 2. Роутинг-промахи: частые промахи на агента
    try:
        raw = await r.lrange("office:routing:misses", 0, 99)
    except Exception:
        raw = []
    counts: dict = {}
    for item in raw:
        try:
            d = json.loads(item)
            agent = d.get("agent", "?")
            counts[agent] = counts.get(agent, 0) + 1
        except Exception:
            continue
    for agent, n in counts.items():
        if n >= 5:
            title = f"Роутинг-промахи {agent}: {n} за окно — проверить доступность/маршрут"
            if title not in open_titles:
                await tb.create_task(r, title, created_by="силли",
                                     assignee=str(agent).lower(), status="open")
                await notify_office(f"🧭 {title}. Завёл задачу на доске.")


# ── Входящая дверь отдела разработки ─────────────────────────────────────────
# Заявка от бота ([DEV_FEATURE:…] → request_dev_feature) ложилась на доску со
# status="open" и лежала: run_dev_pipeline звали только автофикс краша и интент
# dev_task по явной просьбе человека, а management_tick смотрел на уже взятое в
# работу. Отдел был, входящей двери не было — и «🛠 Запрос на доработку» в чате
# оставался уведомлением, а не задачей.
#
# Дверь устроена так: очередь спрашивает владельца ОДИН раз кнопками, и работу
# запускает его ответ. Решение остаётся за ним, исполнение уходит из его рук.

CI_POLL_SEC     = int(os.getenv("CILLY_CI_POLL_SEC", "20"))    # период опроса check-runs
CI_WAIT_SEC     = int(os.getenv("CILLY_CI_WAIT_SEC", "600"))   # потолок ожидания CI
MAX_CI_ROUNDS   = int(os.getenv("CILLY_CI_ROUNDS", "2"))       # заходов «красный CI → переделать»


def _main_file_for_repo(repo: str) -> str:
    """Главный файл репозитория по SERVICES. Неизвестный репо → bot.py."""
    for _repo, _main in SERVICES.values():
        if _repo == repo:
            return _main
    return "bot.py"


async def _wait_for_ci(repo: str, ref: str) -> tuple:
    """
    Дождаться вердикта CI по ветке. Возвращает (состояние, упавшие_джобы, ссылка).

    Состояние "empty" (ни одного прогона за всё время ожидания) НЕ превращается
    в "success". Проверка, которая не смогла выполниться, — провал, а не
    пропуск: иначе репозиторий без workflow автоматически выдавал бы зелёный
    свет любому коду, и приёмка снова стала бы словом без содержания.
    """
    deadline = time.time() + CI_WAIT_SEC
    state, failed, url = "empty", [], ""
    while time.time() < deadline:
        state, failed, url = await get_checks_status(repo, ref)
        if state in ("success", "failure"):
            return state, failed, url
        await asyncio.sleep(CI_POLL_SEC)
    return state, failed, url


async def _run_queued_dev_task(payload: dict, board_id: str) -> None:
    """
    Заявка из очереди — от подтверждения владельца до зелёного CI.

    Живёт в фоне: цепочка отдела идёт минутами, а держать в ней Telegram-callback
    нельзя. Обо всех исходах докладывает сама — молчаливый фон неотличим от
    зависшего.
    """
    repo      = payload.get("repo", "")
    file_path = payload.get("file_path") or "bot.py"
    task_text = payload.get("task_text", "")
    r = await get_redis()

    refusal = dev_queue.self_edit_refusal(repo, file_path)
    if refusal:
        await tb.update_status(r, board_id, "blocked", result=refusal, escalated=True)
        await _escalate_vlad(refusal)
        return

    # Слот один на весь офис: две одобренные заявки не должны идти параллельно.
    # Если слот занят, заявку НЕ теряем и не запускаем следом — возвращаем в
    # очередь. Тихо проглотить одобрение владельца было бы худшим исходом:
    # он нажал кнопку, а не произошло ничего.
    if not await dev_queue.acquire_run_slot(r, board_id):
        busy = await dev_queue.current_run(r)
        await tb.update_status(r, board_id, "open",
                               result=f"жду очереди: сейчас идёт заявка {busy or '—'}")
        await dev_queue.release_claim(r, board_id)
        await dev_queue.clear_asked(r, board_id)
        await _escalate_vlad(
            f"⏳ Заявка [{board_id}] вернулась в очередь: сейчас отдел занят "
            f"заявкой {busy or '—'}. Спрошу снова, когда освободится."
        )
        return

    branch  = f"cilly/dev-{board_id}"
    pr_url  = ""
    pr_num  = 0
    ci_feedback = ""

    try:
        await tb.update_status(r, board_id, "in_progress")

        for ci_round in range(1, MAX_CI_ROUNDS + 1):
            # Файл читаем ЗАНОВО на каждом заходе и ИЗ РАБОЧЕЙ ВЕТКИ, как только
            # она появилась: после первого пуша правка живёт там, и переделка
            # должна идти от неё. Читая main, второй заход правил бы текст,
            # который уже никто не правит, а гейт размера сравнивал бы с ним.
            try:
                context = await read_file(repo, file_path,
                                          ref=branch if pr_num else "")
            except Exception as e:
                await tb.update_status(r, board_id, "blocked",
                                       result=f"не смог прочитать {repo}/{file_path}: {e}",
                                       escalated=True)
                await _escalate_vlad(f"⛔ Заявка [{board_id}]: не читается {repo}/{file_path} — {e}")
                return

            chain = await run_dev_chain(
                task_text, repo=repo, file_path=file_path, context=context,
                board_id=board_id, task_id=board_id, redis_client=r,
                stage_label="заявка попытка", seed_feedback=ci_feedback,
            )
            if not chain["ok"]:
                _fresh = await tb.get_task(r, board_id)
                await tb.update_status(r, board_id, "blocked",
                                       result=chain["reason"] or "рабочий код не получен",
                                       escalated=True)
                if chain.get("infra_error"):
                    # Отдел даже не начал: винить его — значит послать владельца
                    # искать баг там, где его нет.
                    await _escalate_vlad(
                        f"⚙️ Заявка [{board_id}] ({repo}/{file_path}) остановлена: "
                        f"{chain['infra_error']}.\n"
                        f"Это не код и не команда — отдел не смог обратиться к модели. "
                        f"Восстанови доступ и верни задачу в работу.\n"
                        f"{chain['reason'][:300]}"
                    )
                else:
                    await _escalate_vlad(
                        f"⛔ Заявка [{board_id}] ({repo}/{file_path}): команда не дала код, "
                        f"прошедший гейт.\nПричина: {chain['reason'][:200]}\n"
                        + (tb.format_evidence_report(_fresh) if _fresh else "")
                    )
                return

            # ── Ветка + PR, а не пуш в main ────────────────────────────────
            base = await get_default_branch(repo)
            await create_branch(repo, branch, base)
            await push_file_to_branch(
                repo, file_path, chain["final_code"],
                chain["commit_msg"] or f"feat: {task_text[:60]}", branch,
            )
            if not pr_num:
                pr = await create_pull_request(
                    repo,
                    title=(chain["commit_msg"] or f"feat: {task_text[:60]}")[:80],
                    body=(f"Заявка с доски офиса: `{board_id}`\n\n{task_text}\n\n"
                          f"Собрано отделом разработки (Девви → Рикки‖Тести‖Секки → Скрибби). "
                          f"Гейт: compile + ревью без NEEDS_FIX + размер файла. "
                          f"Мёрдж — только по зелёному CI."),
                    head_branch=branch, base_branch=base,
                )
                pr_num, pr_url = pr["number"], pr["html_url"]
                await _escalate_vlad(
                    f"🔧 Заявка [{board_id}]: код готов, PR открыт — {pr_url}\nЖду CI."
                )

            # ── Единственная проверка, которая исполняет код ────────────────
            state, failed, checks_url = await _wait_for_ci(repo, branch)
            if state == "success":
                await tb.add_evidence(
                    r, board_id, dev_queue.CI_ACCEPTANCE, passed=True,
                    proof=f"CI зелёный: {checks_url or pr_url}",
                    checked_by=tb.VERIFIER_GATE,
                )
                break

            proof = ({
                "failure": f"упали джобы: {', '.join(failed)[:200]}",
                "empty":   "в репозитории не нашлось ни одного прогона CI",
            }.get(state) or f"CI не дал вердикта за {CI_WAIT_SEC} с (состояние {state})")
            await tb.add_evidence(r, board_id, dev_queue.CI_ACCEPTANCE,
                                  passed=False, proof=f"{proof} — {checks_url or pr_url}",
                                  checked_by=tb.VERIFIER_GATE)
            await tb.incr_attempts(r, board_id)

            _fresh = await tb.get_task(r, board_id)
            if ci_round >= MAX_CI_ROUNDS or (_fresh and tb.should_escalate(_fresh)):
                await tb.update_status(r, board_id, "blocked",
                                       result=f"CI не зелёный: {proof}", escalated=True)
                await _escalate_vlad(
                    f"⛔ Заявка [{board_id}]: CI не зелёный после {ci_round} захода(ов).\n"
                    f"{proof}\nPR оставила открытым: {pr_url}\nНужен твой разбор."
                )
                return
            # Красный CI — это NEEDS_FIX, а не «готово». Отдаём команде ИМЕНА
            # упавших джоб: ретрай, которому не сказали, что именно сломалось,
            # не сходится, а просто тратит три попытки на подтверждение этого.
            ci_feedback = (f"CI целевого репозитория не прошёл: {proof}. "
                           f"Почини именно это и верни ПОЛНЫЙ файл целиком.")
            await tb.update_status(r, board_id, "needs_fix", result=ci_feedback)

        # ── Зелёный ────────────────────────────────────────────────────────
        if dev_queue.automerge_enabled():
            merged = await merge_pull_request(repo, pr_num,
                                              f"feat: заявка {board_id}")
            if merged:
                await close_task_reported(r, board_id, f"смержено в {repo}: {pr_url}")
                await _escalate_vlad(
                    f"✅ Заявка [{board_id}] закрыта: CI зелёный, PR смержен — {pr_url}"
                )
            else:
                await tb.update_status(r, board_id, "blocked",
                                       result=f"PR зелёный, но не мержится: {pr_url}",
                                       escalated=True)
                await _escalate_vlad(f"⚠️ Заявка [{board_id}]: PR зелёный, но GitHub "
                                     f"отказал в мёрдже — {pr_url}")
        else:
            # Раскатка: сначала отдел доводит до зелёного PR и останавливается.
            # Автомёрдж включается флагом после трёх подряд зелёных прогонов.
            await tb.update_status(r, board_id, "awaiting_approval",
                                   result=f"зелёный PR готов: {pr_url}")
            await _escalate_vlad(
                f"✅ Заявка [{board_id}]: CI зелёный, PR готов к мёрджу — {pr_url}\n"
                f"(автомёрдж выключен: CILLY_DEV_QUEUE_AUTOMERGE=0)"
            )
    except Exception as e:
        logger.error(f"[dev_queue] заявка {board_id} упала: {e}")
        await tb.update_status(r, board_id, "blocked",
                               result=f"ошибка исполнения: {e}", escalated=True)
        await _escalate_vlad(f"⛔ Заявка [{board_id}] оборвалась ошибкой: {e}")
    finally:
        await dev_queue.release_run_slot(r, board_id)
        await dev_queue.release_claim(r, board_id)
        await dev_queue.clear_asked(r, board_id)


async def _dev_queue_tick(r) -> None:
    """
    Один заход по входящим заявкам dev-dept: взять ОДНУ и спросить владельца.

    Одну за тик намеренно: параллельные цепочки пишут те же файлы, а замка на
    доске нет вовсе (list_tasks + update_status — классический двойной захват).
    Тик только СПРАШИВАЕТ; работу запускает ответ владельца, поэтому сам тик
    остаётся быстрым и не задерживает остальную петлю управления.
    """
    if not dev_queue.queue_enabled():
        return
    # Пока отдел занят, новый вопрос задавать незачем: одобрение по нему всё
    # равно упрётся в слот и вернёт заявку в очередь. Лучше не спрашивать, чем
    # спросить и отказать.
    busy = await dev_queue.current_run(r)
    if busy:
        logger.info(f"[dev_queue] отдел занят заявкой {busy}, вопросов не задаю")
        return
    try:
        tasks = await tb.list_tasks(r, status="open",
                                    assignee=dev_queue.DEV_DEPT_ASSIGNEE,
                                    parent_id="", limit=100)
    except Exception as e:
        logger.warning(f"[dev_queue] доска не читается: {e}")
        return
    if not tasks:
        return

    # Потолок раундов раньше замка: задача, исчерпавшая попытки, должна уйти к
    # человеку, а не крутиться в очереди ещё сутки.
    for t in tasks:
        if tb.should_escalate(t):
            await tb.escalate(r, t.get("id"))
            await notify_office(
                f"🚨 Заявка [{t.get('id')}] упёрлась в потолок попыток "
                f"({t.get('attempts')}/{tb.MAX_ROUNDS}): {t.get('title','')[:80]}"
            )

    blocked = await dev_queue.blocked_ids(r, tasks)
    task = dev_queue.pick_next(tasks, blocked=blocked)
    if not task:
        return
    tid   = task.get("id", "")
    title = task.get("title", "")

    if not await dev_queue.claim_task(r, tid):
        return

    repo, why = dev_queue.resolve_target(task)
    if not repo:
        await tb.update_status(r, tid, "blocked",
                               result=f"не определился репозиторий ({why})", escalated=True)
        await dev_queue.release_claim(r, tid)
        await _escalate_vlad(
            f"❓ Заявка [{tid}]: не понял, в каком репозитории чинить ({why}).\n"
            f"«{title[:200]}»\nНазови репозиторий — и я передам отделу."
        )
        return

    file_path = _main_file_for_repo(repo)
    refusal = dev_queue.self_edit_refusal(repo, file_path)
    if refusal:
        await tb.update_status(r, tid, "rejected", result=refusal)
        await dev_queue.release_claim(r, tid)
        await _escalate_vlad(f"🚫 Заявка [{tid}] ведёт в мой собственный код.\n"
                             f"«{title[:200]}»\n{refusal}")
        return

    # Критерии замораживаются ДО работы — set_acceptance откажет, как только
    # статус уедет с open или появятся попытки. Успех, определённый после
    # результата, подгоняется под результат.
    #
    # Отказ проглатывать нельзя: задача БЕЗ критериев закрывается по
    # update_status("done") без единой улики — ровно та дыра, ради которой
    # приёмка и писалась. Пускаем в работу только если критерии есть: свои,
    # только что записанные, или замороженные раньше.
    ok_acc, why_acc = await tb.set_acceptance(r, tid, dev_queue.acceptance_for())
    if not ok_acc:
        _cur = await tb.get_task(r, tid)
        if not (_cur or {}).get("acceptance"):
            await tb.update_status(r, tid, "blocked",
                                   result=f"критерии приёмки не заморожены: {why_acc}",
                                   escalated=True)
            await dev_queue.release_claim(r, tid)
            await _escalate_vlad(
                f"⛔ Заявка [{tid}] не пошла в работу: не удалось заморозить критерии "
                f"приёмки ({why_acc}). Без них «готово» — это ничьё слово, "
                f"и задача закрылась бы без улик."
            )
            return
        logger.info(f"[dev_queue] критерии уже были заморожены для {tid}: {why_acc}")

    # Команде уходит САМО описание, без служебной приписки «[бот] доработка по
    # запросу X:» — она про маршрут заявки, а не про то, что надо написать.
    spec = dev_queue.request_text(title)
    action_id = await stage_pending("dev_queue_run", {
        "repo": repo, "file_path": file_path, "task_text": spec,
    }, task_id=tid, title=f"заявка {repo}/{file_path}")
    await dev_queue.mark_asked(r, tid)
    asked = await send_proposal(
        f"🛠 Заявка от отдела: {title[:300]}\n\n"
        f"📦 Цель: {repo}/{file_path} ({why})\n"
        f"Команда напишет код, откроет PR и дождётся CI. Делать?\n"
        f"(или текстом: /approve {action_id})",
        "pg", action_id,
        chat_id=int(os.getenv("YOUR_TELEGRAM_ID", "391077101")),
        defer=True,
    )
    if not asked:
        # Вопрос не ушёл (пауза исходящих, сбой Telegram) — снимаем отметки,
        # иначе заявка молча простоит сутки как «уже спрошенная», хотя её никто
        # не видел. Незаданный вопрос не должен выглядеть как заданный.
        await dev_queue.clear_asked(r, tid)
        await dev_queue.release_claim(r, tid)
        logger.warning(f"[dev_queue] вопрос по заявке {tid} не отправился — верну в очередь")


async def _management_tick():
    """Один проход проактивного управления: доска + метрики."""
    r = await get_redis()
    if not r:
        return
    # Доска: подвисшие и заблокированные верхнеуровневые задачи
    active = await tb.list_tasks(
        r, status={"in_progress", "needs_fix", "blocked"}, parent_id="", limit=100,
    )
    for t in active:
        tid = t.get("id")
        status = t.get("status")
        age = _age_seconds(t.get("updated_at", ""))
        # Потолок раундов. До этого эскалация была только по ВРЕМЕНИ (задача
        # висит >2ч). Задача, которая бодро крутит попытку за попыткой, под это
        # не попадала: updated_at обновляется на каждом заходе, значит она
        # «свежая» и может переделываться бесконечно. Считаем не время, а
        # попытки.
        if tb.should_escalate(t):
            await tb.escalate(r, tid)
            _fresh = await tb.get_task(r, tid)
            await notify_office(
                f"🚨 Задача [{tid}] упёрлась в потолок попыток "
                f"({t.get('attempts')}/{tb.MAX_ROUNDS}): {t.get('title','')[:80]}\n"
                + (tb.format_evidence_report(_fresh) if _fresh else "")
                + "\nДальше сама не продвину, нужен твой разбор."
            )
            continue

        if status == "blocked" and not t.get("escalated"):
            await tb.update_status(r, tid, "blocked", escalated=True)
            await notify_office(
                f"🚨 Задача [{tid}] заблокирована: {t.get('title','')[:80]}\n"
                f"Причина: {t.get('result','')[:200]}\nНужен твой разбор, шеф."
            )
        elif status in ("in_progress", "needs_fix") and age > MGMT_STUCK_AFTER_SEC \
                and not t.get("escalated"):
            # нудж один раз (помечаем escalated, чтобы не спамить)
            await tb.update_status(r, tid, status, escalated=True)
            await notify_office(
                f"⏳ Задача [{tid}] висит ~{int(age // 3600)}ч в статусе {status}: "
                f"{t.get('title','')[:80]}"
            )
    # Входящая дверь отдела: одна open-заявка dev-dept за тик уходит владельцу
    # вопросом с кнопками. СТРОГО до _review_quality_and_routing — та сама
    # создаёт open-задачи, и очередь после неё хватала бы созданное в этом же
    # тике, не дав никому на них даже взглянуть.
    try:
        await _dev_queue_tick(r)
    except Exception as e:
        logger.error(f"[dev_queue] тик очереди упал: {e}")

    # B3: метрики качества и роутинга → проактивные задачи
    await _review_quality_and_routing(r)


async def management_loop():
    """
    Проактивная петля управления (A4 + B3). В отличие от monitor_loop (реактивно
    чинит баги), эта ревьюит доску задач и метрики и сама инициирует работу.
    Все РИСК-действия всё равно идут через approval-гейт (/approve) — петля только
    нуджит, эскалирует и заводит задачи на доске.
    """
    await asyncio.sleep(60)  # дать боту стартовать
    logger.info("[management] started")
    while True:
        if MONITOR_PAUSED():
            logger.info("[management] paused via CILLY_MONITOR_PAUSED, sleeping...")
            await asyncio.sleep(60)
            continue
        try:
            await _management_tick()
        except Exception as e:
            logger.error(f"[management] tick error: {e}")
        await asyncio.sleep(MANAGEMENT_INTERVAL)


async def main():
    # Загружаем office:decisions из Redis при старте
    await init_office_decisions()
    asyncio.create_task(monitor_loop())
    asyncio.create_task(daily_audit_loop())
    asyncio.create_task(vietnam_cron_loop())
    asyncio.create_task(management_loop())
    asyncio.create_task(publish_pending_on_startup())  # дозалить pending-уроки (без удалений)
    asyncio.create_task(_lessons_en_migration_once())   # одноразовый перепост уроков на английском
    # HTTP server for Filly routing (office RPC auth via middleware — Layer 1/2)
    app = web.Application(middlewares=[office_auth_middleware])
    app.router.add_post("/task", handle_cilly_task)
    app.router.add_get("/task/{id}", handle_task_status)   # Дефект 3: опрос статуса фоновой задачи
    app.router.add_get("/secrets", handle_secrets)
    app.router.add_post("/post_raw", handle_post_raw)
    app.router.add_post("/promote_bots", handle_promote_bots)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/envcheck", handle_envcheck)
    app.router.add_get("/logs", handle_logs)   # сводка по office:logs, см. handle_logs
    app.router.add_post("/redis", handle_redis)
    app.router.add_post("/add_lessons", handle_add_lessons)
    app.router.add_post("/web_search", handle_web_search)
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

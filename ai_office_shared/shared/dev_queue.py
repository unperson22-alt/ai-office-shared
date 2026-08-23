"""
ai_office_shared.shared.dev_queue — входящая дверь отдела разработки.

ПРОБЛЕМА (записана как OPEN 21.08.2026):
    Бот, упёршийся в отсутствующую возможность, эмитит [DEV_FEATURE:...],
    dev_escalation.request_dev_feature() кладёт задачу на доску
    (assignee="dev-dept", status="open") — и на этом всё заканчивается.
    run_dev_pipeline() зовётся только из автофикса краша и из intent'а
    dev_task, когда человек ЯВНО просит отдать команде; management_loop
    смотрит на {in_progress, needs_fix, blocked}, то есть на уже взятое в
    работу. Статус "open" читался ровно в одном месте и только чтобы
    дедуплицировать заголовки.

    Итог: отдел есть, входящей двери нет. Заявка, которую некому взять, —
    это уведомление, а не задача.

ЧТО ЗДЕСЬ:
    Политика очереди — отбор кандидата, замок на захват, отложить/спросить,
    критерии приёмки и запрет самоправки. Всё, что должно быть покрыто
    тестами, лежит здесь, а не в agents/coder.py: тот файл невозможно
    импортировать в тест (на уровне модуля читается os.environ и поднимается
    aiogram) и он исключён из линта CI. coder.py остаётся тонкой обвязкой:
    Telegram, Redis, GitHub.

КОНТРАКТ: как весь taskboard — ни одна функция не пробрасывает исключение
наружу. Сорванная очередь не должна ронять management_loop.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

from .target_repo import repo_looks_valid, target_repo

logger = logging.getLogger("ai_office_shared.dev_queue")

DEV_DEPT_ASSIGNEE = "dev-dept"

# ── Ключи Redis ───────────────────────────────────────────────────────────────
CLAIM_PREFIX = "office:devqueue:claim"
SNOOZE_PREFIX = "office:devqueue:snooze"
ASKED_PREFIX = "office:devqueue:asked"

CLAIM_TTL_SEC = 6 * 3600        # замок на время работы пайплайна
SNOOZE_TTL_SEC = 24 * 3600      # «Отложить» — сутки
ASKED_TTL_SEC = 24 * 3600       # столько же живёт pending-действие в coder.py


def _flag(name: str, default: str) -> bool:
    return str(os.getenv(name, default)).strip().lower() in ("1", "true", "yes", "on")


def queue_enabled() -> bool:
    """Рубильник очереди целиком. Выключенная очередь ведёт себя как до неё."""
    return _flag("CILLY_DEV_QUEUE", "1")


def automerge_enabled() -> bool:
    """
    Мержить ли зелёный PR самой.

    По умолчанию ВЫКЛЮЧЕНО. Раскатка: сначала отдел доводит заявки до зелёного
    PR и останавливается, и только после трёх подряд зелёных прогонов флаг
    поднимается. Три — потому что на меньшем числе нет статистики, а прошлые
    провалы отдела (уроки #70, #80, #81, #82, #100) чинили быстрее, чем
    измеряли.
    """
    return _flag("CILLY_DEV_QUEUE_AUTOMERGE", "0")


# ── Приёмка ───────────────────────────────────────────────────────────────────
# Строки менять НЕЛЬЗЯ: по ним ищутся улики уже заведённых задач
# (taskboard.add_evidence сопоставляет критерий с уликой по тексту).
GATE_ACCEPTANCE = (
    "финальный код компилируется",
    "ревью Рикки без вердикта NEEDS_FIX",
    "файл не схлопнулся (гейт размера)",
)

# Четвёртый критерий — единственный, который меряет РАБОТАЮЩИЙ код, а не текст.
# Первые три статические: compile(), поиск подстроки и счёт строк. Ровно они
# 01.07.2026 пропустили 8-строчную заглушку вместо файла на 5766 строк.
CI_ACCEPTANCE = "CI целевого репо зелёный"


def acceptance_for(*, with_ci: bool = True) -> list:
    """Критерии приёмки, замораживаемые ДО старта работы."""
    out = list(GATE_ACCEPTANCE)
    if with_ci:
        out.append(CI_ACCEPTANCE)
    return out


# ── Запрет самоправки ─────────────────────────────────────────────────────────
# Правило #70 живёт в safe_autonomous_push и потому закрывает push_code,
# fix_bot и agentic_task. Ветка deploy_devtask зовёт push_file напрямую и
# репозиторий не проверяет вовсе — то есть путь, которым офис лёг 01.07.2026,
# формально открыт до сих пор. Запрет должен быть проверяемой функцией, а не
# строчкой в промте: в CODER_PROMPT её и нет, там файл наоборот назван своим.
SELF_EDIT_REPOS = ("ai-office-shared", "ai_office_shared")


def self_edit_refusal(repo: str, path: str = "") -> str:
    """Текст отказа, если правка ведёт в код самой Силли. Иначе ''."""
    name = (repo or "").strip()
    if name in SELF_EDIT_REPOS:
        return (f"🚫 {name}/{path or '*'} — код Силли. Правится ТОЛЬКО вручную: "
                f"ветка → PR → ревью Влада (правило #70, инцидент 01.07.2026). "
                f"Автоматом не трогаю.")
    return ""


# ── Замки и отметки ───────────────────────────────────────────────────────────
# У доски нет ни одного claim-примитива: list_tasks + update_status — классика
# двойного захвата, все мутации last-writer-wins. Идиома SET NX EX взята из
# dedup.claim_answer, но fail-closed, а не fail-open: там цена ошибки — второй
# ответ в чат, здесь — вторая команда, пишущая тот же файл.

def claim_key(task_id: str) -> str:
    return f"{CLAIM_PREFIX}:{task_id}"


def snooze_key(task_id: str) -> str:
    return f"{SNOOZE_PREFIX}:{task_id}"


def asked_key(task_id: str) -> str:
    return f"{ASKED_PREFIX}:{task_id}"


async def claim_task(redis_client, task_id: str, ttl: int = CLAIM_TTL_SEC) -> bool:
    """
    Занять задачу. True — она наша, False — уже занята (или Redis недоступен).

    Fail-CLOSED осознанно: без замка две параллельные ветки напишут один и тот
    же файл разным кодом, и кто победит — решит порядок пушей.
    """
    if redis_client is None or not task_id:
        return False
    try:
        got = await redis_client.set(claim_key(task_id), "1", nx=True, ex=ttl)
        return bool(got)
    except Exception as e:
        logger.warning("[dev_queue] замок недоступен, задачу не беру: %s", e)
        return False


async def release_claim(redis_client, task_id: str) -> None:
    """Снять замок — работа кончилась раньше TTL. Fail-silent."""
    if redis_client is None or not task_id:
        return
    try:
        await redis_client.delete(claim_key(task_id))
    except Exception as e:
        logger.warning("[dev_queue] замок не снялся (%s): %s", task_id, e)


async def snooze(redis_client, task_id: str, ttl: int = SNOOZE_TTL_SEC) -> bool:
    """Отложить задачу: она остаётся open, но не предлагается до истечения TTL."""
    if redis_client is None or not task_id:
        return False
    try:
        await redis_client.set(snooze_key(task_id), "1", ex=ttl)
        return True
    except Exception as e:
        logger.warning("[dev_queue] отложить не вышло (%s): %s", task_id, e)
        return False


async def mark_asked(redis_client, task_id: str, ttl: int = ASKED_TTL_SEC) -> bool:
    """
    Отметить, что вопрос владельцу задан.

    TTL совпадает с временем жизни pending-действия: истёк вопрос — истекла и
    отметка, и очередь спросит заново. Поэтому задача не залипает навсегда,
    если владелец просто не ответил.
    """
    if redis_client is None or not task_id:
        return False
    try:
        await redis_client.set(asked_key(task_id), "1", ex=ttl)
        return True
    except Exception as e:
        logger.warning("[dev_queue] отметка вопроса не записалась (%s): %s", task_id, e)
        return False


async def clear_asked(redis_client, task_id: str) -> None:
    """Снять отметку вопроса (ответ получен). Fail-silent."""
    if redis_client is None or not task_id:
        return
    try:
        await redis_client.delete(asked_key(task_id))
    except Exception as e:
        logger.warning("[dev_queue] отметка вопроса не снялась (%s): %s", task_id, e)


async def blocked_ids(redis_client, tasks: list) -> set:
    """
    id задач, которые сейчас трогать нельзя: отложены, уже спрошены или заняты.

    Один проход по кандидатам вместо трёх запросов на задачу. При недоступном
    Redis возвращаем ВСЕ id — ничего не берём, это безопасная сторона.
    """
    ids = {t.get("id") for t in (tasks or []) if t.get("id")}
    if redis_client is None:
        return ids
    out: set = set()
    for tid in ids:
        try:
            for key in (snooze_key(tid), asked_key(tid), claim_key(tid)):
                if await redis_client.exists(key):
                    out.add(tid)
                    break
        except Exception as e:
            logger.warning("[dev_queue] не прочитал отметки (%s): %s", tid, e)
            out.add(tid)
    return out


# ── Один прогон разом ─────────────────────────────────────────────────────────
# «Одна задача за тик» ограничивает ВОПРОСЫ, а не запуски: одобрив две заявки
# подряд, владелец запустил бы две цепочки одновременно. На разных репозиториях
# это безвредно, на одном файле — два PR с разошедшимися базами и конфликт
# мёрджа. Слот один на весь офис, и его TTL заметно больше самого долгого
# прогона: если процесс умрёт, слот освободится сам, а не заклинит очередь.

RUN_SLOT_KEY = "office:devqueue:running"
RUN_SLOT_TTL_SEC = 2 * 3600


async def acquire_run_slot(redis_client, task_id: str,
                           ttl: int = RUN_SLOT_TTL_SEC) -> bool:
    """Занять единственный слот исполнения. False — уже занят или нет Redis."""
    if redis_client is None or not task_id:
        return False
    try:
        got = await redis_client.set(RUN_SLOT_KEY, task_id, nx=True, ex=ttl)
        return bool(got)
    except Exception as e:
        logger.warning("[dev_queue] слот прогона недоступен, не запускаю: %s", e)
        return False


async def current_run(redis_client) -> str:
    """id задачи, занявшей слот. '' — слот свободен или Redis недоступен."""
    if redis_client is None:
        return ""
    try:
        val = await redis_client.get(RUN_SLOT_KEY)
    except Exception as e:
        logger.warning("[dev_queue] слот прогона не читается: %s", e)
        return ""
    if val is None:
        return ""
    return val.decode() if isinstance(val, (bytes, bytearray)) else str(val)


async def release_run_slot(redis_client, task_id: str) -> None:
    """
    Освободить слот — только если он наш.

    Проверка владельца не формальность: без неё задача, доработавшая уже после
    истечения своего TTL, снесла бы слот у той, что идёт сейчас.
    """
    if redis_client is None or not task_id:
        return
    try:
        if await current_run(redis_client) == task_id:
            await redis_client.delete(RUN_SLOT_KEY)
    except Exception as e:
        logger.warning("[dev_queue] слот прогона не освободился (%s): %s", task_id, e)


# ── Отбор кандидата ───────────────────────────────────────────────────────────

def pick_next(tasks: list, *, blocked: Optional[set] = None) -> Optional[dict]:
    """
    Самая старая заявка, которую можно взять. None — брать нечего.

    Чистая функция: всё состояние приходит аргументами, поэтому правило отбора
    проверяется тестом, а не наблюдением за продом.

    Отбрасываем:
      • подзадачи (parent_id) — их ведёт родитель, самостоятельной работы нет;
      • уже отложенные/спрошенные/занятые (blocked);
      • всё, что не open и не на dev-dept.
    """
    blocked = blocked or set()
    ready = []
    for t in (tasks or []):
        if not isinstance(t, dict):
            continue
        if t.get("parent_id"):
            continue
        if t.get("status") != "open":
            continue
        if (t.get("assignee") or "") != DEV_DEPT_ASSIGNEE:
            continue
        if t.get("id") in blocked:
            continue
        ready.append(t)
    if not ready:
        return None
    # created_at — ISO-строка фиксированной ширины, лексикографический порядок
    # совпадает с хронологическим.
    ready.sort(key=lambda t: (t.get("created_at") or "", t.get("id") or ""))
    return ready[0]


# ── Куда чинить ───────────────────────────────────────────────────────────────

# Заголовок заявки собирает dev_escalation.request_dev_feature:
#     "[крисс] доработка по запросу Яны: <описание>"
# Имя бота в начале — служебная приписка «кто принёс», а не часть просьбы.
_TITLE_PREFIX = re.compile(r"^\s*\[[^\]]+\][^:]*:\s*")


def request_text(title: str) -> str:
    """
    Само описание доработки, без служебной приписки «[бот] доработка по запросу X:».

    Разделять обязательно: иначе имя принёсшего бота ВСЕГДА присутствует в
    тексте, и заявка «у Билли не сохраняются задачи», принесённая Криссом,
    выглядит как названные два бота — а значит цель выбиралась бы наугад.
    """
    text = (title or "").strip()
    stripped = _TITLE_PREFIX.sub("", text, count=1).strip()
    return stripped or text


def resolve_target(task: dict) -> tuple:
    """
    (репозиторий, обоснование). Репозиторий None — цель не определилась.

    Переиспользуем target_repo(): в ОПИСАНИИ назван бот → его репо; иначе заявку
    принёс бот (created_by) → его репо. Догадку модели сюда не пускаем вовсе:
    у заявки от бота оба детерминированных источника всегда есть, а угадывание
    15.08.2026 увело правку в billy-bot, где искомых кнопок не было.
    """
    if not isinstance(task, dict):
        return None, "нет задачи"
    repo, why = target_repo(request_text(task.get("title") or ""),
                            sender=task.get("created_by") or "")
    if repo and not repo_looks_valid(repo):
        return None, f"{why}, но {repo} не похож на репозиторий офиса"
    return repo, why

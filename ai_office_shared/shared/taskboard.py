"""
ai_office_shared.shared.taskboard — персистентная доска задач офиса.

Зачем: у Силли (ops-директор) до сих пор не было ДОЛГОЙ памяти о том, какие
верхнеуровневые задачи в работе, кому делегированы и в каком они статусе.
Был только эфемерный эфир задачи (dev_activity, TTL 24ч) и реактивный bug-monitor.
Этот модуль даёт доску, которая переживает рестарт Силли и видна дашборду.

КОНТРАКТ Redis:

  Задача (HASH):
      office:task:{id}
      Поля (все строки): id, title, created_by, assignee, status, parent_id,
                          result, attempts, escalated, created_at, updated_at

  Индекс (ZSET):
      office:tasks:index
      member = task_id, score = updated_at (epoch). ZREVRANGE → свежие первыми.

  Статусы:
      open | in_progress | needs_fix | blocked | awaiting_approval | done | rejected

TTL: открытые задачи живут бессрочно. При переходе в терминальный статус
(done/rejected) на HASH ставится EXPIRE (TASK_TTL_DONE_SEC), чтобы доска не росла
вечно. Индекс чистится лениво (если HASH исчез — member удаляется при чтении).

ВАЖНО: как и dev_activity/log_event — НИ ОДНА функция не пробрасывает исключения
наружу. Падение/отсутствие Redis = тихий warning + безопасный возврат
(None/[]/False). Доска вторична по отношению к самой работе и не должна её ронять.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

try:
    # Нормализация assignee к canonical-имени бота, если это бот офиса.
    from .identity import canonical
except Exception:  # pragma: no cover — на случай частичной установки пакета
    def canonical(name):  # type: ignore
        return name

_logger = logging.getLogger("ai_office_shared.taskboard")

INDEX_KEY = "office:tasks:index"
TASK_TTL_DONE_SEC = 30 * 24 * 3600   # терминальные задачи живут 30 дней
INDEX_MAX = 1000                     # верхняя граница на размер индекса

STATUSES = {
    "open", "in_progress", "needs_fix", "blocked",
    "awaiting_approval", "done", "rejected",
}
TERMINAL_STATUSES = {"done", "rejected"}

# Поля, которые могут хранить произвольный текст — режем длину при записи.
_TEXT_FIELDS = {"title": 500, "result": 4000}


def task_key(task_id: str) -> str:
    """Канонический ключ HASH задачи."""
    return f"office:task:{task_id}"


def new_task_id() -> str:
    """Короткий уникальный id задачи."""
    return uuid.uuid4().hex[:12]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_assignee(assignee: str) -> str:
    """dev-dept/отдел оставляем как есть; имя бота приводим к canonical."""
    if not assignee:
        return ""
    canon = canonical(assignee)
    return canon or assignee


def _decode(raw):
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


def _parse_task(data: dict) -> dict:
    """HASH → нормализованный dict с правильными типами."""
    out: dict = {}
    for k, v in data.items():
        out[_decode(k)] = _decode(v)
    # числовые поля
    try:
        out["attempts"] = int(out.get("attempts", 0) or 0)
    except (ValueError, TypeError):
        out["attempts"] = 0
    out["escalated"] = str(out.get("escalated", "")).lower() in ("1", "true", "yes")
    return out


async def create_task(
    redis_client,
    title: str,
    created_by: str = "силли",
    *,
    assignee: str = "",
    status: str = "open",
    parent_id: str = "",
    task_id: Optional[str] = None,
) -> Optional[str]:
    """
    Создаёт задачу на доске. Возвращает task_id или None при ошибке/без Redis.
    """
    if redis_client is None:
        return None
    if status not in STATUSES:
        status = "open"
    tid = task_id or new_task_id()
    now = _now_iso()
    mapping = {
        "id": tid,
        "title": (title or "")[:_TEXT_FIELDS["title"]],
        "created_by": _normalize_assignee(created_by) or created_by or "",
        "assignee": _normalize_assignee(assignee),
        "status": status,
        "parent_id": parent_id or "",
        "result": "",
        "attempts": "0",
        "escalated": "0",
        "created_at": now,
        "updated_at": now,
    }
    try:
        score = time.time()
        async with redis_client.pipeline(transaction=False) as pipe:
            pipe.hset(task_key(tid), mapping=mapping)
            pipe.zadd(INDEX_KEY, {tid: score})
            pipe.zremrangebyrank(INDEX_KEY, 0, -(INDEX_MAX + 1))  # обрезаем старейшие
            await pipe.execute()
        return tid
    except Exception as e:
        _logger.warning("create_task failed title=%r: %s", title[:40], e)
        return None


async def get_task(redis_client, task_id: str) -> Optional[dict]:
    """Возвращает задачу как dict или None (нет задачи / нет Redis / ошибка)."""
    if redis_client is None or not task_id:
        return None
    try:
        data = await redis_client.hgetall(task_key(task_id))
    except Exception as e:
        _logger.warning("get_task failed id=%s: %s", task_id, e)
        return None
    if not data:
        return None
    return _parse_task(data)


async def _touch(redis_client, task_id: str, fields: dict) -> bool:
    """Внутреннее: обновить поля + updated_at + score в индексе. Fail-silent."""
    if redis_client is None or not task_id:
        return False
    fields = dict(fields)
    fields["updated_at"] = _now_iso()
    status = fields.get("status")
    try:
        score = time.time()
        async with redis_client.pipeline(transaction=False) as pipe:
            pipe.hset(task_key(task_id), mapping=fields)
            pipe.zadd(INDEX_KEY, {task_id: score})
            if status in TERMINAL_STATUSES:
                pipe.expire(task_key(task_id), TASK_TTL_DONE_SEC)
            await pipe.execute()
        return True
    except Exception as e:
        _logger.warning("update task failed id=%s: %s", task_id, e)
        return False


async def update_status(
    redis_client,
    task_id: str,
    status: str,
    *,
    result: Optional[str] = None,
    escalated: Optional[bool] = None,
) -> bool:
    """Меняет статус задачи (+ опционально result/escalated). Fail-silent."""
    if status not in STATUSES:
        _logger.warning("update_status: unknown status %r", status)
        return False
    fields: dict = {"status": status}
    if result is not None:
        fields["result"] = result[:_TEXT_FIELDS["result"]]
    if escalated is not None:
        fields["escalated"] = "1" if escalated else "0"
    return await _touch(redis_client, task_id, fields)


async def set_result(redis_client, task_id: str, result: str) -> bool:
    """Записывает результат задачи без смены статуса. Fail-silent."""
    return await _touch(redis_client, task_id, {"result": (result or "")[:_TEXT_FIELDS["result"]]})


async def incr_attempts(redis_client, task_id: str) -> int:
    """
    Увеличивает счётчик попыток и возвращает новое значение (0 при ошибке).
    Используется гейтом ретраев dev_task (по образцу fix_count в monitor_loop).
    """
    if redis_client is None or not task_id:
        return 0
    try:
        new_val = await redis_client.hincrby(task_key(task_id), "attempts", 1)
        await _touch(redis_client, task_id, {})  # обновить updated_at/score
        return int(new_val)
    except Exception as e:
        _logger.warning("incr_attempts failed id=%s: %s", task_id, e)
        return 0


async def add_subtask(
    redis_client,
    parent_id: str,
    title: str,
    *,
    assignee: str = "",
    status: str = "open",
    created_by: str = "силли",
) -> Optional[str]:
    """Создаёт подзадачу, привязанную к parent_id. Возвращает id подзадачи."""
    return await create_task(
        redis_client, title, created_by,
        assignee=assignee, status=status, parent_id=parent_id,
    )


async def list_tasks(
    redis_client,
    *,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    parent_id: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """
    Возвращает задачи (свежие первыми), опционально фильтруя по
    status / assignee / parent_id. status может быть строкой или множеством/списком.
    Лениво чистит индекс от исчезнувших (по TTL) задач. Fail-silent → [].
    """
    if redis_client is None:
        return []
    try:
        # Берём с запасом — фильтрация идёт в Python.
        ids = await redis_client.zrevrange(INDEX_KEY, 0, max(limit * 5, 200) - 1)
    except Exception as e:
        _logger.warning("list_tasks index read failed: %s", e)
        return []

    if isinstance(status, str):
        status_set = {status}
    elif status:
        status_set = set(status)
    else:
        status_set = None
    assignee_norm = _normalize_assignee(assignee) if assignee else None

    out: list[dict] = []
    stale: list[str] = []
    for raw_id in ids:
        tid = _decode(raw_id)
        task = await get_task(redis_client, tid)
        if task is None:
            stale.append(tid)
            continue
        if status_set is not None and task.get("status") not in status_set:
            continue
        if assignee_norm is not None and task.get("assignee") != assignee_norm:
            continue
        if parent_id is not None and task.get("parent_id") != parent_id:
            continue
        out.append(task)
        if len(out) >= limit:
            break

    if stale:
        try:
            await redis_client.zrem(INDEX_KEY, *stale)
        except Exception:
            pass
    return out


def format_board_for_prompt(tasks: list[dict], *, max_lines: int = 25) -> str:
    """Компактный рендер доски для вставки в промпт/отчёт Силли."""
    if not tasks:
        return ""
    mark = {
        "open": "🆕", "in_progress": "🔄", "needs_fix": "🛠",
        "blocked": "⛔", "awaiting_approval": "⏳", "done": "✅", "rejected": "❌",
    }
    lines: list[str] = []
    for t in tasks[:max_lines]:
        m = mark.get(t.get("status", ""), "•")
        who = t.get("assignee") or "—"
        title = (t.get("title") or "").replace("\n", " ")[:80]
        att = t.get("attempts", 0)
        att_s = f" (попыток: {att})" if att else ""
        lines.append(f"{m} [{t.get('id','?')}] {who}: {title}{att_s}")
    return "[ДОСКА ЗАДАЧ ОФИСА]\n" + "\n".join(lines)

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

from .gates import criterion_gate, criterion_text, run_gate

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
_TEXT_FIELDS = {"title": 500, "result": 4000,
                "acceptance": 4000, "evidence": 8000}


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


def _dump_list(value) -> str:
    """Список → JSON-строка для HASH. Пустое/мусор → "[]"."""
    if not value:
        return "[]"
    try:
        return json.dumps(list(value), ensure_ascii=False)[:_TEXT_FIELDS["evidence"]]
    except (TypeError, ValueError):
        return "[]"


def _load_list(raw) -> list:
    """JSON-строка из HASH → список. Битое значение не должно ронять чтение доски."""
    raw = _decode(raw)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


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
    out["acceptance"] = _load_list(out.get("acceptance"))
    out["evidence"] = _load_list(out.get("evidence"))
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
    acceptance: Optional[list] = None,
) -> Optional[str]:
    """
    Создаёт задачу на доске. Возвращает task_id или None при ошибке/без Redis.

    Args:
        acceptance: критерии приёмки — список проверяемых утверждений. Пишутся
            ЗДЕСЬ, до раздачи задания, и дальше не меняются (см. set_acceptance).
            Задача с критериями не сможет стать done, пока каждый из них не
            закрыт уликой через add_evidence.
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
        "acceptance": _dump_list(acceptance),
        "evidence": "[]",
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



# ── Приёмка: критерии замораживаются ДО работы, улики собираются ПОСЛЕ ────────
# ПРОБЛЕМА, которую решаем:
#     Пока «выполнено» — это утверждение исполнителя, оно всегда истинно.
#     Сегодняшний день дал два учебных примера: Милли считала задачу решённой,
#     хотя её реплика молча выбрасывалась, а «баг починен» держалось ровно до
#     момента, когда кто-то записал, что значит «починен».
# ПРАВИЛО:
#     Критерии пишутся при создании задачи, до раздачи, и после старта не
#     меняются. Кто определяет успех, уже увидев результат, определит его так,
#     чтобы результат подошёл.

async def set_acceptance(redis_client, task_id: str, criteria: list) -> tuple[bool, str]:
    """
    Задать критерии приёмки. Разрешено только пока работа не началась.

    Returns:
        (получилось, причина_отказа)
    """
    if redis_client is None or not task_id:
        return False, "нет redis или task_id"
    if not criteria:
        return False, "пустой список критериев"
    task = await get_task(redis_client, task_id)
    if not task:
        return False, f"задача {task_id} не найдена"
    if task.get("acceptance"):
        return False, ("критерии уже заморожены и не переписываются — "
                       "иначе успех подгоняется под результат")
    if task.get("status") != "open" or int(task.get("attempts") or 0) > 0:
        return False, (f"работа уже началась (status={task.get('status')}, "
                       f"attempts={task.get('attempts')}) — критерии задаются до неё")
    ok = await _touch(redis_client, task_id, {"acceptance": _dump_list(criteria)})
    return (True, "") if ok else (False, "ошибка записи")


# Имя проверяющего для улик, добытых детерминированным гейтом. Не бот и не
# человек: у гейта нет интереса в исходе, поэтому правило «не проверяй сам
# себя» на него не распространяется.
VERIFIER_GATE = "гейт"


async def add_evidence(
    redis_client,
    task_id: str,
    criterion: str,
    *,
    passed: bool,
    proof: str = "",
    checked_by: str = "",
) -> tuple[bool, str]:
    """
    Приложить улику по одному критерию.

    Returns:
        (записано, причина_отказа)

    ПРАВИЛО РАЗДЕЛЕНИЯ РОЛЕЙ: успех подтверждает НЕ тот, кто делал работу.
    `passed=True` от исполнителя отклоняется — это не проверка, а повторное
    утверждение. `passed=False` принимается от кого угодно, включая
    исполнителя и аноним: признание провала не выгодно тому, кто его признаёт,
    и лишний барьер здесь только заставил бы молчать о поломке.

    Args:
        criterion: текст критерия — ровно как он записан в acceptance.
        proof:     чем подтверждено: код возврата, строка лога, диф, ответ HTTP.
        checked_by: кто проверял. VERIFIER_GATE — улика от гейта (см. verify_task).
    """
    if redis_client is None or not task_id or not criterion:
        return False, "нет redis, task_id или criterion"
    task = await get_task(redis_client, task_id)
    if not task:
        return False, f"задача {task_id} не найдена"

    who = _normalize_assignee(checked_by) or checked_by or ""
    if passed and who != VERIFIER_GATE:
        if not who:
            return False, ("улику об успехе должен подписать проверяющий — "
                           "анонимное «работает» проверкой не считается")
        if who == (task.get("assignee") or ""):
            return False, (f"{who} — исполнитель этой задачи; свою работу "
                           f"подтверждает не исполнитель, а проверяющий")

    ev = list(task.get("evidence") or [])
    ev = [e for e in ev if not (isinstance(e, dict) and e.get("criterion") == criterion)]
    ev.append({
        "criterion": criterion,
        "passed": bool(passed),
        "proof": (proof or "")[:600],
        "checked_by": who,
        "at": _now_iso(),
    })
    ok = await _touch(redis_client, task_id, {"evidence": _dump_list(ev)})
    return (True, "") if ok else (False, "ошибка записи")


async def verify_task(redis_client, task_id: str) -> tuple[int, int, list]:
    """
    Прогнать все критерии задачи, у которых есть детерминированный гейт, и
    записать улики от имени VERIFIER_GATE.

    Критерии без гейта не трогаются — их закрывает проверяющий бот/человек
    через add_evidence.

    Returns:
        (сколько_прошло, сколько_провалено, список_записей)
    """
    task = await get_task(redis_client, task_id)
    if not task:
        return 0, 0, []
    passed_n = failed_n = 0
    records: list[dict] = []
    for item in (task.get("acceptance") or []):
        gate = criterion_gate(item)
        if gate is None:
            continue
        text = criterion_text(item)
        ok, proof = await run_gate(gate, redis_client=redis_client)
        await add_evidence(redis_client, task_id, text,
                           passed=ok, proof=proof, checked_by=VERIFIER_GATE)
        records.append({"criterion": text, "passed": ok, "proof": proof})
        if ok:
            passed_n += 1
        else:
            failed_n += 1
    return passed_n, failed_n, records


def acceptance_verdict(task: dict) -> tuple[bool, list, list]:
    """
    Сверяет критерии с уликами. Чистая функция — тестируется без Redis.

    Returns:
        (всё_закрыто, непокрытые_критерии, проваленные_критерии)
    """
    criteria = [criterion_text(c) for c in (task.get("acceptance") or []) if c]
    criteria = [c for c in criteria if c]
    if not criteria:
        return True, [], []   # задача без критериев ведёт себя как раньше
    by_criterion = {
        e.get("criterion"): e
        for e in (task.get("evidence") or []) if isinstance(e, dict)
    }
    missing, failed = [], []
    for c in criteria:
        ev = by_criterion.get(c)
        if ev is None:
            missing.append(c)
        elif not ev.get("passed"):
            failed.append(c)
    return (not missing and not failed), missing, failed


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
    # Гейт: «готово» нельзя объявить, его надо доказать. Задача без критериев
    # ведёт себя как раньше — гейт включается только там, где критерии заданы.
    if status == "done":
        task = await get_task(redis_client, task_id)
        if task:
            ok_all, missing, failed = acceptance_verdict(task)
            if not ok_all:
                _logger.warning(
                    "update_status: %s не может стать done — не закрыто %d, "
                    "провалено %d критериев", task_id, len(missing), len(failed))
                return False

    fields: dict = {"status": status}
    if result is not None:
        fields["result"] = result[:_TEXT_FIELDS["result"]]
    if escalated is not None:
        fields["escalated"] = "1" if escalated else "0"
    return await _touch(redis_client, task_id, fields)


async def reopen_task(redis_client, task_id: str, reason: str = "") -> tuple[bool, str]:
    """
    Вернуть задачу в очередь: status=open, счётчик попыток и флаг эскалации сброшены.

    Зачем отдельная функция, а не «просто HSET»: 23.08.2026 две заявки dev-dept
    легли в blocked из-за пустого счёта Anthropic — сбой инфраструктуры, а не
    работа команды. Вернуть их в строй было нечем: Силли умеет читать Redis
    четырьмя готовыми аудитами и не умеет писать вовсе, а `/redis` требует токен,
    которого у сессии Клода нет. Возможность, которую нельзя назвать, не
    существует (урок #80) — поэтому здесь именованная и узкая операция вместо
    права писать в Redis что угодно через LLM-интент.

    Счётчик обнуляем намеренно: попытки съел сбой доступа, а не работа. Оставь их
    — и очередь на первом же тике увидит исчерпанный потолок и заблокирует задачу
    снова, то есть заявка умрёт из-за чужого счёта, ни разу не дойдя до отдела.

    Критерии приёмки и улики НЕ трогаем: они заморожены до работы и переживают
    возврат, а новый прогон перезапишет улики по тем же критериям.

    Returns:
        (получилось, причина_отказа)
    """
    if redis_client is None or not task_id:
        return False, "нет redis или task_id"
    task = await get_task(redis_client, task_id)
    if not task:
        return False, f"задача {task_id} не найдена"
    if task.get("status") == "in_progress":
        # По ней прямо сейчас идёт цепочка и занят слот исполнения. Сброс
        # счётчика под работающим прогоном — гонка, а не починка.
        return False, "задача уже в работе — возвращать нечего"
    ok = await _touch(redis_client, task_id, {
        "status": "open",
        "attempts": "0",
        "escalated": "0",
        "result": (reason or "возвращена в очередь")[:_TEXT_FIELDS["result"]],
    })
    if ok:
        _logger.info("reopen_task %s: %s", task_id, (reason or "")[:80])
    return (True, "") if ok else (False, "ошибка записи")


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


# ── Потолок раундов: цикл «доработай» обязан кончаться ────────────────────────
# ПРОБЛЕМА: план→работа→ревью→доработка — петля без выхода. Модель, которой
# сказали «не годится, переделай», переделывает бесконечно и каждый раз
# уверена, что теперь-то всё. Токены жгутся, задача стоит, человек не знает,
# что она стоит, потому что формально «идёт работа».
# ПРАВИЛО: у петли есть счётчик (существующий attempts) и потолок. Упёрлись —
# это не «ещё раз», а blocked + escalated: разбирается человек. Дешевле
# позвать Влада на третьем круге, чем на тридцатом.

MAX_ROUNDS = 3


def rounds_left(task: dict, *, max_rounds: int = MAX_ROUNDS) -> int:
    """Сколько попыток осталось у задачи. 0 — потолок исчерпан."""
    try:
        used = int(task.get("attempts") or 0)
    except (ValueError, TypeError):
        used = 0
    return max(0, max_rounds - used)


def should_escalate(task: dict, *, max_rounds: int = MAX_ROUNDS) -> bool:
    """
    True, если задачу пора отдать человеку: попытки исчерпаны, а задача всё
    ещё не в терминальном статусе и ещё не эскалирована.
    """
    if not task:
        return False
    if task.get("status") in TERMINAL_STATUSES:
        return False
    if task.get("escalated"):
        return False
    return rounds_left(task, max_rounds=max_rounds) <= 0


async def escalate(redis_client, task_id: str, reason: str = "") -> bool:
    """
    Снять задачу с автоматической петли и отдать человеку.

    Ставит blocked + escalated=1 и пишет причину в result, чтобы у человека
    сразу был контекст, а не только факт остановки.
    """
    task = await get_task(redis_client, task_id)
    attempts = (task or {}).get("attempts", 0)
    text = reason or f"исчерпан потолок попыток ({attempts}) — нужен человек"
    return await update_status(redis_client, task_id, "blocked",
                               result=text, escalated=True)


def format_evidence_report(task: dict) -> str:
    """
    Отчёт по задаче таблицей улик, а не прозой.

    Проза («всё сделал, работает») не проверяема: читатель не может отличить
    выполненную работу от уверенно описанной. Таблица показывает по каждому
    критерию, кто и чем его закрыл, — и сразу видно строки, где проверяющий
    совпал бы с исполнителем или где улика пустая.
    """
    criteria = [criterion_text(c) for c in (task.get("acceptance") or []) if c]
    criteria = [c for c in criteria if c]
    title = (task.get("title") or "").replace("\n", " ")[:120]
    head = f"[{task.get('id', '?')}] {title}"
    if not criteria:
        return f"{head}\nКритериев приёмки не было — проверить нечем."

    by_criterion = {
        e.get("criterion"): e
        for e in (task.get("evidence") or []) if isinstance(e, dict)
    }
    ok_all, missing, failed = acceptance_verdict(task)
    lines = [head, "", "| критерий | вердикт | чем подтверждено | кто |",
             "|---|---|---|---|"]
    for c in criteria:
        ev = by_criterion.get(c)
        if ev is None:
            verdict, proof, who = "⬜ не проверен", "—", "—"
        elif ev.get("passed"):
            verdict = "✅"
            proof = (ev.get("proof") or "—").replace("\n", " ")[:120]
            who = ev.get("checked_by") or "—"
        else:
            verdict = "❌"
            proof = (ev.get("proof") or "—").replace("\n", " ")[:120]
            who = ev.get("checked_by") or "—"
        lines.append(f"| {c[:80]} | {verdict} | {proof} | {who} |")

    lines.append("")
    if ok_all:
        lines.append(f"Итог: приёмка пройдена, {len(criteria)}/{len(criteria)}.")
    else:
        lines.append(f"Итог: НЕ пройдена — не проверено {len(missing)}, "
                     f"провалено {len(failed)} из {len(criteria)}.")
    return "\n".join(lines)


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
        # Прогресс по критериям — иначе Силли не видит, что задача не может
        # закрыться, и будет пытаться объявить её готовой.
        crit = [c for c in (t.get("acceptance") or []) if c]
        acc_s = ""
        if crit:
            _, missing, failed = acceptance_verdict(t)
            done_n = len(crit) - len(missing) - len(failed)
            acc_s = f" | приёмка {done_n}/{len(crit)}"
            if failed:
                acc_s += f", провалено: {len(failed)}"
        lines.append(f"{m} [{t.get('id','?')}] {who}: {title}{att_s}{acc_s}")
    return "[ДОСКА ЗАДАЧ ОФИСА]\n" + "\n".join(lines)

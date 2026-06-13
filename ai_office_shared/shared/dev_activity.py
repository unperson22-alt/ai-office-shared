"""
ai_office_shared.shared.dev_activity — общий «эфир» активности dev-dept.

Зачем: когда отдел разработки работает ПАРАЛЛЕЛЬНО (Рикки ‖ Тести ‖ Секки
одновременно ревьюят/тестируют/аудируют код Девви), каждый участник должен
видеть действия остальных в реальном времени. Этот модуль — единый канал,
через который Силли и все воркеры публикуют и читают действия команды по
конкретной задаче.

КОНТРАКТ (одинаков для shared-либы и для inline-реализации в воркерах):

  Ключ-лента задачи:
      dev-dept:activity:{task_id}
      Тип: Redis LIST. LPUSH (новые сверху). LTRIM до FEED_MAX_LEN.
      TTL: ACTIVITY_TTL_SEC (EXPIRE на ключ).

  Pub/sub канал (live-наблюдатели: монитор Силли, дашборд):
      dev-dept:activity
      PUBLISH JSON каждого события.

  Запись (JSON):
      {
        "ts":      "2026-06-13T15:40:01Z",   UTC ISO8601
        "task_id": "<короткий id задачи>",
        "bot":     "<lowercase canonical: девви/рикки/...>",
        "phase":   "plan"|"start"|"done"|"error"|"deploy",
        "summary": "<что именно сделал, кратко>",
        "level":   "info"|"warn"|"error"
      }

ВАЖНО: ни одна функция здесь НИКОГДА не пробрасывает исключения наружу —
вещание не должно ронять пайплайн (как и log_event). Падение/отсутствие
Redis = тихий warning, пайплайн продолжает работать без эфира.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

_logger = logging.getLogger("ai_office_shared.dev_activity")

ACTIVITY_TTL_SEC = 24 * 3600     # лента задачи живёт сутки
FEED_MAX_LEN = 200               # не даём ленте расти бесконечно под нагрузкой
ACTIVITY_CHANNEL = "dev-dept:activity"
_VALID_PHASES = {"plan", "start", "done", "error", "deploy"}
_VALID_LEVELS = {"info", "warn", "error"}


def activity_key(task_id: str) -> str:
    """Канонический ключ ленты активности задачи."""
    return f"dev-dept:activity:{task_id}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def publish_activity(
    redis_client,
    task_id: str,
    bot: str,
    phase: str,
    summary: str = "",
    *,
    level: str = "info",
) -> None:
    """
    Публикует одно действие участника команды в эфир задачи. Fail-silent.

    Пишет и в ленту задачи (LIST), и в pub/sub канал — за один pipeline.
    Если redis_client отсутствует (REDIS_URL не задан) — тихо выходит.
    """
    if redis_client is None or not task_id:
        return
    if phase not in _VALID_PHASES:
        phase = "info" if phase not in _VALID_LEVELS else phase
    if level not in _VALID_LEVELS:
        level = "info"

    entry = {
        "ts": _now_iso(),
        "task_id": task_id,
        "bot": bot,
        "phase": phase,
        "summary": (summary or "")[:500],
        "level": level,
    }
    payload = json.dumps(entry, ensure_ascii=False, default=str)

    try:
        key = activity_key(task_id)
        async with redis_client.pipeline(transaction=False) as pipe:
            pipe.lpush(key, payload)
            pipe.ltrim(key, 0, FEED_MAX_LEN - 1)
            pipe.expire(key, ACTIVITY_TTL_SEC)
            pipe.publish(ACTIVITY_CHANNEL, payload)
            await pipe.execute()
    except Exception as e:
        _logger.warning("publish_activity failed task=%s bot=%s: %s", task_id, bot, e)


async def read_activity(
    redis_client,
    task_id: str,
    *,
    limit: int = 50,
) -> list[dict]:
    """
    Возвращает действия команды по задаче в ХРОНОЛОГИЧЕСКОМ порядке
    (старые → новые), чтобы участник видел историю «как развивалась работа».
    Fail-silent: при ошибке/отсутствии Redis возвращает [].
    """
    if redis_client is None or not task_id:
        return []
    try:
        raw_entries = await redis_client.lrange(activity_key(task_id), 0, limit - 1)
    except Exception as e:
        _logger.warning("read_activity failed task=%s: %s", task_id, e)
        return []

    out: list[dict] = []
    for raw in raw_entries:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            out.append(json.loads(raw))
        except Exception:
            continue
    out.reverse()  # LPUSH хранит новые сверху → разворачиваем в хронологию
    return out


def format_activity_for_prompt(
    entries: list[dict],
    *,
    exclude_bot: Optional[str] = None,
    max_lines: int = 30,
) -> str:
    """
    Рендерит ленту в компактный блок для вставки в промпт воркера —
    «вот что в этот момент делает остальная команда над той же задачей».
    exclude_bot: не показывать собственные строки воркеру (чтобы не дублировать).
    """
    lines: list[str] = []
    for e in entries:
        bot = e.get("bot", "?")
        if exclude_bot and bot == exclude_bot:
            continue
        phase = e.get("phase", "")
        summary = (e.get("summary") or "").replace("\n", " ").strip()
        mark = {"plan": "🧠", "start": "▶️", "done": "✅",
                "error": "❌", "deploy": "🚀"}.get(phase, "•")
        line = f"{mark} {bot} [{phase}]"
        if summary:
            line += f": {summary[:160]}"
        lines.append(line)
    if not lines:
        return ""
    lines = lines[-max_lines:]
    return "[ДЕЙСТВИЯ КОМАНДЫ DEV-DEPT]\n" + "\n".join(lines)

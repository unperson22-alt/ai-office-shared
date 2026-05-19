"""
ai_office_shared.shared.logging — единый структурный логгер всего офиса.

Все боты пишут события в Redis в одном формате. Силли потом читает и анализирует.

Контракт ключа:
    office:logs:{bot}:{YYYY-MM-DD}
    Тип: Redis LIST. LPUSH в начало. LTRIM до 1000 записей.
    TTL: 7 дней (EXPIRE на ключ, не на запись).

Контракт записи (JSON-сериализованный dict):
    {
      "ts":      "2026-05-19T20:15:43Z",   UTC ISO8601
      "level":   "info" | "warn" | "error",
      "event":   "<snake_case_name>",
      "bot":     "<lowercase canonical>",
      "user_id": int | null,
      "chat_id": int | null,
      "context": { произвольные kwargs }
    }

Использование (пишем):
    from ai_office_shared.shared.logging import log_event

    await log_event(redis, "филли", "route_miss",
                    level="warn",
                    user_id=msg.from_user.id,
                    chat_id=msg.chat.id,
                    target="билли",
                    reason="http_timeout",
                    elapsed_ms=25000)

Использование (читаем — Силли):
    from ai_office_shared.shared.logging import read_logs

    misses = await read_logs(redis, "филли", days=2,
                             event_filter="route_miss", limit=50)

ВАЖНО: log_event никогда не пробрасывает исключения наружу — логирование
не должно ронять бота. Падение Redis = тихий warning в stderr, ничего больше.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

_logger = logging.getLogger("ai_office_shared.logging")

LOG_KEY_TTL_SEC = 7 * 24 * 3600   # 7 days
LOG_KEY_MAX_LEN = 1000            # хранится последняя 1000 событий на день/бота
_VALID_LEVELS = {"info", "warn", "error"}


def _today_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_iso() -> str:
    # Без микросекунд, с явным Z — компактно и читается невооружённым глазом.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_key(bot: str, date: Optional[str] = None) -> str:
    """Канонический ключ для логов бота за день. date в формате YYYY-MM-DD, None = сегодня."""
    return f"office:logs:{bot}:{date or _today_utc_str()}"


async def log_event(
    redis_client,
    bot: str,
    event: str,
    *,
    level: str = "info",
    user_id: Optional[int] = None,
    chat_id: Optional[int] = None,
    **context: Any,
) -> None:
    """
    Пишет одну структурную запись в Redis. Fail-silent.

    Параметры:
        redis_client : async Redis client (redis.asyncio.Redis)
        bot          : каноническое lowercase имя ("филли", "билли", ...)
        event        : машинно-читаемое имя события (snake_case)
        level        : "info" | "warn" | "error"   (default "info")
        user_id      : Telegram user id   (опц.)
        chat_id      : Telegram chat id   (опц.)
        **context    : любые дополнительные поля идут в context

    Поведение:
        - LPUSH (новые сверху) → LTRIM (хранится последняя 1000) → EXPIRE (7 дней).
        - Всё в одном pipeline, без транзакции (atomicity не критична для логов).
        - Исключения логируются и проглатываются.
    """
    if level not in _VALID_LEVELS:
        level = "info"

    entry = {
        "ts": _now_iso(),
        "level": level,
        "event": event,
        "bot": bot,
        "user_id": user_id,
        "chat_id": chat_id,
        "context": context,
    }

    try:
        key = log_key(bot)
        payload = json.dumps(entry, ensure_ascii=False, default=str)
        async with redis_client.pipeline(transaction=False) as pipe:
            pipe.lpush(key, payload)
            pipe.ltrim(key, 0, LOG_KEY_MAX_LEN - 1)
            pipe.expire(key, LOG_KEY_TTL_SEC)
            await pipe.execute()
    except Exception as e:
        # Логирование не должно ломать бизнес-логику. Тихо предупреждаем в stderr.
        _logger.warning("log_event failed for bot=%s event=%s: %s", bot, event, e)


async def read_logs(
    redis_client,
    bot: str,
    *,
    days: int = 1,
    limit: int = 100,
    event_filter: Optional[str] = None,
    level_filter: Optional[str] = None,
) -> list[dict]:
    """
    Читает последние записи по боту за N последних дней (UTC).

    Возвращает list[dict] в обратном хронологическом порядке (новые первыми).
    Силли использует это в auto-pull сценарии при расследовании багов.

    Параметры:
        bot          : каноническое lowercase имя
        days         : сколько последних дней просканировать (включая сегодня)
        limit        : максимум возвращаемых записей (после фильтров)
        event_filter : вернуть только записи с event == <значение>
        level_filter : вернуть только записи с level == <значение>
    """
    today = datetime.now(timezone.utc).date()
    out: list[dict] = []

    for d_offset in range(max(1, days)):
        day = (today - timedelta(days=d_offset)).strftime("%Y-%m-%d")
        key = log_key(bot, day)
        try:
            raw_entries = await redis_client.lrange(key, 0, LOG_KEY_MAX_LEN - 1)
        except Exception as e:
            _logger.warning("read_logs failed for %s: %s", key, e)
            continue

        for raw in raw_entries:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if event_filter and obj.get("event") != event_filter:
                continue
            if level_filter and obj.get("level") != level_filter:
                continue
            out.append(obj)
            if len(out) >= limit:
                return out
    return out


async def list_event_types(
    redis_client,
    bot: str,
    *,
    days: int = 1,
) -> dict[str, int]:
    """
    Возвращает {event_name: count} за N дней. Удобно для дашборда и для
    Силли — "что вообще происходило у этого бота вчера".
    """
    entries = await read_logs(redis_client, bot, days=days, limit=LOG_KEY_MAX_LEN * days)
    counts: dict[str, int] = {}
    for e in entries:
        ev = e.get("event", "unknown")
        counts[ev] = counts.get(ev, 0) + 1
    return counts

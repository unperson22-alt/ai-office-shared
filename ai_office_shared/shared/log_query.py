"""
Ответы про логи цифрами, а не дампом.

16.08.2026: чтобы понять, почему dev-отдел шесть раз подряд «не дал код», нужно
было узнать одно — как часто Девви получал ту же задачу. Спросить это было
негде. Четыре попытки подряд вернули сырой список событий, обрезанный на 3000
символах: ни счёта, ни первой метки, ни последней, ни интервала. Разбор
инцидента упёрся не в сложность, а в то, что офис не умеет ответить на вопрос
о самом себе — только вывалить всё, что помнит.

Дамп — это не наблюдаемость. Наблюдаемость — это когда «сколько, когда и с
каким тактом» помещается в одну строку.

Модуль читает то же, что и read_logs, и добавляет две вещи:

    discover_bots()  — какие боты вообще писали логи в этот день (реестр BOTS
                       тут не годится: воркеры dev-отдела в нём не числятся, а
                       именно они и понадобились);
    summarize()      — сколько всего, первая и последняя метка, разбивка по
                       событиям и уровням и МЕДИАННЫЙ интервал между
                       повторами. Медиана, а не среднее: одна длинная пауза
                       посреди ровного такта не должна его маскировать.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from .logging import LOG_KEY_MAX_LEN, read_logs

logger = logging.getLogger(__name__)

# Потолок на разбор. Ключ и так подрезан до LOG_KEY_MAX_LEN на день, но запрос
# на 30 дней по всем ботам не должен превращаться в минутную паузу.
MAX_SCAN_DAYS = 30


def _parse_ts(value: str) -> Optional[datetime]:
    """'2026-08-16T04:07:52Z' → datetime. Мусор → None, без исключения."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


async def discover_bots(redis_client, *, days: int = 1) -> list[str]:
    """Имена ботов, у которых есть логи за последние N дней.

    Идём по ключам, а не по реестру identity.BOTS: воркеры dev-отдела (девви,
    рикки, тести, секки, скрибби) в реестре не числятся, а разбор 16.08 был
    именно про них. Реестр отвечает «кто есть в офисе», Redis — «кто говорил».
    """
    today = datetime.now(timezone.utc).date()
    found: set[str] = set()
    for offset in range(max(1, min(days, MAX_SCAN_DAYS))):
        day = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        pattern = f"office:logs:*:{day}"
        try:
            cursor = 0
            while True:
                cursor, keys = await redis_client.scan(cursor, match=pattern, count=200)
                for key in keys:
                    if isinstance(key, bytes):
                        key = key.decode("utf-8", errors="replace")
                    parts = key.split(":")
                    # office:logs:<bot>:<date> — имя бота само может быть с ':'?
                    # Нет: log_key его не экранирует, значит и мы берём срез.
                    if len(parts) >= 4:
                        found.add(":".join(parts[2:-1]))
                if cursor == 0:
                    break
        except Exception as e:
            logger.warning("discover_bots: scan failed for %s: %s", pattern, e)
    return sorted(found)


def summarize(entries: Iterable[dict]) -> dict:
    """Сводка по записям: сколько, когда, как часто.

    cadence_sec — медианный интервал между СОСЕДНИМИ записями самого частого
    события. Именно этим числом отличается «бот поработал восемь раз за день»
    от «бот молотит одно и то же каждые две минуты», и именно его пришлось
    считать глазами по обрезанному дампу.
    """
    items = list(entries)
    out: dict = {
        "total": len(items),
        "first": None,
        "last": None,
        "span_sec": None,
        "by_event": {},
        "by_level": {},
        "top_event": None,
        "cadence_sec": None,
    }
    if not items:
        return out

    stamps: list[datetime] = []
    per_event: dict[str, list[datetime]] = {}
    for e in items:
        ev = e.get("event", "unknown")
        lv = e.get("level", "unknown")
        out["by_event"][ev] = out["by_event"].get(ev, 0) + 1
        out["by_level"][lv] = out["by_level"].get(lv, 0) + 1
        ts = _parse_ts(e.get("ts", ""))
        if ts:
            stamps.append(ts)
            per_event.setdefault(ev, []).append(ts)

    if stamps:
        stamps.sort()
        out["first"] = stamps[0].strftime("%Y-%m-%dT%H:%M:%SZ")
        out["last"] = stamps[-1].strftime("%Y-%m-%dT%H:%M:%SZ")
        out["span_sec"] = int((stamps[-1] - stamps[0]).total_seconds())

    top = max(out["by_event"].items(), key=lambda kv: kv[1])[0]
    out["top_event"] = top
    marks = sorted(per_event.get(top, []))
    if len(marks) >= 2:
        gaps = [(b - a).total_seconds() for a, b in zip(marks, marks[1:])]
        med = _median(gaps)
        out["cadence_sec"] = int(med) if med is not None else None
    return out


def format_summary(bot: str, summary: dict) -> str:
    """Сводка одной строкой — то, что хотелось получить в ответ на вопрос."""
    if not summary["total"]:
        return f"{bot}: записей нет"
    parts = [f"{bot}: {summary['total']} записей"]
    if summary["first"]:
        parts.append(f"{summary['first'][11:19]}–{summary['last'][11:19]}")
    top, count = summary["top_event"], summary["by_event"].get(summary["top_event"], 0)
    cadence = summary["cadence_sec"]
    if cadence and count >= 2:
        parts.append(f"чаще всего {top} ×{count}, каждые ~{cadence // 60}м{cadence % 60:02d}с")
    else:
        parts.append(f"чаще всего {top} ×{count}")
    warn = summary["by_level"].get("warn", 0) + summary["by_level"].get("error", 0)
    if warn:
        parts.append(f"warn/error: {warn}")
    return " | ".join(parts)


async def query(
    redis_client,
    *,
    bot: str = "",
    days: int = 1,
    event: str = "",
    level: str = "",
    limit: int = 50,
) -> dict:
    """Свод по одному боту или по всем сразу.

    Ответ ВСЕГДА содержит сводку и лишь опционально сами записи: вопрос «что
    происходит» почти никогда не требует всех строк, а обрезанный дамп не
    отвечает вообще ни на что.
    """
    days = max(1, min(int(days or 1), MAX_SCAN_DAYS))
    bots = [bot] if bot else await discover_bots(redis_client, days=days)

    result: dict = {"days": days, "bots": {}, "filters": {}}
    if event:
        result["filters"]["event"] = event
    if level:
        result["filters"]["level"] = level

    for name in bots:
        entries = await read_logs(
            redis_client, name, days=days,
            limit=LOG_KEY_MAX_LEN * days,
            event_filter=event or None,
            level_filter=level or None,
        )
        summary = summarize(entries)
        block: dict = {"summary": summary, "line": format_summary(name, summary)}
        if limit > 0:
            block["entries"] = entries[:limit]
        result["bots"][name] = block

    result["lines"] = [b["line"] for b in result["bots"].values()]
    return result

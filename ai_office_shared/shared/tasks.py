"""
ai_office_shared.shared.tasks — фоновые задачи общие для всех ботов.

auto_extract_interests — Haiku извлекает факты из сообщения пользователя.
weekly_review         — Haiku компактизирует профиль раз в неделю.
weekly_review_loop    — asyncio loop для запуска review.
schedule_loop         — asyncio loop для динамических напоминаний.
parse_schedule_tag    — парсит тег [SCHEDULE:...] из ответа Claude.
add_scheduled_task    — добавляет задачу в Redis ZSET.
list_scheduled_tasks  — возвращает список задач пользователя.
remove_scheduled_task — удаляет задачу по index.

Зависимости передаются явно: redis_client, bot_name, anthropic_client.

ИСПОЛЬЗОВАНИЕ:
    from ai_office_shared.shared.tasks import (
        auto_extract_interests,
        weekly_review,
        weekly_review_loop,
        schedule_loop,
        parse_schedule_tag,
        add_scheduled_task,
        list_scheduled_tasks,
        remove_scheduled_task,
    )

    # В main():
    asyncio.create_task(weekly_review_loop(redis_client, "билли", anthropic_client))
    asyncio.create_task(schedule_loop(redis_client, "билли", bot))

    # В process() — после получения ответа от Claude:
    tag = parse_schedule_tag(response_text)
    if tag:
        await add_scheduled_task(redis_client, bot_name, user_id, tag)

ФОРМАТ ТЕГОВ (Claude вставляет в ответ):
    [SCHEDULE:daily:09:00:текст напоминания]       — каждый день в 09:00 UTC
    [SCHEDULE:weekly:mon:09:00:текст]               — каждый понедельник
    [SCHEDULE:interval:30m:текст]                   — каждые 30 минут
    [SCHEDULE:once:2026-05-25:09:00:текст]          — один раз в дату
    [CANCEL_SCHEDULE:1]                             — отменить задачу #1
    [LIST_SCHEDULES]                                — показать список задач
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time as _time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

from .redis_helpers import (
    redis_get_notes,
    redis_add_note,
    redis_set_notes,
    redis_get_history,
    redis_get_all_user_ids,
)

logger = logging.getLogger("ai_office_shared.tasks")

HAIKU_MODEL = "claude-haiku-4-5-20251001"
WEEK_SECONDS = 7 * 24 * 3600

# ─── Existing functions (unchanged) ──────────────────────────────────────────

async def auto_extract_interests(
    redis_client,
    bot_name: str,
    user_id: int,
    message: str,
    anthropic_client,
) -> None:
    """
    Фоновое авто-извлечение фактов о пользователе через Haiku.
    Fail-silent — никогда не бросает исключение.

    Записывает строку вида "[auto] факт" в notes пользователя.
    """
    try:
        existing = await redis_get_notes(redis_client, bot_name, user_id)
        r = await anthropic_client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=150,
            system=(
                "Ты извлекаешь факты о пользователе из его сообщений. "
                "Найди конкретные факты, интересы, предпочтения или важные детали О ПОЛЬЗОВАТЕЛЕ. "
                "Если нашёл что-то новое и конкретное — верни одну строку начинающуюся с [auto]. "
                "Если ничего конкретного нет или это уже есть в заметках — верни пустую строку. "
                "Не записывай временные состояния (устал, болит голова сегодня). "
                "Только устойчивые факты: работа, семья, питание, хобби, предпочтения."
            ),
            messages=[{"role": "user", "content":
                f"Сообщение: {message}\n\nУже известно:\n{existing or '(ничего)'}"}],
        )
        fact = r.content[0].text.strip()
        if fact.startswith("[auto]"):
            await redis_add_note(redis_client, bot_name, user_id, fact)
            logger.info("Auto-extracted for %s/%s: %s", bot_name, user_id, fact)
    except Exception as e:
        logger.warning("auto_extract_interests(%s, %s) failed: %s", bot_name, user_id, e)


async def weekly_review(
    redis_client,
    bot_name: str,
    user_id: int,
    anthropic_client,
) -> None:
    """
    Еженедельная компактизация профиля пользователя через Haiku.
    Читает историю + старые заметки → создаёт компактный профиль.
    """
    try:
        history = await redis_get_history(redis_client, bot_name, user_id)
        notes = await redis_get_notes(redis_client, bot_name, user_id)
        if not history and not notes:
            return

        history_text = "\n".join(
            f"{m['role']}: {m['content'][:200]}" for m in history[-30:]
        )
        r = await anthropic_client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=400,
            system=(
                "Ты обновляешь профиль пользователя на основе переписки и старых заметок. "
                "Создай компактный список фактов о пользователе (не более 10 строк). "
                "Каждый факт с новой строки, начинается с [auto]. "
                "Убери дубли, объедини похожее, удали устаревшее. "
                "Только конкретные устойчивые факты."
            ),
            messages=[{"role": "user", "content":
                f"Старые заметки:\n{notes or '(нет)'}\n\n"
                f"Последние сообщения:\n{history_text}"}],
        )
        new_profile = r.content[0].text.strip()
        await redis_set_notes(redis_client, bot_name, user_id, new_profile)
        logger.info("Weekly review done for %s/%s", bot_name, user_id)
    except Exception as e:
        logger.warning("weekly_review(%s, %s) failed: %s", bot_name, user_id, e)


async def weekly_review_loop(
    redis_client,
    bot_name: str,
    anthropic_client,
    interval_check_sec: int = 3600,
) -> None:
    """
    asyncio loop — раз в неделю запускает weekly_review для всех пользователей бота.
    Запускается через asyncio.create_task() при старте бота.

    interval_check_sec — как часто проверяем не пришла ли неделя (default 1 час).
    """
    from .identity import display as _display
    last_key = f"weekly_review:last_run:{_display(bot_name) or bot_name}"

    while True:
        try:
            last_raw = await redis_client.get(last_key)
            now = int(_time.time())
            last = int(last_raw.decode()) if last_raw else 0

            if (now - last) > WEEK_SECONDS:
                await redis_client.set(last_key, str(now))
                user_ids = await redis_get_all_user_ids(redis_client, bot_name)
                logger.info("Weekly review: starting for %d users (%s)", len(user_ids), bot_name)
                for uid in user_ids:
                    await weekly_review(redis_client, bot_name, uid, anthropic_client)
                logger.info("Weekly review: done (%s)", bot_name)
        except Exception as e:
            logger.warning("weekly_review_loop(%s) error: %s", bot_name, e)

        await asyncio.sleep(interval_check_sec)


# ─── Dynamic Scheduler ────────────────────────────────────────────────────────

def _schedule_key(bot_name: str, user_id: int) -> str:
    return f"office:schedule:{bot_name}:{user_id}"


def _calc_next_run(task: dict) -> float:
    """Вычисляет следующий timestamp запуска задачи."""
    now = datetime.now(timezone.utc)
    t = task["type"]

    if t == "daily":
        h, m = task["hour"], task["minute"]
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target.timestamp()

    elif t == "weekly":
        # day_of_week: 0=mon ... 6=sun
        h, m, dow = task["hour"], task["minute"], task["day_of_week"]
        days_ahead = (dow - now.weekday()) % 7
        target = now.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=days_ahead)
        if target <= now:
            target += timedelta(weeks=1)
        return target.timestamp()

    elif t == "interval":
        return now.timestamp() + task["interval_sec"]

    elif t == "once":
        dt = datetime.fromisoformat(task["run_at"])
        return dt.timestamp()

    return now.timestamp() + 86400  # fallback: 1 день


_DOW_MAP = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
            "пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}

_TAG_RE = re.compile(
    r'\[SCHEDULE:(?P<body>[^\]]+)\]'
    r'|\[CANCEL_SCHEDULE:(?P<cancel>\d+)\]'
    r'|\[LIST_SCHEDULES\]',
    re.IGNORECASE
)


def parse_schedule_tag(text: str) -> dict | None:
    """
    Парсит первый тег [SCHEDULE:...] / [CANCEL_SCHEDULE:N] / [LIST_SCHEDULES] из текста.
    Возвращает dict с action или None если тегов нет.

    Форматы:
      [SCHEDULE:daily:09:00:текст]
      [SCHEDULE:weekly:mon:09:00:текст]
      [SCHEDULE:interval:30m:текст]
      [SCHEDULE:once:2026-05-25:09:00:текст]
      [CANCEL_SCHEDULE:1]
      [LIST_SCHEDULES]
    """
    m = _TAG_RE.search(text)
    if not m:
        return None

    if m.group("cancel"):
        return {"action": "cancel", "index": int(m.group("cancel"))}

    if "[LIST_SCHEDULES]" in m.group(0).upper():
        return {"action": "list"}

    body = m.group("body")
    parts = body.split(":", 1)
    if len(parts) < 2:
        return None

    stype = parts[0].lower()
    rest = parts[1]

    try:
        if stype == "daily":
            time_part, msg = rest.split(":", 1)
            h, mi = int(time_part[:2]), int(time_part[3:5])
            return {"action": "add", "type": "daily", "hour": h, "minute": mi, "message": msg.strip()}

        elif stype == "weekly":
            dow_s, time_part, msg = rest.split(":", 2)
            h, mi = int(time_part[:2]), int(time_part[3:5])
            dow = _DOW_MAP.get(dow_s.lower(), 0)
            return {"action": "add", "type": "weekly", "day_of_week": dow, "hour": h, "minute": mi, "message": msg.strip()}

        elif stype == "interval":
            interval_s, msg = rest.split(":", 1)
            interval_s = interval_s.strip().lower()
            if interval_s.endswith("m"):
                sec = int(interval_s[:-1]) * 60
            elif interval_s.endswith("h"):
                sec = int(interval_s[:-1]) * 3600
            else:
                sec = int(interval_s)
            return {"action": "add", "type": "interval", "interval_sec": sec, "message": msg.strip()}

        elif stype == "once":
            date_s, time_part, msg = rest.split(":", 2)
            h, mi = int(time_part[:2]), int(time_part[3:5])
            run_at = f"{date_s}T{h:02d}:{mi:02d}:00+00:00"
            return {"action": "add", "type": "once", "run_at": run_at, "message": msg.strip()}

    except Exception as e:
        logger.warning("parse_schedule_tag failed for %r: %s", body, e)

    return None


async def add_scheduled_task(
    redis_client,
    bot_name: str,
    user_id: int,
    tag: dict,
) -> str:
    """
    Добавляет задачу в Redis ZSET.
    tag — результат parse_schedule_tag() с action=="add".
    Возвращает task_id.
    """
    task = {
        "id": str(uuid.uuid4())[:8],
        "user_id": user_id,
        **{k: v for k, v in tag.items() if k != "action"},
    }
    next_run = _calc_next_run(task)
    task["next_run"] = next_run
    key = _schedule_key(bot_name, user_id)
    await redis_client.zadd(key, {json.dumps(task, ensure_ascii=False): next_run})
    logger.info("Scheduled task added: %s/%s → %s", bot_name, user_id, task)
    return task["id"]


async def list_scheduled_tasks(
    redis_client,
    bot_name: str,
    user_id: int,
) -> list[dict]:
    """Возвращает список активных задач пользователя."""
    key = _schedule_key(bot_name, user_id)
    raw = await redis_client.zrange(key, 0, -1, withscores=True)
    tasks = []
    for member, score in raw:
        try:
            t = json.loads(member)
            t["next_run"] = score
            tasks.append(t)
        except Exception:
            pass
    return tasks


async def remove_scheduled_task(
    redis_client,
    bot_name: str,
    user_id: int,
    index: int,
) -> bool:
    """Удаляет задачу по порядковому номеру (1-based)."""
    key = _schedule_key(bot_name, user_id)
    raw = await redis_client.zrange(key, 0, -1)
    if not raw or index < 1 or index > len(raw):
        return False
    member = raw[index - 1]
    removed = await redis_client.zrem(key, member)
    return removed > 0


async def format_task_list(tasks: list[dict]) -> str:
    """Форматирует список задач для отправки пользователю."""
    if not tasks:
        return "У тебя нет активных напоминаний."
    lines = ["📋 Твои напоминания:"]
    for i, t in enumerate(tasks, 1):
        next_dt = datetime.fromtimestamp(t["next_run"], tz=timezone.utc)
        next_str = next_dt.strftime("%d.%m %H:%M UTC")
        ttype = t.get("type", "?")
        msg = t.get("message", "")[:60]
        if ttype == "daily":
            desc = f"каждый день в {t['hour']:02d}:{t['minute']:02d} UTC"
        elif ttype == "weekly":
            day_names = ["пн","вт","ср","чт","пт","сб","вс"]
            desc = f"каждый {day_names[t['day_of_week']]} в {t['hour']:02d}:{t['minute']:02d} UTC"
        elif ttype == "interval":
            sec = t.get("interval_sec", 0)
            desc = f"каждые {sec//60} мин"
        elif ttype == "once":
            desc = f"один раз {next_str}"
        else:
            desc = ttype
        lines.append(f"{i}. {desc} — «{msg}»")
    lines.append("\nДля отмены: «отмени напоминание #N»")
    return "\n".join(lines)


async def schedule_loop(
    redis_client,
    bot_name: str,
    bot,  # telegram.Bot instance
    check_interval_sec: int = 60,
) -> None:
    """
    asyncio loop — каждую минуту проверяет ZSET и выполняет просроченные задачи.
    Запускается через asyncio.create_task() при старте бота.

    bot — экземпляр telegram.Bot для отправки сообщений.
    """
    logger.info("schedule_loop started for %s", bot_name)
    while True:
        try:
            now = _time.time()
            # Собираем все ключи для этого бота
            pattern = f"office:schedule:{bot_name}:*"
            keys = []
            async for key in redis_client.scan_iter(pattern):
                keys.append(key)

            for key in keys:
                # Берём все задачи с next_run <= now
                due = await redis_client.zrangebyscore(key, 0, now, withscores=True)
                for member, score in due:
                    try:
                        task = json.loads(member)
                        user_id = task["user_id"]
                        message = task.get("message", "")
                        # Отправляем сообщение
                        await bot.send_message(chat_id=user_id, text=f"🔔 {message}")
                        logger.info("Sent scheduled msg to %s: %s", user_id, message[:50])
                        # Удаляем старую запись
                        await redis_client.zrem(key, member)
                        # Если задача повторяется — добавляем с новым next_run
                        if task.get("type") != "once":
                            next_run = _calc_next_run(task)
                            task["next_run"] = next_run
                            await redis_client.zadd(key, {json.dumps(task, ensure_ascii=False): next_run})
                    except Exception as e:
                        logger.error("schedule_loop task error (%s): %s", bot_name, e)
        except Exception as e:
            logger.error("schedule_loop outer error (%s): %s", bot_name, e)

        await asyncio.sleep(check_interval_sec)

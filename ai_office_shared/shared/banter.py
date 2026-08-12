"""
ai_office_shared.shared.banter — болталка офис-чата (реплики ботов друг другу).

ПОЧЕМУ ЭТО ЖИВЁТ ЗДЕСЬ, А НЕ В ХЕНДЛЕРАХ БОТОВ:
    Telegram НЕ доставляет боту сообщения других ботов. Поэтому весь код вида
    «если отправитель бот — ответить с шансом N%», который был написан в
    billy/kriss/gosling/villy/doctor/milly/tilly/prophet, никогда не исполнялся:
    апдейт до бота просто не доходит. Отсюда жалоба «Гослинг вообще не отвечает
    Билли» — ветка `is_billy → отвечаем всегда` мёртвая.

    Единственный работающий канал — HTTP: кто-то должен ПОЗВАТЬ бота по /task.
    Оркестрацию делает Филли после того, как основной агент ответил в группе.
    Раньше эта логика лежала внутри filly-bot одной функцией `_banter_fanout` и
    текла в трёх местах: Милли и Тилли реплику генерировали и молча выбрасывали
    (их /task постил в группу только при notify=True), Пророка в пуле не было, а
    счётчика глубины не существовало вовсе — «перекинуться 1-2 фразами и
    затихнуть» реализовать было нечем.

КОНТРАКТ /task ДЛЯ БОЛТАЛКИ:
    {"message": ..., "user_id": ..., "group_ctx": ..., "source": "BANTER",
     "sender": "<кто спровоцировал>", "depth": <1..BANTER_MAX_DEPTH>}
    Принимающий бот обязан: ответить КОРОТКО и запостить в группу сам
    (source=BANTER игнорирует флаг notify).

Использование:
    from ai_office_shared.shared.banter import fanout
    spawn(fanout(redis, primary_agent="БИЛЛИ", trigger_text=msg,
                 group_ctx=ctx, sender="Влад"))
"""
from __future__ import annotations

import asyncio
import logging
import os
import random

import httpx

from .identity import canonical, route_key, url as bot_url, who_is

logger = logging.getLogger("ai_office_shared.banter")

# Шанс, что после ответа основного агента кто-то ещё вставит реплику.
BANTER_CHANCE = float(os.environ.get("BANTER_CHANCE", "0.35"))

# «Перекинутся 1-2 репликой и затихнуть» — буквально это число.
# depth=1 — первая волна реплик; depth=2 — ответ на реплику; дальше тишина.
BANTER_MAX_DEPTH = int(os.environ.get("BANTER_MAX_DEPTH", "2"))

# Кого зовём. Решение Влада 2026-08-12: офисное ядро + Пророк.
# Эллис не участвует (семейный контекст в общей группе) и Силли тоже
# (её реплики путались бы с алертами о падениях).
BANTER_POOL: list[str] = ["БИЛЛИ", "КРИС", "ГОСЛИНГ", "МИЛЛИ",
                          "ВИЛЛИ", "ТИЛЛИ", "ДИЛЛИ", "ПРОРОК"]

BANTER_PROMPT = (
    "[Болталка офис-чата] Это не задача и не вопрос к тебе — просто живой чат "
    "коллег. Кинь ОДНУ короткую реплику (одна строка) в своём характере: можно "
    "поддеть, съязвить, согласиться. Без приветствий, без развёрнутых ответов, "
    "не повторяй уже сказанное. Если добавить нечего — ответь одним словом."
)

# Дедуп нити: кто уже вставил реплику в текущий всплеск разговора.
_THREAD_KEY = "office:banter:thread"
_THREAD_TTL = 300  # 5 минут — всплеск закончился, можно чирикать заново


async def _thread_members(redis_client, thread_id: str) -> set[str]:
    if redis_client is None:
        return set()
    try:
        raw = await redis_client.smembers(f"{_THREAD_KEY}:{thread_id}")
        return {r.decode() if isinstance(r, bytes) else r for r in (raw or set())}
    except Exception:
        return set()


async def _thread_mark(redis_client, thread_id: str, agents: list[str]) -> None:
    if redis_client is None or not agents:
        return
    try:
        key = f"{_THREAD_KEY}:{thread_id}"
        await redis_client.sadd(key, *agents)
        await redis_client.expire(key, _THREAD_TTL)
    except Exception:
        pass


def _norm(agent: str) -> str | None:
    """Любое написание → route_key, которым бот адресуется по HTTP."""
    canon = canonical(agent)
    return route_key(canon) if canon else None


async def pick(
    redis_client,
    primary_agent: str,
    thread_id: str = "office",
    pool: list[str] | None = None,
    limit: int | None = None,
    health_check=None,
) -> list[str]:
    """
    Кого позвать в этот раз. Вынесено отдельно от отправки, чтобы это можно было
    протестировать без сети.

    Исключаются: сам основной агент, боты без URL, те кто уже говорил в этой
    нити, и (если передан health_check) отмеченные как down.
    """
    primary = _norm(primary_agent)
    candidates = []
    for name in (pool if pool is not None else BANTER_POOL):
        key = _norm(name)
        if not key or key == primary:
            continue
        if not bot_url(key):
            continue
        candidates.append(key)

    already = await _thread_members(redis_client, thread_id)
    candidates = [c for c in candidates if c not in already]

    if health_check is not None:
        alive = []
        for c in candidates:
            try:
                if (await health_check(c)) != "down":
                    alive.append(c)
            except Exception:
                alive.append(c)  # нет данных — считаем живым, пусть HTTP решит
        candidates = alive

    if not candidates:
        return []
    random.shuffle(candidates)
    return candidates[:(limit if limit is not None else random.randint(1, 2))]


async def fanout(
    redis_client,
    primary_agent: str,
    trigger_text: str,
    group_ctx: str = "",
    sender: str = "",
    depth: int = 0,
    user_id: int = 0,
    thread_id: str = "office",
    chance: float | None = None,
    health_check=None,
    pool: list[str] | None = None,
) -> list[str]:
    """
    Fire-and-forget: с шансом BANTER_CHANCE зовём 1–2 живых бота кинуть реплику.

    Args:
        primary_agent: кто только что ответил — его самого не зовём.
        depth:         глубина текущей нити. На depth >= BANTER_MAX_DEPTH
                       фанаут не запускается — это и есть «затихнуть».
        sender:        кто спровоцировал реплику (Влад / имя бота) — уезжает в
                       payload, чтобы отвечающий знал, с кем говорит.
        health_check:  async (route_key) -> "up"/"down"/None. Опционально.

    Returns:
        Список реально пинганутых ботов (пустой — если не сработал шанс,
        некого звать или достигнут потолок глубины).
    """
    try:
        if depth >= BANTER_MAX_DEPTH:
            logger.info(f"[banter] depth {depth} >= {BANTER_MAX_DEPTH} — затихаем")
            return []

        roll = BANTER_CHANCE if chance is None else chance
        if random.random() >= roll:
            return []

        chosen = await pick(redis_client, primary_agent, thread_id=thread_id,
                            pool=pool, health_check=health_check)
        if not chosen:
            return []

        await _thread_mark(redis_client, thread_id, chosen)

        from .auth import office_headers
        sender_line = f"\n\nПоследним говорил: {sender}." if sender else ""
        msg = (f"{BANTER_PROMPT}{sender_line}\n\n"
               f"Последнее в чате: {trigger_text[:200]}")

        pinged = []
        for agent in chosen:
            target = bot_url(agent)
            if not target:
                continue
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as c:
                    await c.post(f"{target}/task", headers=office_headers(), json={
                        "message":   msg,
                        "user_id":   user_id,
                        "group_ctx": group_ctx,
                        "source":    "BANTER",
                        "sender":    sender,
                        "depth":     depth + 1,
                    })
                pinged.append(agent)
                logger.info(f"[banter] pinged {agent} (depth={depth + 1})")
            except Exception as e:
                logger.info(f"[banter] {agent} ping failed: {e}")
            await asyncio.sleep(random.uniform(1.5, 4.0))  # разносим во времени
        return pinged
    except Exception as e:
        logger.warning(f"[banter] fanout error: {e}")
        return []


def is_banter(task_data: dict) -> bool:
    """True если /task пришёл из болталки — ответ должен быть коротким."""
    return str(task_data.get("source", "")).upper() == "BANTER"


def depth_of(task_data: dict) -> int:
    """Глубина нити из payload /task. 0 — обычный запрос, не болталка."""
    try:
        return int(task_data.get("depth", 0) or 0)
    except (TypeError, ValueError):
        return 0


def sender_of(task_data: dict) -> str:
    """
    Кто написал — для префикса в промт. Пусто, если вызывающий не передал.

    Это ровно то поле, отсутствие которого сломало идентификацию 2026-08-07:
    Гослинг получал по HTTP голый текст без автора и называл Билли «Йодкой».
    """
    raw = str(task_data.get("sender", "") or "").strip()
    if not raw or raw.upper() == "HTTP":
        return ""
    # who_is() покрывает и людей, и ботов: «Yodka» → «Влад», «БИЛЛИ» → «Билли».
    return who_is(raw) or raw

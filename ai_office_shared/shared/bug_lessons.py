"""Какие сообщения в Bug Lessons принадлежат одному уроку.

Зачем отдельно: перезаписать ОДИН урок в группе — не то же самое, что
`/migrate_lessons_en confirm`. Тот сносит все сообщения-уроки и постит архив
заново; для одной исправленной записи это 118 удалений, столько же отправок и
перепост записей #105/#106, которые как были русскими, так и останутся.

Задача здесь ровно одна и чисто вычислительная: по списку текстов сообщений
сказать, какие относятся к уроку N. Она живёт в пакете, а не в `coder.py`,
потому что `coder.py` невозможно импортировать в тест.

ДВЕ ЛОВУШКИ, ради которых это не однострочник:

1. `#11` — не `#118`. Совпадение по номеру целиком, а не по подстроке
   (инвариант офиса №7). Без границы `/repost_lesson 11` снёс бы и одиннадцатый
   урок, и сто одиннадцатый, и сто восемнадцатый.
2. Длинный урок уходит НЕСКОЛЬКИМИ сообщениями. Заголовок несёт только первое;
   хвосты опознаются маркером `[2/3]` и тем, что идут подряд за своей головой.
   Удалить одну голову — оставить в группе хвост без начала.
"""

from __future__ import annotations

import json
import logging
import re

from .telegram_text import is_continuation_part

logger = logging.getLogger("ai_office_shared.bug_lessons")

# Публикатор пишет «🐛 Lesson #<id> — <title>»; в группе лежат и старые русские
# «Урок #<id>», отсюда оба слова. `\b` после номера и есть та самая граница.
_HEADER = r"(?:Урок|Lesson)\s*#\s*{id}\b"
_ANY_HEADER = re.compile(r"(?:Урок|Lesson)\s*#\s*\d+\b")


def header_pattern(lesson_id) -> re.Pattern:
    """Регексп заголовка конкретного урока. Отдельно — чтобы был проверяем."""
    return re.compile(_HEADER.format(id=re.escape(str(lesson_id))))


def select_lesson_parts(texts, lesson_id) -> list:
    """Индексы сообщений урока `lesson_id` в списке текстов.

    `texts` — тексты сообщений группы В ХРОНОЛОГИЧЕСКОМ ПОРЯДКЕ (Telegram отдаёт
    новые первыми, так что вызывающий обязан развернуть). Порядок здесь значим:
    хвост принадлежит той голове, за которой он идёт.

    Возвращает индексы, а не тексты: удалять надо по id сообщений, которые знает
    только вызывающий.
    """
    head = header_pattern(lesson_id)
    picked: list = []
    collecting = False
    for i, raw in enumerate(texts or []):
        text = str(raw or "")
        if head.search(text):
            picked.append(i)
            collecting = True
            continue
        # Хвост чужого урока прерывает сбор, даже если сам несёт маркер части.
        if collecting and is_continuation_part(text) and not _ANY_HEADER.search(text):
            picked.append(i)
            continue
        collecting = False
    return picked


# ── Какими сообщениями урок лежит в группе ────────────────────────────────────
# Зачем помнить: перепост через telethon (найти по тексту → удалить → отправить
# заново) 24.08.2026 упёрся в мёртвую сессию — `The key is not registered in the
# system (caused by GetHistoryRequest)`. И даже живой он двигает урок в конец
# ленты, ломая порядок.
#
# Если id сообщений известны, ничего искать и удалять не нужно: Bot API правит
# СВОЙ текст на месте. Порядок не меняется, telethon не участвует, старой копии
# не остаётся. Поэтому публикатор запоминает id сразу при отправке.
#
# Данные — в Redis, а не в git: это состояние чата, а не код (инвариант офиса).

MSGIDS_PREFIX = "office:lesson:msgids"


def msgids_key(lesson_id) -> str:
    return f"{MSGIDS_PREFIX}:{lesson_id}"


async def remember_messages(redis_client, lesson_id, message_ids) -> bool:
    """Запомнить, какими сообщениями урок лежит в группе. Порядок частей значим.

    Fail-silent: не сумели запомнить — перепост просто уйдёт по старому пути.
    """
    ids = [int(m) for m in (message_ids or []) if str(m).lstrip("-").isdigit()]
    if not ids or redis_client is None:
        return False
    try:
        await redis_client.set(msgids_key(lesson_id), json.dumps(ids))
        return True
    except Exception as e:
        logger.warning("remember_messages(#%s) failed: %s", lesson_id, e)
        return False


async def known_messages(redis_client, lesson_id) -> list:
    """id сообщений урока, по порядку частей. Пустой список — не знаем."""
    if redis_client is None:
        return []
    try:
        raw = await redis_client.get(msgids_key(lesson_id))
    except Exception as e:
        logger.warning("known_messages(#%s) failed: %s", lesson_id, e)
        return []
    if not raw:
        return []
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        ids = json.loads(raw)
    except Exception:
        return []
    return [int(m) for m in ids if str(m).lstrip("-").isdigit()]


def edit_plan(known: list, parts: list) -> tuple:
    """(можно_править, причина_отказа) для правки на месте.

    Правка не умеет разбивать одно сообщение на два и не умеет удалять лишние:
    число частей обязано совпасть. Отказ называет ОБА числа — «не совпало» без
    цифр не говорит, что делать.
    """
    if not known:
        return False, "id сообщений в группе неизвестны"
    if len(known) != len(parts):
        return False, (f"урок занимает {len(known)} сообщени(й) в группе, "
                       f"а новый текст — {len(parts)}: правка на месте не делит "
                       f"и не склеивает сообщения")
    return True, ""


async def forget_messages(redis_client, lesson_id) -> bool:
    """Забыть id сообщений урока. Нужно, когда они оказались неверными.

    24.08.2026 в память уехал id из ЛИЧКИ вместо группы: id уникален внутри
    своего чата, и в Bug Lessons его просто нет. Записанный неверный id хуже
    незаписанного — он уводит перепост на путь правки, который обречён.
    """
    if redis_client is None:
        return False
    try:
        await redis_client.delete(msgids_key(lesson_id))
        return True
    except Exception as e:
        logger.warning("forget_messages(#%s) failed: %s", lesson_id, e)
        return False


# Ответы Telegram, означающие «этого сообщения тут нет / править его нельзя».
# Сообщение сервера цитируем как есть (инвариант офиса: провал называет тот,
# кто упал), но реагировать надо по существу — сбросить неверную привязку.
_STALE_EDIT_TELLS = (
    "message to edit not found",
    "message can't be edited",
    "message_id_invalid",
    "chat not found",
)


def stale_link(error_text: str) -> bool:
    """Правка провалилась потому, что привязка неверна, а не из-за текста."""
    low = str(error_text or "").lower()
    return any(t in low for t in _STALE_EDIT_TELLS)

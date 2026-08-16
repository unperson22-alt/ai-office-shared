"""
ai_office_shared.shared.dedup — один ответ на одно сообщение.

ПРОБЛЕМА, которую решаем (16.08.2026):
    У части ботов ДВА независимых входа в офис-группу, и друг о друге они не
    знают:
      1. свой телеграм-хендлер, который берёт слово сам с некоторым шансом
         (у Гослинга HUMAN_REPLY_CHANCE = 0.40, у Пророка
         RANDOM_REPLY_CHANCE_HUMAN = 0.40);
      2. HTTP /task от Филли, когда роутер выбрал этого бота.

    08:41:57 «С добрым утром головы картонные!» пришло Гослингу обоими путями,
    и он ответил ДВАЖДЫ: в 08:42:01 короткой репликой и в 08:42:06 монологом на
    пол-экрана. У каждого пути был свой порог «отвечать ли»; потолка «не
    ответил ли я уже» не было ни у одного. Тот же класс, что алерт без дедупа.

РЕШЕНИЕ:
    SET NX с TTL — одной операцией. Раздельные «проверить» и «занять» оставили
    бы щель ровно того размера, в которую эти два пути и попадают: между
    телеграм-апдейтом и HTTP-вызовом Филли прошло 1.5 секунды.

ПОЧЕМУ КЛЮЧ ПО ТЕКСТУ, А НЕ ПО message_id:
    У HTTP-пути message_id нет вовсе — Филли передаёт только текст. Общий у
    двух путей ровно он, по нему и сходимся. Нормализуем пробелы и регистр:
    телеграм-путь видит оригинал, а payload может приехать с другой раскладкой
    пробелов, и без нормализации замок расщепился бы надвое.

Использование:
    from ai_office_shared.shared.dedup import claim_answer

    # в телеграм-хендлере — ПОСЛЕ броска шанса, иначе несработавший бросок
    # затыкает HTTP-путь, ради которого Филли и звала
    if not await claim_answer(redis_client, "гослинг", text):
        return

    # в handle_task — кроме болталки: её пинг это отдельная реплика,
    # а не второй заход на то же сообщение
    if not is_banter and not await claim_answer(redis_client, "гослинг", message):
        return web.json_response({"status": "ok", "response": "",
                                  "skipped": "duplicate"})
"""
from __future__ import annotations

import hashlib
import logging

from .identity import canonical

logger = logging.getLogger("ai_office_shared.dedup")

KEY_PREFIX = "office:answered"

# Заведомо больше самого долгого ответа: Sonnet с веб-поиском укладывается в
# десятки секунд, но повтор того же текста через три минуты — уже новая реплика
# человека, а не дубль доставки.
DEFAULT_TTL = 180

# Сколько символов текста берём в ключ. Дальше начинается контекст, приклеенный
# вызывающим, а он у двух путей разный.
_KEY_CHARS = 300


def answer_key(bot: str, text: str) -> str:
    """
    Ключ замка. Формат сохранён с версий, живших в gosling-bot и prophet-bot,
    чтобы выкат не разъехался с замками, уже стоящими в Redis.
    """
    name = canonical(bot) or str(bot or "").strip().lower()
    norm = " ".join(str(text or "").split()).lower()[:_KEY_CHARS]
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]
    return f"{KEY_PREFIX}:{name}:{digest}"


async def claim_answer(redis_client, bot: str, text: str,
                       ttl: int = DEFAULT_TTL) -> bool:
    """
    Занять право ответить на это сообщение.

    Returns:
        True  — окно наше, отвечаем.
        False — по этому сообщению уже отвечает другой путь, молчим.

    Fail-open: без Redis, без текста или при ошибке возвращаем True. Болталка и
    реплики в чат не тот путь, ради которого стоит молчать из-за инфраструктуры
    — лучше два ответа, чем ни одного.
    """
    if redis_client is None or not str(text or "").strip():
        return True
    try:
        got = await redis_client.set(answer_key(bot, text), "1", nx=True, ex=ttl)
        return bool(got)
    except Exception as e:
        logger.warning("[dedup] замок недоступен, отвечаю без него: %s", e)
        return True

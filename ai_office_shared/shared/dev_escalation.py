"""
ai_office_shared.shared.dev_escalation — «просят X, инструмента под X нет» → задача в dev-dept.

Зачем: 2026-07-22 Влад попросил Билли дёрнуть отдел разработки, а Билли ответил «у меня
нет рук, ног и корпоративного Slack» и отправил Влада к самому себе. Механизма эскалации
у него не было вообще, а у Криса была полу-случайная кнопка «💡 Запросить функцию»,
которая рисовалась по ключевым словам про задачи (а не тогда, когда бот реально упёрся
в отсутствие возможности) и слала простое сообщение в личку, минуя доску задач.

Здесь механизм один на всех и завязан на канонический taskboard, а не на чат:
  1. Бот эмитит в ответе тег [DEV_FEATURE:описание] — этому его учит DEV_FEATURE_PROMPT_BLOCK.
  2. Бот парсит тег, вырезает его из текста и вызывает request_dev_feature().
  3. Задача ложится на доску (assignee="dev-dept") и переживает рестарт — в отличие от
     сообщения в чат, которое терялось.

ИСПОЛЬЗОВАНИЕ:
    from ai_office_shared.shared.dev_escalation import (
        DEV_FEATURE_PROMPT_BLOCK, parse_dev_feature_tag,
        strip_dev_feature_tag, request_dev_feature,
    )

    SYSTEM_PROMPT = BASE + DEV_FEATURE_PROMPT_BLOCK

    # после process():
    feature = parse_dev_feature_tag(response)
    if feature:
        response = strip_dev_feature_tag(response)
        await request_dev_feature(redis_client, BOT_NAME, user_name, feature, notify=...)

КОНТРАКТ: как весь taskboard — ни одна функция не пробрасывает исключение наружу.
Провал эскалации не должен ронять ответ пользователю.
"""

from __future__ import annotations

import logging
import re
from typing import Awaitable, Callable, Optional

from . import taskboard

logger = logging.getLogger("ai_office_shared.dev_escalation")

DEV_DEPT_ASSIGNEE = "dev-dept"

_TAG_RE = re.compile(r"\[DEV_FEATURE:(?P<body>[^\]]+)\]", re.IGNORECASE)
_STRIP_RE = re.compile(r"\[DEV_FEATURE:[^\]]*\]", re.IGNORECASE)

MAX_DESCRIPTION_LEN = 400


DEV_FEATURE_PROMPT_BLOCK = """

== КОГДА ТЕБЕ НЕ ХВАТАЕТ ВОЗМОЖНОСТИ ==

Железное правило: если пользователь просит сделать что-то, а инструмента под это у тебя
нет — НЕ отшивайся и НЕ отправляй пользователя решать это самому. Ты не говоришь «я не
могу», «у меня нет доступа», «напиши разработчику» — это запрещённые ответы.

Вместо этого добавь в конец ответа тег (он невидим для пользователя):
[DEV_FEATURE:чёткое описание недостающей возможности одним предложением]

И скажи обычным текстом, что передал запрос в отдел разработки.

Тег ставится ТОЛЬКО когда у тебя реально нет механизма под просьбу. Если механизм есть
(напоминания, веб-поиск, чтение картинок, вызов офисных агентов) — просто сделай, тег не нужен.
"""


def parse_dev_feature_tag(text: str) -> Optional[str]:
    """
    Возвращает описание из первого [DEV_FEATURE:...] или None.
    Пустое/пробельное описание игнорируется как битый тег.
    """
    if not text:
        return None
    m = _TAG_RE.search(text)
    if not m:
        return None
    body = (m.group("body") or "").strip()
    if not body:
        return None
    return body[:MAX_DESCRIPTION_LEN]


def strip_dev_feature_tag(text: str) -> str:
    """Убирает все [DEV_FEATURE:...] из текста перед отправкой пользователю."""
    if not text:
        return text
    return _STRIP_RE.sub("", text).strip()


async def request_dev_feature(
    redis_client,
    bot_name: str,
    from_user: str,
    description: str,
    *,
    notify: Optional[Callable[[str], Awaitable[None]]] = None,
) -> Optional[str]:
    """
    Заводит задачу на доработку в dev-dept. Возвращает task_id или None.

    notify — опциональная корутина (текст) -> None для уведомления владельца.
    Ошибка notify не отменяет уже созданную задачу.
    """
    description = (description or "").strip()[:MAX_DESCRIPTION_LEN]
    if not description:
        return None

    title = f"[{bot_name}] доработка по запросу {from_user or 'пользователя'}: {description}"
    task_id = await taskboard.create_task(
        redis_client,
        title,
        created_by=bot_name,
        assignee=DEV_DEPT_ASSIGNEE,
        status="open",
    )
    if task_id:
        logger.info("dev feature requested by %s (%s): %s", bot_name, from_user, description[:80])
    else:
        logger.warning("dev feature NOT persisted (%s): %s", bot_name, description[:80])

    if notify is not None:
        text = (
            f"🛠 Запрос на доработку\n\n"
            f"Бот: {bot_name}\n"
            f"От: {from_user or '—'}\n"
            f"Что нужно: {description}\n"
            f"Задача на доске: {task_id or 'не сохранена (Redis недоступен)'}"
        )
        try:
            await notify(text)
        except Exception as e:
            logger.warning("dev feature notify failed (%s): %s", bot_name, e)

    return task_id

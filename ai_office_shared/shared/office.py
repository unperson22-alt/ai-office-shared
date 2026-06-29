"""
ai_office_shared.shared.office
Реестр агентов AI Office и вспомогательные функции для межбот-взаимодействия.

Использование:
    from ai_office_shared.shared.office import OFFICE_AGENTS, call_office, parse_office_tag

    response = await call_office("ТИЛЛИ", "что с биткоином?", user_id=391077101)
    agent, query = parse_office_tag(llm_response)
"""
import re
import logging
import httpx

logger = logging.getLogger(__name__)

OFFICE_AGENTS: dict[str, dict] = {
    "СИЛЛИ":  {"url": "https://ai-office-shared-production.up.railway.app",
               "desc": "код, автоматизация, технические задачи"},
    "ТИЛЛИ":  {"url": "https://tilly-bot-production.up.railway.app",
               "desc": "крипто, веб-поиск, актуальные данные, новости"},
    "МИЛЛИ":  {"url": "https://milly-bot-production.up.railway.app",
               "desc": "бизнес, монетизация, стратегия"},
    "ДОКТОР": {"url": "https://dilly-bot-production-4a9b.up.railway.app",
               "desc": "здоровье, медицинские советы"},
    "БИЛЛИ":  {"url": "https://billy-bot-production.up.railway.app",
               "desc": "мотивация, жизненные решения"},
    "КРИСС":  {"url": "https://kriss-bot-production.up.railway.app",
               "desc": "личный ассистент Влада, планирование"},
    "ВИЛЛИ":  {"url": "https://villy-bot-production.up.railway.app",
               "desc": "арт-директор, дизайн, визуал"},
    "НЭЛЛИ":  {"url": "https://nelli-bot-production.up.railway.app",
               "desc": "ноготочки, nail-бизнес, контент"},
    "РЭЙ":    {"url": "https://ray-bot-production-d754.up.railway.app",
               "desc": "партнёрки, travel, affiliate"},
    "ПИЛЛИ":  {"url": "https://pilly-bot-production.up.railway.app",
               "desc": "генерация изображений"},
}


async def call_office(
    agent_name: str,
    message: str,
    user_id: int,
    source: str = "BOT",
    timeout: float = 25.0,
) -> str:
    """
    Отправить задачу агенту офиса по имени.

    Returns:
        Ответ агента или пустая строка при ошибке
    """
    info = OFFICE_AGENTS.get(agent_name.upper())
    if not info:
        logger.warning(f"[office] Unknown agent: {agent_name}")
        return ""
    try:
        from .auth import office_headers
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(
                f"{info['url']}/task",
                json={"message": message, "user_id": user_id, "source": source},
                headers=office_headers(),
            )
        if r.status_code == 200:
            return r.json().get("response", "")
        logger.warning(f"[office] {agent_name} returned {r.status_code}")
    except Exception as e:
        logger.warning(f"[office] {agent_name}: {e}")
    return ""


async def instructions_suffix(redis_client, bot_name: str) -> str:
    """
    Рантайм-инструкции тимлида Cilly для бота: читает office:instructions:{canon}
    и возвращает готовый суффикс для системного промпта (или '' если их нет).

    Боты дописывают результат к системному промпту в build_system() — это позволяет
    Cilly менять поведение бота БЕЗ редеплоя (writer — set_bot_instruction в coder.py).
    Fail-silent: при любой ошибке/отсутствии Redis возвращает ''.
    """
    if redis_client is None or not bot_name:
        return ""
    try:
        from .identity import canonical
        canon = canonical(bot_name) or bot_name
        raw = await redis_client.get(f"office:instructions:{canon}")
        if not raw:
            return ""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        raw = raw.strip()
        return f"\n\n[Указания тимлида Cilly — обязательно учитывай]\n{raw}" if raw else ""
    except Exception:
        return ""


def parse_office_tag(text: str) -> tuple[str | None, str | None]:
    """
    Парсит тег [OFFICE:АГЕНТ:запрос] из текста ответа LLM.

    Returns:
        (agent_name_upper, query) или (None, None) если тега нет
    """
    m = re.search(r"\[OFFICE:(\w+):(.+?)\]", text)
    return (m.group(1).upper(), m.group(2).strip()) if m else (None, None)

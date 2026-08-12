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

from .identity import BOTS, canonical, route_key

class _AgentRegistry(dict):
    """
    dict route_key → {url, desc}, который на чтение прощает любое написание имени.

    Итерация отдаёт ровно по одной записи на бота (иначе списки агентов в промтах
    Нэлли/Рэя раздулись бы дублями), а `.get("ДОКТОР")` и `.get("КРИСС")`
    продолжают работать: в тегах [OFFICE:ИМЯ:...] исторически ходят оба
    написания, и вызывающий код в других репо делает именно `.get(name.upper())`.
    """

    def __missing__(self, key):
        canon = canonical(key)
        if canon and canon in BOTS:
            rk = BOTS[canon]["route_key"]
            if rk != key and rk in self:
                return dict.__getitem__(self, rk)
        raise KeyError(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key):
        if dict.__contains__(self, key):
            return True
        canon = canonical(key) if isinstance(key, str) else None
        return bool(canon and dict.__contains__(self, BOTS[canon]["route_key"]))


# OFFICE_AGENTS выводится из identity.BOTS, а не хардкодится вторым списком.
# Раньше это были два независимых словаря (+ третий, BOT_URLS у Филли), и они
# разъехались: КРИС/КРИСС, ДИЛЛИ/ДОКТОР, разные URL Доктора, Гослинга и Пророка
# тут не было вовсе. Теперь адрес и написание имени ровно одно — в identity.
# Филли исключена намеренно: она диспетчер, а не специалист — звать её тегом
# [OFFICE:...] значит гонять сообщение по кругу.
OFFICE_AGENTS: dict[str, dict] = _AgentRegistry({
    meta["route_key"]: {"url": meta["url"], "desc": meta["persona"]}
    for meta in BOTS.values()
    if meta.get("url") and meta.get("role") != "router"
})


def resolve_agent_url(agent_name: str) -> tuple[str | None, str | None]:
    """
    (route_key, url) по любому написанию имени. Прощает КРИСС/КРИС и
    ДОКТОР/ДИЛЛИ — историю двух написаний в тегах [OFFICE:ИМЯ:...] и в BOT_URLS.
    """
    canon = canonical(agent_name)
    if not canon:
        return None, None
    return route_key(canon), BOTS[canon].get("url")


async def call_office(
    agent_name: str,
    message: str,
    user_id: int,
    source: str = "BOT",
    timeout: float = 25.0,
    sender: str = "",
) -> str:
    """
    Отправить задачу агенту офиса по имени.

    Args:
        sender: кто зовёт — display-имя бота или человека. Уезжает в payload,
                чтобы принимающий знал, с кем разговаривает: без этого поля
                бот получал голый текст и додумывал автора (инцидент 2026-08-07,
                Гослинг принял Билли за Влада).

    Returns:
        Ответ агента или пустая строка при ошибке
    """
    key, agent_url = resolve_agent_url(agent_name)
    if not agent_url:
        logger.warning(f"[office] Unknown agent: {agent_name}")
        return ""
    payload = {"message": message, "user_id": user_id, "source": source}
    if sender:
        payload["sender"] = sender
    try:
        from .auth import office_headers
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(
                f"{agent_url}/task",
                json=payload,
                headers=office_headers(),
            )
        if r.status_code == 200:
            return r.json().get("response", "")
        logger.warning(f"[office] {key} returned {r.status_code}")
    except Exception as e:
        logger.warning(f"[office] {key}: {e}")
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

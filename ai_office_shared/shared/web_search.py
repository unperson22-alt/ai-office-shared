"""
ai_office_shared.shared.web_search
Веб-поиск для всех ботов офиса.

Приоритет провайдеров:
  1. Serper (Google Search API) — если есть SERPER_API_KEY в env или Redis
  2. Tavily — если есть TAVILY_API_KEY
  3. Anthropic web_search_20250305 — fallback, всегда доступен

Использование:
    from ai_office_shared.shared.web_search import web_search, web_search_text

    results = await web_search("Airbnb creator program requirements 2025")
    text = await web_search_text("Klook affiliate contacts")
"""

import os
import json
import logging
import httpx

logger = logging.getLogger(__name__)

# ── Anthropic server-side web search tool declaration ────────────────────────
# Используется как tools= в messages.create() — Claude сам вызывает поиск.
# Импортируй эту константу вместо хардкода в каждом боте:
#   from ai_office_shared.shared.web_search import WEB_SEARCH_TOOLS
WEB_SEARCH_TOOLS: list[dict] = [
    {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
]

# Для случаев когда нужно ограничить до 3 (классификаторы, инсайты):
WEB_SEARCH_TOOLS_LIGHT: list[dict] = [
    {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
]

_DEFAULT_N = 5
_TIMEOUT   = 15.0

_REDIS_PROXY = "https://ai-office-shared-production.up.railway.app/redis"
_REDIS_TOKEN = "5245769f-c5db-4d2b-9256-4ce456d4218b"


async def _get_secret(env_name: str, redis_key: str) -> str:
    """Читает секрет из env, потом из Redis."""
    val = os.environ.get(env_name, "").strip()
    if val:
        return val
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(
                _REDIS_PROXY,
                headers={"X-Auth-Token": _REDIS_TOKEN, "Content-Type": "application/json"},
                json={"cmd": "get", "args": [redis_key]},
            )
            if r.status_code == 200:
                return r.json().get("result", "") or ""
    except Exception as e:
        logger.debug(f"[web_search] Redis fallback: {e}")
    return ""


async def _serper(query: str, n: int) -> list[dict]:
    """Google Search через Serper.dev"""
    key = await _get_secret("SERPER_API_KEY", "office:secrets:serper_api_key")
    if not key:
        return []
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": key, "Content-Type": "application/json"},
                json={"q": query, "num": n},
            )
            r.raise_for_status()
        return [
            {"title": i.get("title", ""), "url": i.get("link", ""), "snippet": i.get("snippet", "")}
            for i in r.json().get("organic", [])[:n]
        ]
    except Exception as e:
        logger.warning(f"[web_search] Serper failed: {e}")
        return []


async def _tavily(query: str, n: int) -> list[dict]:
    """Tavily AI Search"""
    key = await _get_secret("TAVILY_API_KEY", "office:secrets:tavily_api_key")
    if not key:
        return []
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                "https://api.tavily.com/search",
                headers={"Content-Type": "application/json"},
                json={"api_key": key, "query": query, "max_results": n},
            )
            r.raise_for_status()
        return [
            {"title": i.get("title", ""), "url": i.get("url", ""), "snippet": i.get("content", "")}
            for i in r.json().get("results", [])[:n]
        ]
    except Exception as e:
        logger.warning(f"[web_search] Tavily failed: {e}")
        return []


async def _anthropic(query: str, n: int) -> list[dict]:
    """Anthropic web_search_20250305 — fallback"""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return []
    try:
        system = (
            "You are a search assistant. Use web_search to find results. "
            "Return top " + str(n) + " results as JSON array: "
            '[{"title":"...","url":"...","snippet":"..."}]. '
            "Output ONLY the JSON array, no other text."
        )
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1024,
                    "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}],
                    "system": system,
                    "messages": [{"role": "user", "content": query}],
                },
            )
            r.raise_for_status()
        for block in r.json().get("content", []):
            if block.get("type") == "text":
                text = block["text"].strip()
                s, e = text.find("["), text.rfind("]") + 1
                if s != -1 and e > s:
                    try:
                        return json.loads(text[s:e])[:n]
                    except json.JSONDecodeError:
                        pass
        return []
    except Exception as e:
        logger.error(f"[web_search] Anthropic fallback failed: {e}")
        return []


async def web_search(query: str, n: int = _DEFAULT_N) -> list[dict]:
    """
    Поиск с автовыбором провайдера: Serper -> Tavily -> Anthropic.
    Возвращает список: [{"title": str, "url": str, "snippet": str}]
    """
    results = await _serper(query, n)
    if results:
        logger.debug(f"[web_search] Serper: {len(results)} results")
        return results
    results = await _tavily(query, n)
    if results:
        logger.debug(f"[web_search] Tavily: {len(results)} results")
        return results
    results = await _anthropic(query, n)
    if results:
        logger.debug(f"[web_search] Anthropic: {len(results)} results")
    return results


async def web_search_text(query: str, n: int = _DEFAULT_N) -> str:
    """
    Результаты поиска как строка для инжекта в промпт.
    """
    results = await web_search(query, n)
    if not results:
        return "Поиск не дал результатов."
    lines = []
    for idx, item in enumerate(results, 1):
        lines.append(f"{idx}. {item.get('title', '')}")
        if item.get("url"):
            lines.append(f"   {item['url']}")
        if item.get("snippet"):
            lines.append(f"   {item['snippet']}")
        lines.append("")
    return "\n".join(lines).strip()

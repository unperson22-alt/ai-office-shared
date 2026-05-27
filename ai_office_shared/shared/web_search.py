"""
ai_office_shared.shared.web_search
Веб-поиск для всех ботов офиса через Anthropic web_search tool.

Использование:
    from ai_office_shared.shared.web_search import web_search, web_search_text

    results = await web_search("Airbnb creator program requirements 2025")
    # → [{"title": ..., "url": ..., "snippet": ...}, ...]

    text = await web_search_text("Klook affiliate contacts")
    # → готовая строка для инжекта в промпт

# ── Резервные провайдеры (не активированы, ключей нет) ───────────────────────
# Когда понадобится дешёвый fallback — раскомментировать нужный блок
# и добавить ключ в env.
#
# BRAVE  → BRAVE_API_KEY  + endpoint api.search.brave.com/res/v1/web/search
# SERPER → SERPER_API_KEY + endpoint google.serper.dev/search
#
# async def _brave(query, n):
#     async with httpx.AsyncClient(timeout=10) as c:
#         r = await c.get(
#             "https://api.search.brave.com/res/v1/web/search",
#             headers={"X-Subscription-Token": os.environ["BRAVE_API_KEY"]},
#             params={"q": query, "count": n},
#         )
#         r.raise_for_status()
#     return [{"title": i["title"], "url": i["url"], "snippet": i.get("description","")}
#             for i in r.json().get("web",{}).get("results",[])[:n]]
#
# async def _serper(query, n):
#     async with httpx.AsyncClient(timeout=10) as c:
#         r = await c.post(
#             "https://google.serper.dev/search",
#             headers={"X-API-KEY": os.environ["SERPER_API_KEY"]},
#             json={"q": query, "num": n},
#         )
#         r.raise_for_status()
#     return [{"title": i["title"], "url": i["link"], "snippet": i.get("snippet","")}
#             for i in r.json().get("organic",[])[:n]]
# ─────────────────────────────────────────────────────────────────────────────
"""

import os
import json
import logging
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

_ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
_DEFAULT_N     = 5
_TIMEOUT       = 30.0


async def web_search(query: str, n: int = _DEFAULT_N) -> list[dict]:
    """
    Поиск через Anthropic web_search_20250305 tool.
    Возвращает список: [{"title": str, "url": str, "snippet": str}]
    """
    if not _ANTHROPIC_KEY:
        logger.error("[web_search] ANTHROPIC_API_KEY not set")
        return []

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         _ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      "claude-haiku-4-5-20251001",
                    "max_tokens": 1024,
                    "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}],
                    "system": (
                        "You are a search assistant. "
                        "Use web_search to find results for the query. "
                        f"Return top {n} results as JSON array: "
                        '[{"title":"...","url":"...","snippet":"..."}]. '
                        "Output ONLY the JSON array, no other text."
                    ),
                    "messages": [{"role": "user", "content": query}],
                },
            )
            r.raise_for_status()

        content = r.json().get("content", [])

        # Собираем текстовые блоки — ищем JSON массив
        for block in content:
            if block.get("type") == "text":
                text = block["text"].strip()
                s, e = text.find("["), text.rfind("]") + 1
                if s != -1 and e > s:
                    try:
                        results = json.loads(text[s:e])
                        return results[:n]
                    except json.JSONDecodeError:
                        pass

        # Если JSON не нашли — парсим tool_result блоки
        for block in content:
            if block.get("type") == "tool_result":
                for sub in block.get("content", []):
                    if sub.get("type") == "text":
                        text = sub["text"].strip()
                        s, e = text.find("["), text.rfind("]") + 1
                        if s != -1 and e > s:
                            try:
                                return json.loads(text[s:e])[:n]
                            except json.JSONDecodeError:
                                pass

        logger.warning("[web_search] No structured results in response")
        return []

    except Exception as ex:
        logger.error(f"[web_search] failed: {ex}")
        return []


async def web_search_text(query: str, n: int = _DEFAULT_N) -> str:
    """
    Возвращает результаты поиска как строку для инжекта в промпт.

    Формат:
    1. Title
       URL
       Snippet
    """
    results = await web_search(query, n)
    if not results:
        return "Поиск не дал результатов."
    lines = []
    for idx, r in enumerate(results, 1):
        lines.append(f"{idx}. {r.get('title','')}")
        if r.get("url"):
            lines.append(f"   {r['url']}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet']}")
        lines.append("")
    return "\n".join(lines).strip()

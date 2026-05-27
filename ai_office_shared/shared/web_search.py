"""
ai_office_shared.shared.web_search
Универсальный веб-поиск для всех ботов офиса.

Провайдеры (выбор через env SEARCH_PROVIDER):
  - brave   → BRAVE_API_KEY   (api.search.brave.com)
  - serper  → SERPER_API_KEY  (google.serper.dev) [default]

Использование:
    from ai_office_shared.shared.web_search import web_search

    results = await web_search("Airbnb creator program requirements 2025")
    # → [{"title": ..., "url": ..., "snippet": ...}, ...]

    # Или сразу получить строку для промпта:
    text = await web_search_text("Klook affiliate program contacts")
    # → "1. Title\nURL\nSnippet\n\n2. ..."
"""

import os
import logging
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

_PROVIDER    = os.environ.get("SEARCH_PROVIDER", "serper").lower()
_BRAVE_KEY   = os.environ.get("BRAVE_API_KEY", "")
_SERPER_KEY  = os.environ.get("SERPER_API_KEY", "")
_TIMEOUT     = 10.0
_DEFAULT_N   = 5


# ── Brave Search ──────────────────────────────────────────────────────────────

async def _brave(query: str, n: int) -> list[dict]:
    if not _BRAVE_KEY:
        raise RuntimeError("BRAVE_API_KEY not set")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": _BRAVE_KEY,
            },
            params={"q": query, "count": n, "safesearch": "moderate"},
        )
        r.raise_for_status()
    items = r.json().get("web", {}).get("results", [])
    return [
        {"title": i.get("title", ""), "url": i.get("url", ""), "snippet": i.get("description", "")}
        for i in items[:n]
    ]


# ── Serper (Google) ───────────────────────────────────────────────────────────

async def _serper(query: str, n: int) -> list[dict]:
    if not _SERPER_KEY:
        raise RuntimeError("SERPER_API_KEY not set")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": _SERPER_KEY, "Content-Type": "application/json"},
            json={"q": query, "num": n},
        )
        r.raise_for_status()
    data = r.json()
    results = []
    for i in data.get("organic", [])[:n]:
        results.append({
            "title":   i.get("title", ""),
            "url":     i.get("link", ""),
            "snippet": i.get("snippet", ""),
        })
    return results


# ── Public API ────────────────────────────────────────────────────────────────

async def web_search(query: str, n: int = _DEFAULT_N) -> list[dict]:
    """
    Выполняет поиск и возвращает список словарей:
    [{"title": str, "url": str, "snippet": str}]

    Провайдер выбирается через env SEARCH_PROVIDER (brave | serper).
    При ошибке провайдера — пробует второй (fallback).
    """
    provider = _PROVIDER

    async def _call(p: str) -> list[dict]:
        if p == "brave":
            return await _brave(query, n)
        return await _serper(query, n)

    try:
        return await _call(provider)
    except Exception as e:
        fallback = "serper" if provider == "brave" else "brave"
        logger.warning(f"[web_search] {provider} failed ({e}), trying {fallback}")
        try:
            return await _call(fallback)
        except Exception as e2:
            logger.error(f"[web_search] both providers failed: {e2}")
            return []


async def web_search_text(query: str, n: int = _DEFAULT_N) -> str:
    """
    Возвращает результаты поиска как текст для инжекта в промпт.

    Формат:
    1. Title
       URL
       Snippet

    2. ...
    """
    results = await web_search(query, n)
    if not results:
        return "Поиск не дал результатов."
    lines = []
    for idx, r in enumerate(results, 1):
        lines.append(f"{idx}. {r['title']}")
        if r["url"]:
            lines.append(f"   {r['url']}")
        if r["snippet"]:
            lines.append(f"   {r['snippet']}")
        lines.append("")
    return "\n".join(lines).strip()

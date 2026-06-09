"""
ai_office_shared.shared.wiki
Краткие справки из Wikipedia для всех ботов офиса (бесплатно, без ключа).

Использование:
    from ai_office_shared.shared.wiki import wiki_summary, wiki_text

    summary = await wiki_summary("Биткоин")
    text = await wiki_text("нейросети")   # поиск + резюме
"""
import logging
import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0


async def wiki_summary(title: str, sentences: int = 3, lang: str = "ru") -> str | None:
    """Краткое резюме статьи по точному названию."""
    base = f"https://{lang}.wikipedia.org/w/api.php"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(base, params={
                "action": "query",
                "prop": "extracts",
                "exsentences": sentences,
                "exintro": True,
                "explaintext": True,
                "titles": title,
                "format": "json",
                "redirects": 1,
            })
            r.raise_for_status()
            pages = r.json().get("query", {}).get("pages", {})
            for page in pages.values():
                if "extract" in page and page["extract"]:
                    return page["extract"].strip()
        return None
    except Exception as e:
        logger.warning(f"[wiki] summary {title}: {e}")
        return None


async def wiki_search(query: str, limit: int = 5, lang: str = "ru") -> list[str]:
    """Поиск статей по запросу. Возвращает список заголовков."""
    base = f"https://{lang}.wikipedia.org/w/api.php"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(base, params={
                "action": "opensearch",
                "search": query,
                "limit": limit,
                "format": "json",
            })
            r.raise_for_status()
            data = r.json()
            return data[1] if len(data) > 1 else []
    except Exception as e:
        logger.warning(f"[wiki] search {query}: {e}")
        return []


async def wiki_text(query: str, sentences: int = 3, lang: str = "ru") -> str:
    """Поиск + резюме в одном вызове. Удобно для инжекта в промпт."""
    titles = await wiki_search(query, limit=1, lang=lang)
    if not titles:
        return f"Wikipedia: статья по запросу '{query}' не найдена."
    summary = await wiki_summary(titles[0], sentences=sentences, lang=lang)
    return summary or f"Wikipedia: не удалось загрузить статью '{titles[0]}'."

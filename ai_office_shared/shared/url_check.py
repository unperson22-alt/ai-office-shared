"""
ai_office_shared.shared.url_check
Проверка живости URL перед отправкой пользователю.
Используется всеми ботами офиса.
"""
import asyncio
import logging
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

# Таймаут на один запрос
_TIMEOUT = 8.0

# Коды которые считаем "живыми"
_OK_CODES = {200, 201, 301, 302, 303, 307, 308}

# User-Agent чтобы не получать 403 от CloudFlare и подобных
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
}


async def check_url(url: str) -> bool:
    """
    Проверяет что URL отвечает и не возвращает 404/410/5xx.
    HEAD-запрос, при ошибке — GET fallback.
    Возвращает True если живой, False если мёртвый.
    """
    if not url or not url.startswith("http"):
        return False
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT,
            follow_redirects=True,
            headers=_HEADERS,
        ) as client:
            # Сначала HEAD — дешевле
            try:
                r = await client.head(url)
                if r.status_code in _OK_CODES:
                    return True
                if r.status_code == 405:
                    # HEAD не поддерживается — пробуем GET
                    r = await client.get(url)
                    return r.status_code in _OK_CODES
                return False
            except httpx.HTTPStatusError:
                return False
    except Exception as e:
        logger.debug(f"url_check failed for {url}: {e}")
        return False


async def filter_live_urls(urls: list[str]) -> list[str]:
    """Параллельно проверяет список URL, возвращает только живые."""
    if not urls:
        return []
    results = await asyncio.gather(*[check_url(u) for u in urls], return_exceptions=True)
    return [u for u, ok in zip(urls, results) if ok is True]


def extract_urls(text: str) -> list[str]:
    """Вытаскивает все http(s) ссылки из текста."""
    import re
    return re.findall(r'https?://[^\s\)\]\,\"\']+', text)


async def verify_text_urls(text: str) -> tuple[str, list[str]]:
    """
    Принимает текст, находит в нём URL, проверяет каждый.
    Возвращает (текст_с_пометками, список_мёртвых_url).

    Живые URL остаются как есть.
    Мёртвые помечаются суффиксом [⚠️ недоступна].
    """
    urls = extract_urls(text)
    if not urls:
        return text, []

    dead = []
    for url in urls:
        alive = await check_url(url)
        if not alive:
            dead.append(url)
            text = text.replace(url, f"{url} [⚠️ недоступна]")

    return text, dead

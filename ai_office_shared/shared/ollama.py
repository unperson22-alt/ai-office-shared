"""
ai_office_shared.shared.ollama — Ollama local LLM fallback.

Единственный источник кода для Ollama-интеграции во всех ботах.
Раньше каждый бот копировал _OllamaResult / _try_ollama — теперь здесь.

Зависимости передаются через env-переменные которые бот читает сам.
Модуль использует httpx.Client (sync) — подходит для sync и async ботов
через run_in_executor или прямой вызов в sync-контексте.

ИСПОЛЬЗОВАНИЕ (в боте):
    from ai_office_shared.shared.ollama import try_ollama, OllamaResult

    # Вместо прямого вызова Anthropic:
    result = try_ollama(messages, system=system_prompt)
    if result is None:
        result = anthropic_client.messages.create(...)

    # result.content[0].text — одинаковый интерфейс с Anthropic

ENV-переменные (бот читает сам):
    OLLAMA_HOST     — URL Ollama сервера (пусто = выключено)
    OLLAMA_MODEL    — имя модели (default: "gemma3:4b")
    OLLAMA_ENABLED  — "1"/"true"/"yes" = включено
"""

from __future__ import annotations

import logging
import os
from types import SimpleNamespace
from typing import Optional

logger = logging.getLogger("ai_office_shared.ollama")


class OllamaResult:
    """Имитирует структуру ответа Anthropic для прозрачного fallback."""
    __slots__ = ("content",)

    def __init__(self, text: str):
        self.content = [SimpleNamespace(text=text)]


def try_ollama(
    messages: list[dict],
    system: Optional[str] = None,
    timeout: float = 20.0,
) -> Optional[OllamaResult]:
    """
    Sync-вызов локальной Ollama. Возвращает OllamaResult или None при любой ошибке.

    None означает: Ollama недоступна → используй Anthropic как fallback.
    Никогда не бросает исключений наружу — fail-silent.

    Args:
        messages: список dict {"role": str, "content": str} (формат Anthropic)
        system: системный промт (опционально)
        timeout: таймаут в секундах

    Returns:
        OllamaResult с .content[0].text или None
    """
    host = os.environ.get("OLLAMA_HOST", "").strip().rstrip("/\\")
    model = os.environ.get("OLLAMA_MODEL", "gemma3:4b")
    enabled = os.environ.get("OLLAMA_ENABLED", "").lower() in ("1", "true", "yes")

    if not (enabled and host):
        return None

    try:
        import httpx  # опциональная зависимость — не тянем если не нужен

        ol_messages = []
        if system:
            ol_messages.append({"role": "system", "content": system})
        for m in messages:
            content = m["content"] if isinstance(m["content"], str) else str(m["content"])
            ol_messages.append({"role": m["role"], "content": content})

        with httpx.Client(timeout=timeout) as cli:
            r = cli.post(
                f"{host}/api/chat",
                json={"model": model, "messages": ol_messages,
                      "stream": False, "keep_alive": "30m"},
            )
            if r.status_code != 200:
                logger.debug("Ollama returned HTTP %d", r.status_code)
                return None
            text = r.json().get("message", {}).get("content", "")
            if text:
                logger.debug("Ollama OK: %d chars", len(text))
                return OllamaResult(text)
            return None

    except ImportError:
        logger.warning("httpx not installed — Ollama unavailable")
        return None
    except Exception as e:
        logger.info("Ollama unavailable, fallback to Anthropic: %s: %s", type(e).__name__, e)
        return None

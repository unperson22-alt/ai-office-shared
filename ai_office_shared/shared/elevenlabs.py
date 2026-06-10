"""
ai_office_shared.shared.elevenlabs
Text-to-Speech через ElevenLabs API для всех ботов офиса.

Использование:
    from ai_office_shared.shared.elevenlabs import text_to_voice, VOICE_IDS

    audio_bytes = await text_to_voice("Привет! Как дела?")
    # → bytes (mp3) или None при ошибке

    # Отправить голосовое в Telegram:
    if audio_bytes:
        await context.bot.send_voice(chat_id, voice=audio_bytes)

ENV-переменные:
    ELEVENLABS_API_KEY — ключ (env или Redis office:secrets:elevenlabs_api_key)
"""

import os
import logging
import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.elevenlabs.io/v1"
_TIMEOUT = 30.0
_REDIS_PROXY = "https://ai-office-shared-production.up.railway.app/redis"
_REDIS_TOKEN = "5245769f-c5db-4d2b-9256-4ce456d4218b"

# Голоса ElevenLabs — можно заменить на свои клонированные
VOICE_IDS = {
    "default":  "21m00Tcm4TlvDq8ikWAM",   # Rachel — нейтральный женский
    "male":     "TxGEqnHWrfWFTfGW9XjX",   # Josh — мужской
    "friendly": "EXAVITQu4vr4xnSDxMaL",   # Bella — дружелюбный
}

DEFAULT_VOICE = "default"
DEFAULT_MODEL = "eleven_multilingual_v2"   # поддерживает русский


async def _get_api_key() -> str:
    """Читает ELEVENLABS_API_KEY из env или Redis."""
    val = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if val:
        return val
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(
                _REDIS_PROXY,
                headers={"X-Auth-Token": _REDIS_TOKEN, "Content-Type": "application/json"},
                json={"cmd": "get", "args": ["office:secrets:elevenlabs_api_key"]},
            )
            if r.status_code == 200:
                return r.json().get("result", "") or ""
    except Exception as e:
        logger.debug(f"[elevenlabs] Redis fallback: {e}")
    return ""


async def text_to_voice(
    text: str,
    voice: str = DEFAULT_VOICE,
    model: str = DEFAULT_MODEL,
) -> bytes | None:
    """
    Синтез речи из текста.

    Args:
        text: текст для синтеза
        voice: ключ из VOICE_IDS или прямой voice_id ElevenLabs
        model: модель (default: eleven_multilingual_v2)

    Returns:
        bytes (mp3) при успехе или None при ошибке
    """
    api_key = await _get_api_key()
    if not api_key:
        logger.error("[elevenlabs] ELEVENLABS_API_KEY не задан")
        return None

    voice_id = VOICE_IDS.get(voice, voice)  # поддерживаем и ключ и прямой ID

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                f"{_BASE}/text-to-speech/{voice_id}",
                headers={
                    "xi-api-key": api_key,
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg",
                },
                json={
                    "text": text,
                    "model_id": model,
                    "voice_settings": {
                        "stability": 0.5,
                        "similarity_boost": 0.75,
                    },
                },
            )
            if r.status_code == 200:
                return r.content
            logger.warning(f"[elevenlabs] HTTP {r.status_code}: {r.text[:100]}")
            return None
    except Exception as e:
        logger.error(f"[elevenlabs] text_to_voice failed: {e}")
        return None


async def get_remaining_chars() -> int | None:
    """Возвращает остаток символов в текущем плане или None."""
    api_key = await _get_api_key()
    if not api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{_BASE}/user/subscription",
                headers={"xi-api-key": api_key},
            )
            if r.status_code == 200:
                data = r.json()
                limit = data.get("character_limit", 0)
                used = data.get("character_count", 0)
                return limit - used
        return None
    except Exception as e:
        logger.warning(f"[elevenlabs] get_remaining_chars: {e}")
        return None

"""
ai_office_shared.shared.voice
Транскрипция голосовых сообщений через Groq Whisper для всех ботов офиса.

Использование:
    from ai_office_shared.shared.voice import transcribe_voice

    text, err = await transcribe_voice(file_url)
    if err:
        await update.message.reply_text(f"Не смог распознать: {err}")
    else:
        # обрабатываем text как обычное сообщение

ENV-переменные:
    GROQ_API_KEY  — ключ Groq (берётся из env или Redis office:secrets:groq_api_key)
    REDIS_URL     — для fallback чтения ключа из Redis (опционально)
"""
import os
import logging
import httpx

logger = logging.getLogger(__name__)

_GROQ_ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
_MODEL = "whisper-large-v3-turbo"
_LANGUAGE = "ru"


async def _get_groq_key() -> str:
    """Читает GROQ_API_KEY из env, потом из Redis как fallback."""
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if key:
        return key
    try:
        import redis.asyncio as aioredis
        redis_url = os.environ.get("REDIS_URL", "")
        if redis_url:
            r = aioredis.from_url(redis_url, decode_responses=True)
            key = await r.get("office:secrets:groq_api_key") or ""
            await r.aclose()
    except Exception as e:
        logger.debug(f"[voice] Redis fallback failed: {e}")
    return key.strip()


async def transcribe_voice(file_url: str) -> tuple[str | None, str | None]:
    """
    Транскрибирует голосовое сообщение по URL.

    Args:
        file_url: прямой URL файла (Telegram file URL)

    Returns:
        (text, None) при успехе
        (None, error_message) при ошибке
    """
    groq_key = await _get_groq_key()
    if not groq_key:
        return None, "GROQ_API_KEY не задан"

    try:
        async with httpx.AsyncClient(timeout=30) as c:
            dl = await c.get(file_url)
            if dl.status_code != 200:
                return None, f"Telegram HTTP {dl.status_code}"
            audio_data = dl.content
            if len(audio_data) < 100:
                return None, f"Файл слишком мал ({len(audio_data)} байт)"
            r2 = await c.post(
                _GROQ_ENDPOINT,
                headers={"Authorization": f"Bearer {groq_key}"},
                files={"file": ("voice.ogg", audio_data, "audio/ogg")},
                data={"model": _MODEL, "language": _LANGUAGE},
            )
            if r2.status_code != 200:
                return None, f"Groq {r2.status_code}: {r2.text[:80]}"
            text = r2.json().get("text", "").strip()
            return (text, None) if text else (None, "Groq вернул пустой текст")
    except Exception as e:
        logger.error(f"[voice] transcription failed: {e}")
        return None, str(e)

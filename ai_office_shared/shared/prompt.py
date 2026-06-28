"""
ai_office_shared.shared.prompt
Уточнение пользовательского запроса перед отправкой основной модели.

Использование:
    from ai_office_shared.shared.prompt import enhance_prompt

    text = await enhance_prompt(user_text, client)

Каноничная реализация (эталон — Крисс): короткие/бессмысленные фразы возвращаются
дословно, длинный текст не трогается, при любой ошибке возвращается исходный текст
(fail-silent — enhance никогда не должен ронять обработку сообщения).
"""
import logging

from .models import MODEL_HAIKU

logger = logging.getLogger(__name__)

_SYSTEM = (
    "Улучши запрос пользователя — чётче и конкретнее. Если это приветствие, короткий "
    "ответ или фраза без смысловой нагрузки (например: привет, ок, да, нет, спасибо, "
    "понял) — верни текст ДОСЛОВНО без изменений. Верни ТОЛЬКО результат."
)


async def enhance_prompt(text, client, model: str = MODEL_HAIKU, max_len: int = 400) -> str:
    """
    Уточнить короткий запрос пользователя через лёгкую модель.

    Args:
        text:    исходный текст пользователя
        client:  AsyncAnthropic-клиент (messages.create)
        model:   id модели (по умолчанию MODEL_HAIKU)
        max_len: длиннее этого порога текст считается уже конкретным и не трогается

    Returns:
        Уточнённый текст или исходный (при длинном тексте, пустом результате, ошибке).
    """
    if len(text) > max_len:
        return text  # длинный текст уже конкретный — не трогаем
    try:
        r = await client.messages.create(
            model=model,
            max_tokens=200,
            system=_SYSTEM,
            messages=[{"role": "user", "content": text}],
        )
        enhanced = r.content[0].text.strip() if r.content else ""
        return enhanced if enhanced and len(enhanced) > 5 else text
    except Exception:
        return text

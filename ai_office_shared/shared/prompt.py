"""
ai_office_shared.shared.prompt
Уточнение пользовательского запроса перед отправкой основной модели.

Использование:
    from ai_office_shared.shared.prompt import enhance_prompt, enhance_prompt_ex

    text = await enhance_prompt(user_text, client)          # строковый API (legacy)
    res  = await enhance_prompt_ex(user_text, client)        # оригинал + подсказка

ГЛАВНОЕ ПРАВИЛО (инцидент 2026-07-25, Крис ↔ Яна):
Оригинал пользователя — авторитетный вход. Haiku-уточнение НЕ является сообщением
пользователя: это подсказка, которую можно применить только если она прошла проверку
контракта (`_is_safe_enhancement`). Раньше `enhance_prompt` возвращал вывод Haiku без
единой проверки, главная модель никогда не видела слов пользователя и реагировала на
артефакт («система сама собі поставила запитання»). Промпт-заплатка этот класс багов
уже не удержала однажды (kriss-bot/lessons.json #1) — поэтому проверка структурная.

Строковый API намеренно сохранён: боты, которые всё ещё делают
`message = await enhance_prompt(message, ...)`, получают защиту без правки call-site.
"""
import logging
from typing import NamedTuple

from .models import MODEL_HAIKU

logger = logging.getLogger(__name__)

_SYSTEM = (
    "Улучши запрос пользователя — чётче и конкретнее. Если это приветствие, короткий "
    "ответ или фраза без смысловой нагрузки (например: привет, ок, да, нет, спасибо, "
    "понял) — верни текст ДОСЛОВНО без изменений. Верни ТОЛЬКО результат. "
    "Сохраняй язык оригинала. Не переводи, не добавляй вопросов, не комментируй."
)

# Короче этого — уточнять нечего, а ломается чаще всего (обращения: «Кріс», «Билли»).
MIN_ENHANCE_LEN = 40
# Длиннее — текст уже конкретный, не трогаем.
MAX_ENHANCE_LEN = 400

# Коридор длины: уточнение не должно ни схлопывать, ни раздувать смысл.
_LEN_RATIO_MIN = 0.5
_LEN_RATIO_MAX = 3.0

# Буквы, которые есть в украинском и отсутствуют в русском. Их пропажа = перевод.
_UK_MARKERS = set("іїєґ")
# То же в обратную сторону: русские буквы, которых нет в украинском.
_RU_MARKERS = set("ыэъё")

# Мета-текст: Haiku объясняет, что он сделал, вместо того чтобы вернуть результат.
_META_MARKERS = (
    "улучшенн", "уточнённ", "уточненн", "вот улучш", "вот уточн", "вот более",
    "запрос пользователя", "пользователь спрашивает", "пользователь просит",
    "here is", "here's the", "the user", "improved version", "enhanced version",
    "переформулиров", "перефразиров", "як я розумію", "як розумію",
)


class EnhanceResult(NamedTuple):
    """Результат уточнения. `original` всегда авторитетен."""
    original: str
    enhanced: str   # == original, если уточнение отклонено
    used: bool      # применено ли уточнение
    reason: str     # почему применено/отклонено — для логов


def _letter_sets(text: str) -> tuple[set, int, int]:
    """(множество букв в нижнем регистре, кол-во кириллицы, кол-во латиницы)."""
    low = text.lower()
    cyr = sum(1 for ch in low if "Ѐ" <= ch <= "ӿ")
    lat = sum(1 for ch in low if "a" <= ch <= "z")
    return set(low), cyr, lat


def _is_safe_enhancement(original: str, enhanced: str) -> tuple[bool, str]:
    """
    Проверяет, можно ли доверять уточнению.

    Возвращает (ok, reason). Любой провал → используем оригинал.
    Проверки намеренно консервативны: ложно отклонить уточнение дёшево
    (модель получит исходный текст), ложно принять — это инцидент 2026-07-25.
    """
    if not enhanced:
        return False, "empty"
    if enhanced == original:
        return False, "identical"

    o_len, e_len = len(original), len(enhanced)
    if o_len == 0:
        return False, "empty_original"

    ratio = e_len / o_len
    if not (_LEN_RATIO_MIN <= ratio <= _LEN_RATIO_MAX):
        return False, f"length_ratio={ratio:.2f}"

    low = enhanced.lower()
    for marker in _META_MARKERS:
        if marker in low:
            return False, f"meta_marker={marker!r}"

    # Появился вопрос там, где его не было → уточнение задаёт вопрос вместо
    # переформулировки. Ровно так Крис «сам себе задал вопрос».
    if "?" in enhanced and "?" not in original:
        return False, "question_added"

    o_chars, o_cyr, o_lat = _letter_sets(original)
    e_chars, e_cyr, e_lat = _letter_sets(enhanced)

    # Смена языка: украинские/русские маркеры пропали или появились.
    if (o_chars & _UK_MARKERS) and not (e_chars & _UK_MARKERS):
        return False, "uk_markers_lost"
    if (o_chars & _RU_MARKERS) and not (e_chars & _RU_MARKERS) and (e_chars & _UK_MARKERS):
        return False, "ru_to_uk"
    # Кириллица → латиница (или наоборот) как доминирующий алфавит.
    if o_cyr > o_lat and e_lat > e_cyr:
        return False, "script_cyr_to_lat"
    if o_lat > o_cyr and e_cyr > e_lat:
        return False, "script_lat_to_cyr"

    return True, "ok"


def validate_enhancement(original: str, enhanced: str) -> tuple[bool, str]:
    """
    Публичный контракт на вывод enhance-модели: (можно_доверять, причина).

    Для ботов, которые вызывают LLM своим кодом (raw httpx вместо anthropic-клиента)
    и потому не могут использовать enhance_prompt/enhance_prompt_ex напрямую —
    например ray-bot. Правило одно на всех: уточнение применяется, только если
    прошло эту проверку; иначе используется оригинал пользователя.
    """
    return _is_safe_enhancement(original, enhanced)


async def _log_decision(
    redis_client,
    bot_name: str,
    original: str,
    enhanced: str,
    used: bool,
    reason: str,
) -> None:
    """
    Делает подмену наблюдаемой. Раньше в MSG_IN писался оригинал, а модель ела другое —
    поэтому баг был невидим в логах.
    """
    logger.info(
        "enhance_prompt: used=%s reason=%s original=%r enhanced=%r",
        used, reason, original[:120], enhanced[:120],
    )
    if redis_client is None or not bot_name:
        return
    try:
        from .logging import log_event
        await log_event(
            redis_client, bot_name, "prompt_enhanced",
            used=used, reason=reason,
            original=original[:200], enhanced=enhanced[:200],
        )
    except Exception as e:  # наблюдаемость не должна ронять обработку сообщения
        logger.debug("log_event(prompt_enhanced) failed: %s", e)


async def enhance_prompt_ex(
    text: str,
    client,
    model: str = MODEL_HAIKU,
    max_len: int = MAX_ENHANCE_LEN,
    *,
    min_len: int = MIN_ENHANCE_LEN,
    redis_client=None,
    bot_name: str = "",
) -> EnhanceResult:
    """
    Уточнить короткий запрос пользователя через лёгкую модель.

    В отличие от `enhance_prompt`, отдаёт оригинал и уточнение раздельно — вызывающий
    сам решает, куда положить подсказку (рекомендуется: в системный промпт, а в историю
    диалога — всегда оригинал).

    Fail-silent: при любой ошибке возвращает EnhanceResult с used=False.
    """
    original = text or ""

    if len(original) < min_len:
        return EnhanceResult(original, original, False, "too_short")
    if len(original) > max_len:
        return EnhanceResult(original, original, False, "too_long")

    try:
        r = await client.messages.create(
            model=model,
            max_tokens=200,
            system=_SYSTEM,
            messages=[{"role": "user", "content": original}],
        )
        enhanced = r.content[0].text.strip() if r.content else ""
    except Exception as e:
        logger.warning("enhance_prompt call failed: %s", e)
        return EnhanceResult(original, original, False, "api_error")

    ok, reason = _is_safe_enhancement(original, enhanced)
    await _log_decision(redis_client, bot_name, original, enhanced, ok, reason)
    if not ok:
        return EnhanceResult(original, original, False, reason)
    return EnhanceResult(original, enhanced, True, reason)


async def enhance_prompt(
    text: str,
    client,
    model: str = MODEL_HAIKU,
    max_len: int = MAX_ENHANCE_LEN,
    *,
    min_len: int = MIN_ENHANCE_LEN,
    redis_client=None,
    bot_name: str = "",
) -> str:
    """
    Строковый API (обратно совместимый).

    Возвращает уточнённый текст ТОЛЬКО если он прошёл `_is_safe_enhancement`,
    иначе — исходный текст. Никогда не бросает исключение.
    """
    res = await enhance_prompt_ex(
        text, client, model, max_len,
        min_len=min_len, redis_client=redis_client, bot_name=bot_name,
    )
    return res.enhanced


def intent_hint(res: EnhanceResult) -> str:
    """
    Готовая строка-подсказка для системного промпта. Пустая, если уточнения нет.

    Кладётся в СИСТЕМНЫЙ промпт, а не в историю — так модель всегда видит настоящие
    слова пользователя, а уточнение остаётся вторичным и явно помеченным как машинное.
    """
    if not res.used:
        return ""
    return (
        "\n\nУточнение намерения (сгенерировано автоматически, может быть неточным — "
        "приоритет всегда у дословного сообщения пользователя): " + res.enhanced
    )

"""
ai_office_shared.shared.photo_ai — «арт-директор»: модель смотрит на кадр и
назначает параметры обработки.

ЗАЧЕМ. Детерминированный движок (photo_looks) подстраивается под гистограмму,
но не понимает, ЧТО на фото. Он не отличит закат от снятого в полдень портрета,
блюдо в кафе от скриншота, и не знает, что для товарного фото украшения нужен
чистый фон и холодный блик, а для портрета в контровом свете — тёплые света и
мягкая кожа. Модель это видит.

ПРО «СМОТРЕТЬ НА РЕФЕРЕНСЫ ТОПОВЫХ ОБРАБОТОК». Ходить за чужими обработками в
интернет на каждый кадр нельзя: это чужие изображения, поштучный поиск медленный
(единицы секунд на запрос), а «топовость» из выдачи никак не измеряется. Зато
модель уже видела огромный корпус профессионально обработанных фотографий —
знание о том, как выглядит хорошая обработка, у неё внутри. Поэтому референс
здесь не картинка из поиска, а суждение модели о конкретном кадре, выраженное
в тех же числах, которыми работает движок.

ЧТО ВАЖНО: модель НЕ рисует картинку. Она возвращает только числа, а рендер
идёт локально на Pillow в полном разрешении. Поэтому:
  • лицо не «переписывается» нейросетью, как в img2img;
  • качество не режется до 1024px;
  • сбой модели ничего не ломает — откат на детерминированный план.

Все числа модели проходят через клампы (_LIMITS): что бы она ни вернула,
в рендер уйдут значения из допустимого диапазона.

ENV:
    PHOTO_AI_DIRECTOR=1   — включить (по умолчанию выключено: каждый кадр
                            стоит одного запроса к модели)
    ANTHROPIC_API_KEY     — ключ (тот же, что у бота)
    PHOTO_AI_MODEL        — модель, по умолчанию MODEL_SONNET
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re

logger = logging.getLogger("ai_office_shared.photo_ai")

_ENDPOINT = "https://api.anthropic.com/v1/messages"
_TIMEOUT = 40.0
# Больше модели не нужно: параметры считаются по общему виду кадра, а не по
# деталям. 512px — и запрос дешёвый, и тон/композиция видны.
_PREVIEW_SIDE = 512

# Границы, за которые не пустим модель ни при каких обстоятельствах.
_LIMITS = {
    "gamma": (0.70, 1.45),
    "curve": (0.0, 1.2),
    "shoulder": (0.0, 1.0),
    "black": (0.0, 60.0),
    "white": (190.0, 255.0),
    "sat_factor": (0.0, 2.0),
    "clarity": (0.0, 1.5),
    "soften": (0.0, 1.0),
    "sharpen": (0.8, 1.6),
    "fade": (0.0, 0.5),
    "halation": (0.0, 1.0),
    "grain": (0.0, 1.0),
    "vignette": (0.0, 1.0),
}

_SYSTEM = """Ты — ретушёр. Тебе дают фотографию, замеры её гистограммы и базовый
план обработки. Ты возвращаешь ТОЛЬКО JSON с поправками к плану.

Ориентир — как этот кадр обработал бы хороший ретушёр: сюжет, свет, материал.
Портрет в контровом свете, товарное фото украшения, еда, городской пейзаж и
скриншот требуют разного. Учитывай, что уже сделано в базовом плане, и меняй
только то, что действительно улучшит именно этот кадр.

Формат ответа (без пояснений вокруг, только JSON):
{"adjust": {"<параметр>": <число>, ...},
 "split": {"sh": [r,g,b], "hi": [r,g,b], "amount": 0..1},
 "why": "одна короткая фраза по-русски, что и зачем сделал"}

Параметры и смысл:
  gamma      0.70..1.45  >1 темнее, <1 светлее
  curve      0..1.2      сила S-кривой (контраст средних тонов)
  shoulder   0..1        мягкость светов, спасает пересветы
  black      0..60       точка чёрного (0 — глубокий чёрный, 30+ — матовость)
  white      190..255    точка белого
  sat_factor 0..2        насыщенность (1 = без изменений)
  clarity    0..1.5      микроконтраст (на коже держи ниже 0.5)
  soften     0..1        смягчение кожи
  sharpen    0.8..1.6    резкость
  fade       0..0.5      подъём чёрного
  halation   0..1        свечение вокруг ярких участков (плёночный эффект)
  grain      0..1        зерно
  vignette   0..1        затемнение краёв
  split                  тонировка: sh — тени, hi — света, amount — сила

Правила: не перенасыщай кожу; замысел кадра (намеренно светлый или тёмный)
сохраняй; если базовый план уже хорош — верни пустой adjust."""


def director_enabled() -> bool:
    """Включён ли арт-директор. По умолчанию нет — это платные токены."""
    if os.environ.get("PHOTO_AI_DIRECTOR", "").strip() not in ("1", "true", "yes"):
        return False
    return bool(_api_key())


def _api_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def _preview_b64(im) -> str:
    from PIL import Image
    small = im.convert("RGB").copy()
    small.thumbnail((_PREVIEW_SIDE, _PREVIEW_SIDE), Image.LANCZOS)
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()


def _clamp_adjustments(raw: dict) -> dict:
    """
    Оставляет только известные параметры и загоняет их в границы.
    Модель может вернуть что угодно — в рендер это «что угодно» не попадёт.
    """
    out: dict = {}
    for key, value in (raw.get("adjust") or {}).items():
        if key not in _LIMITS:
            continue
        try:
            num = float(value)
        except (TypeError, ValueError):
            continue
        lo, hi = _LIMITS[key]
        out[key] = max(lo, min(hi, num))

    split = raw.get("split")
    if isinstance(split, dict) and "sh" in split and "hi" in split:
        try:
            sh = tuple(max(0, min(255, int(v))) for v in list(split["sh"])[:3])
            hi = tuple(max(0, min(255, int(v))) for v in list(split["hi"])[:3])
            amount = max(0.0, min(0.8, float(split.get("amount", 0.2))))
            if len(sh) == 3 and len(hi) == 3 and amount > 0:
                out["split"] = {"sh": sh, "hi": hi, "amount": amount}
        except (TypeError, ValueError):
            pass
    return out


def _extract_json(text: str) -> dict:
    """Модель иногда оборачивает JSON в текст или ```-блок."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


async def suggest(im, look: str, base_plan: dict, stats, request: str = "",
                  timeout: float = _TIMEOUT) -> tuple[dict, str]:
    """
    Спрашивает модель, что поправить в плане для ЭТОГО кадра.

    Returns:
        (поправки, комментарий) — при любой ошибке ({}, "") и молча работаем
        по детерминированному плану: обработка не должна зависеть от сети.
    """
    key = _api_key()
    if not key:
        return {}, ""
    try:
        import httpx
        from ai_office_shared.shared.models import MODEL_SONNET

        facts = (
            f"Запрос пользователя: {request or 'обработай хорошо'}\n"
            f"Выбранная обработка: {look}\n"
            f"Замеры кадра: средняя яркость {stats.mean:.0f}, "
            f"точка чёрного {stats.p1:.0f}, точка белого {stats.p99:.0f}, "
            f"контраст {stats.contrast:.0f}, насыщенность {stats.saturation:.0f}, "
            f"доля тёмных {stats.shadows:.2f}, доля светлых {stats.highlights:.2f}, "
            f"доля кожи {stats.skin:.2f}, сторона {stats.side}px\n"
            f"Базовый план: {json.dumps(_plain(base_plan), ensure_ascii=False)}"
        )
        payload = {
            "model": os.environ.get("PHOTO_AI_MODEL", MODEL_SONNET),
            "max_tokens": 600,
            "system": _SYSTEM,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64",
                                                 "media_type": "image/jpeg",
                                                 "data": _preview_b64(im)}},
                    {"type": "text", "text": facts},
                ],
            }],
        }
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(_ENDPOINT, headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }, json=payload)
        if r.status_code != 200:
            logger.warning("[photo_ai] HTTP %s: %s", r.status_code, r.text[:150])
            return {}, ""
        blocks = r.json().get("content") or []
        text = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        data = _extract_json(text)
        adjust = _clamp_adjustments(data)
        why = str(data.get("why", ""))[:160]
        if adjust:
            logger.info("[photo_ai] поправки: %s (%s)", adjust, why)
        return adjust, why
    except Exception as e:
        logger.warning("[photo_ai] suggest failed: %s", e)
        return {}, ""


def _plain(plan: dict) -> dict:
    """План без служебных полей — модели их знать незачем."""
    skip = {"in_black", "in_white", "scale", "intent", "pull", "sat", "sat_cap",
            "sat_pull", "target_mean", "bw"}
    return {k: v for k, v in plan.items() if k not in skip}


async def plan_with_director(im, look: str, request: str = "") -> tuple[dict, str]:
    """
    Полный план с учётом мнения модели. Если директор выключен или недоступен —
    возвращает обычный детерминированный план.
    """
    from ai_office_shared.shared import photo_looks

    stats = photo_looks.analyze(im)
    base = photo_looks.plan(look, stats)
    if not director_enabled():
        return base, ""

    adjust, why = await suggest(im, look, base, stats, request)
    if not adjust:
        return base, ""

    merged = dict(base)
    merged.update(adjust)
    # Ч/Б остаётся ч/б, даже если модель насоветовала насыщенность.
    if base.get("bw"):
        merged["bw"] = base["bw"]
        merged.pop("sat_factor", None)
    return merged, why


def describe(adjust: dict, why: str) -> str:
    """Короткая приписка пользователю: что именно решил директор."""
    if not adjust:
        return ""
    return why or "подобрала параметры под этот кадр"


__all__ = ["director_enabled", "plan_with_director", "suggest", "describe"]

"""
ai_office_shared.shared.imagegen — генерация картинок по тексту для ботов офиса.

ПОЧЕМУ ЭТОТ МОДУЛЬ ПОЯВИЛСЯ. Старая схема (`_generate_replicate` →
`_generate_pollinations`, скопированная по ботам) отдавала «не то, что просили»
и часто вообще ничего. Проверено на живых запросах 2026-08-15:

1. ПРОМПТ УХОДИЛ КАК ЕСТЬ — по-русски и вместе с обращением к боту.
   «Сыш, сгенерируй мне жирную мамашу которая жрёт бургер сидя на унитазе» на
   том же провайдере дал аниме-девочку с котом. Тот же провайдер, тот же
   размер, но запрос по-английски и без мусорных слов — картинка по теме.
   Модели генерации обучены на английских подписях; кириллица для них шум.
2. ЦЕПОЧКА ПРОВАЙДЕРОВ БЫЛА МЁРТВОЙ. Replicate — платный (без токена/кредитов
   тихо возвращал None), Pollinations анонимно отдаёт только модель `sana` и
   душит 1 запросом в 15 секунд → «оба провайдера недоступны».
3. БОТ ОТДАВАЛ ССЫЛКУ, А НЕ ФАЙЛ. `reply_photo(photo=url)` заставляет Telegram
   самому сходить за картинкой; когда провайдер отвечал медленно или 429,
   падало уже на стороне Telegram, и пользователь видел просто ошибку.

Здесь: промпт переводится и достраивается лёгкой моделью, картинка приходит
БАЙТАМИ, а первым провайдером идёт Cloudflare Workers AI с FLUX.1-schnell —
он бесплатный в пределах 10 000 нейронов в день (≈150 картинок 1024×1024) и
на голову выше того, что отдавала анонимная Pollinations.

ИСПОЛЬЗОВАНИЕ:

    from ai_office_shared.shared.imagegen import generate_image

    res = await generate_image("Сыш, нарисуй кота в скафандре")
    if res.error:
        await update.message.reply_text(res.error)      # НИКОГДА не молчим
    else:
        await update.message.reply_photo(photo=res.as_file(), caption=res.caption)

КОНТРАКТ: `generate_image` не бросает исключений — всегда ImageResult, у
которого либо `data`, либо `error` с готовым текстом пользователю.

ENV (все опциональны, модуль деградирует по цепочке):
    CF_ACCOUNT_ID / CF_API_TOKEN — Cloudflare Workers AI (те же, что у photo.py;
                                   читаются из env, иначе из Redis office:secrets)
    IMAGEGEN_CF_MODEL           — модель CF, по умолчанию flux-1-schnell
    IMAGEGEN_STEPS              — шагов у flux-schnell (1..8, по умолчанию 4)
    POLLINATIONS_TOKEN          — токен Pollinations, снимает анонимный троттлинг
    REPLICATE_API_TOKEN         — платный запасной провайдер
    ANTHROPIC_API_KEY           — для перевода/достройки промпта (MODEL_HAIKU)
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import random
import re
import urllib.parse
from typing import NamedTuple, Optional

logger = logging.getLogger("ai_office_shared.imagegen")

_ANTHROPIC = "https://api.anthropic.com/v1/messages"
_CF_RUN = "https://api.cloudflare.com/client/v4/accounts/{acc}/ai/run/{model}"
_POLLINATIONS = "https://image.pollinations.ai/prompt/{prompt}"
_REPLICATE = "https://api.replicate.com/v1/models/black-forest-labs/flux-schnell/predictions"

_CF_TIMEOUT = 90.0
_POLL_TIMEOUT = 120.0
_PROMPT_TIMEOUT = 15.0

# Слова-обёртки, которыми просят картинку. Убираются локально, до модели:
# «Сыш, сгенерируй мне X» → «X». Без этого они уезжают в промпт как часть сюжета.
_STRIP_PATTERNS = [
    r"^(сыш|слушай|слышь|эй|ок|окей|давай|плиз|пожалуйста)[\s,!]+",
    r"^(а\s+)?(можешь|сможешь|можно)\s+",
    r"^(мне\s+)?(нужн[аоы]|надо|хочу)\s+",
    # Глагол + необязательное «мне» + необязательное «картинку»: иначе
    # «Можешь сделать картинку заката» доезжает до модели вместе с «сделать
    # картинку», и она рисует буквально картинку в рамке.
    r"\b(сгенерируй|сгенерировать|сгенерь|генерируй|нарисуй|нарисовать|отрисуй|"
    r"отрисовать|создай|создать|сделай|сделать|покажи|показать|"
    r"draw|generate|create|make|show)\b\s*(мне|нам|пожалуйста|плиз)?\s*"
    r"(картинку|картину|изображение|фото|фотку|пикчу|image|picture)?\s*",
    r"^(картинку|картину|изображение|фото|фотку|пикчу|image|picture)\s+",
    r"[\s,]+(пожалуйста|плиз|please)[\s.!]*$",
]

_PROMPT_SYSTEM = """Ты переводишь просьбу нарисовать картинку в промпт для
генеративной модели изображений.

Правила:
- Отвечай ТОЛЬКО промптом на английском. Без кавычек, без пояснений, без «here is».
- Сохрани сюжет ТОЧНО: кто, что делает, где, в каком стиле. Ничего не выкидывай
  и не смягчай — если просят гротеск, сатиру или абсурд, так и описывай.
- Добавь то, что модель не додумает сама: свет, ракурс, материал, стиль,
  качество съёмки или рисунка. 40-70 слов.
- Никаких служебных слов из исходной просьбы («сгенерируй», «мне нужно»).
- Если в просьбе есть текст, который должен быть НА картинке, оставь его в
  кавычках как есть."""


class ImageResult(NamedTuple):
    """Либо data, либо error — третьего не бывает."""
    data: Optional[bytes]
    provider: str = ""       # cloudflare | pollinations | replicate
    prompt: str = ""         # промпт, который реально ушёл в модель
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.data) and not self.error

    @property
    def caption(self) -> str:
        return f"🎨 {self.prompt[:180]}" if self.prompt else "🎨"

    def as_file(self) -> io.BytesIO:
        buf = io.BytesIO(self.data or b"")
        buf.name = "image.png"
        return buf


def clean_request(text: str) -> str:
    """Убирает обращения и командные слова, оставляет сюжет."""
    out = (text or "").strip()
    for pattern in _STRIP_PATTERNS:
        out = re.sub(pattern, " ", out, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", out).strip(" ,.!-") or (text or "").strip()


async def build_prompt(text: str) -> str:
    """
    Русская просьба → английский промпт. Без ключа Anthropic возвращает
    очищенный исходник: хуже, но лучше, чем полное «сгенерируй мне» в модель.
    """
    subject = clean_request(text)
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key or not subject:
        return subject
    try:
        import httpx
        from ai_office_shared.shared.models import MODEL_HAIKU
        async with httpx.AsyncClient(timeout=_PROMPT_TIMEOUT) as c:
            r = await c.post(_ANTHROPIC, headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }, json={
                "model": os.environ.get("IMAGEGEN_PROMPT_MODEL", MODEL_HAIKU),
                "max_tokens": 300,
                "system": _PROMPT_SYSTEM,
                "messages": [{"role": "user", "content": subject}],
            })
        if r.status_code != 200:
            logger.warning("[imagegen] prompt HTTP %s: %s", r.status_code, r.text[:120])
            return subject
        blocks = r.json().get("content") or []
        built = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        return built or subject
    except Exception as e:
        logger.warning("[imagegen] build_prompt failed: %s", e)
        return subject


async def _cf_generate(prompt: str) -> tuple[Optional[bytes], str]:
    """
    Cloudflare Workers AI. Бесплатно 10 000 нейронов в день на аккаунт;
    flux-1-schnell при 4 шагах — примерно 150 картинок 1024×1024 в сутки.

    Ответ у CF бывает двух видов: JSON с base64 в result.image (так отвечает
    flux) и голый image/* (так отвечают sd-модели). Разбираем оба.
    """
    from ai_office_shared.shared.photo import _cf_creds

    acc, tok = await _cf_creds()
    if not (acc and tok):
        return None, "нет ключей Cloudflare"
    model = os.environ.get("IMAGEGEN_CF_MODEL", "@cf/black-forest-labs/flux-1-schnell")
    try:
        steps = int(os.environ.get("IMAGEGEN_STEPS", "4"))
    except ValueError:
        steps = 4
    try:
        import httpx
        url = _CF_RUN.format(acc=acc, model=model)
        async with httpx.AsyncClient(timeout=_CF_TIMEOUT) as c:
            r = await c.post(url, headers={"Authorization": f"Bearer {tok}"},
                             json={"prompt": prompt[:2000],
                                   "steps": max(1, min(8, steps))})
        if r.status_code == 429:
            return None, "дневной лимит Cloudflare исчерпан"
        if r.status_code != 200:
            return None, f"Cloudflare HTTP {r.status_code}"
        if r.headers.get("content-type", "").startswith("image/"):
            return r.content, ""
        payload = r.json()
        if not payload.get("success", True):
            errs = payload.get("errors") or []
            return None, f"Cloudflare: {errs[0].get('message') if errs else 'отказ'}"
        b64 = (payload.get("result") or {}).get("image") or ""
        if not b64:
            return None, "Cloudflare вернул пустой результат"
        return base64.b64decode(b64), ""
    except Exception as e:
        logger.warning("[imagegen] cloudflare failed: %s", e)
        return None, f"Cloudflare: {e}"


async def _pollinations_generate(prompt: str) -> tuple[Optional[bytes], str]:
    """
    Бесплатно и без ключа, но анонимный тир — 1 запрос в 15 секунд и только
    модель sana. Токен в POLLINATIONS_TOKEN снимает троттлинг. Скачиваем
    БАЙТЫ: отдавать ссылку в Telegram нельзя, он идёт за ней сам и падает.
    """
    params = {
        "width": "1024", "height": "1024", "nologo": "true",
        "seed": str(random.randint(1, 10**6)),
        "model": os.environ.get("IMAGEGEN_POLLINATIONS_MODEL", "flux"),
    }
    headers = {}
    token = os.environ.get("POLLINATIONS_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = (_POLLINATIONS.format(prompt=urllib.parse.quote(prompt[:1500]))
           + "?" + urllib.parse.urlencode(params))
    try:
        import httpx
        async with httpx.AsyncClient(timeout=_POLL_TIMEOUT) as c:
            r = await c.get(url, headers=headers)
            if r.status_code == 429:
                # Анонимный тир: одна пауза и одна повторная попытка. Дальше
                # молотить бессмысленно — окно 15 секунд на запрос.
                await asyncio.sleep(16)
                r = await c.get(url, headers=headers)
        if r.status_code != 200:
            return None, f"Pollinations HTTP {r.status_code}"
        if len(r.content) < 2000:
            return None, "Pollinations вернул пустую картинку"
        return r.content, ""
    except Exception as e:
        logger.warning("[imagegen] pollinations failed: %s", e)
        return None, f"Pollinations: {e}"


async def _replicate_generate(prompt: str) -> tuple[Optional[bytes], str]:
    """Платный запасной путь. Работает только если в env лежит живой токен."""
    token = os.environ.get("REPLICATE_API_TOKEN", "").strip()
    if not token:
        return None, "нет токена Replicate"
    try:
        import httpx
        async with httpx.AsyncClient(timeout=_CF_TIMEOUT) as c:
            r = await c.post(_REPLICATE, headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Prefer": "wait",
            }, json={"input": {"prompt": prompt[:2000], "num_outputs": 1,
                               "output_format": "webp"}})
            if r.status_code not in (200, 201):
                return None, f"Replicate HTTP {r.status_code}"
            output = r.json().get("output")
            link = output[0] if isinstance(output, list) and output else output
            if not isinstance(link, str) or not link.startswith("http"):
                return None, "Replicate не вернул ссылку"
            img = await c.get(link)
            if img.status_code != 200:
                return None, f"Replicate download HTTP {img.status_code}"
            return img.content, ""
    except Exception as e:
        logger.warning("[imagegen] replicate failed: %s", e)
        return None, f"Replicate: {e}"


# Порядок принципиальный: сначала лучшее качество из бесплатного (CF FLUX),
# потом безлимитный, но слабый запасной (Pollinations), потом платный.
_PROVIDERS = (
    ("cloudflare", _cf_generate),
    ("pollinations", _pollinations_generate),
    ("replicate", _replicate_generate),
)


async def generate_image(request: str, *, enhance: bool = True) -> ImageResult:
    """
    Рисует картинку по просьбе пользователя.

    Args:
        request: текст как его написал человек («Сыш, нарисуй кота в скафандре»)
        enhance: переводить и достраивать промпт лёгкой моделью

    Returns:
        ImageResult — либо data+prompt, либо error с готовым текстом.
    """
    if not (request or "").strip():
        return ImageResult(None, error="⚠️ Не понял, что рисовать — опиши словами.")

    prompt = await build_prompt(request) if enhance else clean_request(request)
    if not prompt:
        return ImageResult(None, error="⚠️ Не понял, что рисовать — опиши словами.")

    reasons = []
    for name, fn in _PROVIDERS:
        data, why = await fn(prompt)
        if data:
            logger.info("[imagegen] %s ok, %d байт, промпт: %s", name, len(data), prompt[:120])
            return ImageResult(data, provider=name, prompt=prompt)
        reasons.append(f"{name}: {why}")

    logger.warning("[imagegen] все провайдеры отказали — %s", "; ".join(reasons))
    return ImageResult(None, prompt=prompt, error=(
        "❌ Не получилось нарисовать. " + _explain(reasons)))


def _explain(reasons: list[str]) -> str:
    """Человеческая причина вместо «оба провайдера недоступны»."""
    joined = " ".join(reasons).lower()
    if "лимит" in joined:
        return "Дневной бесплатный лимит генерации исчерпан — он обнуляется в 03:00 по Киеву."
    if "нет ключей cloudflare" in joined:
        return ("Генерация не настроена: нет CF_ACCOUNT_ID / CF_API_TOKEN. "
                "Скажи Владу — это переменные Railway.")
    return "Провайдеры не ответили, попробуй ещё раз через минуту."

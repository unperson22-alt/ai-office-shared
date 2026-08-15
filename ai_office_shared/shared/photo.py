"""
ai_office_shared.shared.photo — обработка фотографий для всех ботов офиса.

Зачем: Инстаграм режет обработку по лимиту в день. Здесь лимита нет — основной
движок локальный (Pillow), работает в контейнере бота, ничего никуда не шлёт и
не стоит денег. Внешние API подключаются опционально, поверх, и НИКОГДА не
являются условием работы: если ключа нет или провайдер лёг — бот всё равно
обработает фото локально и скажет, что именно сделал.

ИСПОЛЬЗОВАНИЕ (в хендлере бота, вместе с media.extract_image):

    from ai_office_shared.shared.media import IMAGE_FILTER, extract_image, ImageError
    from ai_office_shared.shared.photo import process_photo, PHOTO_HELP

    img = await extract_image(update.message, context.bot)
    if isinstance(img, ImageError):
        await update.message.reply_text(img.user_message)
        return
    res = await process_photo(base64.b64decode(img.b64), img.caption)
    if res.error:
        await update.message.reply_text(res.error)      # НИКОГДА не молчим
        return
    await update.message.reply_document(res.as_file())  # document = без пережатия

КОНТРАКТ (тот же, что у media.extract_image): `process_photo` не бросает
исключений — всегда PhotoResult, у которого либо `data`, либо `error` с готовым
текстом пользователю.

ENV-переменные (все опциональны):
    PHOTO_REMBG=0        — выключить работу с фоном. По умолчанию она включена,
                           если в окружении есть пакет rembg — отдельного флага
                           «включить» не нужно, ставится он не всем ботам
    PHOTO_REMBG_MODEL    — модель rembg: u2netp (по умолчанию, 4.5 МБ, ~530 МБ RSS)
                           или u2net (176 МБ, ~1.1 ГБ RSS, аккуратнее по краям)
    CF_ACCOUNT_ID        — Cloudflare account id  ┐ AI-перерисовка (img2img):
    CF_API_TOKEN         — Cloudflare API token   ┘ 10 000 нейронов/день бесплатно
                           (или Redis: office:secrets:cf_account_id / cf_api_token)
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import re
from typing import NamedTuple, Optional

logger = logging.getLogger("ai_office_shared.photo")

# Телеграм всё равно жмёт photo; document отдаём как есть. 10 МБ — потолок отдачи.
MAX_OUTPUT_BYTES = 10 * 1024 * 1024
# Сторона, до которой ужимаем перед обработкой: 2560 хватает для печати A4,
# а память и время держит в разумных рамках (Railway-контейнер — не воркстанция).
MAX_SIDE = 2560
JPEG_QUALITY = 92

_REDIS_PROXY = "https://ai-office-shared-production.up.railway.app/redis"
# Единственная img2img-модель бесплатного тира Cloudflare (в каталоге помечена
# Beta). Качество — уровня Stable Diffusion 1.5, до нынешних редакторов ей
# далеко, зато 10 000 нейронов/день это сотни картинок. Модель переопределяется
# через CF_IMAGE_MODEL, но схема запроса ниже — из семейства SD 1.5; у моделей
# с другим форматом входа (например flux-2-dev) параметры другие.
_CF_MODEL_IMG2IMG = os.environ.get(
    "CF_IMAGE_MODEL", "@cf/runwayml/stable-diffusion-v1-5-img2img").strip()
_CF_TIMEOUT = 90.0


class PhotoResult(NamedTuple):
    """Результат обработки. Либо data, либо error — третьего не бывает."""
    data: Optional[bytes]
    fmt: str          # "jpeg" | "png"
    op: str           # что сделали — для логов и для подписи
    caption: str      # текст пользователю («сделала ярче, добавила резкости»)
    error: str = ""   # готовый текст пользователю при отказе

    @property
    def ok(self) -> bool:
        return bool(self.data) and not self.error

    def as_file(self) -> tuple[io.BytesIO, str]:
        """(BytesIO, filename) — прямо в reply_document/reply_photo."""
        return io.BytesIO(self.data or b""), f"{self.op}.{'jpg' if self.fmt == 'jpeg' else self.fmt}"


def _err(text: str, op: str = "error") -> PhotoResult:
    return PhotoResult(None, "jpeg", op, "", text)


# ── Пресеты ────────────────────────────────────────────────────────────────────
# Каждый пресет — набор множителей и эффектов. Значения подобраны так, чтобы
# результат был заметен на глаз, но не выглядел «фильтром из 2013-го»:
# brightness/contrast/color/sharpness — множители (1.0 = без изменений),
# tint — (r,g,b) множители по каналам, vignette/grain/fade — сила эффекта 0..1.

PRESETS: dict[str, dict] = {
    "авто":     {"auto": True, "sharpness": 1.25, "color": 1.06,
                 "desc": "автокоррекция: выровняла свет, добавила цвета и резкости"},
    "яркое":    {"brightness": 1.18, "contrast": 1.08, "color": 1.12,
                 "desc": "подняла яркость и насыщенность"},
    "сочное":   {"auto": True, "color": 1.45, "contrast": 1.12, "sharpness": 1.2,
                 "desc": "сочные цвета, контраст, резкость"},
    "нежное":   {"soften": 0.45, "brightness": 1.06, "color": 0.96, "fade": 0.10,
                 "desc": "мягкий портретный свет, кожа ровнее"},
    "портрет":  {"soften": 0.35, "auto": True, "color": 1.08, "sharpness": 1.15,
                 "vignette": 0.18,
                 "desc": "портретная обработка: кожа мягче, лицо в фокусе"},
    "чб":       {"mono": True, "auto": True, "contrast": 1.15, "sharpness": 1.2,
                 "desc": "чёрно-белое с контрастом"},
    "нуар":     {"mono": True, "contrast": 1.45, "brightness": 0.92,
                 "vignette": 0.45, "grain": 0.35,
                 "desc": "нуар: жёсткий контраст, виньетка, зерно"},
    "сепия":    {"tint": (1.20, 1.02, 0.82), "mono": True, "sepia": True,
                 "contrast": 1.06, "desc": "сепия"},
    "винтаж":   {"tint": (1.10, 1.00, 0.90), "fade": 0.22, "grain": 0.25,
                 "vignette": 0.30, "contrast": 0.94,
                 "desc": "винтаж: выцветшие тени, зерно, виньетка"},
    "плёнка":   {"tint": (1.04, 1.00, 1.06), "fade": 0.15, "grain": 0.30,
                 "contrast": 1.10, "color": 1.05,
                 "desc": "плёночный вид: зерно и мягкие тени"},
    "тепло":    {"tint": (1.12, 1.02, 0.90), "brightness": 1.04,
                 "desc": "тёплый оттенок"},
    "холод":    {"tint": (0.92, 1.00, 1.14), "contrast": 1.06,
                 "desc": "холодный оттенок"},
    "матовое":  {"fade": 0.28, "contrast": 0.92, "color": 0.94,
                 "desc": "матовый пастельный вид"},
    "чёткое":   {"clarity": 1.0, "sharpness": 1.35, "contrast": 1.08,
                 "desc": "детали и микроконтраст вытянуты"},
    "драма":    {"clarity": 1.4, "contrast": 1.30, "color": 1.15,
                 "vignette": 0.35, "desc": "драматичный контраст"},
}

# Синонимы к пресетам — как реально пишут в чате.
_PRESET_ALIASES = {
    "авто": ("авто", "автокоррекц", "улучш", "обработ", "почини", "поправь",
             "сделай красив", "enhance", "auto", "фикс"),
    "яркое": ("ярче", "яркое", "яркость", "светлее", "bright"),
    "сочное": ("сочн", "насыщен", "vivid", "hdr", "живее", "цвета ярче"),
    "нежное": ("нежн", "мягч", "мягк", "сгладь", "кожу", "ретушь", "soft"),
    "портрет": ("портрет", "селфи", "лицо", "portrait"),
    "чб": ("чб", "ч/б", "чёрно-бел", "черно-бел", "монохром", "grayscale", "bw"),
    "нуар": ("нуар", "noir", "мрачн"),
    "сепия": ("сепи", "sepia"),
    "винтаж": ("винтаж", "ретро", "старин", "vintage", "retro", "90-е", "90е"),
    "плёнка": ("плёнк", "пленк", "film", "аналог", "кодак", "kodak"),
    "тепло": ("тепл", "warm", "золот"),
    "холод": ("холод", "cold", "cool", "синев"),
    "матовое": ("матов", "пастель", "matte", "приглуш"),
    "чёткое": ("чётк", "четк", "резч", "резкост", "детал", "sharp", "clarity"),
    "драма": ("драма", "драматич", "контраст", "dramatic"),
}

# Операции сверх пресетов.
_OP_ALIASES = {
    "remove_bg": ("убери фон", "удали фон", "убрать фон", "без фона", "вырежи",
                  "remove bg", "no background", "прозрачн фон", "прозрачный фон"),
    "blur_bg":   ("размой фон", "размытый фон", "боке", "bokeh", "blur bg",
                  "фон размыт"),
    "white_bg":  ("белый фон", "фон белым", "на белом", "для документ",
                  "на белый фон"),
    "sticker":   ("стикер", "sticker", "стикерпак"),
    "avatar":    ("аватар", "аватарк", "avatar", "на аву", "профиль"),
    "square":    ("квадрат", "квадратн", "square", "1:1"),
    "story":     ("стори", "story", "9:16", "вертикал"),
    "upscale":   ("увеличь", "апскейл", "upscale", "качество подними", "больше размер",
                  "разрешение"),
    "compress":  ("сожми", "уменьш вес", "compress", "полегче", "меньше весил"),
    "ai":        ("преврати", "перерисуй", "нарисуй", "в стиле", "restyle",
                  "сделай из меня", "измени фон на"),
}

HELP_LINES = [
    "Что умею с фото (пришли картинку и подпиши, что сделать):",
    "• «улучши» / «авто» — автокоррекция света, цвета и резкости",
    "• фильтры: " + ", ".join(f"«{k}»" for k in PRESETS if k != "авто"),
    "• «ярче на 30%», «контраст +20», «насыщенность -15» — точечная подкрутка",
    "• «убери фон» / «размой фон» / «белый фон» — работа с фоном",
    "• «стикер» — 512px PNG для стикерпака, «аватар» — квадрат 640×640",
    "• «квадрат» / «стори» — кроп под Инсту, «увеличь» — апскейл ×2, «сожми» — вес",
    "Без подписи делаю автокоррекцию.",
]
PHOTO_HELP = "\n".join(HELP_LINES)


class PhotoRequest(NamedTuple):
    op: str                 # "preset" | "remove_bg" | ... | "ai"
    preset: str = ""        # ключ PRESETS для op == "preset"
    tweaks: dict = {}       # {"brightness": 1.3, ...} — точечная подкрутка
    prompt: str = ""        # для op == "ai"


# «ярче на 30», «контраст +20», «убавь насыщенность» → множители.
_TWEAK_WORDS = {
    "brightness": ("ярче", "яркост", "светлее", "темнее", "затемни"),
    "contrast": ("контраст",),
    "color": ("насыщен", "сочност", "цветност", "цвета"),
    "sharpness": ("резкост", "резче", "чётче", "четче"),
}
_NEGATIVE_WORDS = ("темнее", "затемни", "убавь", "меньше", "снизь", "приглуши", "-")


def parse_request(text: str) -> PhotoRequest:
    """
    Разбирает подпись к фото в операцию. Никогда не падает: непонятный текст —
    это «авто», потому что молчать или ругаться на живую речь нельзя.
    """
    low = (text or "").strip().lower()
    if not low:
        return PhotoRequest("preset", "авто")

    for op, words in _OP_ALIASES.items():
        if any(w in low for w in words):
            if op == "ai":
                return PhotoRequest("ai", prompt=text.strip())
            return PhotoRequest(op)

    tweaks, tweak_words = _parse_tweaks(low)

    for preset, words in _PRESET_ALIASES.items():
        hits = [w for w in words if w in low]
        if not hits:
            continue
        # «контраст -30» не должен тянуть за собой весь пресет «драма» с
        # виньеткой: если пресет опознан ровно по тому слову, из которого уже
        # вычли подкрутку, — это подкрутка, а не пресет. Сравнение точное:
        # по вхождению подстроки «сочнее цвета» теряло пресет «сочное».
        if tweaks and all(w in tweak_words for w in hits):
            break
        return PhotoRequest("preset", preset, tweaks)

    if tweaks:
        return PhotoRequest("preset", "", tweaks)

    return PhotoRequest("preset", "авто")


def is_photo_request(text: str) -> bool:
    """
    Просят ОБРАБОТАТЬ фото, а не рассказать что на нём?

    Нужно ботам: «убери фон» — работа модуля, «что это за здание?» — вопрос к
    vision-модели. parse_request на любой текст отвечает «авто», поэтому по нему
    одному отличить нельзя — здесь именно проверка на явное совпадение.
    """
    low = (text or "").strip().lower()
    if not low:
        return False
    if any(w in low for words in _OP_ALIASES.values() for w in words):
        return True
    if any(w in low for words in _PRESET_ALIASES.values() for w in words):
        return True
    return bool(_parse_tweaks(low)[0])


def _parse_tweaks(low: str) -> tuple[dict, set]:
    """
    «ярче на 30%» → {'brightness': 1.3}; «контраст -20» → {'contrast': 0.8}.

    Вторым значением — какие именно слова сработали: parse_request по ним
    отличает подкрутку от пресета.
    """
    out: dict[str, float] = {}
    seen: set[str] = set()
    for key, words in _TWEAK_WORDS.items():
        for w in words:
            pos = low.find(w)
            if pos < 0:
                continue
            window = low[max(0, pos - 20): pos + 30]
            m = re.search(r"([+-]?\d{1,3})\s*%?", window)
            pct = int(m.group(1)) if m else 25
            pct = max(-90, min(300, pct))
            negative = any(n in window for n in _NEGATIVE_WORDS) or pct < 0
            factor = 1.0 - abs(pct) / 100.0 if negative else 1.0 + abs(pct) / 100.0
            out[key] = max(0.1, factor)
            seen.add(w)
            break
    return out, seen


# ── Локальный движок (Pillow) ──────────────────────────────────────────────────

def _pil():
    """Ленивый импорт Pillow: shared ставится и в окружения без него."""
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    return Image, ImageEnhance, ImageFilter, ImageOps


def _open(raw: bytes):
    """Открывает картинку, чинит EXIF-поворот, ужимает до MAX_SIDE, гасит альфу."""
    Image, _, _, ImageOps = _pil()
    im = Image.open(io.BytesIO(raw))
    im = ImageOps.exif_transpose(im)          # телефонные фото приходят «лёжа»
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
    if max(im.size) > MAX_SIDE:
        im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    return im


def _encode(im, fmt: str = "jpeg") -> bytes:
    """В байты. JPEG — без EXIF (метаданные фото наружу не утекают)."""
    Image, _, _, _ = _pil()
    buf = io.BytesIO()
    if fmt == "png":
        im.save(buf, "PNG", optimize=True)
    else:
        if im.mode == "RGBA":
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        im.convert("RGB").save(buf, "JPEG", quality=JPEG_QUALITY,
                               optimize=True, progressive=True)
    return buf.getvalue()


def _vignette(im, strength: float):
    Image, _, ImageFilter, _ = _pil()
    w, h = im.size
    mask = Image.new("L", (w, h), 0)
    # Радиальный градиент дёшево: эллипс + сильное размытие.
    from PIL import ImageDraw
    ImageDraw.Draw(mask).ellipse(
        (-w * 0.15, -h * 0.15, w * 1.15, h * 1.15), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(min(w, h) * 0.18))
    dark = Image.new("RGB", (w, h), (0, 0, 0))
    return Image.composite(im.convert("RGB"), Image.blend(im.convert("RGB"), dark,
                                                          min(0.9, strength)), mask)


def _grain(im, strength: float):
    """
    Зерно через overlay, а НЕ через blend с серым шумом: blend подмешивает
    нейтральный 128-й серый и вымывает чёрное — «нуар» на проверке выходил
    пыльно-серым вместо контрастного. Overlay держит тона на месте.
    """
    from PIL import ImageChops
    Image, _, _, _ = _pil()
    im = im.convert("RGB")
    noise = Image.effect_noise(im.size, 16 + 30 * strength).convert("L")
    grained = ImageChops.overlay(im, Image.merge("RGB", (noise, noise, noise)))
    return Image.blend(im, grained, min(0.55, strength))


def _fade(im, strength: float):
    """Матовые «поднятые» тени — как выцветшая плёнка."""
    Image, _, _, _ = _pil()
    veil = Image.new("RGB", im.size, (226, 222, 214))
    return Image.blend(im.convert("RGB"), veil, min(0.4, strength))


def _soften(im, strength: float):
    """Мягкая кожа: размытый слой поверх, детали глаз/краёв возвращаем резкостью."""
    Image, ImageEnhance, ImageFilter, _ = _pil()
    blurred = im.filter(ImageFilter.GaussianBlur(max(1.2, min(im.size) / 400)))
    mixed = Image.blend(im.convert("RGB"), blurred.convert("RGB"), min(0.7, strength))
    return ImageEnhance.Sharpness(mixed).enhance(1.35)


def _clarity(im, strength: float):
    """
    Микроконтраст. Радиус намеренно маленький и силу гоним через percent, а не
    через радиус. Проверено на контровом портрете (тёмные волосы на светлом
    небе — худший случай для ореолов): radius 6+ даёт светящуюся кайму по
    контуру головы, radius 2–3 при percent 75–90 и threshold 5 вытягивает
    волосы и фактуру ткани без гало.
    """
    _, _, ImageFilter, _ = _pil()
    strength = min(1.5, max(0.1, strength))
    return im.filter(ImageFilter.UnsharpMask(
        radius=2 if strength <= 1.0 else 3,
        percent=int(45 + 30 * strength),
        threshold=5))


def _autocontrast(im, cutoff: int = 1):
    """
    Автоуровни С СОХРАНЕНИЕМ ТОНА. Без preserve_tone Pillow тянет каналы
    независимо и сносит баланс белого: закатный портрет на проверке уезжал
    в синеву — золотой час превращался в пасмурный день. preserve_tone
    появился в Pillow 9.5, на старых версиях откатываемся на обычный режим.
    """
    _, _, _, ImageOps = _pil()
    im = im.convert("RGB")
    try:
        return ImageOps.autocontrast(im, cutoff=cutoff, preserve_tone=True)
    except TypeError:                       # Pillow < 9.5
        return ImageOps.autocontrast(im, cutoff=cutoff)


def _tint(im, rgb: tuple[float, float, float]):
    Image, _, _, _ = _pil()
    r, g, b = im.convert("RGB").split()
    r = r.point(lambda v, m=rgb[0]: min(255, int(v * m)))
    g = g.point(lambda v, m=rgb[1]: min(255, int(v * m)))
    b = b.point(lambda v, m=rgb[2]: min(255, int(v * m)))
    return Image.merge("RGB", (r, g, b))


def apply_preset(raw: bytes, preset: str = "авто", tweaks: Optional[dict] = None) -> bytes:
    """
    Применяет пресет и точечные подкрутки. Синхронная, чистая функция —
    именно её гоняют тесты. Бросает исключения (их ловит process_photo).
    """
    Image, ImageEnhance, _, ImageOps = _pil()
    spec = dict(PRESETS.get(preset, {}))
    spec.update(tweaks or {})

    im = _open(raw)
    if im.mode == "RGBA":                      # эффекты считаем по RGB
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg

    if spec.get("auto"):
        im = _autocontrast(im, cutoff=1)
    if spec.get("mono"):
        im = ImageOps.grayscale(im).convert("RGB")
    if spec.get("sepia"):
        im = _tint(im, (1.18, 1.02, 0.80))
    if spec.get("tint") and not spec.get("sepia"):
        im = _tint(im, spec["tint"])
    if spec.get("soften"):
        im = _soften(im, float(spec["soften"]))
    if spec.get("clarity"):
        im = _clarity(im, float(spec["clarity"]))

    for key, cls in (("brightness", ImageEnhance.Brightness),
                     ("contrast", ImageEnhance.Contrast),
                     ("color", ImageEnhance.Color),
                     ("sharpness", ImageEnhance.Sharpness)):
        if spec.get(key):
            im = cls(im.convert("RGB")).enhance(float(spec[key]))

    if spec.get("fade"):
        im = _fade(im, float(spec["fade"]))
    if spec.get("grain"):
        im = _grain(im, float(spec["grain"]))
    if spec.get("vignette"):
        im = _vignette(im, float(spec["vignette"]))

    return _encode(im, "jpeg")


def crop_to(raw: bytes, shape: str) -> bytes:
    """square (1:1) | story (9:16) | avatar (640×640, квадрат по центру)."""
    Image, _, _, ImageOps = _pil()
    im = _open(raw).convert("RGB")
    if shape == "story":
        target = (1080, 1920)
    elif shape == "avatar":
        target = (640, 640)
    else:
        side = min(im.size)
        target = (side, side)
    return _encode(ImageOps.fit(im, target, Image.LANCZOS, centering=(0.5, 0.4)), "jpeg")


def upscale(raw: bytes, factor: int = 2) -> bytes:
    """Апскейл LANCZOS + микроконтраст. Не нейросеть, но детали не мылит."""
    Image, ImageEnhance, _, _ = _pil()
    im = _open(raw).convert("RGB")
    w, h = im.size
    factor = max(1, min(4, factor))
    if max(w, h) * factor > 4096:
        factor = max(1, 4096 // max(w, h))
    im = im.resize((w * factor, h * factor), Image.LANCZOS)
    im = _clarity(im, 0.8)
    im = ImageEnhance.Sharpness(im).enhance(1.2)
    return _encode(im, "jpeg")


def compress(raw: bytes, target_kb: int = 400) -> bytes:
    """Ужимает под вес: сначала качеством, потом размером. Для почты и чатов."""
    im = _open(raw).convert("RGB")
    for quality in (85, 75, 65, 55, 45):
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
        if buf.tell() <= target_kb * 1024:
            return buf.getvalue()
    Image, _, _, _ = _pil()
    small = im.copy()
    small.thumbnail((1280, 1280), Image.LANCZOS)
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=70, optimize=True, progressive=True)
    return buf.getvalue()


# ── Фон: rembg локально (опционально) ──────────────────────────────────────────

_REMBG_SESSION = None
_REMBG_LOCK = asyncio.Lock()


def rembg_available() -> bool:
    """Включён ли локальный вырез фона (пакет стоит И PHOTO_REMBG не выключен)."""
    if os.environ.get("PHOTO_REMBG", "1").strip() in ("0", "false", "no"):
        return False
    # find_spec, а не import: pyflakes в CI валит job за неиспользуемый импорт,
    # а тащить onnxruntime в память только ради проверки наличия — расточительно.
    from importlib.util import find_spec
    try:
        return find_spec("rembg") is not None
    except Exception:
        return False


def _remove_bg_sync(raw: bytes) -> bytes:
    """PNG с прозрачным фоном. Тяжёлая — зовётся только из потока."""
    global _REMBG_SESSION
    from rembg import new_session, remove
    if _REMBG_SESSION is None:
        model = os.environ.get("PHOTO_REMBG_MODEL", "u2netp").strip() or "u2netp"
        _REMBG_SESSION = new_session(model)
    return remove(raw, session=_REMBG_SESSION)


async def remove_background(raw: bytes) -> bytes:
    """PNG без фона. Модель грузится один раз, инференс — в отдельном потоке."""
    async with _REMBG_LOCK:                 # onnxruntime жрёт память — по одному
        return await asyncio.to_thread(_remove_bg_sync, raw)


def _compose_on(cutout_png: bytes, background) -> bytes:
    Image, _, _, _ = _pil()
    cut = Image.open(io.BytesIO(cutout_png)).convert("RGBA")
    bg = background if hasattr(background, "paste") else Image.new(
        "RGB", cut.size, background)
    bg = bg.convert("RGB").resize(cut.size)
    bg.paste(cut, mask=cut.split()[-1])
    return _encode(bg, "jpeg")


async def blur_background(raw: bytes, radius: int = 14) -> bytes:
    """Эффект портретного режима: объект резкий, фон размыт."""
    _, _, ImageFilter, _ = _pil()
    cutout = await remove_background(raw)
    base = _open(raw).convert("RGB")
    return _compose_on(cutout, base.filter(ImageFilter.GaussianBlur(radius)))


async def make_sticker(raw: bytes) -> bytes:
    """512px PNG с прозрачным фоном — формат стикера Telegram."""
    Image, _, _, _ = _pil()
    data = await remove_background(raw) if rembg_available() else _encode(_open(raw), "png")
    im = Image.open(io.BytesIO(data)).convert("RGBA")
    im.thumbnail((512, 512), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ── AI-перерисовка: Cloudflare Workers AI (10 000 нейронов/день бесплатно) ─────

async def _cf_creds() -> tuple[str, str]:
    """(account_id, token) из env, иначе из Redis — как в elevenlabs.py."""
    acc = os.environ.get("CF_ACCOUNT_ID", "").strip()
    tok = os.environ.get("CF_API_TOKEN", "").strip()
    if acc and tok:
        return acc, tok
    try:
        import httpx
        token = os.environ.get("REDIS_PROXY_TOKEN") or os.environ.get("RAILWAY_TOKEN", "")
        async with httpx.AsyncClient(timeout=5) as c:
            for key, target in (("office:secrets:cf_account_id", "acc"),
                                ("office:secrets:cf_api_token", "tok")):
                r = await c.post(_REDIS_PROXY,
                                 headers={"X-Auth-Token": token,
                                          "Content-Type": "application/json"},
                                 json={"cmd": "get", "args": [key]})
                if r.status_code == 200:
                    val = (r.json().get("result") or "").strip()
                    if target == "acc":
                        acc = acc or val
                    else:
                        tok = tok or val
    except Exception as e:
        logger.debug("[photo] Redis fallback for CF creds: %s", e)
    return acc, tok


async def ai_restyle(raw: bytes, prompt: str, strength: float = 0.55) -> PhotoResult:
    """
    Перерисовка по тексту через Cloudflare Workers AI (Stable Diffusion img2img).

    Бесплатный лимит — 10 000 нейронов в день на аккаунт, это порядка сотен
    картинок 512×512. Ключа нет → честный отказ с подсказкой, а не молчание.
    """
    acc, tok = await _cf_creds()
    if not (acc and tok):
        return _err("🔑 AI-перерисовка не настроена: нет CF_ACCOUNT_ID / CF_API_TOKEN. "
                    "Фильтры и работа с фоном при этом работают.", "ai")
    try:
        import httpx
        # 1024 хватает модели с головой, а вес запроса держит в разумных рамках.
        from PIL import Image
        im = _open(raw).convert("RGB")
        im.thumbnail((1024, 1024), Image.LANCZOS)
        payload = {
            "prompt": prompt[:1500],
            "image_b64": base64.b64encode(_encode(im, "jpeg")).decode(),
            "strength": max(0.1, min(0.95, strength)),
            "guidance": 7.5,
            "num_steps": 20,
        }
        url = f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/run/{_CF_MODEL_IMG2IMG}"
        async with httpx.AsyncClient(timeout=_CF_TIMEOUT) as c:
            r = await c.post(url, headers={"Authorization": f"Bearer {tok}"}, json=payload)
        ctype = r.headers.get("content-type", "")
        if r.status_code == 200 and ctype.startswith("image/"):
            return PhotoResult(r.content, "png", "ai", f"перерисовала: {prompt[:80]}")
        if r.status_code == 429:
            return _err("⏳ Дневной бесплатный лимит AI-перерисовки исчерпан. "
                        "Фильтры и фон работают без лимита — попробуй их.", "ai")
        logger.warning("[photo] CF %s: %s", r.status_code, r.text[:200])
        return _err(f"⚠️ AI-перерисовка не ответила (HTTP {r.status_code}). "
                    "Могу обработать фильтрами — скажи какой.", "ai")
    except Exception as e:
        logger.error("[photo] ai_restyle failed: %s", e)
        return _err("⚠️ AI-перерисовка сорвалась. Фильтры и фон работают — попробуй их.", "ai")


# ── Главный вход ───────────────────────────────────────────────────────────────

async def process_photo(raw: bytes, request: str = "") -> PhotoResult:
    """
    Обрабатывает фото по текстовой просьбе. Не бросает исключений никогда.

    Args:
        raw: байты картинки (например base64.b64decode(ImagePayload.b64))
        request: подпись к фото («сделай ярче», «убери фон», «винтаж»)

    Returns:
        PhotoResult — либо data+caption, либо error с готовым текстом.
    """
    if not raw:
        return _err("⚠️ Пустая картинка — пришли ещё раз.")
    try:
        _pil()
    except Exception:
        return _err("⚠️ На сервере не установлен Pillow — обработка фото недоступна. "
                    "Скажи Силли: добавить pillow в requirements бота.")

    req = parse_request(request)
    try:
        return await _dispatch(raw, req)
    except Exception as e:
        logger.error("[photo] op=%s failed: %s", req.op, e)
        return _err("⚠️ Не смогла обработать эту картинку. "
                    "Попробуй другую или другой фильтр.", req.op)


async def _dispatch(raw: bytes, req: PhotoRequest) -> PhotoResult:
    needs_bg = req.op in ("remove_bg", "blur_bg", "white_bg")
    if needs_bg and not rembg_available():
        # Не молчим и не падаем: отдаём то, что можем, и говорим почему.
        data = await asyncio.to_thread(apply_preset, raw, "портрет")
        return PhotoResult(data, "jpeg", "preset",
                           "Вырезать фон здесь пока не могу (не подключён модуль rembg) — "
                           "сделала портретную обработку. Скажи Силли добавить "
                           "rembg[cpu] в requirements этого бота.")

    if req.op == "remove_bg":
        return PhotoResult(await remove_background(raw), "png", "remove_bg",
                           "убрала фон — прозрачный PNG")
    if req.op == "blur_bg":
        return PhotoResult(await blur_background(raw), "jpeg", "blur_bg",
                           "размыла фон — эффект портретного режима")
    if req.op == "white_bg":
        cut = await remove_background(raw)
        data = await asyncio.to_thread(_compose_on, cut, (255, 255, 255))
        return PhotoResult(data, "jpeg", "white_bg", "поставила белый фон")
    if req.op == "sticker":
        return PhotoResult(await make_sticker(raw), "png", "sticker",
                           "готов стикер 512px — добавляй в @Stickers")
    if req.op in ("square", "story", "avatar"):
        data = await asyncio.to_thread(crop_to, raw, req.op)
        titles = {"square": "квадрат для ленты", "story": "вертикаль под стори",
                  "avatar": "аватарка 640×640"}
        return PhotoResult(data, "jpeg", req.op, titles[req.op])
    if req.op == "upscale":
        return PhotoResult(await asyncio.to_thread(upscale, raw, 2), "jpeg", "upscale",
                           "увеличила ×2 с вытягиванием деталей")
    if req.op == "compress":
        return PhotoResult(await asyncio.to_thread(compress, raw, 400), "jpeg", "compress",
                           "сжала до ~400 КБ без видимой потери")
    if req.op == "ai":
        return await ai_restyle(raw, req.prompt)

    preset = req.preset or "авто"
    data = await asyncio.to_thread(apply_preset, raw, preset, req.tweaks)
    desc = PRESETS.get(preset, {}).get("desc", "подкрутила по твоей просьбе")
    if len(data) > MAX_OUTPUT_BYTES:
        data = await asyncio.to_thread(compress, data, 5000)
    return PhotoResult(data, "jpeg", f"preset:{preset}", desc)

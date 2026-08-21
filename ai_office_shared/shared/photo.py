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
    PHOTO_AI_DIRECTOR=1  — арт-директор: модель смотрит на кадр и назначает
                           параметры обработки (см. photo_ai). Картинку она НЕ
                           рисует — рендер локальный, в полном разрешении.
                           Стоит один запрос к Anthropic на фото, поэтому
                           выключено по умолчанию
    CF_ACCOUNT_ID        — Cloudflare account id  ┐ AI-перерисовка (img2img):
    CF_API_TOKEN         — Cloudflare API token   ┘ 10 000 нейронов/день бесплатно
                           (или Redis: office:secrets:cf_account_id / cf_api_token)

СЛОИ ОБРАБОТКИ (каждый следующий опционален и не ломает предыдущий):
    photo_looks — адаптивные рецепты: числа считаются по гистограмме кадра
    photo_ai    — модель правит эти числа, глядя на сюжет (PHOTO_AI_DIRECTOR=1)
    ai_restyle  — полная перерисовка чужой моделью (Cloudflare, платит нейронами)
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
# Сами обработки живут в photo_looks.LOOKS — там рецепты в терминах цели
# («точка чёрного на 2, S-кривая, ч/б микшером каналов»), а конкретные числа
# считаются под каждый кадр по его гистограмме. Здесь остаются только описания
# для ответа пользователю: ключи обязаны совпадать с LOOKS (есть тест).

PRESETS: dict[str, dict] = {
    "авто":    {"desc": "автокоррекция: выровняла свет, цвет и резкость под этот кадр"},
    "яркое":   {"desc": "подняла яркость и насыщенность"},
    "сочное":  {"desc": "сочные цвета, контраст, резкость"},
    "нежное":  {"desc": "мягкий портретный свет, кожа ровнее"},
    "портрет": {"desc": "портретная обработка: кожа мягче, лицо в фокусе"},
    "чб":      {"desc": "чёрно-белое через красный светофильтр — кожа светлая, небо глубокое"},
    "нуар":    {"desc": "нуар: жёсткий контраст, зерно, виньетка"},
    "сепия":   {"desc": "сепия"},
    "винтаж":  {"desc": "винтаж: выцветшие тени, зерно, засветка"},
    "плёнка":  {"desc": "плёночный вид: зерно, тёплые света, мягкие тени"},
    "тепло":   {"desc": "тёплый оттенок"},
    "холод":   {"desc": "холодный оттенок"},
    "матовое": {"desc": "матовый пастельный вид"},
    "чёткое":  {"desc": "детали и микроконтраст вытянуты"},
    "драма":   {"desc": "драматичный контраст"},
}

# Синонимы к пресетам — как реально пишут в чате.
_PRESET_ALIASES = {
    "авто": ("авто", "автокоррекц", "улучш", "обработ", "почини", "поправь",
             "сделай красив", "enhance", "auto", "фикс",
             "покращ", "оброби", "зроби красив", "підправ"),
    "яркое": ("ярче", "яркое", "яркость", "светлее", "bright",
              "яскрав", "світліше"),
    "сочное": ("сочн", "насыщен", "vivid", "hdr", "живее", "цвета ярче",
               "соковит", "насичен"),
    "нежное": ("нежн", "мягч", "мягк", "сгладь", "soft", "ніжн", "м'якш"),
    "портрет": ("портрет", "селфи", "лицо", "portrait", "селфі", "обличч"),
    "чб": ("чб", "ч/б", "чёрно-бел", "черно-бел", "монохром", "grayscale", "bw",
           "чорно-біл", "чорно біл"),
    "нуар": ("нуар", "noir", "мрачн", "похмур"),
    "сепия": ("сепи", "sepia", "сепі"),
    "винтаж": ("винтаж", "ретро", "старин", "vintage", "retro", "90-е", "90е",
               "вінтаж", "старовин"),
    "плёнка": ("плёнк", "пленк", "film", "аналог", "кодак", "kodak", "плівк"),
    "тепло": ("тепл", "warm", "золот", "тепліш"),
    "холод": ("холод", "cold", "cool", "синев", "холодніш", "синяв"),
    "матовое": ("матов", "пастель", "matte", "приглуш", "матов", "пастельн"),
    "чёткое": ("чётк", "четк", "резч", "резкост", "детал", "sharp", "clarity",
               "чітк", "різкіст", "різкіш"),
    "драма": ("драма", "драматич", "контраст", "dramatic", "драматичн"),
}

# Операции сверх пресетов. Проверяются ДО пресетов — «ретушь» обязана попасть
# в точечную ретушь, а не в смягчающий фильтр «нежное».
#
# Украинские написания здесь не для красоты: Крисс отвечает и Владу, и Яне, а
# Яна пишет по-украински. До 2026-08-21 «прибери недоліки на обличчі» не
# совпадало ни с одним словом — просьба обработать фото уезжала в LLM, и та
# отвечала, что редактировать картинки не умеет (инцидент с ретушью у Крисса).
_OP_ALIASES = {
    "retouch":   ("ретуш", "прыщ", "прищ", "недостатк", "недолік", "недолик",
                  "дефект", "изъян", "вади шкіри", "прибери вади",
                  "убери пятна", "прибери плям", "плями на", "высыпан", "висип",
                  "почисти кожу", "почисть кожу", "очисти кожу", "очисти шкіру",
                  "вычисти кожу", "разгладь кожу", "розгладь шкіру",
                  "убери морщин", "прибери зморшк", "покрасне", "почервонін",
                  "retouch", "blemish", "acne", "skin defect", "clean up skin"),
    "remove_bg": ("убери фон", "удали фон", "убрать фон", "без фона", "вырежи",
                  "remove bg", "no background", "прозрачн фон", "прозрачный фон",
                  "прибери фон", "видали фон", "без фону", "прозорий фон"),
    "blur_bg":   ("размой фон", "размытый фон", "боке", "bokeh", "blur bg",
                  "фон размыт", "розмий фон", "розмитий фон"),
    "white_bg":  ("белый фон", "фон белым", "на белом", "для документ",
                  "на белый фон", "білий фон", "фон білим", "для документ"),
    "sticker":   ("стикер", "sticker", "стикерпак", "стікер"),
    "avatar":    ("аватар", "аватарк", "avatar", "на аву", "профиль", "профіль"),
    "square":    ("квадрат", "квадратн", "square", "1:1"),
    "story":     ("стори", "story", "9:16", "вертикал", "сторіз", "вертикальн"),
    "upscale":   ("увеличь", "апскейл", "upscale", "качество подними", "больше размер",
                  "разрешение", "збільш", "роздільн"),
    "compress":  ("сожми", "уменьш вес", "compress", "полегче", "меньше весил",
                  "стисни", "стиснути", "легше важил"),
    "ai":        ("преврати", "перерисуй", "нарисуй", "в стиле", "restyle",
                  "сделай из меня", "измени фон на", "перетвори", "перемалюй",
                  "у стилі", "зміни фон на"),
}

HELP_LINES = [
    "Что умею с фото (пришли картинку и подпиши, что сделать):",
    "• «улучши» / «авто» — автокоррекция света, цвета и резкости",
    "• фильтры: " + ", ".join(f"«{k}»" for k in PRESETS if k != "авто"),
    "• «ярче на 30%», «контраст +20», «насыщенность -15» — точечная подкрутка",
    "• «ретушь» / «убери прыщи» — точечная ретушь лица: убираю сами пятна, "
    "текстуру кожи, брови и ресницы не трогаю. «Ретушь сильнее» — ещё и "
    "выровнять тон, «чуть-чуть» — вполсилы",
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
    "brightness": ("ярче", "яркост", "светлее", "темнее", "затемни",
                   "яскравіш", "яскравіст", "світліше", "темніше"),
    "contrast": ("контраст",),
    "color": ("насыщен", "сочност", "цветност", "цвета", "насичен", "соковит"),
    "sharpness": ("резкост", "резче", "чётче", "четче", "різкіст", "різкіш",
                  "чіткіш"),
}
_NEGATIVE_WORDS = ("темнее", "затемни", "убавь", "меньше", "снизь", "приглуши", "-",
                   "темніше", "прибери", "менше", "знизь", "зменш")


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
            if op in ("ai", "retouch"):
                # prompt несёт исходный текст: у «ai» это задание модели,
                # у «retouch» — сила («чуть-чуть» / «сильнее»).
                return PhotoRequest(op, prompt=text.strip())
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


def apply_preset(raw: bytes, preset: str = "авто", tweaks: Optional[dict] = None) -> bytes:
    """
    Применяет обработку. Синхронная, чистая функция — именно её гоняют тесты.
    Бросает исключения (их ловит process_photo).

    Числа не зашиты: photo_looks замеряет кадр (гистограмма, тени/света,
    насыщенность, доля кожи) и считает параметры под него. Один и тот же
    «винтаж» на тёмном и на пересвеченном кадре получит разные значения —
    цель у рецепта одна, а исходное состояние разное.

    tweaks («ярче на 40%») применяются ПОСЛЕ обработки и буквально: человек
    попросил конкретную величину, подменять её адаптацией нельзя.
    """
    from ai_office_shared.shared import photo_looks

    im = _flatten(_open(raw))
    look = preset if preset in photo_looks.LOOKS else "авто"
    im = photo_looks.render(im, photo_looks.plan(look, photo_looks.analyze(im)))
    return _finish(im, tweaks)


def _flatten(im):
    """RGBA → RGB на белом: эффекты и кодирование считаем без альфы."""
    Image, _, _, _ = _pil()
    if im.mode != "RGBA":
        return im
    bg = Image.new("RGB", im.size, (255, 255, 255))
    bg.paste(im, mask=im.split()[-1])
    return bg


async def apply_look(raw: bytes, preset: str = "авто",
                     tweaks: Optional[dict] = None, request: str = "") -> tuple[bytes, str]:
    """
    То же, что apply_preset, но с арт-директором: если PHOTO_AI_DIRECTOR=1 и
    есть ключ Anthropic, модель смотрит на кадр и правит параметры под сюжет.

    Директор НЕ рисует картинку — он возвращает только числа, рендер идёт
    локально в полном разрешении. Выключен, недоступен, ответил мусором —
    работаем по детерминированному плану и молчим об этом.

    Returns:
        (jpeg-байты, приписка о том, что решил директор — может быть пустой)
    """
    from ai_office_shared.shared import photo_ai, photo_looks

    im = _flatten(_open(raw))
    look = preset if preset in photo_looks.LOOKS else "авто"
    if not photo_ai.director_enabled():
        return await asyncio.to_thread(apply_preset, raw, look, tweaks), ""

    params, why = await photo_ai.plan_with_director(im, look, request)
    rendered = await asyncio.to_thread(photo_looks.render, im, params)
    return await asyncio.to_thread(_finish, rendered, tweaks), why


def _finish(im, tweaks: Optional[dict] = None) -> bytes:
    """Точечные подкрутки пользователя поверх обработки + кодирование."""
    _, ImageEnhance, _, _ = _pil()
    for key, cls in (("brightness", ImageEnhance.Brightness),
                     ("contrast", ImageEnhance.Contrast),
                     ("color", ImageEnhance.Color),
                     ("sharpness", ImageEnhance.Sharpness)):
        if (tweaks or {}).get(key):
            im = cls(im.convert("RGB")).enhance(float(tweaks[key]))

    return _encode(im, "jpeg")


_RETOUCH_LIGHT = ("чуть", "слегка", "легк", "немного", "трохи", "ледь", "трішки",
                  "мягче", "м\'якше", "light", "subtle")
_RETOUCH_STRONG = ("сильн", "максимальн", "как следует", "получше", "дуже",
                   "якнайкраще", "strong", "hard")


def retouch_strength(text: str) -> float:
    """«чуть-чуть подретушируй» → 0.55, обычная просьба → 1.0."""
    low = (text or "").lower()
    if any(w in low for w in _RETOUCH_LIGHT) and not any(w in low for w in _RETOUCH_STRONG):
        return 0.55
    return 1.0


def wants_even_tone(text: str) -> bool:
    """
    Просят ли ВЫРОВНЯТЬ кожу поверх точечной ретуши.

    По умолчанию ретушь трогает только сами пятна. Общее смягчение кожи —
    отдельная просьба, а не бонус: 21.08.2026 оно шло всегда и съело брови,
    ресницы и бороду на фото Влада.
    """
    low = (text or "").lower()
    return any(w in low for w in (
        "сильн", "гладк", "ровн", "разглад", "розглад", "выровн", "вирівн",
        "смягч", "пом'якш", "помякш", "как в журнал", "glam", "smooth"))


def retouch(raw: bytes, strength: float = 1.0,
            even_tone: bool = False) -> tuple[bytes, str, float]:
    """
    Точечная ретушь лица: убрать дефекты кожи, не тронув глаза, губы и фон.
    Синхронная и чистая — её же гоняют тесты. Сама обработка в photo_retouch.

    Returns:
        (jpeg-байты, что сделали словами, доля кадра, опознанная как кожа)
    """
    from ai_office_shared.shared import photo_retouch

    im = _flatten(_open(raw))
    out, report = photo_retouch.retouch(im, strength, even_tone)
    return _encode(out, "jpeg"), report.note, report.skin


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
    from ai_office_shared.shared import photo_looks
    im = photo_looks._clarity(im, 0.8, max(w, h) * factor / 1200)
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
    if req.op == "retouch":
        data, note, skin = await asyncio.to_thread(
            retouch, raw, retouch_strength(req.prompt), wants_even_tone(req.prompt))
        if skin < 0.02:
            # Кожи в кадре нет — врать «отретушировала» нельзя. Отдаём честную
            # автокоррекцию и говорим, чего не нашли.
            data = await asyncio.to_thread(apply_preset, raw, "авто")
            return PhotoResult(data, "jpeg", "preset:авто",
                               "лица на этом фото не нашла — сделала автокоррекцию. "
                               "Если ретушь нужна именно здесь, пришли кадр, где лицо крупнее")
        return PhotoResult(data, "jpeg", "retouch", note)
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
    data, why = await apply_look(raw, preset, req.tweaks, req.prompt or preset)
    desc = PRESETS.get(preset, {}).get("desc", "подкрутила по твоей просьбе")
    if why:
        desc = f"{desc} · {why}"
    if len(data) > MAX_OUTPUT_BYTES:
        data = await asyncio.to_thread(compress, data, 5000)
    return PhotoResult(data, "jpeg", f"preset:{preset}", desc)

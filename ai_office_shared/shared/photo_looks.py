"""
ai_office_shared.shared.photo_looks — адаптивные «луки» (обработки) для фото.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ. Первая версия фильтров крутила фиксированные
множители: «+18% яркости, +12% насыщенности» — одинаково тёмному кадру и
пересвеченному, портрету и закату. Так фильтр и выглядит фильтром: ч/б выходит
серой картинкой без цвета, зерно — равномерным шумом «на 25%».

Здесь по-другому: рецепт задаёт ЦЕЛЬ («точка чёрного на 4, средняя яркость 118,
S-кривая средней силы, зерно как у плёнки ISO 400»), а конкретные числа
считаются по гистограмме конкретного кадра. Плоскому кадру достаётся более
крутая кривая, контрастному — пологая; на портрете микроконтраст режется вдвое,
чтобы не подчёркивать поры.

ОТКУДА ЧИСЛА. Не из «топовых обработок в интернете» — их нельзя ни легально
скачать пачкой, ни надёжно измерить. Числа взяты из того, ЧТО эти обработки
делают, и это давно описанные вещи:

* Ч/Б. Плоскость обычного grayscale — следствие luma-коэффициентов
  0.299/0.587/0.114. Плёночники всегда снимали ч/б через цветной светофильтр:
  красный/оранжевый гасит небо и высветляет кожу. Это и есть «сочное ч/б» —
  здесь оно сделано микшером каналов (см. LOOKS["чб"]["bw"]).
* S-кривая. Плёнка имеет S-образную характеристическую кривую: подъём в
  средних тонах, мягкие «плечо» и «подножие». Линейный contrast из Pillow
  вместо этого просто клиппит света и тени.
* Зерно. Плёночное зерно сильнее всего в средних тонах и почти отсутствует в
  светах и глубоких тенях, монохромно и имеет физический размер — поэтому
  здесь оно маскируется по яркости и масштабируется от разрешения кадра, а не
  сыплется равномерным попиксельным шумом.
* Сплит-тонирование. Холодные тени + тёплые света («teal & orange») — базовый
  приём цветокоррекции кино и Instagram-пресетов.
* Хало. Засветка вокруг ярких участков на плёнке (halation) — то, что делает
  контровой свет «плёночным».

Всё это считается локально на Pillow: без ключей, без лимитов, без сети.

ИСПОЛЬЗОВАНИЕ:
    from ai_office_shared.shared import photo_looks
    stats = photo_looks.analyze(im)
    plan  = photo_looks.plan("чб", stats)
    im    = photo_looks.render(im, plan)
"""

from __future__ import annotations

import logging
from typing import NamedTuple, Optional

logger = logging.getLogger("ai_office_shared.photo_looks")

# Размер уменьшенной копии для анализа. 192px хватает для гистограммы и оценки
# кожи, а считается в чистом Python за единицы миллисекунд.
_ANALYSIS_SIDE = 192
# Разрешение, под которое подобраны размеры зерна и радиусы размытий.
_REFERENCE_SIDE = 1200


class ImageStats(NamedTuple):
    """Что мы измерили в кадре. Всё в единицах 0..255, доли — 0..1."""
    mean: float          # средняя яркость
    p1: float            # точка чёрного (1-й перцентиль)
    p50: float           # медиана
    p99: float           # точка белого
    contrast: float      # стандартное отклонение яркости
    shadows: float       # доля пикселей темнее 32
    highlights: float    # доля пикселей светлее 224
    saturation: float    # средняя насыщенность (S из HSV)
    skin: float          # доля пикселей телесного оттенка
    side: int            # длинная сторона оригинала


def _percentile(hist: list[int], total: int, frac: float) -> float:
    """Значение яркости, ниже которого лежит frac пикселей."""
    target = total * frac
    acc = 0
    for value, count in enumerate(hist):
        acc += count
        if acc >= target:
            return float(value)
    return 255.0


def analyze(im) -> ImageStats:
    """Замер кадра по уменьшенной копии. Не бросает — на любой беде даёт нейтраль."""
    from PIL import Image
    try:
        small = im.convert("RGB").copy()
        small.thumbnail((_ANALYSIS_SIDE, _ANALYSIS_SIDE), Image.BILINEAR)
        lum = small.convert("L")
        hist = lum.histogram()
        total = sum(hist) or 1

        mean = sum(v * c for v, c in enumerate(hist)) / total
        var = sum((v - mean) ** 2 * c for v, c in enumerate(hist)) / total
        shadows = sum(hist[:32]) / total
        highlights = sum(hist[224:]) / total

        sat_hist = small.convert("HSV").getchannel("S").histogram()
        sat_total = sum(sat_hist) or 1
        saturation = sum(v * c for v, c in enumerate(sat_hist)) / sat_total

        skin = _skin_fraction(small)

        return ImageStats(
            mean=mean,
            p1=_percentile(hist, total, 0.01),
            p50=_percentile(hist, total, 0.50),
            p99=_percentile(hist, total, 0.99),
            contrast=var ** 0.5,
            shadows=shadows,
            highlights=highlights,
            saturation=saturation,
            skin=skin,
            side=max(im.size),
        )
    except Exception as e:                                   # pragma: no cover
        logger.warning("analyze failed, беру нейтральные значения: %s", e)
        return ImageStats(128, 8, 128, 247, 55, 0.05, 0.05, 90, 0.0, max(im.size))


def _skin_fraction(small) -> float:
    """
    Доля пикселей телесного оттенка — грубо, зато без моделей и за миллисекунды.
    Нужна не для «распознавания лица», а чтобы не долбить портрет
    микроконтрастом и перенасыщением: на коже это видно первым.
    """
    # tobytes, а не getdata(): getdata объявлен устаревшим в Pillow 13, а
    # плоские байты ещё и быстрее — на 192×192 это доли миллисекунды.
    raw = small.convert("RGB").tobytes()
    if not raw:
        return 0.0
    hits = 0
    total = len(raw) // 3
    for i in range(0, total * 3, 3):
        r, g, b = raw[i], raw[i + 1], raw[i + 2]
        if r > 95 and g > 40 and b > 20 and r > g > b and (r - b) > 15 and (r - g) > 8:
            hits += 1
    return hits / total


# ── Рецепты ───────────────────────────────────────────────────────────────────
# Ключи рецепта (все опциональны):
#   target_mean   — к какой средней яркости тянуть кадр
#   black/white   — куда сажать точку чёрного/белого (0..255)
#   curve         — сила S-кривой 0..1
#   shoulder      — компрессия светов 0..1 (спасает пересветы)
#   bw            — (r,g,b) микшер каналов для ч/б
#   sat           — целевая насыщенность (в единицах S 0..255)
#   split         — {"sh": (r,g,b), "hi": (r,g,b), "amount": 0..1}
#   clarity       — микроконтраст 0..1.5
#   soften        — смягчение кожи 0..1
#   sharpen       — финальная резкость (множитель)
#   fade          — подъём чёрного (матовость) 0..1
#   halation      — свечение в светах 0..1
#   grain         — сила зерна 0..1
#   vignette      — сила виньетки 0..1

LOOKS: dict[str, dict] = {
    # «Авто» намеренно скромное: это «тот же кадр, только лучше», а не
    # перекраска. На проверке пастельный портрет от агрессивного авто ушёл в
    # жёлтое небо и фиолетовые волосы — цвет усиливал уже имевшийся оттенок.
    "авто": {
        "target_mean": 122, "black": 3, "white": 252, "curve": 0.2,
        "shoulder": 0.35, "sat": 100, "clarity": 0.4, "sharpen": 1.08,
        "pull": 0.4, "sat_cap": 1.15,
    },
    "яркое": {
        "target_mean": 138, "black": 4, "white": 253, "curve": 0.2,
        "shoulder": 0.45, "sat": 118, "clarity": 0.3, "sharpen": 1.05,
        "pull": 0.6, "sat_cap": 1.5,
    },
    "сочное": {
        "target_mean": 124, "black": 2, "white": 254, "curve": 0.5,
        "shoulder": 0.25, "sat": 145, "clarity": 0.8, "sharpen": 1.15,
        "pull": 0.8, "sat_cap": 1.8,
    },
    "нежное": {
        "target_mean": 134, "black": 10, "white": 250, "curve": 0.15,
        "shoulder": 0.5, "sat": 92, "soften": 0.5, "fade": 0.12,
        "split": {"sh": (250, 244, 236), "hi": (255, 250, 240), "amount": 0.10},
        "pull": 0.5,
    },
    "портрет": {
        "target_mean": 128, "black": 5, "white": 251, "curve": 0.3,
        "shoulder": 0.45, "sat": 108, "soften": 0.35, "clarity": 0.35,
        "sharpen": 1.1, "vignette": 0.16,
        "split": {"sh": (232, 238, 250), "hi": (255, 246, 232), "amount": 0.12},
        "pull": 0.5, "sat_cap": 1.2,
    },
    # Ч/Б через красно-оранжевый светофильтр: кожа светлеет, небо темнеет.
    "чб": {
        "target_mean": 120, "black": 2, "white": 254, "curve": 0.55,
        "shoulder": 0.3, "bw": (0.46, 0.36, 0.18), "clarity": 0.7, "sharpen": 1.2,
        "grain": 0.10,
        "pull": 0.85,
    },
    "нуар": {
        "target_mean": 104, "black": 0, "white": 250, "curve": 0.85,
        "shoulder": 0.15, "bw": (0.34, 0.36, 0.30), "clarity": 0.8,
        "grain": 0.42, "vignette": 0.45,
        "pull": 0.95,
    },
    "сепия": {
        "target_mean": 124, "black": 6, "white": 250, "curve": 0.35,
        "shoulder": 0.4, "bw": (0.42, 0.40, 0.18), "grain": 0.12,
        "split": {"sh": (150, 120, 84), "hi": (255, 232, 190), "amount": 0.55},
        "pull": 0.8,
    },
    "винтаж": {
        "target_mean": 126, "black": 22, "white": 244, "curve": 0.2,
        "shoulder": 0.55, "sat": 88, "fade": 0.20, "grain": 0.30,
        "vignette": 0.28, "halation": 0.25,
        "split": {"sh": (120, 132, 150), "hi": (255, 226, 178), "amount": 0.30},
        "pull": 0.75,
    },
    "плёнка": {
        "target_mean": 126, "black": 12, "white": 250, "curve": 0.42,
        "shoulder": 0.55, "sat": 112, "fade": 0.10, "grain": 0.34,
        "halation": 0.35, "clarity": 0.3,
        "split": {"sh": (108, 132, 148), "hi": (255, 236, 206), "amount": 0.24},
        "pull": 0.7,
    },
    "тепло": {
        "target_mean": 128, "black": 4, "white": 252, "curve": 0.25,
        "shoulder": 0.4, "sat": 112,
        "split": {"sh": (255, 238, 214), "hi": (255, 226, 176), "amount": 0.30},
        "pull": 0.5,
    },
    "холод": {
        "target_mean": 124, "black": 3, "white": 252, "curve": 0.3,
        "shoulder": 0.35, "sat": 106,
        "split": {"sh": (200, 220, 255), "hi": (232, 244, 255), "amount": 0.30},
        "pull": 0.5,
    },
    "матовое": {
        "target_mean": 132, "black": 26, "white": 242, "curve": 0.12,
        "shoulder": 0.6, "sat": 84, "fade": 0.26,
        "split": {"sh": (176, 186, 196), "hi": (252, 246, 238), "amount": 0.22},
        "pull": 0.6,
    },
    "чёткое": {
        "target_mean": 124, "black": 3, "white": 253, "curve": 0.35,
        "shoulder": 0.3, "sat": 108, "clarity": 1.15, "sharpen": 1.3,
        "pull": 0.6,
    },
    "драма": {
        "target_mean": 112, "black": 0, "white": 252, "curve": 0.8,
        "shoulder": 0.2, "sat": 118, "clarity": 1.0, "vignette": 0.38,
        "split": {"sh": (96, 124, 148), "hi": (255, 226, 186), "amount": 0.26},
        "pull": 0.9, "sat_cap": 1.6,
    },
}


def plan(look: str, stats: ImageStats, tweaks: Optional[dict] = None) -> dict:
    """
    Рецепт + замеры кадра → конкретные числа.

    Здесь и живёт «адаптивность»: одна и та же кнопка на плоском и на
    контрастном кадре даёт разные параметры, потому что цель у них одна, а
    исходное состояние разное.
    """
    spec = dict(LOOKS.get(look, LOOKS["авто"]))
    out = dict(spec)

    # 0. Насколько жёстко тянем кадр к цели. Мягкие обработки уважают замысел
    #    съёмки (пастельный портрет должен остаться пастельным), а «нуар» и
    #    «драма» на то и заказаны, чтобы кадр переделать.
    pull = float(spec.get("pull", 0.55))

    # 0.5. Замысел кадра. Светлый воздушный портрет (high key) и тёмный
    #      закатный кадр (low key) сняты такими НАМЕРЕННО. Тянуть их к средней
    #      яркости — ровно та ошибка, из-за которой автообработка выглядит
    #      грубой: на проверке пастельный портрет уезжал в тёмное жёлтое пятно.
    #      Чем «характернее» заказанная обработка (нуар, драма), тем меньше она
    #      обязана уважать исходный замысел — за этим и просили.
    intent = 0.0
    if stats.mean > 158 and stats.shadows < 0.03:
        intent = min(1.0, 0.5 + (stats.mean - 158) / 40.0)
    elif stats.mean < 86 and stats.highlights < 0.03:
        intent = min(1.0, 0.5 + (86 - stats.mean) / 40.0)
    respect = intent * (1.0 - pull * 0.6)
    out["intent"] = round(intent, 2)

    # 1. Экспозиция. Тянем к цели гаммой, а не сложением: сложение выбивает
    #    света в белый, гамма сохраняет и тени, и света.
    target = float(spec.get("target_mean", 124))
    target = target + (stats.mean - target) * respect
    measured = max(4.0, min(251.0, stats.mean))
    gamma = 1.0 + (_log_ratio(measured, target) - 1.0) * pull
    # Ограничитель: тёмный кадр — это часто замысел (закат, ночь), выкручивать
    # его в серый день нельзя.
    out["gamma"] = round(max(0.70, min(1.45, gamma)), 3)

    # 2. Кривая. Плоскому кадру — сильнее, уже контрастному — слабее.
    #    Замер: std яркости обычного бытового снимка ≈ 45–65.
    flatness = max(0.45, min(1.6, 58.0 / max(18.0, stats.contrast)))
    # Кривая тоже уважает замысел: на воздушном кадре она топит средние тона,
    # и волосы с кожей уходят в грязь — при том что средняя яркость по цифрам
    # не меняется вовсе. Ловится это только глазами, поэтому и записано.
    out["curve"] = round(float(spec.get("curve", 0.3)) * flatness * (1 - respect * 0.5), 3)

    # 3. Света. Если кадр уже пересвечен — усиливаем «плечо», чтобы не жечь.
    shoulder = float(spec.get("shoulder", 0.35))
    if stats.highlights > 0.08:
        shoulder = min(1.0, shoulder + stats.highlights * 2.0)
    out["shoulder"] = round(shoulder, 3)

    # 4. Точки чёрного и белого — тоже частично. Воздушный светлый кадр,
    #    у которого чёрного нет вовсе (p1 = 57), от жёсткой посадки на 4
    #    становится тяжёлым и теряет ровно то, ради чего снимался.
    black = float(spec.get("black", 4))
    if stats.shadows > 0.25:
        black = max(black, 6 + stats.shadows * 12)
    black = black + (stats.p1 - black) * respect      # у high-key чёрного нет
    white = float(spec.get("white", 252))
    out["black"] = round(stats.p1 + (black - stats.p1) * pull, 1)
    out["white"] = round(stats.p99 + (white - stats.p99) * pull, 1)
    out["in_black"] = stats.p1
    out["in_white"] = stats.p99

    # 5. Насыщенность. Рецепт задаёт ЦЕЛЬ, а не множитель: закат уже сочный,
    #    добивать его до клиппинга незачем. И тоже частично — пастельный кадр
    #    от «дотягивания» до средней насыщенности превращается в открытку.
    if "sat" in spec and "bw" not in spec:
        want = float(spec["sat"]) / max(25.0, stats.saturation)
        # Намеренно приглушённый кадр (пастель, туман, плёночный скан) тоже
        # замысел: дотягивать его до «средней» насыщенности нельзя.
        sat_pull = float(spec.get("sat_pull", pull)) * (1.0 - respect * 0.7)
        out["sat_factor"] = round(
            max(0.6, min(float(spec.get("sat_cap", 1.45)),
                         1.0 + (want - 1.0) * sat_pull)), 3)

    # 6. Кожа. Микроконтраст и резкость по коже — главный источник «пластика».
    if stats.skin > 0.12:
        damp = 0.45 if stats.skin > 0.3 else 0.65
        if "clarity" in out:
            out["clarity"] = round(float(out["clarity"]) * damp, 3)
        if "sharpen" in out:
            out["sharpen"] = round(1.0 + (float(out["sharpen"]) - 1.0) * damp, 3)
        if "sat_factor" in out:
            out["sat_factor"] = round(min(float(out["sat_factor"]), 1.35), 3)

    # 7. Зерно и радиусы — в долях от разрешения: на 4000px кадре
    #    попиксельный шум не виден вовсе, на 600px забивает картинку.
    out["scale"] = max(0.35, min(3.0, stats.side / _REFERENCE_SIDE))

    for key, value in (tweaks or {}).items():
        out[key] = value
    return out


def _log_ratio(measured: float, target: float) -> float:
    """Гамма, переводящая measured в target (обе в 0..255)."""
    import math
    m = max(0.02, min(0.98, measured / 255.0))
    t = max(0.02, min(0.98, target / 255.0))
    return math.log(t) / math.log(m)


# ── Примитивы рендера ─────────────────────────────────────────────────────────

def _lut(fn) -> list[int]:
    return [max(0, min(255, int(round(fn(i / 255.0) * 255)))) for i in range(256)]


def _apply_rgb_lut(im, lut: list[int]):
    """Одна и та же кривая по трём каналам — цвет не уезжает."""
    return im.convert("RGB").point(lut * 3)


def _gamma(im, g: float):
    """
    x ** g, а НЕ x ** (1/g). Показатель здесь — прямой выход _log_ratio: g > 1
    затемняет, g < 1 осветляет. Через 1/g значение уезжало в другую сторону —
    пересвеченный портрет (средняя 186 при цели 122) вместо затемнения
    вылезал ещё светлее.
    """
    if abs(g - 1.0) < 0.02:
        return im
    return _apply_rgb_lut(im, _lut(lambda x: x ** g))


def _levels(im, in_black: float, in_white: float, out_black: float, out_white: float):
    """Растяжка уровней по измеренным точкам кадра."""
    in_black = max(0.0, min(240.0, in_black))
    in_white = max(in_black + 12.0, min(255.0, in_white))
    span = (in_white - in_black) / 255.0
    ob, ow = out_black / 255.0, out_white / 255.0

    def f(x: float) -> float:
        y = (x - in_black / 255.0) / span
        return ob + max(0.0, min(1.0, y)) * (ow - ob)

    return _apply_rgb_lut(im, _lut(f))


def _tone_curve(im, curve: float, shoulder: float):
    """
    S-кривая с «плечом». curve — подъём средних тонов, shoulder — насколько
    мягко сходятся света. Линейный контраст такого не умеет: он просто
    обрезает оба конца.
    """
    curve = max(0.0, min(1.2, curve))
    shoulder = max(0.0, min(1.0, shoulder))
    if curve < 0.02 and shoulder < 0.02:
        return im

    def f(x: float) -> float:
        s = x * x * (3 - 2 * x)                    # smoothstep — мягкая S
        y = x + (s - x) * curve
        # Клампим ДО «плеча»: при curve > 1 (плоский кадр + сильный рецепт
        # вроде «нуара») y вылезает за 1, а (1 - y) ** 1.7 от отрицательного
        # основания даёт комплексное число — Pillow такое не проглатывает,
        # и обработка падала прямо в рендере.
        y = max(0.0, min(1.0, y))
        if shoulder:                                # мягкий откат светов
            y = y * (1 - shoulder * 0.18) + (1 - (1 - y) ** 1.7) * shoulder * 0.18
        return max(0.0, min(1.0, y))

    return _apply_rgb_lut(im, _lut(f))


def _to_bw(im, mix: tuple[float, float, float]):
    """
    Ч/Б микшером каналов. Смысл — тот же, что у цветного светофильтра на
    объективе: подняв вес красного, получаем светлую кожу и тёмное небо.
    Обычный convert("L") этого не делает и оттого выглядит вяло.
    """
    from PIL import Image
    r, g, b = mix
    total = (r + g + b) or 1.0
    gray = im.convert("RGB").convert("L", (r / total, g / total, b / total, 0))
    return Image.merge("RGB", (gray, gray, gray))


def _saturation(im, factor: float):
    from PIL import ImageEnhance
    if abs(factor - 1.0) < 0.02:
        return im
    return ImageEnhance.Color(im.convert("RGB")).enhance(factor)


def _split_tone(im, shadows: tuple, highlights: tuple, amount: float):
    """Тонировка теней и светов в разные стороны — база киношной цветокоррекции."""
    from PIL import Image
    amount = max(0.0, min(1.0, amount))
    if amount < 0.02:
        return im
    im = im.convert("RGB")
    lum = im.convert("L")

    # Маска светов — сама яркость; маска теней — она же инвертированная.
    hi_mask = lum.point(lambda v: int((v / 255.0) ** 1.4 * 255))
    sh_mask = lum.point(lambda v: int((1 - v / 255.0) ** 1.4 * 255))

    out = im
    for color, mask in ((highlights, hi_mask), (shadows, sh_mask)):
        solid = Image.new("RGB", im.size, tuple(int(c) for c in color))
        tinted = Image.blend(out, _multiply(out, solid), amount * 0.85)
        out = Image.composite(tinted, out, mask)
    return out


def _multiply(a, b):
    from PIL import ImageChops
    # Тонировка через умножение на цвет: сохраняет структуру, красит тон.
    return ImageChops.multiply(a, b.point(lambda v: 128 + v // 2))


def _clarity(im, strength: float, scale: float):
    """
    Микроконтраст. Радиус маленький и растёт от разрешения: на проверке
    radius 6+ давал светящуюся кайму по контуру головы (контровой портрет —
    худший случай). Силу гоним через percent.
    """
    from PIL import ImageFilter
    strength = max(0.0, min(1.5, strength))
    if strength < 0.03:
        return im
    return im.filter(ImageFilter.UnsharpMask(
        radius=max(2, int(2 * scale)),
        percent=int(40 + 45 * strength),
        threshold=5))


def _soften(im, strength: float, scale: float):
    """Кожа мягче, глаза и края — нет: размытый слой + возврат резкости."""
    from PIL import Image, ImageEnhance, ImageFilter
    strength = max(0.0, min(1.0, strength))
    if strength < 0.03:
        return im
    blurred = im.filter(ImageFilter.GaussianBlur(max(1.0, 2.2 * scale)))
    mixed = Image.blend(im.convert("RGB"), blurred.convert("RGB"), strength * 0.7)
    return ImageEnhance.Sharpness(mixed).enhance(1.0 + 0.5 * strength)


def _fade(im, strength: float):
    """Матовость: поднимаем чёрное, а не наливаем серую пелену поверх всего."""
    strength = max(0.0, min(0.5, strength))
    if strength < 0.02:
        return im
    lift = strength * 0.22
    return _apply_rgb_lut(im, _lut(lambda x: lift + x * (1 - lift)))


def _halation(im, strength: float, scale: float):
    """
    Свечение вокруг ярких участков — то, от чего контровой свет читается как
    плёночный. Берём только света, размываем, подмешиваем тёплым.
    """
    from PIL import Image, ImageChops, ImageFilter
    strength = max(0.0, min(1.0, strength))
    if strength < 0.03:
        return im
    im = im.convert("RGB")
    lum = im.convert("L")
    mask = lum.point(lambda v: 0 if v < 190 else int((v - 190) / 65 * 255))
    glow = Image.new("RGB", im.size, (255, 176, 120))
    glow = Image.composite(glow, Image.new("RGB", im.size, (0, 0, 0)), mask)
    glow = glow.filter(ImageFilter.GaussianBlur(max(4.0, 14 * scale)))
    return Image.blend(im, ImageChops.screen(im, glow), strength * 0.5)


def _grain(im, strength: float, scale: float):
    """
    Плёночное зерно: монохромное, с физическим размером и сильнее всего в
    средних тонах. Равномерный шум по всему кадру — главный признак
    «фильтра из приложения»; в светах и глубоких тенях зерна почти нет.
    """
    from PIL import Image, ImageChops
    strength = max(0.0, min(1.0, strength))
    if strength < 0.02:
        return im
    im = im.convert("RGB")
    w, h = im.size

    # Зерно генерим в уменьшенном масштабе и растягиваем — так у него
    # появляется размер. Попиксельный шум на 12-мегапиксельном кадре не виден.
    grain_px = max(1.0, 1.4 * scale)
    small = (max(1, int(w / grain_px)), max(1, int(h / grain_px)))
    noise = Image.effect_noise(small, 22 + 34 * strength).resize((w, h), Image.BILINEAR)
    noise_rgb = Image.merge("RGB", (noise, noise, noise))
    grained = ImageChops.overlay(im, noise_rgb)

    # Треугольная маска по яркости: пик в средних тонах.
    mid = im.convert("L").point(lambda v: int(255 - abs(2 * v - 255)))
    return Image.composite(Image.blend(im, grained, min(0.85, strength)), im, mid)


def _vignette(im, strength: float):
    from PIL import Image, ImageDraw, ImageFilter
    strength = max(0.0, min(1.0, strength))
    if strength < 0.02:
        return im
    im = im.convert("RGB")
    w, h = im.size
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).ellipse((-w * 0.18, -h * 0.18, w * 1.18, h * 1.18), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(min(w, h) * 0.2))
    dark = Image.new("RGB", (w, h), (0, 0, 0))
    return Image.composite(im, Image.blend(im, dark, min(0.85, strength)), mask)


def render(im, params: dict):
    """
    Применяет план. Порядок не случайный: сначала тон (экспозиция → уровни →
    кривая), потом цвет, потом локальные эффекты, и только в конце зерно и
    виньетка — иначе зерно размазывается резкостью, а виньетка вытягивается
    уровнями.
    """
    from PIL import Image, ImageEnhance
    im = im.convert("RGB")
    scale = float(params.get("scale", 1.0))

    im = _gamma(im, float(params.get("gamma", 1.0)))
    im = _levels(im,
                 float(params.get("in_black", 0)), float(params.get("in_white", 255)),
                 float(params.get("black", 4)), float(params.get("white", 252)))
    im = _tone_curve(im, float(params.get("curve", 0)), float(params.get("shoulder", 0)))

    if params.get("bw"):
        im = _to_bw(im, tuple(params["bw"]))
    elif params.get("sat_factor"):
        im = _saturation(im, float(params["sat_factor"]))

    split = params.get("split")
    if split:
        im = _split_tone(im, tuple(split["sh"]), tuple(split["hi"]),
                         float(split.get("amount", 0.2)))

    if params.get("soften"):
        im = _soften(im, float(params["soften"]), scale)
    if params.get("clarity"):
        im = _clarity(im, float(params["clarity"]), scale)
    if params.get("sharpen"):
        im = ImageEnhance.Sharpness(im).enhance(float(params["sharpen"]))
    if params.get("fade"):
        im = _fade(im, float(params["fade"]))
    if params.get("halation"):
        im = _halation(im, float(params["halation"]), scale)
    if params.get("grain"):
        im = _grain(im, float(params["grain"]), scale)
    if params.get("vignette"):
        im = _vignette(im, float(params["vignette"]))

    assert isinstance(im, Image.Image)
    return im

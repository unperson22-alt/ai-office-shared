"""
ai_office_shared.shared.photo_retouch — ретушь лица: убрать дефекты кожи.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ (инцидент 2026-08-21):
    Яна прислала Криссу фото и попросила «прибрати недоліки на обличчі». Кнопка
    «🧴 Ретушь» у бота была, но вела она в пресет «нежное» — общее смягчение
    всего кадра. Прыщ после него остаётся прыщом, только слегка размытым, а лицо
    теряет текстуру целиком. Это не ретушь, это мыло. Здесь — настоящая точечная
    работа: находим МАЛЕНЬКИЕ пятна на коже и заменяем их окружением, всё
    остальное (глаза, губы, брови, волосы, фон) не трогаем вообще.

ПРИНЦИП (три шага, все локальные, Pillow, без сети и ключей):
    1. Маска кожи — по цветовому тесту (тот же, что в photo_looks._skin_fraction).
       Ретушь работает ТОЛЬКО внутри неё: свитер и обои не «лечим».
    2. Точечное лечение. Дефект — это пятно, которое темнее или краснее своего
       окружения И при этом маленькое. «Маленькое» проверяется морфологическим
       открытием: то, что переживает эрозию, — это глаз, бровь, ноздря, тень от
       носа, и в маску дефектов оно не попадает. Найденные пятна заменяются
       медианой окрестности — так работает spot healing в редакторах.
    3. Мягкое выравнивание тона по коже с сохранением краёв и последующим
       возвратом микротекстуры. Слабое: цель — кожа без пятен, а не пластик.

ЧЕГО ЗДЕСЬ СОЗНАТЕЛЬНО НЕТ:
    Распознавания лиц (не нужно: цветовая маска + «размер пятна» дают ту же
    защиту дешевле) и нейросетей (обещали работу без лимитов и ключей).

Функция не бросает исключений: на любой беде возвращает исходную картинку и
честный отчёт, потому что молчаливая порча фото хуже, чем «ничего не изменил».
"""

from __future__ import annotations

import logging
from typing import NamedTuple

logger = logging.getLogger("ai_office_shared.photo_retouch")

# Разрешение, под которое подобраны радиусы. Настоящая обработка идёт в полном
# размере, здесь только пересчёт «сколько пикселей занимает прыщ».
_REFERENCE_SIDE = 1200
# Дефекты ищутся на маленькой копии — и это не экономия, а суть метода. Ядра
# фильтров фиксированные, поэтому «маленькое пятно» и «крупная черта лица»
# определяются в долях кадра, а не в пикселях: прыщ на селфи 4000px и на
# сжатом Телеграмом 900px — это одна и та же доля лица.
_DETECT_SIDE = 384
_PROBE = 13          # окно «каким это место должно быть» ≈ 3.4% кадра
_OPEN = 9            # что переживает эрозию этим ядром — не прыщ, а глаз/бровь
_GROW = 5            # дорастить пятно от найденного контура внутрь
# Маски и слой лечения считаются на копии не длиннее этого: полноразмерная
# медиана стоит секунды, а результат внутри пятна от неё не зависит.
_WORK_SIDE = 1600
# Больше этой доли кожи ретушь не трогает: если «дефектов» вдруг четверть лица —
# это не дефекты, это тень, борода или шум матрицы. Порог поднимается, пока
# площадь не станет правдоподобной.
_MAX_SPOT_SHARE = 0.06


class RetouchReport(NamedTuple):
    """Что именно сделали — для подписи к результату и для тестов."""
    skin: float          # доля кадра, опознанная как кожа
    spots: float         # доля кожи, попавшая под точечное лечение
    softened: bool       # выравнивали ли тон
    note: str            # готовый текст пользователю


def _ge(channel, threshold: int):
    """Канал → бинарная маска (0/255): «значение не меньше порога»."""
    return channel.point(lambda v, t=threshold: 255 if v >= t else 0)


def _both(a, b):
    """Логическое И для бинарных масок."""
    from PIL import ImageChops
    return ImageChops.darker(a, b)


def _either(a, b):
    from PIL import ImageChops
    return ImageChops.lighter(a, b)


def _coverage(mask) -> float:
    """Доля белого в бинарной маске."""
    hist = mask.histogram()
    total = sum(hist) or 1
    return sum(hist[128:]) / total


def _le(channel, threshold: int):
    """Канал → бинарная маска: «значение не больше порога»."""
    return channel.point(lambda v, t=threshold: 255 if v <= t else 0)


def skin_mask(im):
    """
    Маска кожи (L, 0/255) — объединение двух правил.

    1. RGB-тест (r>95, g>40, b>20, r>g>b, r-b>15, r-g>8) — тот же, что в
       photo_looks._skin_fraction.
    2. Тест по цветности YCbCr (77≤Cb≤127, 133≤Cr≤173, Y>35). Он нужен ровно
       затем, чтобы ретушь работала на тёмной коже: у неё r почти никогда не
       больше 95, и по одному первому правилу бот ответил бы «лица на фото не
       нашла». Оттенок кожи в Cb/Cr держится почти одинаково у всех тонов —
       меняется яркость, а не цветность.

    Медиана на выходе убирает одиночные пиксели: дырявая маска даёт рваную
    ретушь по краю щеки.
    """
    from PIL import ImageChops, ImageFilter

    r, g, b = im.convert("RGB").split()
    by_rgb = _ge(r, 96)
    by_rgb = _both(by_rgb, _ge(g, 41))
    by_rgb = _both(by_rgb, _ge(b, 21))
    by_rgb = _both(by_rgb, _ge(ImageChops.subtract(r, g), 9))    # r - g > 8
    by_rgb = _both(by_rgb, _ge(ImageChops.subtract(r, b), 16))   # r - b > 15
    by_rgb = _both(by_rgb, _ge(ImageChops.subtract(g, b), 1))    # g > b

    y, cb, cr = im.convert("YCbCr").split()
    by_chroma = _both(_ge(cb, 77), _le(cb, 127))
    by_chroma = _both(by_chroma, _both(_ge(cr, 133), _le(cr, 173)))
    by_chroma = _both(by_chroma, _ge(y, 36))                     # не чёрный провал

    return _either(by_rgb, by_chroma).filter(ImageFilter.MedianFilter(5))


def _blemish_mask(small, skin):
    """
    Маска дефектов на уменьшенной копии: маленькие пятна на коже, которые
    темнее или краснее своего окружения.

    Ключевой шаг — открытие (эрозия + дилатация). Всё, что переживает эрозию
    ядром _OPEN, крупнее прыща: глаз, бровь, ноздря, тень под скулой, граница
    волос. Оно вычитается из маски, поэтому ретушь физически не может
    «залечить» глаз. После этого маска подращивается: у крупного пятна
    медианный зонд видит только контур, и без дорастания серединка прыща
    осталась бы нетронутой (ровно это и происходило в первой версии).
    """
    from PIL import ImageChops, ImageFilter

    lum = small.convert("L")
    r, g, _ = small.convert("RGB").split()

    local = lum.filter(ImageFilter.MedianFilter(_PROBE))
    darker = ImageChops.subtract(local, lum)              # темнее окружения
    redness = ImageChops.subtract(r, g)
    redder = ImageChops.subtract(
        redness, redness.filter(ImageFilter.MedianFilter(_PROBE)))

    # Порог подбирается по площади: на чистой коже он останется низким и
    # заберёт единичные точки, на шумном/бородатом кадре сам поднимется.
    small_only = None
    for level in (10, 16, 24, 34, 48):
        spots = _either(_ge(darker, level), _ge(redder, level + 6))
        spots = _both(spots, skin)
        big = spots.filter(ImageFilter.MinFilter(_OPEN)).filter(
            ImageFilter.MaxFilter(_OPEN))
        small_only = _both(ImageChops.subtract(spots, big),
                           skin.filter(ImageFilter.MaxFilter(3)))
        if _coverage(small_only) <= _MAX_SPOT_SHARE:
            break
    return small_only.filter(ImageFilter.MaxFilter(_GROW))


def _edge_mask(im, scale: float):
    """
    Где в кадре настоящие детали: ресницы, губы, ноздри, край волос.
    Выравнивание тона туда не заходит — иначе лицо становится пластиковым.
    """
    from PIL import ImageChops, ImageFilter
    lum = im.convert("L")
    blurred = lum.filter(ImageFilter.GaussianBlur(max(1.0, 2.0 * scale)))
    edges = _either(ImageChops.subtract(lum, blurred),
                    ImageChops.subtract(blurred, lum))
    return _ge(edges, 10).filter(ImageFilter.MaxFilter(3)).filter(
        ImageFilter.GaussianBlur(max(1.0, 1.2 * scale)))


def retouch(im, strength: float = 1.0) -> tuple:
    """
    Ретушь лица. Возвращает (изображение, RetouchReport).

    Args:
        im: PIL.Image (после photo._open/_flatten).
        strength: 0.2 — «чуть-чуть», 1.0 — обычная ретушь. Выше 1 не берём:
                  за границей начинается пластик, а его никто не просил.

    Три разрешения в работе, и это осознанно:
        detect (384px) — поиск пятен: фиксированные ядра = размер в долях кадра;
        work   (≤1600) — слой-заплатка: медиана в полном размере стоит секунды;
        полный размер  — маска кожи и склейка: границу глаза и губ нельзя
                         решать по уменьшенной копии, там она в 8 пикселей.

    Никогда не бросает: на исключении отдаёт исходный кадр и говорит об этом.
    """
    from PIL import Image, ImageFilter

    strength = max(0.2, min(1.0, float(strength)))
    try:
        base = im.convert("RGB")
        full_side = max(base.size)
        full_scale = max(0.5, full_side / _REFERENCE_SIDE)

        skin_full = skin_mask(base)
        skin_share = _coverage(skin_full)
        if skin_share < 0.02:
            # Кожи в кадре нет — ретушировать нечего. Это не ошибка: пусть
            # вызывающий скажет человеку правду, а не выдаёт мыло за работу.
            return base, RetouchReport(skin_share, 0.0, False,
                                       "лица на фото не нашла")

        detect = base.copy()
        detect.thumbnail((_DETECT_SIDE, _DETECT_SIDE), Image.LANCZOS)
        spots = _blemish_mask(detect, skin_mask(detect))
        spot_share = _coverage(spots)

        out = base
        if spot_share > 0.0005:
            # Заплатка — медиана маленькой копии: внутри прыща нужен цвет
            # соседней кожи, а не его собственная текстура.
            patch = detect.filter(ImageFilter.MedianFilter(_PROBE))
            patch = patch.resize(base.size, Image.LANCZOS)
            mask = spots.resize(base.size, Image.BILINEAR)
            # Пересечение с полноразмерной кожей — единственная защита от того,
            # чтобы подросшая маска заехала на ресницу или губу.
            mask = _both(mask, skin_full)
            mask = mask.filter(ImageFilter.GaussianBlur(max(1.0, 1.5 * full_scale)))
            if strength < 1.0:
                mask = mask.point(lambda v, s=strength: int(v * s))
            out = base.copy()
            out.paste(patch, (0, 0), mask)

        # Выравнивание тона: слабое, только по коже и мимо краёв.
        tone = _both(skin_full, _edge_mask(base, full_scale).point(lambda v: 255 - v))
        tone = tone.filter(ImageFilter.GaussianBlur(max(1.0, 2.0 * full_scale)))
        tone = tone.point(lambda v, a=0.42 * strength: int(v * a))
        smoothed = out.filter(ImageFilter.GaussianBlur(max(1.2, 2.4 * full_scale)))
        out = out.copy()
        out.paste(smoothed, (0, 0), tone)

        # Возврат микротекстуры: после любого смешивания кожа теряет поры, и
        # без этого шага результат читается как «замылил», а не «отретушировал».
        out = out.filter(ImageFilter.UnsharpMask(
            radius=max(1.0, 1.1 * full_scale), percent=int(45 * strength), threshold=4))

        note = ("убрала пятна и выровняла тон кожи" if spot_share > 0.0005
                else "явных дефектов не нашла — выровняла тон кожи")
        return out, RetouchReport(skin_share, spot_share, True, note)
    except Exception as e:                                   # pragma: no cover
        logger.warning("retouch failed, отдаю оригинал: %s", e)
        return im, RetouchReport(0.0, 0.0, False, "ретушь не получилась")

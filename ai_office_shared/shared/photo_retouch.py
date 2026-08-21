"""
ai_office_shared.shared.photo_retouch — ретушь лица: убрать дефекты кожи.

ЗАЧЕМ (инцидент 2026-08-21):
    Яна попросила Крисса «прибрати недоліки на обличчі». Кнопка «🧴 Ретушь»
    вела в пресет «нежное» — общее смягчение кадра. Прыщ после него остаётся
    прыщом, а лицо теряет текстуру целиком.

ЧЕМУ НАУЧИЛ ПЕРВЫЙ ЗАХОД (тот же день, фото Влада):
    Первая версия этого модуля прошла все тесты и превратила живое лицо в
    мыло: брови размазаны, ресницы съедены, кожа пластиковая. Тесты гонялись
    на синтетическом «лице» из плоских заливок, где ни бровей, ни пор нет, и
    класс ошибки был им физически невидим. Отсюда три правила, на которых
    построен модуль:

    1. РЕТУШЬ — ТОЧЕЧНАЯ ОПЕРАЦИЯ. По умолчанию меняются только пиксели
       найденных пятен (доли процента кадра), всё остальное возвращается байт
       в байт. Общее выравнивание тона — отдельная просьба (even_tone), а не
       бонус.
    2. ЗАЩИТА ВАЖНЕЕ ПОЛНОТЫ. Не убрать дефект — досадно; съесть бровь —
       непоправимо. Все пороги настроены в эту сторону, а на кадре, где
       дефект неотличим от шума, модуль честно ничего не делает.
    3. ГРАНИЦЫ ПРОВЕРЯЮТСЯ ГЛАЗАМИ НА НАСТОЯЩИХ ЛИЦАХ. Синтетика ловит
       регрессы, но не заменяет просмотр реальных портретов.

КАК УСТРОЕНО (всё локально, Pillow, без сети и ключей):
    Маска кожи — цветовой тест плюс страховка по цветности для тёмной кожи.
    Маска волос — брови, ресницы, борода: три признака (глубина темноты
        относительно кожи, плотность+протяжённость, мелкая текстура). Ретушь
        внутрь неё не заходит.
    Маска дефектов — «темнее или краснее окружения» И «маленькое» И
        «компактное»: открытие отсекает глаз и ноздрю, реконструкция по ядру —
        кайму вдоль брови, потолок площади не даёт ретуши расползтись.
    Лечение — медиана уменьшенной копии, вклеенная только внутри маски.

ГРАНИЦЫ, КОТОРЫЕ ЗНАЕМ:
    Морщины и крупные родинки не трогаем — они «крупные» по построению.
    При яркости кожи ниже ~60 из 255 перепад дефекта тонет в шуме сжатия:
    модуль честно оставляет кадр как есть.

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
_GROW = 3            # дорастить пятно от найденного контура внутрь
# Потолок площади ретуши. Считается ПОСЛЕ дорастания маски — в первой версии
# он стоял до, и дилатация раздувала уже проверенную маску втрое: на реальном
# портрете «дефектами» оказались 19% кадра при лимите 6%, лицо превратилось в
# мыло (фото Влада, 21.08.2026). Настоящие пятна — доли процента кадра.
_MAX_SPOT_SHARE = 0.02
# Ниже этой яркости RGB-правило кожи не работает в принципе — только там
# подключается тест по цветности. Иначе он тянет в маску всё телесно-рыжее:
# деревянный шкаф на фоне, пол, светлое дерево.
_DARK_SKIN_Y = 110


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
    Маска кожи (L, 0/255) — RGB-правило плюс страховка для тёмной кожи.

    1. RGB-тест (r>95, g>40, b>20, r>g>b, r-b>15, r-g>8) — тот же, что в
       photo_looks._skin_fraction. Работает на светлой и средней коже.
    2. Тест по цветности YCbCr (77≤Cb≤127, 133≤Cr≤173) — но ТОЛЬКО там, где
       яркость ниже _DARK_SKIN_Y. У тёмной кожи r почти никогда не больше 95, и
       без этой ветки бот отвечал бы «лица на фото не нашла». Ограничение по
       яркости обязательно: без него тест тянул в маску всё телесно-рыжее —
       деревянный шкаф за спиной, пол, светлое дерево, — и «кожей» оказывалась
       половина кадра (54% на реальном портрете, 21.08.2026).

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
    dark = _both(_ge(cb, 77), _le(cb, 127))
    dark = _both(dark, _both(_ge(cr, 133), _le(cr, 173)))
    dark = _both(dark, _both(_ge(y, 36), _le(y, _DARK_SKIN_Y)))

    return _either(by_rgb, dark).filter(ImageFilter.MedianFilter(5))


def _detect_darker(small):
    """Карта «насколько темнее локальной медианы» — вход для hair_mask."""
    from PIL import ImageChops, ImageFilter
    lum = small.convert("L")
    return ImageChops.subtract(lum.filter(ImageFilter.MedianFilter(_PROBE)), lum)


def hair_mask(small, darker, skin_luma: float = 0.0):
    """
    Где в кадре ВОЛОСЫ: брови, ресницы, щетина, борода, край причёски.

    Два независимых признака, и решающий — первый:

    1. ГЛУБИНА ТЕМНОТЫ. Волос темнее кожи в разы, прыщ — лишь чуть: у брови
       яркость около половины от кожи, у дефекта 0.85–0.9. Порог берётся долей
       от средней яркости кожи, поэтому правило одинаково работает на любом
       тоне кожи — у тёмной кожи и волосы, и порог опускаются вместе.
    2. ПЛОТНОСТЬ ПЛЮС ПРОТЯЖЁННОСТЬ. Добивает густые тёмные массивы, которые
       по яркости пограничны: борода, тень от волос.

    Одной плотности не хватило: на 384-пиксельной копии бровь высотой в шесть
    пикселей размывается ниже любого разумного порога, и маска покрывала одни
    зрачки — а «дефекты» ложились ровно на брови и ресницы (реальные портреты,
    21.08.2026).
    """
    from PIL import ImageFilter
    dense = _ge(darker, 9).filter(ImageFilter.GaussianBlur(6.0))
    spread = _ge(dense, 88).filter(ImageFilter.MinFilter(3))
    hair = spread.filter(ImageFilter.MaxFilter(9))

    if skin_luma > 0:
        # Доля берётся не постоянной: у тёмной кожи весь диапазон яркостей
        # сжат, и порог «темнее кожи на четверть» отрезал бы вместе с волосами
        # сами дефекты. Чем темнее кожа, тем глубже порог.
        factor = 0.74 - 0.10 * (1.0 - min(1.0, skin_luma / 150.0))
        deep = _le(small.convert("L"), int(skin_luma * factor))
        hair = _either(hair, deep.filter(ImageFilter.MaxFilter(7)))

    # Третий признак — ТЕКСТУРА. Светлая бровь (рыжая, седая) не темнее кожи и
    # первыми двумя правилами не ловится, но она всегда «мохнатая»: десятки
    # мелких перепадов на пятачке. Прыщ, наоборот, гладкое плавное пятно —
    # внутри него градиентов нет.
    from PIL import ImageChops
    lum = small.convert("L")
    fine = _either(
        ImageChops.subtract(lum, lum.filter(ImageFilter.GaussianBlur(1.5))),
        ImageChops.subtract(lum.filter(ImageFilter.GaussianBlur(1.5)), lum))
    busy = _ge(fine, 10).filter(ImageFilter.GaussianBlur(5.0))
    return _either(hair, _ge(busy, 84).filter(ImageFilter.MaxFilter(5)))


def _skin_luma(small, skin) -> float:
    """Средняя яркость кожи в кадре. 0 — измерить не удалось."""
    from PIL import ImageStat
    try:
        return ImageStat.Stat(small.convert("L"), skin).mean[0]
    except Exception:                                    # pragma: no cover
        return 0.0


def _skin_contrast_scale(luma: float) -> float:
    """
    Во сколько раз ужать пороги детекта под яркость кожи в кадре.

    Опорная точка — светлая кожа (яркость ~150). Тёмная кожа даёт тот же
    дефект меньшим перепадом яркости, поэтому порог для неё обязан быть ниже,
    иначе бот честно скажет «дефектов не нашла» на лице, где они есть.
    Границы жёсткие: ниже 0.45 начинается ловля шума матрицы.
    """
    if luma <= 0:
        return 1.0
    return max(0.45, min(1.15, luma / 150.0))


def _blemish_mask(small, skin):
    """
    Маска дефектов на уменьшенной копии: маленькие пятна на коже, которые
    темнее или краснее своего окружения.

    Четыре защиты, каждая от своего провала:

    1. Открытие (эрозия + дилатация) ядром _OPEN. Всё, что его переживает,
       крупнее прыща: глаз, ноздря, тень под скулой, граница волос. Оно
       вычитается — ретушь физически не может залечить глаз.
    2. Маска волос по плотности (hair_mask). Открытие не спасает от тонких
       структур: отдельный волосок брови мелкий и «темнее окружения», то есть
       формально неотличим от прыща. Спасает только плотность.
    3. Эрозия маски кожи перед пересечением: веко и край губы формально
       проходят цветовой тест, а ресницы на них — нет.
    4. Потолок площади — ПОСЛЕ дорастания. Проверять до бессмысленно:
       дилатация раздувает уже одобренную маску втрое.

    Порог поднимается, пока площадь не станет правдоподобной; не уложились ни
    на одном — возвращаем пусто. Лучше не тронуть ничего, чем замылить лицо.
    """
    from PIL import ImageChops, ImageFilter

    lum = small.convert("L")
    r, g, _ = small.convert("RGB").split()

    local = lum.filter(ImageFilter.MedianFilter(_PROBE))
    darker = ImageChops.subtract(local, lum)              # темнее окружения
    redness = ImageChops.subtract(r, g)
    redder = ImageChops.subtract(
        redness, redness.filter(ImageFilter.MedianFilter(_PROBE)))

    # Дырки в маске кожи размером с прыщ надо ЗАКРЫТЬ, иначе дефект вылетает
    # из области работы вместе с окрестностью: сам прыщ цветовой тест кожи не
    # проходит (на тёмной коже — тем более), а последующая эрозия расширяет
    # дырку втрое. Закрытие крупных дыр — глаз, ноздрей — не трогает.
    closed = skin.filter(ImageFilter.MaxFilter(9)).filter(ImageFilter.MinFilter(9))
    inner = closed.filter(ImageFilter.MinFilter(5))       # отступ от края кожи
    luma = _skin_luma(small, inner)
    allowed = _both(inner,
                    hair_mask(small, darker, luma).point(lambda v: 255 - v))

    # Пороги масштабируются по яркости кожи. Абсолютный порог — ошибка: тот же
    # дефект на тёмной коже даёт вдвое меньший перепад в единицах яркости, и
    # фиксированные 22 просто не замечали его (провал на тоне 66,45,38).
    k = _skin_contrast_scale(luma)

    for level in (round(22 * k), round(30 * k), round(40 * k),
                  round(52 * k), round(66 * k)):
        # Цветовой порог тоже масштабируется: на тёмной коже дефект выдаёт
        # себя краснотой, а не яркостью, и абсолютные 10 единиц он не берёт.
        spots = _either(_ge(darker, max(6, level)),
                        _ge(redder, max(6, level + round(8 * k))))
        spots = _both(spots, allowed)
        # Снять «песок»: на тёмной коже и на сжатом JPEG порог ловит шум
        # матрицы — одиночные пиксели раздували площадь, порог повышался, и
        # настоящий дефект терялся вместе с шумом (провал на тоне 66,45,38).
        spots = spots.filter(ImageFilter.MinFilter(3)).filter(
            ImageFilter.MaxFilter(3))
        big = spots.filter(ImageFilter.MinFilter(_OPEN)).filter(
            ImageFilter.MaxFilter(_OPEN))
        small_only = ImageChops.subtract(spots, big)

        # КОМПАКТНОСТЬ. Прыщ круглый, у него есть плотное ядро; кайма вдоль
        # брови или ресницы — вытянутая полоска в два-три пикселя шириной и
        # ядра не имеет. Оставляем только те пятна, что пережили эрозию, и
        # достраиваем их обратно до исходной формы (открытие с реконструкцией).
        # Без этого «дефекты» ложились полосами ровно по краю брови.
        seed = small_only.filter(ImageFilter.MinFilter(3))
        core = _both(seed.filter(ImageFilter.MaxFilter(_PROBE)), small_only)
        grown = _both(core.filter(ImageFilter.MaxFilter(_GROW)), allowed)
        if _coverage(grown) <= _MAX_SPOT_SHARE:
            return grown
    return ImageChops.constant(skin, 0)


def _edge_mask(im, scale: float):
    """
    Где в кадре настоящие детали: ресницы, губы, ноздри, край волос.
    Выравнивание тона туда не заходит — иначе лицо становится пластиковым.

    Порог намеренно высокий: на реальной коже поры и пушок дают слабый отклик,
    и с чувствительным порогом «краем» оказывалось всё лицо — тогда
    выравнивание не делало ровно ничего и обещание «ретушь сильнее» было
    пустым.
    """
    from PIL import ImageChops, ImageFilter
    lum = im.convert("L")
    blurred = lum.filter(ImageFilter.GaussianBlur(max(1.5, 2.5 * scale)))
    edges = _either(ImageChops.subtract(lum, blurred),
                    ImageChops.subtract(blurred, lum))
    return _ge(edges, 26).filter(ImageFilter.GaussianBlur(max(1.0, 1.0 * scale)))


def retouch(im, strength: float = 1.0, even_tone: bool = False) -> tuple:
    """
    Ретушь лица. Возвращает (изображение, RetouchReport).

    Args:
        im: PIL.Image (после photo._open/_flatten).
        strength: непрозрачность заплатки, 0.2 — «чуть-чуть», 1.0 — обычная.
        even_tone: дополнительно выровнять тон кожи. ПО УМОЛЧАНИЮ ВЫКЛЮЧЕНО и
            включается только явной просьбой («сильнее», «гладкая кожа»).
            Причина — фото Влада 21.08.2026: общее смягчение по всей маске кожи
            съело брови, ресницы и бороду. «Убери дефекты» не значит «замыль
            лицо»: по умолчанию меняются ТОЛЬКО пиксели найденных пятен, всё
            остальное возвращается байт в байт.

    Три разрешения в работе, и это осознанно:
        detect (384px) — поиск пятен: фиксированные ядра = размер в долях кадра;
        detect         — слой-заплатка: внутри прыща нужен цвет соседней кожи,
                         а не его собственная текстура, поэтому медиана мелкой
                         копии здесь не потеря, а ровно то, что нужно;
        полный размер  — маска кожи и склейка: границу глаза и губ нельзя
                         решать по копии, где она в 8 пикселей.

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
        skin_small = skin_mask(detect)
        spots = _blemish_mask(detect, skin_small)
        spot_share = _coverage(spots)

        out = base
        if spot_share > 0.0005:
            patch = detect.filter(ImageFilter.MedianFilter(_PROBE))
            patch = patch.resize(base.size, Image.LANCZOS)
            mask = spots.resize(base.size, Image.BILINEAR)
            # Ограничиваем область склейки. Полноразмерная маска кожи нужна,
            # чтобы не заехать на ресницы и брови, но одной её мало: сам
            # дефект цветовой тест кожи не проходит и оказывается ДЫРКОЙ в
            # ней — на тёмной коже так терялось всё лечение целиком. Поэтому
            # к коже добавляются закрытые дырки размером с прыщ, а волосы
            # вычитаются отдельной маской.
            closed = skin_small.filter(ImageFilter.MaxFilter(9)).filter(
                ImageFilter.MinFilter(9)).resize(base.size, Image.BILINEAR)
            hair = hair_mask(detect, _detect_darker(detect),
                             _skin_luma(detect, skin_small)).resize(
                base.size, Image.BILINEAR).filter(ImageFilter.MaxFilter(3))
            allow = _both(_either(skin_full, closed),
                          hair.point(lambda v: 255 - v))
            mask = _both(mask, allow.filter(ImageFilter.MinFilter(3)))
            mask = mask.filter(ImageFilter.GaussianBlur(max(1.0, 1.2 * full_scale)))
            if strength < 1.0:
                mask = mask.point(lambda v, s=strength: int(v * s))
            out = base.copy()
            out.paste(patch, (0, 0), mask)

        toned = False
        if even_tone:
            # Выравнивание тона: только в глубине кожи, мимо волос и мимо
            # краёв. Волосяная маска здесь та же, что защищает пятна, — она
            # единственное, что отличает бровь от тени на щеке.
            hair = hair_mask(detect, _detect_darker(detect),
                             _skin_luma(detect, skin_mask(detect))).resize(
                base.size, Image.BILINEAR).filter(ImageFilter.MaxFilter(3))
            tone = _both(skin_full.filter(ImageFilter.MinFilter(9)),
                         hair.point(lambda v: 255 - v))
            tone = _both(tone, _edge_mask(base, full_scale).point(lambda v: 255 - v))
            tone = tone.filter(ImageFilter.GaussianBlur(max(1.0, 1.5 * full_scale)))
            tone = tone.point(lambda v, a=0.38 * strength: int(v * a))
            smoothed = out.filter(ImageFilter.GaussianBlur(max(1.2, 1.8 * full_scale)))
            out = out.copy()
            out.paste(smoothed, (0, 0), tone)
            toned = True

        if spot_share > 0.0005:
            note = ("убрала пятна и выровняла тон кожи" if toned
                    else "точечно убрала дефекты кожи — остальное не трогала")
        else:
            note = ("выровняла тон кожи" if toned
                    else "явных дефектов на коже не нашла — оставила как есть")
        return out, RetouchReport(skin_share, spot_share, toned, note)
    except Exception as e:                                   # pragma: no cover
        logger.warning("retouch failed, отдаю оригинал: %s", e)
        return im, RetouchReport(0.0, 0.0, False, "ретушь не получилась")

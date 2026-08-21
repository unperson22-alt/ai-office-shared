"""
ai_office_shared.shared.photo_retouch — ретушь лица: убрать дефекты кожи.

ЧТО ЭТО ЗА ЗАДАЧА (три захода за 21.08.2026, все с фото на руках):
    1. Кнопка «🧴 Ретушь» вела в пресет «нежное» — размытие всего кадра. Прыщ
       остаётся прыщом, лицо теряет текстуру.
    2. Первая точечная версия прошла все тесты и превратила живое лицо в мыло:
       брови размазаны, ресницы съедены. Тесты гонялись на синтетическом лице
       из плоских заливок, где этот класс ошибки физически невидим.
    3. Вторая версия, зажатая до предела ради безопасности, вернула фото БЕЗ
       ЕДИНОГО изменения: она искала изолированные тёмные точки на гладком
       фоне, а настоящее «прибрати недоліки» — это пятнистость тона на
       пол-щеки, а не россыпь точек.

ЧТО РАБОТАЕТ (то, чем ретушируют в редакторах):
    ЧАСТОТНОЕ РАЗДЕЛЕНИЕ. Кадр раскладывается на тон (низкие частоты —
    покраснения, пятна, неровный цвет) и текстуру (высокие — поры, волоски,
    микрорельеф). Тон берётся сглаженный, текстура — исходная, и они
    собираются обратно. Пятнистость уходит, кожа остаётся кожей.
    Поверх — точечное лечение выраженных элементов медианой окрестности.

ГРАНИЦЫ РАБОТЫ:
    Только внутри маски кожи и мимо маски волос. Волос отличается от дефекта
    не темнотой (воспалённый прыщ тоже тёмный — на этом сгорел заход №3), а
    ТЕКСТУРОЙ: волос — десятки мелких перепадов на пятачке, прыщ — гладкое
    плавное пятно.

ЧЕГО ЗДЕСЬ НЕТ И ПОЧЕМУ:
    Распознавания лиц (цветовая маска плюс текстурные признаки дают ту же
    защиту дешевле) и нейросетей (обещали работу без ключей и лимитов).
    Морщины и крупные родинки не убираем — они крупные по построению.

ПРАВИЛО, ВЫСТРАДАННОЕ ЭТИМ МОДУЛЕМ:
    Зелёные тесты на синтетике не заменяют просмотр реальных фотографий. Всё,
    что здесь настроено, проверялось глазами на настоящих портретах — включая
    лица с настоящим акне, а не только с гладкой кожей.

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
# Дефекты ищутся на уменьшенной копии — и это не экономия, а суть метода. Ядра
# фильтров задаются в долях кадра, поэтому «маленькое пятно» и «крупная черта
# лица» определяются одинаково для селфи 4000px и сжатого Телеграмом 900px.
#
# Размер копии выбран не «поменьше»: на 384px прыщ занимал 4–6 пикселей и
# распадался на обрывки тоньше трёх, которые чистка шума честно выбрасывала
# вместе с самим дефектом — на лице с настоящим акне ретушь находила крохи и
# возвращала фото без изменений (фото Влада, третий заход 21.08.2026).
_DETECT_SIDE = 384
_K = _DETECT_SIDE / 384.0          # все ядра ниже подобраны при 384


def _odd(value: float, lo: int = 3, hi: int = 31) -> int:
    """Ближайший нечётный размер ядра (PIL требует нечётный) в разумных рамках."""
    size = int(round(value))
    if size % 2 == 0:
        size += 1
    return max(lo, min(hi, size))


_PROBE = _odd(13 * _K)       # окно «каким это место должно быть» ≈ 3.4% кадра
_OPEN = _odd(9 * _K)         # что переживает эрозию этим ядром — не прыщ, а глаз
_GROW = _odd(3 * _K)         # дорастить пятно от найденного контура внутрь
_GRAIN = _odd(3 * _K)        # чистка шума: одиночные пиксели матрицы
_HOLE = _odd(9 * _K)         # дырки в маске кожи размером с прыщ — закрыть
_EDGE = _odd(5 * _K)         # отступ от края кожи
# Потолок площади ретуши — доля КОЖИ, а не кадра, и считается ПОСЛЕ дорастания
# маски. Обе оговорки выстраданы за один день 21.08.2026: сначала потолок стоял
# до дорастания, и дилатация раздувала уже проверенную маску втрое (лицо
# превратилось в мыло); потом он был выставлен в 2% кадра — и на лице с
# настоящим акне, где поражена пятая часть щеки, ретушь упиралась в лимит,
# поднимала порог до максимума и не находила ничего.
#
# 15% кожи — это «много, но правдоподобно». Выше начинается не ретушь, а
# заливка лица, и правильный ответ — поднять порог и взять только худшее.
_MAX_SPOT_SHARE = 0.15
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

    Волос отличается от дефекта НЕ темнотой. Воспалённый прыщ тоже темнее кожи
    (иногда сильно), и правило «темнее кожи — значит волос» пометило волосами
    ровно все дефекты на лице: ретушь честно ничего не нашла и вернула фото без
    изменений (фото Влада, второй заход 21.08.2026).

    Отличает волос ТЕКСТУРА. Волос — это десятки мелких перепадов на пятачке;
    прыщ — гладкое плавное пятно, внутри него градиентов нет. Отсюда три
    правила, и в каждом текстура обязательна:

    1. Плотная мелкая текстура сама по себе — брови любого цвета, включая
       светлые и седые, ресницы, щетина.
    2. Умеренная текстура ПЛЮС темнее кожи — тёмные волосы, борода в тени.
    3. Плотность «темнее локальной медианы» плюс протяжённость — густые
       массивы волос, где отдельные волоски уже не разрешаются.
    """
    from PIL import ImageChops, ImageFilter

    dense = _ge(darker, 9).filter(ImageFilter.GaussianBlur(6.0 * _K))
    spread = _ge(dense, 88).filter(ImageFilter.MinFilter(_odd(3 * _K)))
    hair = spread.filter(ImageFilter.MaxFilter(_odd(9 * _K)))

    lum = small.convert("L")
    fine = _either(
        ImageChops.subtract(lum, lum.filter(ImageFilter.GaussianBlur(1.5))),
        ImageChops.subtract(lum.filter(ImageFilter.GaussianBlur(1.5)), lum))
    busy = _ge(fine, 10).filter(ImageFilter.GaussianBlur(5.0))
    hair = _either(hair, _ge(busy, 130).filter(ImageFilter.MaxFilter(5)))

    if skin_luma > 0:
        # Доля берётся не постоянной: у тёмной кожи весь диапазон яркостей
        # сжат, и порог «темнее кожи на четверть» отрезал бы вместе с волосами
        # сами дефекты. Чем темнее кожа, тем глубже порог.
        factor = 0.74 - 0.10 * (1.0 - min(1.0, skin_luma / 150.0))
        deep = _le(lum, int(skin_luma * factor))
        hair = _either(hair, _both(deep, _ge(busy, 84)).filter(
            ImageFilter.MaxFilter(_odd(5 * _K))))
    return hair


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
    closed = skin.filter(ImageFilter.MaxFilter(_HOLE)).filter(
        ImageFilter.MinFilter(_HOLE))
    inner = closed.filter(ImageFilter.MinFilter(_EDGE))   # отступ от края кожи
    luma = _skin_luma(small, inner)
    skin_share = _coverage(inner)
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
        spots = spots.filter(ImageFilter.MinFilter(_GRAIN)).filter(
            ImageFilter.MaxFilter(_GRAIN))
        big = spots.filter(ImageFilter.MinFilter(_OPEN)).filter(
            ImageFilter.MaxFilter(_OPEN))
        small_only = ImageChops.subtract(spots, big)

        # КОМПАКТНОСТЬ. Прыщ круглый, у него есть плотное ядро; кайма вдоль
        # брови или ресницы — вытянутая полоска в два-три пикселя шириной и
        # ядра не имеет. Оставляем только те пятна, что пережили эрозию, и
        # достраиваем их обратно до исходной формы (открытие с реконструкцией).
        # Без этого «дефекты» ложились полосами ровно по краю брови.
        seed = small_only.filter(ImageFilter.MinFilter(_GRAIN))
        core = _both(seed.filter(ImageFilter.MaxFilter(_PROBE)), small_only)
        grown = _both(core.filter(ImageFilter.MaxFilter(_GROW)), allowed)
        if _coverage(grown) <= _MAX_SPOT_SHARE * max(skin_share, 0.05):
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


def even_skin_tone(im, mask, strength: float):
    """
    Выравнивание тона через ЧАСТОТНОЕ РАЗДЕЛЕНИЕ — то, что в редакторах и
    называют ретушью кожи.

    Кадр раскладывается на две части: низкие частоты (тон, пятна, покраснения)
    и высокие (поры, волоски, микрорельеф). Тон берётся сильно сглаженный,
    текстура — исходная, и они собираются обратно. Пятнистость уходит, кожа
    остаётся кожей, а не пластиком.

    Почему именно так, а не точечное лечение пятен: акне и покраснения — это
    не изолированные точки на гладком фоне, а пятнистость тона на пол-щеки.
    Детектор отдельных пятен на таком лице находил крохи и возвращал фото
    практически без изменений (фото Влада, 21.08.2026).

    Радиус тона привязан к стороне кадра и намеренно невелик: сильнее — и
    вместе с пятнами уедет светотень, лицо станет плоским.
    """
    from PIL import ImageChops, ImageFilter

    side = max(im.size)
    tone_radius = max(3.0, side * 0.020)
    detail_radius = max(1.0, side * 0.0035)

    low = im.filter(ImageFilter.GaussianBlur(tone_radius))
    base = im.filter(ImageFilter.GaussianBlur(detail_radius))
    detail = ImageChops.subtract(im, base, scale=1, offset=128)
    rebuilt = ImageChops.add(low, detail, scale=1, offset=-128)

    out = im.copy()
    out.paste(rebuilt, (0, 0), mask.point(lambda v, s=strength: int(v * s)))
    return out


def retouch(im, strength: float = 1.0, even_tone: bool = True) -> tuple:
    """
    Ретушь лица. Возвращает (изображение, RetouchReport).

    Args:
        im: PIL.Image (после photo._open/_flatten).
        strength: 0.2 — «чуть-чуть», 1.0 — обычная ретушь.
        even_tone: выравнивать ли тон кожи. Это основная работа; выключается
            только если человек просит убрать ровно пятна и ничего больше.

    Два шага, оба только по коже и мимо волос:
        1. Точечное лечение выраженных дефектов — медиана окрестности вместо
           пятна.
        2. Выравнивание тона частотным разделением: гладкий тон плюс исходная
           текстура.

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
        darker = _detect_darker(detect)
        hair_small = hair_mask(detect, darker,
                               _skin_luma(detect, skin_small))
        spots = _blemish_mask(detect, skin_small)
        spot_share = _coverage(spots)

        hair = hair_small.resize(base.size, Image.BILINEAR).filter(
            ImageFilter.MaxFilter(3))

        out = base
        if spot_share > 0.0005:
            patch = detect.filter(ImageFilter.MedianFilter(_PROBE))
            patch = patch.resize(base.size, Image.LANCZOS)
            mask = spots.resize(base.size, Image.BILINEAR)
            # Ограничиваем область склейки. Полноразмерная маска кожи нужна,
            # чтобы не заехать на ресницы и брови, но одной её мало: сам
            # дефект цветовой тест кожи не проходит и оказывается ДЫРКОЙ в
            # ней — на тёмной коже так терялось всё лечение целиком.
            closed = skin_small.filter(ImageFilter.MaxFilter(_HOLE)).filter(
                ImageFilter.MinFilter(_HOLE)).resize(base.size, Image.BILINEAR)
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
            skin_area = _both(skin_full, hair.point(lambda v: 255 - v))
            skin_area = skin_area.filter(ImageFilter.MinFilter(3)).filter(
                ImageFilter.GaussianBlur(max(2.0, full_side * 0.004)))
            out = even_skin_tone(out, skin_area, 0.75 * strength)
            toned = True

        if spot_share > 0.0005:
            note = ("убрала дефекты и выровняла тон кожи" if toned
                    else "точечно убрала дефекты кожи — остальное не трогала")
        else:
            note = ("выровняла тон кожи — отдельных дефектов не нашла" if toned
                    else "явных дефектов на коже не нашла — оставила как есть")
        return out, RetouchReport(skin_share, spot_share, toned, note)
    except Exception as e:                                   # pragma: no cover
        logger.warning("retouch failed, отдаю оригинал: %s", e)
        return im, RetouchReport(0.0, 0.0, False, "ретушь не получилась")

"""
Тесты ретуши (shared/photo_retouch.py + маршрутизация в shared/photo.py).

Проверяем ровно то, ради чего фича делалась (запрос Яны 2026-08-21):
1. Пятно на коже действительно пропадает, а не слегка размывается.
2. Глаза, губы и фон остаются на месте — «ретушь», которая лечит глаз, хуже,
   чем её отсутствие.
3. Просьба на украинском («прибери недоліки на обличчі») попадает в ретушь, а
   не уезжает в LLM с ответом «я не умею редактировать изображения».
4. Три причины, по которым ретушь не работала на настоящем акне (02.09.2026) —
   TestRealAcneFailures. Числа там сняты с эталонной пары «до / после
   Фотошопа» и записаны, чтобы починку нельзя было тихо «улучшить» подгонкой
   порогов; сами фотографии в репозиторий не кладутся, поэтому каждый провал
   воспроизведён на фикстуре, а не на кадре.
"""
import asyncio
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared.photo import (  # noqa: E402
    is_photo_request, parse_request, process_photo, retouch, retouch_strength,
)

try:
    from PIL import Image, ImageDraw
    HAS_PIL = True
except Exception:                                    # pragma: no cover
    HAS_PIL = False

SKIN = (222, 172, 148)
SPOTS = [(150, 210), (190, 250), (230, 205), (170, 300), (240, 285)]
EYES = [(160, 150), (240, 150)]
BROWS = [(160, 122), (240, 122)]


def _face(size=(400, 460)) -> Image.Image:
    """
    Синтетическое «лицо»: телесный овал, глаза, губы, БРОВИ ИЗ ОТДЕЛЬНЫХ
    ВОЛОСКОВ и щетина на подбородке. Плюс микротекстура кожи.

    Волоски здесь не для красоты. Первая версия ретуши проверялась на лице без
    них — на плоской заливке не видно ни выеденных бровей, ни съеденных
    ресниц, и тесты были зелёными, пока Влад не прислал реальное фото с
    размазанным лицом (21.08.2026). Тонкий волосок мелкий и темнее окружения,
    то есть для детектора неотличим от прыща: единственное, что его спасает, —
    маска волос по плотности. Без волосков в тестовом кадре её регресс
    невидим.
    """
    im = Image.new("RGB", size, (60, 70, 90))            # фон — не кожа
    d = ImageDraw.Draw(im)
    d.ellipse([70, 40, 330, 420], fill=SKIN)
    px = im.load()
    for y in range(size[1]):                            # микротекстура кожи
        for x in range(size[0]):
            r, g, b = px[x, y]
            if (r, g, b) == SKIN:
                n = ((x * 7 + y * 13) % 11) - 5
                px[x, y] = (r + n, g + n, b + n)
    for ex, ey in EYES:                                 # глаза
        d.ellipse([ex - 22, ey - 12, ex + 22, ey + 12], fill=(250, 250, 250))
        d.ellipse([ex - 10, ey - 10, ex + 10, ey + 10], fill=(35, 30, 30))
    for bx, by in BROWS:                                # брови: 96 волосков
        for k in range(96):
            x0 = bx - 26 + (k % 48)
            row = k // 48
            d.line([x0, by + 4 - row * 3, x0 + 3, by - 3 - row * 3],
                   fill=(70, 55, 45), width=2)
    # Борода занимает нижнюю треть лица — как на настоящем портрете. Мелкий
    # клочок в тестах читался не как волосы, а как пятно на коже.
    for k in range(6000):
        x0 = 120 + (k * 37) % 160
        y0 = 330 + (k * 53) % 85
        d.line([x0, y0, x0 + 1, y0 + 3], fill=(80, 65, 55), width=2)
    d.ellipse([165, 355, 235, 380], fill=(170, 80, 85))  # губы поверх бороды
    return im


def _spotted(im: Image.Image) -> Image.Image:
    """
    Пять пятен «темнее и краснее кожи». Цвет считается ОТ тона кожи, а не
    зашит: на тёмной коже фиксированный (150, 85, 80) почти совпадал с самой
    кожей, и тест проверял не алгоритм, а собственную арифметику.
    """
    # Контраст пятна взят с реальных портретов: дефект краснее кожи и лишь
    # НЕМНОГО темнее (яркость ~0.8 от кожи). Раньше здесь было 0.55 — почти
    # чёрная точка: такая «родинка» темнее брови, и тест проверял не то, что
    # происходит на фото, а собственную арифметику.
    r, g, b = SKIN
    spot = (int(r * 0.95), int(g * 0.70), int(b * 0.68))
    out = im.copy()
    d = ImageDraw.Draw(out)
    for x, y in SPOTS:
        d.ellipse([x - 5, y - 5, x + 5, y + 5], fill=spot)
    return out


def _red_spotted(im: Image.Image) -> Image.Image:
    """
    Пятна, которые КРАСНЕЕ кожи, но почти не темнее её.

    Так выглядит настоящее акне, и это измерено, а не придумано: на эталонном
    кадре внутри щеки отклонение ЯРКОСТИ у вылеченных Фотошопом пятен
    неотличимо от обычной кожи (медиана дефекта ниже 95-го перцентиля чистой
    кожи), а отклонение КРАСНОТЫ отделяется. Детектор, построенный на темноте,
    такое пятно не видит вовсе — и ровно поэтому «ретушь» и «ретушь сильнее»
    на щеке Яны были неотличимы от оригинала.
    """
    # Пятно подобрано РАВНОЯРКИМ коже (яркость расходится на 0.1 из 255) и
    # отличается от неё только краснотой: +15 к r−g. Иначе тест ничего не
    # сторожит — детектор снимет такое пятно по темноте, и подмена канала
    # обратно на яркостный останется незамеченной (проверено мутацией).
    r, g, b = SKIN
    spot = (r + 10, int(round(g - 10 * 0.299 / 0.587)), b)
    out = im.copy()
    d = ImageDraw.Draw(out)
    for x, y in SPOTS:
        d.ellipse([x - 5, y - 5, x + 5, y + 5], fill=spot)
    return out


# Пропорции ЭТАЛОННОГО дефекта, померенные на паре «до / после Фотошопа»
# (кадр 1440×2560, лицо ~700 px): пятно ~40 px, тёмное ядро внутри него
# ~10 px, яркость ядра 0.74 / 0.56 / 0.53 от чистой кожи по каналам.
# Синтетическое лицо для этого увеличивается: на 400 px то же соотношение
# «пятно : ядро» физически не выражается, и фикстура проверяла бы не дефект,
# а собственную арифметику — ошибка, на которой этот файл уже горел дважды.
_CORE_FACE = (1200, 1380)
_CORE_SCALE = 3
_CORE_SPOT_R = 15
_CORE_CORE_R = 4


def _cored(im: Image.Image):
    """
    Лицо с пятнами, у каждого из которых есть тёмное ядро — корка.

    Возвращает (кадр, координаты пятен): координаты уезжают вместе с
    масштабом, и вычислять их на стороне теста — верный способ проверить не то.
    """
    big = im.resize(_CORE_FACE, Image.LANCZOS)
    out = big.copy()
    d = ImageDraw.Draw(out)
    r, g, b = SKIN
    spot = (int(r * 0.95), int(g * 0.70), int(b * 0.68))
    core = (int(r * 0.74), int(g * 0.56), int(b * 0.53))
    points = [(x * _CORE_SCALE, y * _CORE_SCALE) for x, y in SPOTS]
    for x, y in points:
        d.ellipse([x - _CORE_SPOT_R, y - _CORE_SPOT_R,
                   x + _CORE_SPOT_R, y + _CORE_SPOT_R], fill=spot)
        d.ellipse([x - _CORE_CORE_R, y - _CORE_CORE_R,
                   x + _CORE_CORE_R, y + _CORE_CORE_R], fill=core)
    return out, points


def _framed(im: Image.Image, pad: int) -> Image.Image:
    """Тот же кадр, но лицо занимает меньшую долю: вокруг — фон."""
    canvas = Image.new("RGB", (im.width + 2 * pad, im.height + 2 * pad),
                       (60, 70, 90))
    canvas.paste(im, (pad, pad))
    return canvas



# ── Портретная геометрия ──────────────────────────────────────────────────────
# Все фикстуры выше — лицо во весь кадр. У настоящего портрета кожа лежит ещё и
# на плечах, груди и руке, а между этими пятнами — волосы и одежда, поэтому
# ВЫРЕЗ ПО КОЖЕ занимает весь кадр и заполнен кожей лишь наполовину. От этого
# числа зависит рабочий масштаб детекции, то есть все ядра сразу: на эталонном
# кадре Влада доля кожи в вырезе 0.47 и k=2.31, а на «лице во весь кадр» — 0.9
# и k=1.35. Дефекты трёх предыдущих заходов жили именно на 2.3, и ни одна
# фикстура туда не доставала.
_PORTRAIT = (1440, 2560)


def _portrait(face: Image.Image, points):
    """Лицо в портретном кадре: волосы по бокам, плечи и рука снизу, одежда."""
    width, height = _PORTRAIT
    canvas = Image.new("RGB", (width, height), (58, 68, 88))
    d = ImageDraw.Draw(canvas)
    for x0, x1 in ((0, int(width * 0.30)), (int(width * 0.70), width)):
        d.rectangle([x0, int(height * 0.02), x1, int(height * 0.62)],
                    fill=(46, 36, 30))
        for k in range(4000):                       # пряди: волосам нужна текстура
            xx = x0 + (k * 37) % max(1, (x1 - x0))
            yy = int(height * 0.02) + (k * 53) % int(height * 0.60)
            d.line([xx, yy, xx + 2, yy + 9], fill=(78, 62, 50), width=1)
    fx, fy = (width - face.width) // 2, int(height * 0.04)
    canvas.paste(face, (fx, fy))
    d.ellipse([int(width * 0.18), int(height * 0.60),
               int(width * 0.82), int(height * 0.86)], fill=SKIN)      # грудь
    d.rectangle([int(width * 0.02), int(height * 0.66),
                 int(width * 0.24), height], fill=SKIN)                # рука
    d.rectangle([int(width * 0.30), int(height * 0.80),
                 int(width * 0.72), height], fill=(120, 122, 128))     # одежда
    px = canvas.load()
    for y in range(int(height * 0.58), height):     # микротекстура кожи ниже лица
        for x in range(width):
            r, g, b = px[x, y]
            if (r, g, b) == SKIN:
                n = ((x * 7 + y * 13) % 11) - 5
                px[x, y] = (r + n, g + n, b + n)
    moved = [(x + fx, y + fy) for x, y in points]
    for i, (x, y) in enumerate(moved):              # волоски поверх щеки
        d.line([x - 90, y - 96 + i * 7, x + 90, y - 52 + i * 7],
               fill=(96, 78, 64), width=2)
        d.line([x - 100, y + 46 + i * 5, x + 80, y + 104 + i * 5],
               fill=(110, 90, 74), width=1)
    return canvas, moved


def _jpeg(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def _patch_redness(im: Image.Image, xy, half=4) -> float:
    """Средняя КРАСНОТА (r−g) на пятачке. Яркость про акне не отвечает."""
    from PIL import ImageStat
    x, y = xy
    box = im.convert("RGB").crop((x - half, y - half, x + half, y + half))
    red, green, _ = ImageStat.Stat(box).mean
    return red - green


def _patch_mean(im: Image.Image, xy, half=4) -> float:
    x, y = xy
    box = im.convert("L").crop((x - half, y - half, x + half, y + half))
    px = list(box.tobytes())
    return sum(px) / len(px)


class TestRouting(unittest.TestCase):
    """Живая речь → операция ретуши. Русский и украинский одинаково."""

    def test_russian_requests(self):
        for text in ("сделай ретушь", "убери прыщи с лица", "почисти кожу",
                     "убери дефекты кожи"):
            self.assertEqual(parse_request(text).op, "retouch", text)

    def test_ukrainian_requests(self):
        # Ровно та фраза, на которой Крисс сказал «не умею» (21.08.2026).
        for text in ("Кріс, можеш будь ласка прибрати недоліки на обличчі?",
                     "прибери дефекти шкіри", "зроби ретуш", "прибери прищі"):
            self.assertEqual(parse_request(text).op, "retouch", text)
            self.assertTrue(is_photo_request(text), text)

    def test_retouch_beats_soft_preset(self):
        # «ретушь кожи» — это кнопка Крисса. Раньше она вела в пресет «нежное»,
        # то есть в общее размытие кадра.
        self.assertEqual(parse_request("ретушь кожи").op, "retouch")
        self.assertEqual(parse_request("сделай мягче").op, "preset")

    def test_strength_words(self):
        self.assertEqual(retouch_strength("ретушь"), 0.8)
        self.assertEqual(retouch_strength("трохи підретушуй"), 0.55)
        self.assertEqual(retouch_strength("чуть-чуть ретуши"), 0.55)
        self.assertEqual(retouch_strength("ретушь посильнее"), 1.0)
        self.assertEqual(retouch_strength("зроби шкіру гладкою"), 1.0)


@unittest.skipUnless(HAS_PIL, "Pillow не установлен")
class TestRetouchQuality(unittest.TestCase):
    """Главное: пятна уходят, лицо остаётся лицом."""

    @classmethod
    def setUpClass(cls):
        cls.clean = _face()
        cls.dirty = _spotted(cls.clean)
        data, cls.note, cls.skin = retouch(_jpeg(cls.dirty))
        cls.out = Image.open(io.BytesIO(data)).convert("RGB")

    def test_skin_detected(self):
        self.assertGreater(self.skin, 0.05, "кожа на лице обязана найтись")

    def test_spots_are_gone(self):
        """Каждое пятно должно приблизиться к цвету чистой кожи."""
        for xy in SPOTS:
            before = abs(_patch_mean(self.dirty, xy) - _patch_mean(self.clean, xy))
            after = abs(_patch_mean(self.out, xy) - _patch_mean(self.clean, xy))
            self.assertLess(after, before * 0.5,
                            f"пятно {xy} осталось: было {before:.1f}, стало {after:.1f}")

    def test_eyes_survive(self):
        """Зрачок обязан остаться тёмным: открытие маски не пускает ретушь в глаз."""
        for xy in EYES:
            self.assertLess(_patch_mean(self.out, xy, half=6), 90,
                            f"глаз {xy} затёрли ретушью")

    def test_background_untouched(self):
        for xy in ((20, 20), (380, 440)):
            self.assertAlmostEqual(_patch_mean(self.out, xy),
                                   _patch_mean(self.dirty, xy), delta=6)

    def test_size_preserved(self):
        self.assertEqual(self.out.size, self.dirty.size)

    def test_note_is_human_readable(self):
        self.assertIn("кож", self.note)

    def test_brows_and_stubble_survive(self):
        """
        Волоски бровей и щетина обязаны остаться. Именно их съедала первая
        версия: тонкий волосок «темнее окружения» и мелкий, то есть для
        детектора неотличим от прыща (реальное фото, 21.08.2026).
        """
        for xy in BROWS:
            before = _patch_mean(self.dirty, xy, half=20)
            after = _patch_mean(self.out, xy, half=20)
            self.assertLess(abs(after - before), 6,
                            f"бровь {xy} затёрли: было {before:.1f}, стало {after:.1f}")
        stubble = (200, 385)
        self.assertLess(abs(_patch_mean(self.out, stubble, half=18)
                            - _patch_mean(self.dirty, stubble, half=18)), 6,
                        "щетину замылили")

    def test_background_and_non_skin_are_untouched(self):
        """
        Ретушь выравнивает тон КОЖИ, поэтому доля изменённых пикселей уже не
        мала — но за пределы лица она выходить не имеет права. Первая версия
        считала «кожей» половину кадра вместе с деревянным шкафом на фоне.
        """
        from PIL import ImageChops
        diff = ImageChops.difference(self.dirty, self.out).convert("L")
        for xy in ((20, 20), (380, 440), (20, 440)):
            self.assertLess(_patch_mean(diff, xy, half=12), 4,
                            f"фон в точке {xy} тронут")
        w, h = self.dirty.size
        changed = sum(c for v, c in enumerate(diff.histogram()) if v > 6) / (w * h)
        self.assertLess(changed, 0.40,
                        f"изменено {changed * 100:.1f}% кадра — это больше, чем лицо")


@unittest.skipUnless(HAS_PIL, "Pillow не установлен")
class TestRetouchContract(unittest.TestCase):
    """Контракт «никогда не молчим и не падаем» — как у остальных операций."""

    def test_photo_without_face_gets_honest_answer(self):
        im = Image.new("RGB", (300, 300), (40, 90, 200))     # ни кожи, ни лица
        res = asyncio.run(process_photo(_jpeg(im), "зроби ретуш"))
        self.assertFalse(res.error)
        self.assertTrue(res.data)
        self.assertIn("не нашла", res.caption)

    def test_full_pipeline_returns_processed_jpeg(self):
        res = asyncio.run(process_photo(_jpeg(_spotted(_face())),
                                        "прибери недоліки на обличчі"))
        self.assertFalse(res.error)
        self.assertEqual(res.op, "retouch")
        self.assertEqual(Image.open(io.BytesIO(res.data)).size, (400, 460))

    def test_garbage_bytes_do_not_raise(self):
        res = asyncio.run(process_photo(b"not a photo at all", "ретушь"))
        self.assertTrue(res.error)

    def test_dark_skin_is_retouched_too(self):
        """
        Тёмная кожа — не «лицо не найдено». Одного RGB-правила (r>95) для неё
        не хватает, поэтому маска кожи дополнена тестом по цветности, а пороги
        детекта масштабируются яркостью кожи: тот же дефект даёт тем меньший
        перепад, чем темнее кожа.

        Граница метода честная и проверяется отдельным тестом ниже: при
        яркости кожи ниже ~60 из 255 перепад дефекта тонет в шуме сжатия.
        """
        import test_photo_retouch as module
        original = module.SKIN
        try:
            for tone in ((150, 105, 80), (120, 84, 66), (100, 68, 55)):
                module.SKIN = tone
                clean = _face()
                dirty = _spotted(clean)
                data, _, skin = retouch(_jpeg(dirty))
                out = Image.open(io.BytesIO(data)).convert("RGB")
                self.assertGreater(skin, 0.05, f"кожа {tone} не опознана")
                for xy in SPOTS:
                    before = abs(_patch_mean(dirty, xy) - _patch_mean(clean, xy))
                    after = abs(_patch_mean(out, xy) - _patch_mean(clean, xy))
                    self.assertLess(after, before * 0.5, f"{tone}: пятно {xy} осталось")
        finally:
            module.SKIN = original

    def test_too_dark_frame_is_left_alone_not_smeared(self):
        """
        Кадр настолько тёмный, что дефект тонет в шуме сжатия. Убрать его
        метод не сможет — но и портить лицо «на всякий случай» не имеет права:
        глаза, брови и кадр в целом обязаны остаться нетронутыми. Именно так
        выглядел провал, который поймал Влад.
        """
        import test_photo_retouch as module
        original = module.SKIN
        try:
            module.SKIN = (60, 41, 34)
            dirty = _spotted(_face())
            data, _, _ = retouch(_jpeg(dirty))
            out = Image.open(io.BytesIO(data)).convert("RGB")
            for xy in EYES:
                self.assertLess(_patch_mean(out, xy, half=6), 90, "глаз затёрли")
            for xy in BROWS:
                self.assertLess(abs(_patch_mean(out, xy, half=20)
                                    - _patch_mean(dirty, xy, half=20)), 6,
                                "бровь затёрли на тёмном кадре")
            from PIL import ImageChops
            diff = ImageChops.difference(dirty, out).convert("L")
            for xy in ((20, 20), (380, 440)):
                self.assertLess(_patch_mean(diff, xy, half=12), 4,
                                "фон тронут на тёмном кадре")
        finally:
            module.SKIN = original

    def test_tone_is_evened_by_default_and_can_be_switched_off(self):
        """
        Под «ретушью» люди имеют в виду и убрать дефекты, И выровнять тон:
        версия, которая трогала только изолированные точки, на реальном лице
        возвращала фото без единого видимого изменения (фото Влада, 21.08.2026).
        Отказ от выравнивания — отдельная просьба словами.
        """
        from ai_office_shared.shared.photo import wants_spots_only
        self.assertFalse(wants_spots_only("ретушь"))
        self.assertFalse(wants_spots_only("прибери недоліки"))
        self.assertTrue(wants_spots_only("убери только прыщи"))
        self.assertTrue(wants_spots_only("тон не трогай"))

        raw = _jpeg(_spotted(_face()))
        toned, note, _ = retouch(raw)
        spots_only, note2, _ = retouch(raw, even_tone=False)
        self.assertNotEqual(toned, spots_only, "выравнивание ничего не изменило")
        self.assertIn("тон", note)
        self.assertNotIn("тон", note2)

    def test_large_photo_is_handled(self):
        """Путь с уменьшенной копией для масок (кадр длиннее _WORK_SIDE)."""
        big = _face().resize((1400, 1610), Image.LANCZOS)
        data, _, skin = retouch(_jpeg(_spotted(big)))
        self.assertGreater(skin, 0.05)
        self.assertEqual(Image.open(io.BytesIO(data)).size, (1400, 1610))


@unittest.skipUnless(HAS_PIL, "Pillow не установлен")
class TestRealAcneFailures(unittest.TestCase):
    """
    Три причины, по которым ретушь ничего не делала на настоящем акне.

    Найдены 02.09.2026 по эталонной паре «до / после Фотошопа» (кадр
    1440×2560, подтверждённые дефекты в (712,826) и (752,746)). Каждая
    причина здесь воспроизведена отдельно: тест, который проверяет их скопом,
    краснеет от чего угодно и не говорит, что именно сломали.
    """

    # ── а) масштаб детекции был привязан к КАДРУ, а не к лицу ──────────────
    def test_defect_is_removed_whatever_share_of_frame_the_face_takes(self):
        """
        Одно и то же лицо в трёх кадрах разной ширины. Раньше детект-копия
        масштабировалась от стороны КАДРА (384 px), поэтому размер дефекта в
        её пикселях зависел от того, сколько вокруг лица пустого места: на
        ростовом портрете прыщ приходил к детектору втрое мельче, чем на
        селфи, распадался на обрывки в 1–2 px и погибал при чистке шума.
        """
        clean = _face()
        dirty = _spotted(clean)
        for pad in (0, 260, 620):
            frame = _framed(dirty, pad)
            data, _, _ = retouch(_jpeg(frame))
            out = Image.open(io.BytesIO(data)).convert("RGB")
            for x, y in SPOTS:
                xy = (x + pad, y + pad)
                before = abs(_patch_mean(_framed(dirty, pad), xy)
                             - _patch_mean(_framed(clean, pad), xy))
                after = abs(_patch_mean(out, xy)
                            - _patch_mean(_framed(clean, pad), xy))
                self.assertLess(after, before * 0.5,
                                f"поля {pad}px: пятно {x, y} осталось "
                                f"(было {before:.1f}, стало {after:.1f})")

    def test_detect_copy_holds_the_same_amount_of_skin_at_any_framing(self):
        """
        Прямая проверка того же корня: рабочая копия подбирается по ПЛОЩАДИ
        КОЖИ, поэтому кожи в ней всегда примерно поровну — сколько бы фона ни
        было вокруг лица. Именно это и делает один набор ядер пригодным и для
        селфи, и для ростового портрета.
        """
        from ai_office_shared.shared import photo_retouch as pr
        areas = []
        for pad in (0, 260, 620):
            frame = _framed(_spotted(_face()), pad).convert("RGB")
            work, _scale, _share = pr._work_copy(frame)
            skin = pr._coverage(pr.skin_mask(work))
            areas.append(skin * work.size[0] * work.size[1])
            self.assertLessEqual(work.size[0] * work.size[1], pr._DETECT_MAX_PX,
                                 "рабочая копия крупнее потолка — это время в чате")
        self.assertLess(max(areas) / min(areas), 1.7,
                        f"кожи в детект-копии разное количество: {areas} — "
                        "значит масштаб снова привязан к кадру, а не к лицу")

    # ── б) детектор искал темноту, а акне выдаёт себя краснотой ────────────
    def test_red_but_barely_dark_defect_is_removed(self):
        """
        Пятно краснее кожи и почти не темнее её — портрет настоящего акне.
        Детектор, у которого краснота идёт вторым каналом и с порогом ВЫШЕ
        яркостного, такое пятно не берёт: на эталоне отклик по красноте у
        дефектов был 9–11 при яркостном пороге 21.
        """
        clean = _face()
        dirty = _red_spotted(clean)
        # even_tone выключен НАМЕРЕННО: частотное разделение по всей коже само
        # сглаживает низкую частоту и убрало бы это пятно мимо детектора —
        # тогда тест сторожил бы не тот механизм (проверено мутацией).
        data, _, _ = retouch(_jpeg(dirty), even_tone=False)
        out = Image.open(io.BytesIO(data)).convert("RGB")
        for xy in SPOTS:
            # Проверяем по яркости, что пятно и правда равнояркое: иначе тест
            # незаметно превратится в ещё одну проверку темноты.
            self.assertLess(abs(_patch_mean(dirty, xy) - _patch_mean(clean, xy)), 2.0,
                            f"фикстура {xy} темнее кожи — тест проверяет не то")
            before = abs(_patch_redness(dirty, xy) - _patch_redness(clean, xy))
            after = abs(_patch_redness(out, xy) - _patch_redness(clean, xy))
            self.assertLess(after, before * 0.5,
                            f"красное пятно {xy} осталось: краснота была "
                            f"+{before:.1f}, стала +{after:.1f}")

    # ── в) лечение возвращало тёмное ядро дефекта обратно ──────────────────
    def test_dark_core_of_a_spot_is_healed_not_restored(self):
        """
        У пятна есть тёмное ядро мельче его самого. Оно целиком помещалось в
        радиус «своей текстуры» (~0.35% стороны), то есть текстурой и
        считалось: пятно закрашивалось медианой, а ядро подмешивалось
        обратно. На эталонном кадре центр дефекта менялся на 7 единиц из 55
        возможных — работа шла, а на фотографии не было видно ничего.
        """
        clean = _face().resize(_CORE_FACE, Image.LANCZOS)
        dirty, points = _cored(_face())
        data, _, _ = retouch(_jpeg(dirty))
        out = Image.open(io.BytesIO(data)).convert("RGB")
        for xy in points:
            gap = abs(_patch_mean(dirty, xy, half=3) - _patch_mean(clean, xy, half=3))
            left = abs(_patch_mean(out, xy, half=3) - _patch_mean(clean, xy, half=3))
            self.assertLess(left, gap * 0.25,
                            f"ядро пятна {xy} вернулось на место: было {gap:.1f}, "
                            f"стало {left:.1f}")

    def test_a_real_portrait_geometry_is_where_this_bug_lived(self):
        """
        Кадр, где кожа рассыпана по лицу, плечам и руке, а между ними волосы:
        вырез по коже занимает весь кадр и заполнен наполовину. Это геометрия
        эталонного фото Влада (доля кожи 0.47, k=2.31), а фикстуры «лицо во
        весь кадр» дают k≈1.35 — масштаб, которого на реальных портретах не
        бывает, и все ядра на нём другие.

        ЧТО ЭТОТ ТЕСТ СТОРОЖИТ, А ЧТО НЕТ — измерено, а не предположено.
        Провал 02.09 он НЕ воспроизводит: на этой фикстуре старый код убирает
        пятна даже чище нового (осталось 7% против 15%). Синтетическое пятно —
        крупный тёмный диск, то есть ровно то, что детектор «по темноте» брал
        легко на любом масштабе; на настоящем акне, где сигнал в красноте, тот
        же код не находил ничего. Разница между 7% и 15% — цена перехода на
        красноту, и она уплачена сознательно.

        Ценность теста в другом: он единственный гоняет ретушь на том рабочем
        масштабе, на котором она работает у людей. Любая будущая правка,
        ломающая ретушь ИМЕННО на портретной геометрии, здесь покраснеет — а
        на «лице во весь кадр» прошла бы незамеченной.
        """
        from ai_office_shared.shared import photo_retouch as pr
        face, points = _cored(_face())
        dirty, moved = _portrait(face, points)
        clean, _ = _portrait(_face().resize(_CORE_FACE, Image.LANCZOS), points)

        work, box, share = pr._work_copy(dirty.convert("RGB"))
        inside = share * (dirty.width * dirty.height) / \
            ((box[2] - box[0]) * (box[3] - box[1]))
        self.assertLess(inside, 0.60,
                        f"кожа заполняет вырез на {inside:.2f} — это «лицо во "
                        "весь кадр», а не портрет: масштаб проверяется не тот")
        k = max(work.size) / 384.0
        self.assertGreater(k, 2.0,
                           f"рабочий масштаб k={k:.2f} — ниже того, на котором "
                           "жили дефекты (эталон: 2.31)")

        data, _, _ = retouch(_jpeg(dirty))
        out = Image.open(io.BytesIO(data)).convert("RGB")
        for xy in moved:
            gap = abs(_patch_mean(dirty, xy, half=3) - _patch_mean(clean, xy, half=3))
            left = abs(_patch_mean(out, xy, half=3) - _patch_mean(clean, xy, half=3))
            self.assertLess(left, gap * 0.35,
                            f"на портретной геометрии пятно {xy} осталось: "
                            f"было {gap:.1f}, стало {left:.1f}")

    def test_texture_survives_the_healing(self):
        """
        Обратная сторона той же правки: радиус текстуры нельзя занижать без
        предела. Эталон Влада сохраняет поры, пушок и волоски поверх щеки —
        значит после лечения микроконтраст кожи ОБЯЗАН остаться.
        """
        from PIL import ImageStat
        dirty, _points = _cored(_face())
        data, _, _ = retouch(_jpeg(dirty), even_tone=False)
        out = Image.open(io.BytesIO(data)).convert("RGB")
        box = (330, 480, 870, 990)          # чистая кожа щеки, без пятен
        before = ImageStat.Stat(dirty.convert("L").crop(box)).stddev[0]
        after = ImageStat.Stat(out.convert("L").crop(box)).stddev[0]
        self.assertGreater(after, before * 0.7,
                           f"текстуру кожи съели: контраст {before:.2f} → {after:.2f}")


if __name__ == "__main__":
    unittest.main()

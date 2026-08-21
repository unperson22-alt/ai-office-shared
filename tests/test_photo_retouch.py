"""
Тесты ретуши (shared/photo_retouch.py + маршрутизация в shared/photo.py).

Проверяем ровно то, ради чего фича делалась (запрос Яны 2026-08-21):
1. Пятно на коже действительно пропадает, а не слегка размывается.
2. Глаза, губы и фон остаются на месте — «ретушь», которая лечит глаз, хуже,
   чем её отсутствие.
3. Просьба на украинском («прибери недоліки на обличчі») попадает в ретушь, а
   не уезжает в LLM с ответом «я не умею редактировать изображения».
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
    for k in range(1200):                               # борода на подбородке
        x0 = 148 + (k * 17) % 104
        y0 = 370 + (k * 23) % 30
        d.line([x0, y0, x0 + 1, y0 + 3], fill=(80, 65, 55), width=2)
    d.ellipse([160, 330, 240, 360], fill=(170, 80, 85))  # губы
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


def _jpeg(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=95)
    return buf.getvalue()


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
        self.assertEqual(retouch_strength("ретушь"), 1.0)
        self.assertEqual(retouch_strength("трохи підретушуй"), 0.55)
        self.assertEqual(retouch_strength("чуть-чуть ретуши"), 0.55)
        self.assertEqual(retouch_strength("ретушь посильнее"), 1.0)


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

    def test_touches_only_a_small_part_of_the_frame(self):
        """
        Ретушь — точечная операция. Если изменилось больше нескольких
        процентов кадра, это уже не «убрала дефекты», а «замылила лицо»:
        на реальном портрете первая версия трогала 19% кадра.
        """
        from PIL import ImageChops
        diff = ImageChops.difference(self.dirty, self.out).convert("L")
        w, h = self.dirty.size
        changed = sum(c for v, c in enumerate(diff.histogram()) if v > 6) / (w * h)
        self.assertLess(changed, 0.06, f"изменено {changed * 100:.1f}% кадра")


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
            w, h = dirty.size
            changed = sum(c for v, c in enumerate(diff.histogram()) if v > 6) / (w * h)
            self.assertLess(changed, 0.03,
                            f"на тёмном кадре тронуто {changed * 100:.1f}% — это уже мазня")
        finally:
            module.SKIN = original

    def test_large_photo_is_handled(self):
        """Путь с уменьшенной копией для масок (кадр длиннее _WORK_SIDE)."""
        big = _face().resize((1400, 1610), Image.LANCZOS)
        data, _, skin = retouch(_jpeg(_spotted(big)))
        self.assertGreater(skin, 0.05)
        self.assertEqual(Image.open(io.BytesIO(data)).size, (1400, 1610))


if __name__ == "__main__":
    unittest.main()

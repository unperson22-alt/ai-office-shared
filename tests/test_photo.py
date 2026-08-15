"""
Тесты обработки фото (shared/photo.py).

Главное, что здесь проверяется, — контракт «никогда не молчим и не падаем»:
битый файл, пустые байты, непонятная подпись, отсутствующий rembg и
ненастроенный AI-провайдер обязаны давать осмысленный ответ пользователю,
а не исключение в логе. Это тот же класс багов, из-за которого боты молчали
на картинки до media.py (см. его docstring).

Второе — маршрутизация живой речи: «сделай ярче на 40%», «убери фон»,
«на аву» должны попадать в разные операции, иначе фича бесполезна.
"""
import asyncio
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared import photo  # noqa: E402
from ai_office_shared.shared.photo import (  # noqa: E402
    PRESETS, apply_preset, compress, crop_to, parse_request, process_photo, upscale,
)

try:
    from PIL import Image
    HAS_PIL = True
except Exception:                                    # pragma: no cover
    HAS_PIL = False


def _sample(size=(400, 300), mode="RGB") -> bytes:
    """Небелая картинка с градиентом и деталями — на плоской заливке эффекты не видны."""
    im = Image.new(mode, size)
    px = im.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = (x * 255 // size[0], y * 255 // size[1], (x + y) % 256)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=95)
    return buf.getvalue()


class TestParseRequest(unittest.TestCase):
    """Живая речь → операция."""

    def test_empty_caption_is_auto(self):
        self.assertEqual(parse_request(""), ("preset", "авто", {}, ""))
        self.assertEqual(parse_request("   ").preset, "авто")

    def test_unknown_text_falls_back_to_auto(self):
        # Нельзя ругаться на непонятную подпись — делаем разумное по умолчанию.
        self.assertEqual(parse_request("хз сделай что-нибудь").preset, "авто")

    def test_presets_by_natural_words(self):
        cases = {
            "сделай чб": "чб",
            "хочу винтаж": "винтаж",
            "ретушь кожи": "нежное",
            "как на плёнку": "плёнка",
            "сделай сочнее цвета": "сочное",
            "теплее сделай": "тепло",
        }
        for text, preset in cases.items():
            self.assertEqual(parse_request(text).preset, preset, text)

    def test_background_and_format_ops(self):
        cases = {
            "убери фон": "remove_bg",
            "размой фон": "blur_bg",
            "белый фон для документов": "white_bg",
            "сделай стикер": "sticker",
            "на аву": "avatar",
            "квадрат для инсты": "square",
            "под стори": "story",
            "увеличь качество": "upscale",
            "сожми пожалуйста": "compress",
        }
        for text, op in cases.items():
            self.assertEqual(parse_request(text).op, op, text)

    def test_percent_tweaks(self):
        self.assertEqual(parse_request("сделай ярче на 40%").tweaks, {"brightness": 1.4})
        self.assertEqual(parse_request("резче на 50").tweaks, {"sharpness": 1.5})
        self.assertEqual(parse_request("темнее на 20%").tweaks["brightness"], 0.8)

    def test_tweak_word_does_not_drag_in_whole_preset(self):
        # «контраст -30» — это подкрутка, а не пресет «драма» с виньеткой.
        req = parse_request("контраст -30")
        self.assertEqual(req.preset, "")
        self.assertEqual(req.tweaks, {"contrast": 0.7})

    def test_preset_plus_tweak_together(self):
        req = parse_request("драматичный контраст")
        self.assertEqual(req.preset, "драма")

    def test_ai_request_keeps_prompt(self):
        req = parse_request("преврати меня в киберпанк")
        self.assertEqual(req.op, "ai")
        self.assertIn("киберпанк", req.prompt)


@unittest.skipUnless(HAS_PIL, "Pillow не установлен")
class TestLocalEngine(unittest.TestCase):
    """Локальные операции: результат — валидный JPEG нужного размера."""

    def setUp(self):
        self.raw = _sample()

    def test_every_preset_produces_valid_jpeg(self):
        for name in PRESETS:
            out = apply_preset(self.raw, name)
            im = Image.open(io.BytesIO(out))
            self.assertEqual(im.format, "JPEG", name)
            self.assertEqual(im.size, (400, 300), name)
            self.assertNotEqual(out, self.raw, f"{name} ничего не изменил")

    def test_presets_differ_from_each_other(self):
        # Если два «разных» фильтра дают одинаковые байты — один из них мёртв.
        outs = {name: apply_preset(self.raw, name) for name in PRESETS}
        self.assertEqual(len(set(outs.values())), len(outs))

    def test_tweaks_change_image(self):
        base = apply_preset(self.raw, "")
        bright = apply_preset(self.raw, "", {"brightness": 1.5})
        self.assertNotEqual(base, bright)

    def test_unknown_preset_is_not_a_crash(self):
        out = apply_preset(self.raw, "такого-пресета-нет")
        self.assertEqual(Image.open(io.BytesIO(out)).format, "JPEG")

    def test_crop_shapes(self):
        self.assertEqual(Image.open(io.BytesIO(crop_to(self.raw, "square"))).size, (300, 300))
        self.assertEqual(Image.open(io.BytesIO(crop_to(self.raw, "avatar"))).size, (640, 640))
        w, h = Image.open(io.BytesIO(crop_to(self.raw, "story"))).size
        self.assertAlmostEqual(w / h, 9 / 16, places=2)

    def test_upscale_doubles_and_caps(self):
        self.assertEqual(Image.open(io.BytesIO(upscale(self.raw, 2))).size, (800, 600))
        big = Image.open(io.BytesIO(upscale(_sample((2000, 1500)), 4))).size
        self.assertLessEqual(max(big), 4096)

    def test_compress_hits_target(self):
        heavy = _sample((1600, 1200))
        small = compress(heavy, target_kb=100)
        self.assertLess(len(small), len(heavy))
        self.assertLessEqual(len(small), 130 * 1024)

    def test_exif_rotated_photo_comes_back_upright(self):
        # Телефоны шлют кадр «лёжа» + EXIF-флаг поворота. Без exif_transpose
        # обработанное фото уезжает набок — классическая жалоба «ты повернула».
        im = Image.open(io.BytesIO(_sample((400, 300))))
        buf = io.BytesIO()
        exif = im.getexif()
        exif[274] = 6                                  # Orientation: rotate 90° CW
        im.save(buf, "JPEG", exif=exif)
        out = Image.open(io.BytesIO(apply_preset(buf.getvalue(), "авто")))
        self.assertEqual(out.size, (300, 400))

    def test_png_with_alpha_survives(self):
        buf = io.BytesIO()
        Image.new("RGBA", (100, 100), (255, 0, 0, 128)).save(buf, "PNG")
        out = apply_preset(buf.getvalue(), "яркое")
        self.assertEqual(Image.open(io.BytesIO(out)).format, "JPEG")


@unittest.skipUnless(HAS_PIL, "Pillow не установлен")
class TestProcessPhotoContract(unittest.TestCase):
    """process_photo не бросает исключений — всегда data либо error."""

    def test_default_request_is_processed(self):
        res = asyncio.run(process_photo(_sample(), ""))
        self.assertTrue(res.ok)
        self.assertEqual(res.error, "")
        self.assertTrue(res.caption)

    def test_empty_bytes_give_error_text(self):
        res = asyncio.run(process_photo(b"", "улучши"))
        self.assertFalse(res.ok)
        self.assertTrue(res.error)

    def test_garbage_bytes_give_error_text_not_exception(self):
        res = asyncio.run(process_photo(b"\x00\x01not-an-image" * 50, "винтаж"))
        self.assertFalse(res.ok)
        self.assertIn("⚠️", res.error)

    def test_result_carries_filename(self):
        res = asyncio.run(process_photo(_sample(), "квадрат"))
        _, name = res.as_file()
        self.assertTrue(name.endswith(".jpg"))

    def test_background_op_without_rembg_degrades_with_explanation(self):
        # Нет rembg — не отказ и не молчание: отдаём портретную обработку
        # и объясняем, чего не хватает.
        original = photo.rembg_available
        photo.rembg_available = lambda: False
        try:
            res = asyncio.run(process_photo(_sample(), "убери фон"))
        finally:
            photo.rembg_available = original
        self.assertTrue(res.ok)
        self.assertIn("rembg", res.caption)

    def test_ai_without_credentials_explains_and_offers_fallback(self):
        env = {k: os.environ.pop(k, None) for k in ("CF_ACCOUNT_ID", "CF_API_TOKEN")}
        try:
            res = asyncio.run(photo.ai_restyle(_sample(), "в стиле киберпанк"))
        finally:
            for k, v in env.items():
                if v is not None:
                    os.environ[k] = v
        self.assertFalse(res.ok)
        self.assertIn("CF_ACCOUNT_ID", res.error)


class TestHelpText(unittest.TestCase):
    def test_help_lists_real_presets(self):
        # Помощь врёт → пользователь просит несуществующий фильтр и получает «авто».
        for name in ("винтаж", "чб", "нуар"):
            self.assertIn(name, photo.PHOTO_HELP)


if __name__ == "__main__":
    unittest.main()

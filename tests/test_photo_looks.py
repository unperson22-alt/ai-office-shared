"""
Тесты адаптивного движка (shared/photo_looks.py) и арт-директора (photo_ai.py).

Проверяется не «картинка изменилась» — это ничего не доказывает, — а что
движок реагирует на СОДЕРЖИМОЕ кадра: тёмный осветляется, пересвеченный
притормаживается, намеренно светлый остаётся светлым, портрет не получает
полную дозу микроконтраста. Плюс граница безопасности вокруг модели: что бы
она ни вернула, в рендер уходят значения из допустимого диапазона.
"""
import asyncio
import io
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared import photo_ai, photo_looks  # noqa: E402
from ai_office_shared.shared.photo import PRESETS, apply_preset  # noqa: E402

try:
    from PIL import Image
    HAS_PIL = True
except Exception:                                        # pragma: no cover
    HAS_PIL = False


def _frame(mean=128, spread=60, size=(320, 240), tint=(1.0, 1.0, 1.0)):
    """Кадр с заданной средней яркостью и разбросом — вместо реального фото."""
    im = Image.new("RGB", size)
    px = im.load()
    w, h = size
    for y in range(h):
        for x in range(w):
            v = mean + spread * ((x / w) - 0.5) * 2
            v = max(0, min(255, v))
            px[x, y] = (min(255, int(v * tint[0])),
                        min(255, int(v * tint[1])),
                        min(255, int(v * tint[2])))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=95)
    return buf.getvalue(), im


@unittest.skipUnless(HAS_PIL, "Pillow не установлен")
class TestAnalyze(unittest.TestCase):

    def test_measures_brightness_and_contrast(self):
        _, dark = _frame(mean=60, spread=20)
        _, bright = _frame(mean=200, spread=20)
        self.assertLess(photo_looks.analyze(dark).mean, 90)
        self.assertGreater(photo_looks.analyze(bright).mean, 170)

    def test_flat_frame_has_low_contrast(self):
        _, flat = _frame(mean=128, spread=3)
        _, punchy = _frame(mean=128, spread=110)
        self.assertLess(photo_looks.analyze(flat).contrast,
                        photo_looks.analyze(punchy).contrast)

    def test_skin_detected_only_on_skin_tones(self):
        skin = Image.new("RGB", (64, 64), (215, 165, 130))
        grass = Image.new("RGB", (64, 64), (60, 140, 60))
        self.assertGreater(photo_looks.analyze(skin).skin, 0.9)
        self.assertLess(photo_looks.analyze(grass).skin, 0.05)


@unittest.skipUnless(HAS_PIL, "Pillow не установлен")
class TestAdaptivePlan(unittest.TestCase):
    """План обязан зависеть от кадра — иначе это те же фиксированные проценты."""

    def test_dark_frame_is_lightened_bright_is_darkened(self):
        dark = photo_looks.plan("авто", photo_looks.analyze(_frame(mean=55)[1]))
        bright = photo_looks.plan("авто", photo_looks.analyze(_frame(mean=205)[1]))
        self.assertLess(dark["gamma"], 1.0, "тёмный кадр должен осветляться")
        self.assertGreater(bright["gamma"], 1.0, "светлый — затемняться")

    def test_flat_frame_gets_stronger_curve(self):
        flat = photo_looks.plan("сочное", photo_looks.analyze(_frame(spread=6)[1]))
        punchy = photo_looks.plan("сочное", photo_looks.analyze(_frame(spread=115)[1]))
        self.assertGreater(flat["curve"], punchy["curve"])

    def test_high_key_frame_keeps_its_airiness(self):
        # Намеренно светлый кадр без теней: тянуть его к средней яркости —
        # ровно та ошибка, из-за которой автообработка выглядит грубой.
        stats = photo_looks.analyze(_frame(mean=205, spread=25)[1])
        p = photo_looks.plan("авто", stats)
        self.assertGreater(p["intent"], 0.5)
        self.assertLess(p["gamma"], 1.2, "high-key кадр нельзя топить")
        self.assertGreater(p["black"], 10, "чёрную точку ему не сажают в 0")

    def test_portrait_gets_less_clarity_than_landscape(self):
        skin = Image.new("RGB", (128, 128), (214, 166, 132))
        land = Image.new("RGB", (128, 128), (70, 120, 180))
        p_skin = photo_looks.plan("чёткое", photo_looks.analyze(skin))
        p_land = photo_looks.plan("чёткое", photo_looks.analyze(land))
        self.assertLess(p_skin["clarity"], p_land["clarity"])

    def test_grain_scales_with_resolution(self):
        small = photo_looks.plan("плёнка", photo_looks.analyze(_frame(size=(400, 300))[1]))
        big = photo_looks.plan("плёнка", photo_looks.analyze(_frame(size=(3000, 2000))[1]))
        self.assertLess(small["scale"], big["scale"])

    def test_every_preset_has_a_look(self):
        # Пресет без рецепта молча превращается в «авто» — пользователь
        # нажимает «нуар», получает автокоррекцию и не понимает почему.
        self.assertEqual(set(PRESETS), set(photo_looks.LOOKS))


@unittest.skipUnless(HAS_PIL, "Pillow не установлен")
class TestRenderEffects(unittest.TestCase):

    def test_bw_mixer_is_not_plain_grayscale(self):
        # Красный и синий одной яркости обязаны разойтись в тонах — иначе это
        # обычный convert("L"), от которого ч/б и выглядит вялым.
        red = Image.new("RGB", (32, 32), (200, 60, 60))
        blue = Image.new("RGB", (32, 32), (60, 60, 200))
        mix = photo_looks.LOOKS["чб"]["bw"]
        mixed = (photo_looks._to_bw(red, mix).getpixel((5, 5))[0]
                 - photo_looks._to_bw(blue, mix).getpixel((5, 5))[0])
        plain = red.convert("L").getpixel((5, 5)) - blue.convert("L").getpixel((5, 5))
        self.assertGreater(mixed, plain + 10,
                           "микшер обязан разводить цвета сильнее обычного grayscale")

    def test_grain_is_strongest_in_midtones(self):
        from PIL import ImageChops, ImageStat

        def noise_level(value):
            base = Image.new("RGB", (200, 200), (value, value, value))
            out = photo_looks._grain(base, 0.8, 1.0)
            return ImageStat.Stat(ImageChops.difference(base, out)).mean[0]

        self.assertGreater(noise_level(128), noise_level(250) + 1.0)
        self.assertGreater(noise_level(128), noise_level(6) + 1.0)

    def test_gamma_direction(self):
        base = Image.new("RGB", (16, 16), (128, 128, 128))
        self.assertLess(photo_looks._gamma(base, 1.4).getpixel((1, 1))[0], 128)
        self.assertGreater(photo_looks._gamma(base, 0.7).getpixel((1, 1))[0], 128)

    def test_looks_produce_distinct_results(self):
        raw, _ = _frame(mean=130, spread=70, tint=(1.05, 1.0, 0.95))
        outs = {name: apply_preset(raw, name) for name in PRESETS}
        self.assertEqual(len(set(outs.values())), len(outs))


class TestDirectorSafety(unittest.TestCase):
    """Числа модели — не приказ. За границы диапазона она выйти не может."""

    def test_out_of_range_values_are_clamped(self):
        adj = photo_ai._clamp_adjustments({"adjust": {
            "gamma": 9.0, "sat_factor": -5, "clarity": 99, "vignette": 0.5}})
        self.assertEqual(adj["gamma"], 1.45)
        self.assertEqual(adj["sat_factor"], 0.0)
        self.assertEqual(adj["clarity"], 1.5)
        self.assertEqual(adj["vignette"], 0.5)

    def test_unknown_and_broken_keys_are_dropped(self):
        adj = photo_ai._clamp_adjustments({"adjust": {
            "delete_everything": 1, "gamma": "не число", "curve": None}})
        self.assertEqual(adj, {})

    def test_split_is_validated(self):
        ok = photo_ai._clamp_adjustments({"split": {
            "sh": [10, 20, 300], "hi": [255, 240, 200], "amount": 5}})
        self.assertEqual(ok["split"]["sh"], (10, 20, 255))
        self.assertEqual(ok["split"]["amount"], 0.8)
        self.assertEqual(photo_ai._clamp_adjustments({"split": {"sh": [1, 2, 3]}}), {})

    def test_json_survives_markdown_fence_and_chatter(self):
        data = photo_ai._extract_json(
            'Вот план:\n```json\n{"adjust": {"gamma": 1.1}, "why": "ок"}\n```\nГотово')
        self.assertEqual(data["adjust"]["gamma"], 1.1)
        self.assertEqual(photo_ai._extract_json("совсем не json"), {})

    def test_director_off_without_env_or_key(self):
        saved = {k: os.environ.pop(k, None) for k in ("PHOTO_AI_DIRECTOR", "ANTHROPIC_API_KEY")}
        try:
            self.assertFalse(photo_ai.director_enabled())
            os.environ["PHOTO_AI_DIRECTOR"] = "1"
            self.assertFalse(photo_ai.director_enabled(), "без ключа включаться нечему")
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


@unittest.skipUnless(HAS_PIL, "Pillow не установлен")
class TestDirectorIntegration(unittest.TestCase):

    def _run_with_response(self, status, body):
        """Гоняет plan_with_director с подставным ответом Anthropic."""
        _, im = _frame()
        response = MagicMock()
        response.status_code = status
        response.json.return_value = body
        response.text = str(body)
        client = MagicMock()
        client.post = AsyncMock(return_value=response)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        os.environ["PHOTO_AI_DIRECTOR"] = "1"
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        try:
            with patch("httpx.AsyncClient", return_value=client):
                return asyncio.run(photo_ai.plan_with_director(im, "авто", "улучши"))
        finally:
            os.environ.pop("PHOTO_AI_DIRECTOR", None)
            os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_model_suggestions_reach_the_plan(self):
        params, why = self._run_with_response(200, {"content": [{"type": "text", "text":
            '{"adjust": {"clarity": 0.9, "vignette": 0.3}, "why": "вытянула детали"}'}]})
        self.assertEqual(params["clarity"], 0.9)
        self.assertEqual(params["vignette"], 0.3)
        self.assertEqual(why, "вытянула детали")
        # Служебные поля базового плана на месте — рендер без них не соберётся.
        self.assertIn("in_black", params)
        self.assertIn("scale", params)

    def test_api_failure_falls_back_to_deterministic_plan(self):
        params, why = self._run_with_response(500, {"error": "boom"})
        self.assertEqual(why, "")
        self.assertIn("gamma", params)          # обычный план на месте

    def test_garbage_answer_falls_back(self):
        params, why = self._run_with_response(
            200, {"content": [{"type": "text", "text": "не знаю, сделай сам"}]})
        self.assertEqual(why, "")
        self.assertIn("curve", params)

    def test_bw_look_stays_bw_even_if_model_asks_for_saturation(self):
        _, im = _frame()
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"content": [{"type": "text", "text":
            '{"adjust": {"sat_factor": 1.8}, "why": "цвета"}'}]}
        client = MagicMock()
        client.post = AsyncMock(return_value=response)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        os.environ["PHOTO_AI_DIRECTOR"] = "1"
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        try:
            with patch("httpx.AsyncClient", return_value=client):
                params, _ = asyncio.run(photo_ai.plan_with_director(im, "чб", "чб"))
        finally:
            os.environ.pop("PHOTO_AI_DIRECTOR", None)
            os.environ.pop("ANTHROPIC_API_KEY", None)
        self.assertIn("bw", params)
        self.assertNotIn("sat_factor", params)


if __name__ == "__main__":
    unittest.main()

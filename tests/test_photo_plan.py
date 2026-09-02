"""
Тесты составных просьб к обработке фото (shared/photo.py).

Запрос Яны 02.09.2026, дословно: «Мені потрібно на одній фотографії ОДНОЧАСНО
і ретуш і чб фільтр». До этого она сформулировала то же самое дважды, и все три
раза получала одну ретушь: parse_request возвращался на первом совпадении в
_OP_ALIASES, где «ретушь» стоит раньше «чб». Ошибка была не в её словах.

Здесь проверяется то, что ломается молча:
1. Составная просьба — это ЦЕПОЧКА шагов, а не выбор одного из них.
2. Порядок шагов задаёт конвейер, а не порядок слов: ретушь ищет кожу по цвету
   и после «чб» не сделала бы ничего.
3. Слова, называющие сюжет («на обличчі»), не превращаются во второй шаг.
4. Подпись честно перечисляет ВСЁ, что сделано.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared.photo import (  # noqa: E402
    is_photo_request, parse_plan, parse_request, process_photo, wants_original,
)

try:
    from PIL import Image  # noqa: F401
    HAS_PIL = True
except Exception:                                    # pragma: no cover
    HAS_PIL = False

if HAS_PIL:
    from test_photo_retouch import _face, _jpeg, _patch_mean, _spotted, SPOTS


def _ops(text: str) -> list:
    """Список операций плана: [('retouch', ''), ('preset', 'чб')]."""
    return [(s.op, s.preset) for s in parse_plan(text)]


class PlanParsingTest(unittest.TestCase):

    def test_yana_asked_for_both_and_gets_both(self):
        """Три её формулировки подряд — все три должны дать два шага."""
        for text in ("ретуш і ч/б фільтр на одній фотографії",
                     "Мені потрібно на одній фотографії одночасно і ретуш і чб фільтр",
                     "ретушь и чб"):
            self.assertEqual(_ops(text), [("retouch", ""), ("preset", "чб")], text)

    def test_order_is_the_pipeline_not_the_sentence(self):
        """
        «Чб і ретуш» — ретушь всё равно первая. Не косметика: маска кожи ищет
        r > g > b, на обесцвеченном кадре r == g == b и ретушь не найдёт ничего.
        """
        self.assertEqual(_ops("спочатку чб, потім ретуш"),
                         [("retouch", ""), ("preset", "чб")])

    def test_three_steps_keep_size_changing_ops_last(self):
        plan = _ops("ретушь, убери фон и сделай квадрат")
        self.assertEqual(plan, [("retouch", ""), ("remove_bg", ""), ("square", "")])

    def test_subject_words_are_not_a_second_step(self):
        """
        «Прибери недоліки на обличчі» — ретушь ЛИЦА, а не ретушь плюс портретный
        фильтр: тот смягчил бы весь кадр поверх неё. Ровно это Яна и называла
        «маскує все, але не те, що потрібно».
        """
        self.assertEqual(_ops("прибери недоліки на обличчі"), [("retouch", "")])
        self.assertEqual(_ops("ретушь лица"), [("retouch", "")])

    def test_portrait_and_auto_still_work_alone(self):
        """Запасные пресеты не сломаны — кнопки шлют ровно эти слова."""
        self.assertEqual(_ops("портрет"), [("preset", "портрет")])
        self.assertEqual(_ops("улучши"), [("preset", "авто")])
        self.assertEqual(_ops(""), [("preset", "авто")])

    def test_auto_does_not_stack_on_a_named_filter(self):
        """«Улучши и сделай чб» — это чб, а не автокоррекция поверх неё."""
        self.assertEqual(_ops("улучши и сделай чб"), [("preset", "чб")])
        self.assertEqual(_ops("обработай: ретушь"), [("retouch", "")])

    def test_ai_restyle_stays_exclusive(self):
        self.assertEqual(_ops("преврати меня в киберпанк и сделай чб"), [("ai", "")])

    def test_tweaks_survive_next_to_an_operation(self):
        plan = parse_plan("ретушь и ярче на 30%")
        self.assertEqual([s.op for s in plan], ["retouch", "preset"])
        self.assertAlmostEqual(plan[1].tweaks["brightness"], 1.3)

    def test_single_requests_are_unchanged(self):
        """Одиночные просьбы разбираются ровно как раньше."""
        for text, expected in (("чб", [("preset", "чб")]),
                               ("винтаж", [("preset", "винтаж")]),
                               ("убери фон", [("remove_bg", "")]),
                               ("сожми", [("compress", "")]),
                               ("что на этом фото?", [("preset", "авто")])):
            self.assertEqual(_ops(text), expected, text)

    def test_parse_request_returns_the_first_step(self):
        self.assertEqual(parse_request("ретушь и чб").op, "retouch")

    def test_compound_request_is_recognised_as_a_photo_request(self):
        self.assertTrue(is_photo_request("ретуш і ч/б фільтр на одній фотографії"))

    def test_retouch_strength_survives_in_a_chain(self):
        plan = parse_plan("ретушь посильнее и винтаж")
        self.assertIn("посильнее", plan[0].prompt)


class OriginalEscapeTest(unittest.TestCase):
    """Цепочка идёт поверх результата — должен быть выход обратно к исходнику."""

    def test_asking_for_the_original(self):
        for text in ("зроби чб з оригіналу", "с оригинала сделай винтаж",
                     "верни исходник", "чб без обработки"):
            self.assertTrue(wants_original(text), text)

    def test_ordinary_follow_up_is_not_the_original(self):
        for text in ("а тепер додай ч/б фільтр", "теперь ярче", "ретушь"):
            self.assertFalse(wants_original(text), text)


@unittest.skipUnless(HAS_PIL, "нужен Pillow")
class ChainedResultTest(unittest.IsolatedAsyncioTestCase):
    """Цепочка выполняется целиком — на реальных пикселях, а не на плане."""

    def setUp(self):
        self.im = _spotted(_face())
        self.raw = _jpeg(self.im)

    async def test_retouch_plus_bw_does_both(self):
        res = await process_photo(self.raw, "ретуш і ч/б фільтр на одній фотографії")
        self.assertFalse(res.error, res.error)

        out = Image.open(__import__("io").BytesIO(res.data)).convert("RGB")
        # 1) кадр действительно обесцвечен
        w, h = out.size
        px = [out.getpixel((x, y))
              for y in range(0, h, 17) for x in range(0, w, 17)]
        spread = max(abs(r - g) + abs(g - b) for r, g, b in px)
        self.assertLessEqual(spread, 12, "ч/б фильтр не применился")
        # 2) и при этом пятно ушло: на месте прыща яркость близка к соседней коже
        before = _patch_mean(self.im, SPOTS[0])
        after = _patch_mean(out, SPOTS[0])
        self.assertGreater(after, before + 8,
                           "ретушь потерялась — остался только фильтр")

    async def test_caption_names_every_step(self):
        res = await process_photo(self.raw, "ретушь и чб")
        self.assertIn("retouch", res.op)
        self.assertIn("preset:чб", res.op)
        self.assertIn("·", res.caption, "подпись должна перечислять оба шага")
        self.assertLessEqual(len(res.caption), 900, "подпись не влезет в Telegram")

    async def test_filename_is_safe(self):
        res = await process_photo(self.raw, "ретушь и чб")
        _, filename = res.as_file()
        self.assertNotIn(":", filename)
        self.assertNotIn("+", filename)
        self.assertTrue(filename.endswith(".jpg"))

    async def test_single_step_result_is_unchanged(self):
        res = await process_photo(self.raw, "чб")
        self.assertEqual(res.op, "preset:чб")
        self.assertFalse(res.error)

    async def test_unavailable_step_is_skipped_not_substituted(self):
        """
        Нет rembg — раньше _dispatch отдавал вместо фона портретную обработку.
        В цепочке это самоуправство: чб просили, портрет — нет. Шаг пропускаем,
        объяснение оставляем.
        """
        from ai_office_shared.shared import photo as photo_mod
        real = photo_mod.rembg_available
        photo_mod.rembg_available = lambda: False
        try:
            res = await process_photo(self.raw, "убери фон и сделай чб")
        finally:
            photo_mod.rembg_available = real
        self.assertFalse(res.error, res.error)
        self.assertEqual(res.op, "preset:чб")
        self.assertIn("rembg", res.caption.lower())


if __name__ == "__main__":
    unittest.main()

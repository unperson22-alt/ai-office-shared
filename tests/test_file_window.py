"""
Чтение нужного куска файла вместо его начала.

Инцидент 15.08: kriss-bot/bot.py — 51 340 символов, окно чтения 24 000,
целевая строка на 823-й из 1005 (≈42 000-й символ). Силли читала начало
файла, цели там не было, и задача умерла на потолке шагов.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared.file_window import (  # noqa: E402
    find_regions, summary,
)

# Файл, у которого цель лежит далеко за типичным окном чтения.
_FILLER = "    # строка-наполнитель примерно такой же длины, как реальный код бота"
BIG = "\n".join(
    [_FILLER] * 400
    + ["def make_task_keyboard():", "    return KEYBOARD"]
    + [_FILLER] * 400
    + ["    keyboard = make_task_keyboard() if should_show(response) else None"]
    + [_FILLER] * 200
)


class TestFindRegions(unittest.TestCase):
    def test_finds_target_beyond_a_24k_window(self):
        self.assertGreater(len(BIG), 24000, "тестовый файл должен быть больше окна")
        out = find_regions(BIG, "make_task_keyboard")
        self.assertIn("keyboard = make_task_keyboard()", out)
        # И это при том, что цель лежит за пределами первых 24 000 символов.
        self.assertNotIn("keyboard = make_task_keyboard()", BIG[:24000])

    def test_output_carries_line_numbers(self):
        out = find_regions(BIG, "def make_task_keyboard")
        self.assertIn("401|", out.replace(" ", "").replace("|", "|"))

    def test_missing_needle_returns_empty_not_file_head(self):
        # Честное «не найдено» важнее правдоподобного куска: увидев начало
        # файла без цели, модель заключает, что цели не существует.
        self.assertEqual(find_regions(BIG, "такой_строки_нет"), "")

    def test_adjacent_matches_are_merged(self):
        text = "\n".join(["a", "цель", "b", "цель", "c"])
        out = find_regions(text, "цель", ctx_lines=2)
        self.assertEqual(out.count("[строки"), 1, "соседние совпадения — один фрагмент")

    def test_too_many_matches_are_capped_and_reported(self):
        text = "\n".join(["цель"] * 50)
        out = find_regions(text, "цель", ctx_lines=0, max_matches=2)
        self.assertIn("совпадений не показано", out)

    def test_case_insensitive(self):
        self.assertIn("MAKE_TASK", find_regions("MAKE_TASK_KEYBOARD = 1", "make_task"))

    def test_empty_inputs_do_not_raise(self):
        self.assertEqual(find_regions("", "x"), "")
        self.assertEqual(find_regions("abc", ""), "")


class TestSummary(unittest.TestCase):
    def test_reports_size(self):
        s = summary(BIG)
        self.assertIn("строк", s)
        self.assertIn(str(len(BIG)), s)

    def test_empty(self):
        self.assertEqual(summary(""), "файл пуст")

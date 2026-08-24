"""Перезапись одного урока в Bug Lessons бьёт ровно по нему.

23.08.2026 урок #118 уехал в группу по-русски. Существующий механизм —
`/migrate_lessons_en confirm` — сносит ВСЕ сообщения-уроки и постит архив
заново: 118 удалений ради одной записи, да ещё и с перепостом #105/#106,
которые как были русскими, так и останутся.

Две ловушки адресного варианта: номер `#11` не должен цеплять `#118`
(инвариант офиса №7 — совпадение по слову, а не по подстроке), и длинный урок
уходит несколькими сообщениями, из которых заголовок несёт только первое.
"""
import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared.bug_lessons import select_lesson_parts   # noqa: E402
from ai_office_shared.shared.telegram_text import (                   # noqa: E402
    is_continuation_part, split_for_telegram,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODER = os.path.join(ROOT, "agents", "coder.py")

HEAD = "🐛 Lesson #{} — {}"


class TestNumberIsMatchedWhole(unittest.TestCase):
    """`#11` внутри `#118` — не одиннадцатый урок."""

    MSGS = [
        HEAD.format(11, "eleven"),
        HEAD.format(111, "one eleven"),
        HEAD.format(118, "one eighteen"),
    ]

    def test_short_number_does_not_catch_the_longer_one(self):
        self.assertEqual(select_lesson_parts(self.MSGS, 11), [0])

    def test_each_number_finds_only_itself(self):
        for want, lesson_id in ((0, 11), (1, 111), (2, 118)):
            with self.subTest(id=lesson_id):
                self.assertEqual(select_lesson_parts(self.MSGS, lesson_id), [want])

    def test_absent_lesson_selects_nothing(self):
        self.assertEqual(select_lesson_parts(self.MSGS, 9999), [])

    def test_russian_header_of_the_old_archive_is_found_too(self):
        self.assertEqual(select_lesson_parts(["Урок #118 — старый"], 118), [0])


class TestMultipartLesson(unittest.TestCase):
    """Удалить одну голову — оставить в группе хвост без начала."""

    MSGS = [
        HEAD.format(117, "before"),
        "[1/3]\n" + HEAD.format(118, "long one"),
        "[2/3]\nсередина",
        "[3/3]\nконец",
        HEAD.format(119, "after"),
    ]

    def test_head_and_all_tails_are_selected(self):
        self.assertEqual(select_lesson_parts(self.MSGS, 118), [1, 2, 3])

    def test_neighbours_are_untouched(self):
        self.assertEqual(select_lesson_parts(self.MSGS, 117), [0])
        self.assertEqual(select_lesson_parts(self.MSGS, 119), [4])

    def test_a_tail_belongs_to_the_head_it_follows(self):
        """Хвост чужого урока не прилипает к предыдущему."""
        msgs = ["[1/2]\n" + HEAD.format(118, "a"), "[2/2]\nхвост 118",
                "[1/2]\n" + HEAD.format(119, "b"), "[2/2]\nхвост 119"]
        self.assertEqual(select_lesson_parts(msgs, 118), [0, 1])
        self.assertEqual(select_lesson_parts(msgs, 119), [2, 3])

    def test_a_gap_stops_the_collection(self):
        msgs = [HEAD.format(118, "a"), "постороннее сообщение", "[2/2]\nсирота"]
        self.assertEqual(select_lesson_parts(msgs, 118), [0])

    def test_empty_and_none_texts_do_not_crash(self):
        self.assertEqual(select_lesson_parts(["", None, HEAD.format(118, "x")], 118), [2])
        self.assertEqual(select_lesson_parts([], 118), [])


class TestContinuationMarkerMatchesItsWriter(unittest.TestCase):
    """Читатель и писатель маркера обязаны совпадать — они в одном файле."""

    def test_real_split_output_is_classified_correctly(self):
        parts = split_for_telegram("\n\n".join(["x" * 900] * 8))
        self.assertGreater(len(parts), 1)
        self.assertFalse(is_continuation_part(parts[0]), "первая часть — не хвост")
        for p in parts[1:]:
            self.assertTrue(is_continuation_part(p))

    def test_unsplit_message_is_never_a_continuation(self):
        only = split_for_telegram("короткий урок")
        self.assertEqual(len(only), 1)
        self.assertFalse(is_continuation_part(only[0]))


class TestCommandIsWiredAndGuarded(unittest.TestCase):
    """coder.py не импортируется в тест — читаем его AST."""

    @classmethod
    def setUpClass(cls):
        with open(CODER, encoding="utf-8") as f:
            cls.src = f.read()
        cls.tree = ast.parse(cls.src)
        cls.funcs = {n.name: n for n in ast.walk(cls.tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def test_both_the_operation_and_the_handler_exist(self):
        self.assertIn("repost_lesson", self.funcs)
        self.assertIn("cmd_repost_lesson", self.funcs)

    def test_handler_is_owner_only(self):
        body = ast.get_source_segment(self.src, self.funcs["cmd_repost_lesson"])
        self.assertIn("YOUR_TELEGRAM_ID", body)

    def test_dry_run_is_the_default(self):
        body = ast.get_source_segment(self.src, self.funcs["cmd_repost_lesson"])
        self.assertIn('"confirm" in', body)

    def test_it_does_not_commit_to_git(self):
        """Текст урока правится через PR, «когда перепостили» — данные в Redis."""
        body = ast.get_source_segment(self.src, self.funcs["repost_lesson"])
        self.assertNotIn("push_file", body)

    def test_a_failed_send_after_delete_is_escalated(self):
        """Старое снято, новое не ушло — урок исчез бы из группы молча."""
        body = ast.get_source_segment(self.src, self.funcs["repost_lesson"])
        self.assertIn("_escalate_vlad", body)

    def test_messages_are_reversed_into_chronological_order(self):
        """Селектор считает хвост принадлежащим предыдущей голове."""
        body = ast.get_source_segment(self.src, self.funcs["repost_lesson"])
        self.assertIn("reversed(", body)


if __name__ == "__main__":
    unittest.main()

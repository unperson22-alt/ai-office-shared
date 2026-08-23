"""
Длинный урок не должен запирать очередь публикации.

Инцидент 12.08–23.08.2026: группа Bug Lessons одиннадцать дней стояла на уроке
#90, пока lessons.json ушёл вперёд на двадцать три записи. Урок #91
форматируется в 5035 символов при жёстком лимите Telegram 4096, `send_message`
отвечал «message is too long», а публикатор на любой ошибке делал `break` — и
одно слишком длинное сообщение намертво запирало всё остальное.

Единственным сигналом был logger.error. Тихая остановка неотличима от «новых
уроков нет» — поэтому одиннадцать дней никто и не заметил.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared.telegram_text import (  # noqa: E402
    TELEGRAM_LIMIT, split_for_telegram,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def format_lesson(l: dict) -> str:
    """Копия формата из coder.py — тест меряет то, что реально уходит в Telegram."""
    se = {"fixed": "✅", "still_relevant": "⚠️", "outdated": "🗄",
          "documented": "📝"}.get(l.get("status", ""), "❓")
    return (
        f"🐛 Lesson #{l.get('id')} — {l.get('title', '?')}\n\n"
        f"📍 {l.get('bot', '?')} | {l.get('layer', '?')}\n\n"
        f"👁 Symptom:\n{l.get('symptom', '?')}\n\n"
        f"🔍 Root cause:\n{l.get('root_cause', l.get('cause', '?'))}\n\n"
        f"✅ Fix:\n{l.get('fix', '?')}\n\n"
        f"🛡 Prevention:\n{l.get('prevention', '?')}\n\n"
        f"{se} Status: {l.get('status', '?')}"
    )


class TestShortTextIsLeftAlone(unittest.TestCase):
    def test_fitting_text_is_returned_as_is(self):
        # Норму не уродуем ради исключения: маркера «[1/1]» быть не должно.
        self.assertEqual(split_for_telegram("короткий урок"), ["короткий урок"])

    def test_text_exactly_at_the_limit_is_not_split(self):
        text = "x" * TELEGRAM_LIMIT
        self.assertEqual(split_for_telegram(text), [text])

    def test_empty_input_is_survivable(self):
        self.assertEqual(split_for_telegram(""), [""])
        self.assertEqual(split_for_telegram(None), [""])


class TestLongTextIsSplitNotTruncated(unittest.TestCase):
    def _long(self):
        return "\n\n".join(f"Абзац {i}. " + "слово " * 60 for i in range(40))

    def test_every_part_fits_the_limit(self):
        for part in split_for_telegram(self._long()):
            self.assertLessEqual(len(part), TELEGRAM_LIMIT)

    def test_nothing_is_lost(self):
        # Обрезать молча нельзя: потерянный хвост выглядит как целый урок.
        text = self._long()
        joined = "".join(p.split("\n", 1)[1] for p in split_for_telegram(text))
        self.assertEqual(joined.replace("\n", ""), text.replace("\n", ""))

    def test_parts_are_numbered_so_a_reader_sees_the_gap(self):
        parts = split_for_telegram(self._long())
        self.assertGreater(len(parts), 1)
        for i, p in enumerate(parts, 1):
            self.assertTrue(p.startswith(f"[{i}/{len(parts)}]"), p[:20])

    def test_a_single_paragraph_over_the_limit_is_still_split(self):
        for part in split_for_telegram("я" * (TELEGRAM_LIMIT * 3)):
            self.assertLessEqual(len(part), TELEGRAM_LIMIT)

    def test_one_giant_word_does_not_hang_or_overflow(self):
        parts = split_for_telegram("ы" * 20000)
        self.assertTrue(all(len(p) <= TELEGRAM_LIMIT for p in parts))
        self.assertGreater(len(parts), 4)


class TestRealLessonsFitAfterSplitting(unittest.TestCase):
    """Гейт против повторения: любая запись архива обязана уходить в Telegram."""

    def setUp(self):
        with open(os.path.join(ROOT, "lessons", "lessons.json"), encoding="utf-8") as f:
            self.records = json.load(f)

    def test_the_lesson_that_blocked_the_queue_now_goes_through(self):
        l91 = next(r for r in self.records if r["id"] == 91)
        raw = format_lesson(l91)
        self.assertGreater(len(raw), TELEGRAM_LIMIT, "урок #91 должен быть длинным")
        parts = split_for_telegram(raw)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= TELEGRAM_LIMIT for p in parts))

    def test_every_record_in_the_archive_is_sendable(self):
        for r in self.records:
            for part in split_for_telegram(format_lesson(r)):
                self.assertLessEqual(
                    len(part), TELEGRAM_LIMIT,
                    f"запись #{r['id']} не помещается даже после нарезки")


if __name__ == "__main__":
    unittest.main()

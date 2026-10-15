"""
Теги расписания: 06.10.2026 «напоминай каждый 21 день в 7 утра».

Билли ответил «Принято» и сырым тегом [SCHEDULE:interval:21d:05:00:…]:
суффикс «d» парсер не знал, разбор молча возвращал None, тег оставался в
тексте, а в Redis не ложилось ничего.
"""

import asyncio
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared import tasks  # noqa: E402

DAY = 86400
BILLY_06_10 = ("Принято. Буду напоминать каждые 21 день в 7:00 по-германски.\n\n"
               "[SCHEDULE:interval:21d:05:00:⚡ Влад, пополни интернет на мобиле]")


class FakeRedis:
    def __init__(self):
        self.z = {}

    async def zadd(self, key, mapping):
        self.z.setdefault(key, {}).update(mapping)


class ParseInterval(unittest.TestCase):
    def test_days_with_anchor(self):
        t = tasks.parse_schedule_tag(BILLY_06_10)
        self.assertEqual(t["interval_sec"], 21 * DAY)
        self.assertEqual((t["hour"], t["minute"]), (5, 0))
        self.assertEqual(t["message"], "⚡ Влад, пополни интернет на мобиле")

    def test_minutes_unchanged(self):
        t = tasks.parse_schedule_tag("[SCHEDULE:interval:30m:попей воды]")
        self.assertEqual(t["interval_sec"], 1800)
        self.assertNotIn("hour", t)
        self.assertEqual(t["message"], "попей воды")

    def test_text_with_colon_is_not_an_anchor(self):
        t = tasks.parse_schedule_tag("[SCHEDULE:interval:2h:итог: проверь почту]")
        self.assertNotIn("hour", t)
        self.assertEqual(t["message"], "итог: проверь почту")

    def test_zero_interval_refused(self):
        self.assertIsNone(tasks.parse_schedule_tag("[SCHEDULE:interval:0d:x]"))


class NextRun(unittest.TestCase):
    def test_first_run_at_anchor(self):
        nxt = tasks._calc_next_run({"type": "interval", "interval_sec": 21 * DAY,
                                    "hour": 5, "minute": 0})
        dt = datetime.fromtimestamp(nxt, tz=timezone.utc)
        self.assertEqual((dt.hour, dt.minute), (5, 0))
        self.assertLessEqual(nxt - datetime.now(timezone.utc).timestamp(), DAY)

    def test_repeat_steps_from_schedule_not_from_firing(self):
        prev = datetime.now(timezone.utc).timestamp() - 60  # сработало минуту назад
        nxt = tasks._calc_next_run({"type": "interval", "interval_sec": 21 * DAY,
                                    "hour": 5, "minute": 0, "next_run": prev})
        self.assertEqual(nxt, prev + 21 * DAY)


class Apply(unittest.TestCase):
    def test_tag_stored_and_stripped(self):
        r = FakeRedis()
        text, listing = asyncio.run(tasks.apply_schedule_tag(r, "билли", 1, BILLY_06_10))
        self.assertIsNone(listing)
        self.assertNotIn("[SCHEDULE", text)
        self.assertEqual(len(r.z["office:schedule:билли:1"]), 1)

    def test_unparsed_tag_is_stripped_and_named(self):
        r = FakeRedis()
        text, _ = asyncio.run(tasks.apply_schedule_tag(
            r, "билли", 1, "Принято. [SCHEDULE:monthly:5:05:00:счёт]"))
        self.assertNotIn("[SCHEDULE", text)
        self.assertIn("НЕ создано", text)
        self.assertEqual(r.z, {})

    def test_list_description(self):
        t = {"type": "interval", "interval_sec": 21 * DAY, "hour": 5, "minute": 0,
             "message": "x", "next_run": 0}
        out = asyncio.run(tasks.format_task_list([t]))
        self.assertIn("каждые 21 дн. в 05:00 UTC", out)


if __name__ == "__main__":
    unittest.main()

"""
Сводка по логам — инцидент 2026-08-16 (наблюдаемость).

Чтобы понять, почему dev-отдел шесть раз подряд «не дал код», нужно было одно
число: как часто Девви получал ту же задачу. Спросить было негде. Четыре
попытки вернули сырой список, обрезанный на 3000 символах — ни счёта, ни
первой метки, ни последней, ни интервала. Такт (~2 минуты) пришлось считать
глазами по обрывку.

Данные в тестах — настоящие метки из того дампа.

Запуск: cd ai-office-shared && python3 -m unittest discover -s tests -v
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared.log_query import (  # noqa: E402
    discover_bots, format_summary, query, summarize,
)


def run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def ev(ts, event="task_received", level="info", **ctx):
    return {"ts": ts, "event": event, "level": level, "bot": "девви",
            "context": ctx}


# Ровно то, что было в логах: Девви и его же ответы, такт ~2 минуты.
DEVVY = [
    ev("2026-08-16T04:07:52Z", repo="villy-bot"),
    ev("2026-08-16T04:08:14Z", "response_sent"),
    ev("2026-08-16T04:09:54Z", repo="villy-bot"),
    ev("2026-08-16T04:10:16Z", "response_sent"),
    ev("2026-08-16T04:11:54Z", repo="villy-bot"),
    ev("2026-08-16T04:12:08Z", "response_sent"),
    ev("2026-08-16T04:13:55Z", repo="villy-bot"),
    ev("2026-08-16T04:14:26Z", "response_sent"),
    ev("2026-08-16T04:15:57Z", repo="villy-bot"),
    ev("2026-08-16T04:16:22Z", "response_sent"),
    ev("2026-08-16T04:17:57Z", repo="villy-bot"),
    ev("2026-08-16T04:18:27Z", "response_sent"),
    ev("2026-08-16T04:19:58Z", repo="villy-bot"),
    ev("2026-08-16T04:20:27Z", "response_sent"),
]


class TestSummarize(unittest.TestCase):
    def test_counts_and_bounds(self):
        s = summarize(DEVVY)
        self.assertEqual(s["total"], 14)
        self.assertEqual(s["first"], "2026-08-16T04:07:52Z")
        self.assertEqual(s["last"], "2026-08-16T04:20:27Z")
        self.assertEqual(s["span_sec"], 755)
        self.assertEqual(s["by_event"]["task_received"], 7)

    def test_cadence_is_the_answer_i_needed(self):
        # Главное число: «каждые ~2 минуты», а не «14 записей».
        s = summarize(DEVVY)
        self.assertEqual(s["top_event"], "task_received")
        self.assertAlmostEqual(s["cadence_sec"], 121, delta=3)

    def test_median_not_mean(self):
        # Одна долгая пауза не должна маскировать ровный такт: среднее по
        # этим меткам ~20 минут, медиана — 60 секунд.
        marks = [ev(f"2026-08-16T04:{m:02d}:00Z") for m in (0, 1, 2, 3)]
        marks.append(ev("2026-08-16T06:00:00Z"))
        self.assertEqual(summarize(marks)["cadence_sec"], 60)

    def test_levels_are_counted(self):
        s = summarize(DEVVY + [ev("2026-08-16T04:21:00Z", "fail", "error")])
        self.assertEqual(s["by_level"]["error"], 1)

    def test_empty_is_not_a_crash(self):
        s = summarize([])
        self.assertEqual(s["total"], 0)
        self.assertIsNone(s["cadence_sec"])
        self.assertIsNone(s["first"])

    def test_single_entry_has_no_cadence(self):
        # Из одной точки интервал не выводится. Выдумывать нельзя.
        s = summarize([ev("2026-08-16T04:07:52Z")])
        self.assertEqual(s["total"], 1)
        self.assertIsNone(s["cadence_sec"])

    def test_broken_timestamp_does_not_lose_the_record(self):
        s = summarize(DEVVY + [ev("не-дата", "task_received")])
        self.assertEqual(s["by_event"]["task_received"], 8)
        self.assertEqual(s["last"], "2026-08-16T04:20:27Z")


class TestFormatSummary(unittest.TestCase):
    def test_one_line_says_how_many_when_and_how_often(self):
        line = format_summary("девви", summarize(DEVVY))
        self.assertIn("14 записей", line)
        self.assertIn("04:07:52", line)
        self.assertIn("task_received ×7", line)
        self.assertIn("каждые ~2м", line)
        self.assertLess(len(line), 200, "сводка должна быть строкой, а не абзацем")

    def test_empty_says_so_plainly(self):
        self.assertEqual(format_summary("рикки", summarize([])),
                         "рикки: записей нет")


class _FakeRedis:
    """lrange + scan — ровно то, чем пользуются read_logs и discover_bots."""

    def __init__(self, keys):
        self.keys = keys           # {key: [json-строки]}

    async def lrange(self, key, start, stop):
        import json
        return [json.dumps(e, ensure_ascii=False)
                for e in self.keys.get(key, [])][start:stop + 1]

    async def scan(self, cursor, match="", count=100):
        import fnmatch
        return 0, [k for k in self.keys if fnmatch.fnmatch(k, match)]


class TestQuery(unittest.TestCase):
    def setUp(self):
        from datetime import datetime, timezone
        self.today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Метки берём сегодняшние — read_logs ходит по последним N суткам.
        shifted = [dict(e, ts=self.today + e["ts"][10:]) for e in DEVVY]
        self.r = _FakeRedis({
            f"office:logs:девви:{self.today}": shifted,
            f"office:logs:рикки:{self.today}": [
                dict(e, bot="рикки") for e in shifted[:2]],
        })

    def test_discovers_workers_absent_from_the_registry(self):
        # Девви и Рикки нет в identity.BOTS — а разбор был именно про них.
        from ai_office_shared.shared.identity import BOTS
        found = run(discover_bots(self.r, days=1))
        self.assertEqual(found, ["девви", "рикки"])
        self.assertNotIn("девви", BOTS)

    def test_query_all_bots_returns_one_line_each(self):
        res = run(query(self.r, limit=0))
        self.assertEqual(len(res["lines"]), 2)
        self.assertIn("девви: 14 записей", res["lines"][0])

    def test_event_filter_narrows(self):
        res = run(query(self.r, bot="девви", event="task_received", limit=0))
        s = res["bots"]["девви"]["summary"]
        self.assertEqual(s["total"], 7)
        self.assertEqual(s["by_event"], {"task_received": 7})

    def test_limit_zero_returns_no_entries(self):
        # Смысл ручки: сводка без дампа.
        res = run(query(self.r, bot="девви", limit=0))
        self.assertNotIn("entries", res["bots"]["девви"])

    def test_limit_caps_entries(self):
        res = run(query(self.r, bot="девви", limit=3))
        self.assertEqual(len(res["bots"]["девви"]["entries"]), 3)

    def test_unknown_bot_is_empty_not_error(self):
        res = run(query(self.r, bot="нетакого", limit=0))
        self.assertEqual(res["bots"]["нетакого"]["summary"]["total"], 0)

    def test_days_is_clamped(self):
        from ai_office_shared.shared.log_query import MAX_SCAN_DAYS
        self.assertEqual(run(query(self.r, bot="девви", days=999,
                                   limit=0))["days"], MAX_SCAN_DAYS)
        self.assertEqual(run(query(self.r, bot="девви", days=0,
                                   limit=0))["days"], 1)


if __name__ == "__main__":
    unittest.main()

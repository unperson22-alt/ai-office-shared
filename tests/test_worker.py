"""
Тесты рантайма воркера dev-dept (shared/worker.py).

Проверяют, что после схлопывания пяти копипаст-ботов в один модуль контракты
не поехали: те же маршруты, тот же формат ответа, те же события log_event,
тот же эфир dev-dept:activity.

    python3 -m unittest discover -s tests
"""

import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared import worker  # noqa: E402
from ai_office_shared.shared.dev_activity import activity_key  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeRedis:
    """Достаточно для publish_activity/read_activity: pipeline + lrange."""

    def __init__(self):
        self.lists = {}
        self._ops = []
        self.published = []

    def pipeline(self, transaction=False):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def lpush(self, key, val):
        self._ops.append(("lpush", key, val))

    def ltrim(self, key, a, b):
        pass

    def expire(self, key, ttl):
        pass

    def publish(self, ch, val):
        self._ops.append(("publish", ch, val))

    async def execute(self):
        for op in self._ops:
            if op[0] == "lpush":
                self.lists.setdefault(op[1], []).insert(0, op[2])
            elif op[0] == "publish":
                self.published.append(op[2])
        self._ops = []

    async def lrange(self, key, start, end):
        return self.lists.get(key, [])[start:end + 1]


class FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class FakeTgBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


class TestSummarize(unittest.TestCase):

    def test_picks_summary_line(self):
        text = "VERDICT: APPROVED\nISSUES:\n- нет\nSUMMARY: поправил импорт random"
        self.assertEqual(worker.summarize_result(text), "поправил импорт random")

    def test_falls_back_to_head(self):
        self.assertEqual(worker.summarize_result("просто текст\nвторая строка"),
                         "просто текст вторая строка")

    def test_empty(self):
        self.assertEqual(worker.summarize_result(""), "")

    def test_respects_limit(self):
        self.assertEqual(len(worker.summarize_result("x" * 500, limit=20)), 20)


class TestWorkerRoutes(unittest.TestCase):
    """Маршруты и формат ответа должны совпадать с прежними пятью копиями."""

    def setUp(self):
        self.state = {"redis": FakeRedis(), "bot": FakeTgBot()}
        self.app = worker.build_app("рикки", "SYSTEM", self.state)

    def test_registers_exactly_the_three_endpoints(self):
        routes = {(r.method, r.resource.canonical) for r in self.app.router.routes()}
        self.assertIn(("POST", "/task"), routes)
        self.assertIn(("GET", "/health"), routes)
        self.assertIn(("POST", "/reply"), routes)

    def test_health_shape_unchanged(self):
        handler = self._handler("GET", "/health")
        resp = run(handler(FakeRequest({})))
        self.assertEqual(json.loads(resp.text), {"status": "ok", "bot": "рикки"})

    def test_reply_sends_via_bot(self):
        handler = self._handler("POST", "/reply")
        run(handler(FakeRequest({"chat_id": 42, "text": "привет"})))
        self.assertEqual(self.state["bot"].sent, [(42, "привет")])

    def test_reply_without_chat_id_is_noop(self):
        handler = self._handler("POST", "/reply")
        resp = run(handler(FakeRequest({"text": "привет"})))
        self.assertEqual(self.state["bot"].sent, [])
        self.assertEqual(json.loads(resp.text), {"ok": True})

    def _handler(self, method, path):
        for r in self.app.router.routes():
            if r.method == method and r.resource.canonical == path:
                return r.handler
        raise AssertionError(f"{method} {path} не зарегистрирован")


class TestWorkerTask(unittest.TestCase):

    def setUp(self):
        self.redis = FakeRedis()
        self.state = {"redis": self.redis, "bot": FakeTgBot()}
        self.seen = {}

        async def fake_ask(system_prompt, message, context=""):
            self.seen["system"] = system_prompt
            self.seen["message"] = message
            self.seen["context"] = context
            return "VERDICT: APPROVED\nSUMMARY: всё хорошо"

        self._orig = worker.ask_claude
        worker.ask_claude = fake_ask
        self.app = worker.build_app("рикки", "SYSTEM-РИККИ", self.state)
        self.handler = next(r.handler for r in self.app.router.routes()
                            if r.method == "POST" and r.resource.canonical == "/task")

    def tearDown(self):
        worker.ask_claude = self._orig

    def test_response_shape_unchanged(self):
        resp = run(self.handler(FakeRequest({"message": "почини импорт", "task_id": "t1"})))
        self.assertEqual(json.loads(resp.text),
                         {"response": "VERDICT: APPROVED\nSUMMARY: всё хорошо"})

    def test_system_prompt_is_the_bots_own(self):
        run(self.handler(FakeRequest({"message": "x" * 50, "task_id": "t1"})))
        self.assertEqual(self.seen["system"], "SYSTEM-РИККИ")

    def test_artifact_appended_as_previous_stage(self):
        run(self.handler(FakeRequest({
            "message": "проверь", "artifact": "код от Девви", "task_id": "t1"})))
        self.assertIn("[РЕЗУЛЬТАТ ПРЕДЫДУЩЕГО ЭТАПА]", self.seen["message"])
        self.assertIn("код от Девви", self.seen["message"])

    def test_publishes_start_and_done_to_activity_feed(self):
        run(self.handler(FakeRequest({"message": "почини", "task_id": "t7"})))
        entries = [json.loads(x) for x in self.redis.lists[activity_key("t7")]]
        phases = sorted(e["phase"] for e in entries)
        self.assertEqual(phases, ["done", "start"])
        self.assertTrue(all(e["bot"] == "рикки" for e in entries))
        done = next(e for e in entries if e["phase"] == "done")
        self.assertEqual(done["summary"], "всё хорошо")

    def test_team_activity_injected_and_excludes_self(self):
        run(worker_publish(self.redis, "t9", "девви", "done", "написал bot.py"))
        run(worker_publish(self.redis, "t9", "рикки", "done", "моя же строка"))
        run(self.handler(FakeRequest({"message": "ревью", "task_id": "t9"})))
        msg = self.seen["message"]
        self.assertIn("[ДЕЙСТВИЯ КОМАНДЫ DEV-DEPT]", msg)
        self.assertIn("девви", msg)
        self.assertNotIn("моя же строка", msg)

    def test_model_failure_returns_error_string_not_crash(self):
        async def boom(system_prompt, message, context=""):
            raise RuntimeError("529 overloaded")
        worker.ask_claude = boom
        resp = run(self.handler(FakeRequest({"message": "x", "task_id": "t1"})))
        self.assertIn("ERROR: рикки не смог обработать задачу", json.loads(resp.text)["response"])

    def test_no_redis_does_not_crash(self):
        state = {"redis": None, "bot": FakeTgBot()}
        app = worker.build_app("рикки", "S", state)
        h = next(r.handler for r in app.router.routes()
                 if r.method == "POST" and r.resource.canonical == "/task")
        os.environ.pop("REDIS_URL", None)
        resp = run(h(FakeRequest({"message": "x", "task_id": "t1"})))
        self.assertIn("response", json.loads(resp.text))


async def worker_publish(redis, task_id, bot, phase, summary):
    from ai_office_shared.shared.dev_activity import publish_activity
    await publish_activity(redis, task_id, bot, phase, summary)


class TestGhReadFile(unittest.TestCase):

    def test_no_token_returns_empty(self):
        old = os.environ.pop("GH_PAT", None)
        try:
            self.assertEqual(worker.gh_read_file("billy-bot", "bot.py"), "")
        finally:
            if old is not None:
                os.environ["GH_PAT"] = old

    def test_missing_args_return_empty(self):
        os.environ["GH_PAT"] = "x"
        try:
            self.assertEqual(worker.gh_read_file("", "bot.py"), "")
            self.assertEqual(worker.gh_read_file("repo", ""), "")
        finally:
            os.environ.pop("GH_PAT", None)


if __name__ == "__main__":
    unittest.main()


class TestApplyScheduleTag(unittest.TestCase):
    """
    Развязка тега расписания. Была скопирована в каждом умеющем боте, а у доктора,
    эллис и филли schedule_loop крутился вообще без неё — создать напоминание было нельзя.
    """

    def setUp(self):
        from ai_office_shared.shared import tasks
        self.tasks = tasks
        self.redis = ScheduleRedis()

    def test_no_tag_passes_through(self):
        out, listing = run(self.tasks.apply_schedule_tag(self.redis, "эллис", 1, "просто ответ"))
        self.assertEqual(out, "просто ответ")
        self.assertIsNone(listing)

    def test_add_stores_task_and_strips_tag(self):
        resp = "Поставила на 9 утра. [SCHEDULE:daily:09:00:выпить воды]"
        out, listing = run(self.tasks.apply_schedule_tag(self.redis, "эллис", 7, resp))
        self.assertEqual(out, "Поставила на 9 утра.")
        self.assertIsNone(listing)
        stored = self.redis.zsets["office:schedule:эллис:7"]
        self.assertEqual(len(stored), 1)
        self.assertIn("выпить воды", list(stored)[0])

    def test_list_returns_listing_to_send_instead(self):
        run(self.tasks.apply_schedule_tag(
            self.redis, "эллис", 7, "ок [SCHEDULE:daily:09:00:зарядка]"))
        out, listing = run(self.tasks.apply_schedule_tag(
            self.redis, "эллис", 7, "вот список [LIST_SCHEDULES]"))
        self.assertIsNotNone(listing)
        self.assertIn("зарядка", listing)

    def test_tag_never_leaks_to_user(self):
        for resp in ("x [SCHEDULE:interval:30m:пить]", "y [CANCEL_SCHEDULE:1]"):
            out, _ = run(self.tasks.apply_schedule_tag(self.redis, "эллис", 7, resp))
            self.assertNotIn("[", out, f"тег утёк пользователю: {out!r}")


class ScheduleRedis:
    """ZSET-хранилище, достаточное для add/list/remove_scheduled_task."""

    def __init__(self):
        self.zsets = {}

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    async def zrange(self, key, start, end, withscores=False):
        items = sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1])
        sliced = items[start:] if end == -1 else items[start:end + 1]
        return sliced if withscores else [m for m, _ in sliced]

    async def zrem(self, key, member):
        return 1 if self.zsets.get(key, {}).pop(member, None) is not None else 0


class TestParseScheduleTag(unittest.TestCase):
    """
    Регресс на боевой баг: до 2026-07-25 daily/weekly/once не парсились ВООБЩЕ.
    Время передаётся как HH:MM и само содержит двоеточие, а split(":", 1) резал
    его пополам → int("") → ValueError → тег молча превращался в None. Бот отвечал
    «напоминание создано», в Redis не ложилось ничего. Работал только interval.
    """

    def setUp(self):
        from ai_office_shared.shared.tasks import parse_schedule_tag
        self.parse = parse_schedule_tag

    def test_daily(self):
        r = self.parse("Поставил. [SCHEDULE:daily:09:00:иди на пробежку]")
        self.assertEqual(r, {"action": "add", "type": "daily", "hour": 9,
                             "minute": 0, "message": "иди на пробежку"})

    def test_daily_with_colon_inside_message(self):
        r = self.parse("[SCHEDULE:daily:07:30:встреча в 10:30 не забудь]")
        self.assertEqual(r["hour"], 7)
        self.assertEqual(r["minute"], 30)
        self.assertEqual(r["message"], "встреча в 10:30 не забудь")

    def test_weekly(self):
        r = self.parse("[SCHEDULE:weekly:mon:09:00:планёрка]")
        self.assertEqual(r, {"action": "add", "type": "weekly", "day_of_week": 0,
                             "hour": 9, "minute": 0, "message": "планёрка"})

    def test_weekly_russian_day(self):
        self.assertEqual(self.parse("[SCHEDULE:weekly:пт:18:00:отчёт]")["day_of_week"], 4)

    def test_once(self):
        r = self.parse("[SCHEDULE:once:2026-08-01:09:00:день рождения]")
        self.assertEqual(r["type"], "once")
        self.assertEqual(r["run_at"], "2026-08-01T09:00:00+00:00")
        self.assertEqual(r["message"], "день рождения")

    def test_interval_still_works(self):
        r = self.parse("[SCHEDULE:interval:30m:попей воды]")
        self.assertEqual(r["interval_sec"], 1800)

    def test_interval_hours(self):
        self.assertEqual(self.parse("[SCHEDULE:interval:2h:разомнись]")["interval_sec"], 7200)

    def test_list_and_cancel(self):
        self.assertEqual(self.parse("[LIST_SCHEDULES]"), {"action": "list"})
        self.assertEqual(self.parse("[CANCEL_SCHEDULE:3]"), {"action": "cancel", "index": 3})

    def test_no_tag(self):
        self.assertIsNone(self.parse("обычный ответ без тегов"))

    def test_malformed_returns_none_not_crash(self):
        for bad in ("[SCHEDULE:daily:девять:ноль:текст]", "[SCHEDULE:daily:09]",
                    "[SCHEDULE:unknown:09:00:текст]", "[SCHEDULE:]"):
            self.assertIsNone(self.parse(bad), f"должен быть None: {bad}")

    def test_all_documented_formats_parse(self):
        """Каждый формат из промпта ботов обязан распознаваться."""
        for tag in ("[SCHEDULE:daily:09:00:t]", "[SCHEDULE:weekly:mon:09:00:t]",
                    "[SCHEDULE:interval:30m:t]", "[SCHEDULE:once:2026-08-01:09:00:t]",
                    "[LIST_SCHEDULES]", "[CANCEL_SCHEDULE:1]"):
            self.assertIsNotNone(self.parse(tag), f"НЕ РАСПОЗНАН: {tag}")

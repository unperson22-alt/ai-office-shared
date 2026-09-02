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

    def test_version_endpoint_exists_and_names_the_running_build(self):
        """
        `/health` одинаков до и после деплоя, поэтому «работает ли уже фикс»
        было некому подтвердить, кроме того, кто деплой и выполнял (02.09.2026,
        инвариант 5). `/version` отвечает на этот вопрос — и обязан честно
        говорить «не знаю», а не подставлять правдоподобное.
        """
        routes = {(r.method, r.resource.canonical) for r in self.app.router.routes()}
        self.assertIn(("GET", "/version"), routes)
        handler = self._handler("GET", "/version")
        body = json.loads(run(handler(FakeRequest({}))).text)
        self.assertEqual(set(body), {"bot", "service_commit", "shared_commit"})
        self.assertEqual(body["bot"], "рикки")

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

    def test_refuses_a_request_with_no_work_in_it(self):
        """
        Заявка без `message` — отказ 400, а не пустая строка в модель.

        02.09.2026 Силли на просьбу «задеплой kriss-bot», ушедшую полем `task`,
        ответила приветствием и статусом `done`: пустая строка доехала до LLM,
        та ответила на пустой вопрос, а обработчик отрапортовал успех. У
        воркера была ровно та же дыра, и через него ходит весь отдел
        разработки.
        """
        for body in ({"task": "почини импорт"}, {"message": "  "}, {}):
            resp = run(self.handler(FakeRequest(body)))
            self.assertEqual(resp.status, 400, f"тело {body} принято как заявка")
            said = json.loads(resp.text)["response"]
            self.assertIn("message", said, "отказ не назвал нужное поле")
            self.assertNotIn("system", self.seen,
                             f"тело {body} доехало до модели")

    def test_the_refusal_points_at_the_field_the_work_arrived_in(self):
        resp = run(self.handler(FakeRequest({"task": "почини импорт"})))
        self.assertIn("task", json.loads(resp.text)["response"])

    def test_refuses_to_work_when_file_cannot_be_read(self):
        """🔴 Правка вслепую запрещена: модель НЕ должна вызываться вообще.

        Именно молчаливая работа с пустым контекстом дала инцидент 01.07
        (5766-строчный файл заменён 8-строчным стабом) и воспроизвелась 31.07
        на devvy-bot/bot.py. Отказ громче и дешевле уничтоженного файла.
        """
        called = []

        async def must_not_run(system_prompt, message, context=""):
            called.append(1)
            return "не должно случиться"

        worker.ask_claude = must_not_run
        orig = worker.gh_fetch_file
        worker.gh_fetch_file = lambda repo, path, **kw: ("", "HTTP 401")
        try:
            resp = run(self.handler(FakeRequest(
                {"message": "добавь эндпоинт", "repo": "devvy-bot",
                 "file_path": "bot.py", "task_id": "t1"})))
            body = json.loads(resp.text)["response"]
            self.assertTrue(body.startswith("ERROR:"), body)
            self.assertIn("вслепую", body)
            self.assertIn("devvy-bot/bot.py", body)
            self.assertEqual(called, [], "модель вызвана несмотря на нечитаемый файл")
        finally:
            worker.gh_fetch_file = orig

    def test_refuses_when_the_file_does_not_fit_the_window(self):
        """Обрезанный файл читается как успешно прочитанный — и запрет выше
        на него не срабатывал.

        24.08.2026, заявка 62ffa25b5e30: kriss-bot/bot.py — 64 158 символов,
        окно воркера — 8 000. Девви видел 12% файла и должен был вернуть его
        целиком. Три попытки подряд давали «pyflakes (50): 1:1: 're' imported
        but unused; …» — обрубок, а не правку. Править по куску — та же
        слепота, только незаметная.
        """
        called = []

        async def must_not_run(system_prompt, message, context=""):
            called.append(1)
            return "не должно случиться"

        worker.ask_claude = must_not_run
        orig_fetch, orig_limit = worker.gh_fetch_file, worker.MAX_CONTEXT_CHARS
        worker.gh_fetch_file = lambda repo, path, **kw: ("x" * 64158, "")
        worker.MAX_CONTEXT_CHARS = 8000
        try:
            resp = run(self.handler(FakeRequest(
                {"message": "добавь ретушь", "repo": "kriss-bot",
                 "file_path": "bot.py", "task_id": "t1"})))
            body = json.loads(resp.text)["response"]
            self.assertTrue(body.startswith("ERROR:"), body)
            self.assertIn("kriss-bot/bot.py", body)
            self.assertIn("64158", body)
            self.assertIn("8000", body)
            self.assertEqual(called, [], "модель вызвана на куске файла")
        finally:
            worker.gh_fetch_file = orig_fetch
            worker.MAX_CONTEXT_CHARS = orig_limit

    def test_a_file_that_fits_reaches_the_model_whole(self):
        """Отказ не должен превратиться в отказ от работы вообще."""
        orig = worker.gh_fetch_file
        src = "y" * 64158
        worker.gh_fetch_file = lambda repo, path, **kw: (src, "")
        try:
            resp = run(self.handler(FakeRequest(
                {"message": "добавь ретушь", "repo": "kriss-bot",
                 "file_path": "bot.py", "task_id": "t1"})))
            self.assertNotIn("ERROR:", json.loads(resp.text)["response"])
            self.assertEqual(self.seen["context"], src, "модель получила не весь файл")
        finally:
            worker.gh_fetch_file = orig

    def test_new_file_in_existing_repo_still_works(self):
        """404 не должен блокировать задачу «создай новый файл»."""
        orig = worker.gh_fetch_file
        worker.gh_fetch_file = lambda repo, path, **kw: ("", "")
        try:
            resp = run(self.handler(FakeRequest(
                {"message": "создай новый модуль", "repo": "devvy-bot",
                 "file_path": "new.py", "task_id": "t1"})))
            self.assertNotIn("ERROR:", json.loads(resp.text)["response"])
        finally:
            worker.gh_fetch_file = orig


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

    def test_cyrillic_bot_name_does_not_break_the_request(self):
        """🔴 Регресс главного дефекта отдела (найден 31.07.2026).

        urllib кодирует HTTP-заголовки в latin-1. Пока в User-Agent
        подставлялось имя бота, КАЖДОЕ чтение файла у всех пяти воркеров падало
        с UnicodeEncodeError, а fail-silent превращал это в пустой контекст —
        отдел месяцами писал код, ни разу не увидев ни одного файла.

        Тест обязан падать, если имя бота вернут в заголовки.
        """
        import base64 as _b64
        import io
        import json as _json

        captured = {}

        class FakeResp:
            def __init__(self, payload):
                self._b = io.BytesIO(payload)

            def read(self, *a):
                return self._b.read(*a)

            def __enter__(self):
                return self._b

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=0):
            captured["headers"] = dict(req.headers)
            body = _json.dumps({
                "content": _b64.b64encode("BOT_NAME = 'девви'".encode()).decode()
            }).encode()
            return FakeResp(body)

        os.environ["GH_PAT"] = "x"
        orig = worker.urllib.request.urlopen
        worker.urllib.request.urlopen = fake_urlopen
        try:
            text, err = worker.gh_fetch_file("devvy-bot", "bot.py", bot_name="девви")
            self.assertEqual(err, "", "кириллическое имя бота снова ломает чтение")
            self.assertIn("девви", text, "содержимое файла не вернулось")
            for k, v in captured["headers"].items():
                v.encode("latin-1")   # бросит UnicodeEncodeError, если вернут кириллицу
        finally:
            worker.urllib.request.urlopen = orig
            os.environ.pop("GH_PAT", None)

    def test_non_ascii_path_is_percent_encoded(self):
        """Тот же класс дефекта, но в URL: urllib требует ASCII и там тоже.

        Найдено при проверке фикса против живого API — путь с кириллицей ронял
        чтение с UnicodeEncodeError('ascii'), уже после того как заголовок
        починили. Лечится процентным кодированием.
        """
        import io
        import json as _json

        captured = {}

        class FakeResp:
            def __init__(self, payload):
                self._b = io.BytesIO(payload)

            def __enter__(self):
                return self._b

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            req.full_url.encode("ascii")   # бросит, если кодирование потеряли
            import base64 as _b64
            return FakeResp(_json.dumps({
                "content": _b64.b64encode(b"ok").decode()}).encode())

        os.environ["GH_PAT"] = "x"
        orig = worker.urllib.request.urlopen
        worker.urllib.request.urlopen = fake_urlopen
        try:
            text, err = worker.gh_fetch_file("devvy-bot", "папка/файл.py")
            self.assertEqual(err, "")
            self.assertEqual(text, "ok")
            self.assertIn("%", captured["url"], "путь не закодирован")
            self.assertIn("/contents/", captured["url"])
            self.assertNotIn("файл", captured["url"])
        finally:
            worker.urllib.request.urlopen = orig
            os.environ.pop("GH_PAT", None)

    def test_missing_file_is_not_an_error_but_broken_read_is(self):
        """404 = файла ещё нет (штатно для нового файла); прочие сбои = ошибка.

        Различие нужно, чтобы отказ работать вслепую не ломал задачи вида
        «создай новый файл в существующем репозитории».
        """
        os.environ["GH_PAT"] = "x"
        orig = worker.urllib.request.urlopen

        def raising(code):
            def _f(req, timeout=0):
                raise worker.urllib.error.HTTPError(
                    "url", code, "boom", {}, None)
            return _f

        try:
            worker.urllib.request.urlopen = raising(404)
            self.assertEqual(worker.gh_fetch_file("repo", "new.py"), ("", ""))

            worker.urllib.request.urlopen = raising(401)
            text, err = worker.gh_fetch_file("repo", "bot.py")
            self.assertEqual(text, "")
            self.assertIn("401", err, "сбой чтения обязан быть видимой ошибкой")
        finally:
            worker.urllib.request.urlopen = orig
            os.environ.pop("GH_PAT", None)


class TestContextBudget(unittest.TestCase):
    """Потолки воркера — наши числа, а не модели, и они уже раз истекли молча."""

    # Оба файла из заявок 24.08.2026. Обе упали одинаково — это форма
    # невыполнимости, а не слабой модели.
    REAL_FILES = {
        "kriss-bot/bot.py": 64158,     # 1459 строк, pyflakes в проде чист
        "billy-bot/bot.py": 70638,     # 1637 строк, pyflakes в проде чист
    }
    REAL_FILE = max(REAL_FILES.values())

    def test_fitting_context_gives_no_reason(self):
        self.assertEqual(worker.oversize_reason("x" * 100, 8000), "")

    def test_exactly_at_the_limit_still_fits(self):
        self.assertEqual(worker.oversize_reason("x" * 8000, 8000), "")

    def test_empty_context_never_refuses(self):
        """Пустой контекст — это «создай новый файл», а не отказ."""
        self.assertEqual(worker.oversize_reason("", 8000), "")
        self.assertEqual(worker.oversize_reason(None, 8000), "")

    def test_reason_carries_both_numbers_and_the_share_seen(self):
        for size, share in ((64158, "12%"), (70638, "11%")):
            with self.subTest(size=size):
                reason = worker.oversize_reason("x" * size, 8000)
                self.assertIn(str(size), reason)
                self.assertIn("8000", reason)
                self.assertIn(share, reason)

    def test_both_incident_files_fit_the_new_default(self):
        for name, size in self.REAL_FILES.items():
            with self.subTest(file=name):
                self.assertEqual(worker.oversize_reason("x" * size), "")

    def test_both_incident_files_were_refused_by_the_old_window(self):
        """Гейт, не ловящий свой инцидент, — не гейт."""
        for name, size in self.REAL_FILES.items():
            with self.subTest(file=name):
                self.assertIn("%", worker.oversize_reason("x" * size, 8000))

    def test_output_budget_leaves_room_for_the_whole_file(self):
        """Вернуть файл целиком нужно СИМВОЛАМИ, а печатать модель может токенами."""
        printable_chars = worker.WORKER_MAX_TOKENS * 35 // 10   # ~3.5 симв/токен для кода
        for name, size in self.REAL_FILES.items():
            with self.subTest(file=name):
                self.assertGreater(printable_chars, size)

    def test_both_budgets_stay_under_the_models_documented_ceilings(self):
        """claude-haiku-4-5: контекст 200k токенов, вывод 64k (docs, 24.08.2026)."""
        self.assertLessEqual(worker.WORKER_MAX_TOKENS, 64000)
        self.assertLessEqual(worker.MAX_CONTEXT_CHARS * 10 // 35, 200000)



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

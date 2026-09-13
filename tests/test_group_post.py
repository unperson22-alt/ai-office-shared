"""
Отправка в группу отчитывается по факту, а не по замыслу.

Инцидент 2026-09-10 22:38 UTC: Log-бот показал три MSG_OUT подряд (Гослинг,
Милли, Доктор), в группе оказалась одна реплика — Гослинга. Милли и Доктор
сгенерировали и оплатили ответ, `send_to_group` его потерял, а лог отчитался
за всех трёх. Искать после такого лога начинают в болталке — то есть там, где
всё исправно.

Здесь закреплено ровно то свойство, отсутствие которого сделало отказ
невидимым:

  1. Незаданный OFFICE_CHAT_ID — отказ (`no_chat_id`), а не тихий успех.
     Раньше `if not OFFICE_CHAT_ID: return None` не оставлял вообще следа.
  2. `ok:false` от Telegram — отказ, и в `reason` его СВОИ слова («chat not
     found», «Too Many Requests: retry after 37»), а не наш пересказ.
     Инвариант №8: провал называет того, кто упал.
  3. Успех нельзя получить, не дочитав ответ до `ok:true` (инвариант №4).
  4. В `office:group:history` реплика попадает ТОЛЬКО после подтверждённой
     доставки — иначе коллеги читают в group_ctx призрак и отвечают на него.
  5. Каждый отказ оставляет запись в `office:logs`: Влад смотрит логи, а не
     Railway stdout.
  6. `log_delivery` пишет MSG_OUT только на доставленное, ERROR — на потерянное,
     и адресатом ставит КАНАЛ («group»), а не собеседника: «Кому: Влад» на
     реплике в общую группу и было второй половиной путаницы.

Запуск:
    cd ai-office-shared && python3 -m unittest discover -s tests -v
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared import group_post as gp  # noqa: E402


def run(coro):
    """См. пояснение в test_inter_bot.run — чужой event loop убираем за собой."""
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


class FakeRedis:
    """Лента группы + office:logs — только то, что трогает group_post."""

    def __init__(self):
        self.history: list[str] = []
        self.logs: list[str] = []

    async def lpush(self, key, value):
        (self.logs if key.startswith("office:logs") else self.history).insert(0, value)

    async def ltrim(self, key, start, stop):
        pass

    async def expire(self, key, ttl):
        pass

    def pipeline(self, transaction=False):
        outer = self

        class _Pipe:
            def __init__(self):
                self._ops = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def lpush(self, key, value):
                self._ops.append((key, value))

            def ltrim(self, *a):
                pass

            def expire(self, *a):
                pass

            async def execute(self):
                for key, value in self._ops:
                    await outer.lpush(key, value)
                self._ops.clear()

        return _Pipe()


def fake_client(body=None, *, status=200, raise_exc=None, bad_json=False, seen=None):
    """Подменяет httpx.AsyncClient одним ответом Telegram."""

    class _Resp:
        status_code = status
        text = "" if body is None else str(body)

        def json(self):
            if bad_json:
                raise ValueError("not json")
            return body

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            if seen is not None:
                seen.append({"url": url, "json": kw.get("json") or {}})
            if raise_exc is not None:
                raise raise_exc
            return _Resp()

    return _Client


class _Patched:
    """Подмена httpx на время одного прогона."""

    def __init__(self, client):
        self.client = client

    def __enter__(self):
        self._orig = gp.httpx.AsyncClient
        gp.httpx.AsyncClient = self.client
        return self

    def __exit__(self, *a):
        gp.httpx.AsyncClient = self._orig
        return False


OK_BODY = {"ok": True, "result": {"message_id": 4242}}


class TestSilentExits(unittest.TestCase):
    """Три выхода, два из которых раньше не оставляли следа."""

    def test_no_chat_id_is_a_failure_not_a_success(self):
        r = FakeRedis()
        res = run(gp.post_to_group(token="t", chat_id="", text="привет",
                                   redis_client=r, bot="милли"))
        self.assertFalse(res.ok)
        self.assertFalse(bool(res))                  # __bool__ тоже не врёт
        self.assertEqual(res.kind, gp.NO_CHAT_ID)
        self.assertIn("OFFICE_CHAT_ID", res.reason)
        self.assertIsNone(res.message_id)

    def test_no_chat_id_leaves_a_trace_in_office_logs(self):
        """Главное отличие от прежнего `return None`: отказ видно при разборе."""
        r = FakeRedis()
        run(gp.post_to_group(token="t", chat_id="", text="привет",
                             redis_client=r, bot="милли"))
        self.assertEqual(len(r.logs), 1)
        self.assertIn("group_post_failed", r.logs[0])
        self.assertIn(gp.NO_CHAT_ID, r.logs[0])

    def test_missing_token_is_named_separately(self):
        res = run(gp.post_to_group(token="", chat_id="-100", text="привет"))
        self.assertFalse(res.ok)
        self.assertEqual(res.kind, gp.NO_TOKEN)

    def test_telegram_refusal_quotes_telegram(self):
        """Инвариант №8: причину называет дальняя сторона, не мы."""
        r = FakeRedis()
        body = {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
        with _Patched(fake_client(body)):
            res = run(gp.post_to_group(token="t", chat_id="-100", text="привет",
                                       redis_client=r, bot="милли"))
        self.assertFalse(res.ok)
        self.assertEqual(res.kind, gp.REFUSED)
        self.assertEqual(res.error_code, 400)
        self.assertIn("chat not found", res.reason)
        self.assertEqual(len(r.logs), 1)

    def test_flood_control_is_a_refusal_too(self):
        body = {"ok": False, "error_code": 429,
                "description": "Too Many Requests: retry after 37"}
        with _Patched(fake_client(body, status=429)):
            res = run(gp.post_to_group(token="t", chat_id="-100", text="привет"))
        self.assertFalse(res.ok)
        self.assertIn("retry after 37", res.reason)
        self.assertEqual(res.error_code, 429)

    def test_transport_failure_names_what_fell(self):
        with _Patched(fake_client(raise_exc=RuntimeError("connect timeout"))):
            res = run(gp.post_to_group(token="t", chat_id="-100", text="привет"))
        self.assertFalse(res.ok)
        self.assertEqual(res.kind, gp.UNREACHABLE)
        self.assertIn("connect timeout", res.reason)
        self.assertIn("api.telegram.org", res.reason)

    def test_unparseable_body_is_not_a_success(self):
        with _Patched(fake_client("<html>502</html>", status=502, bad_json=True)):
            res = run(gp.post_to_group(token="t", chat_id="-100", text="привет"))
        self.assertFalse(res.ok)
        self.assertEqual(res.kind, gp.BAD_BODY)
        self.assertEqual(res.error_code, 502)

    def test_no_exception_escapes(self):
        """
        Отправка в группу не имеет права ронять обработчик /task: что угодно
        от httpx (ConnectError, ReadTimeout, PoolTimeout, кривой TLS) должно
        стать отказом-значением, а не исключением.

        Именно Exception, а не BaseException: KeyboardInterrupt и
        CancelledError обязаны пролетать наружу — поймать отмену таски и
        отчитаться «не доставлено» значило бы завести здесь ту же ложь, от
        которой модуль и написан, только про собственную остановку.
        """
        for exc in (OSError("connection reset by peer"),
                    TimeoutError("read timeout"),
                    ValueError("bad url")):
            with self.subTest(exc=type(exc).__name__):
                with _Patched(fake_client(raise_exc=exc)):
                    res = run(gp.post_to_group(token="t", chat_id="-1", text="x"))
                self.assertFalse(res.ok)
                self.assertEqual(res.kind, gp.UNREACHABLE)

    def test_cancellation_is_not_swallowed(self):
        """Отмену таски глотать нельзя — иначе она станет «не доставлено»."""
        with _Patched(fake_client(raise_exc=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                run(gp.post_to_group(token="t", chat_id="-1", text="x"))


class TestSupergroupMigration(unittest.TestCase):
    """
    13.09.2026 офис встал целиком: группу повысили до супергруппы, её chat_id
    сменился, и КАЖДЫЙ бот получал «Bad Request: group chat was upgraded to a
    supergroup chat» на старый -5194783850. Причину мы называли (это уже
    работало), а новый id, который Telegram кладёт рядом в
    parameters.migrate_to_chat_id, выбрасывали — то есть применяли инвариант №8
    наполовину: процитировали жалобу и потеряли решение.
    """

    BODY = {
        "ok": False, "error_code": 400,
        "description": "Bad Request: group chat was upgraded to a supergroup chat",
        "parameters": {"migrate_to_chat_id": -1002194783850},
    }

    def test_new_chat_id_is_captured(self):
        with _Patched(fake_client(self.BODY)):
            res = run(gp.post_to_group(token="t", chat_id="-5194783850", text="привет"))
        self.assertFalse(res.ok)
        self.assertEqual(res.migrate_to, "-1002194783850")

    def test_migration_has_its_own_kind(self):
        """Отдельный kind, потому что это чинится иначе, чем «chat not found»."""
        with _Patched(fake_client(self.BODY)):
            res = run(gp.post_to_group(token="t", chat_id="-5194783850", text="привет"))
        self.assertEqual(res.kind, gp.MIGRATED)

    def test_describe_tells_what_to_do(self):
        with _Patched(fake_client(self.BODY)):
            res = run(gp.post_to_group(token="t", chat_id="-5194783850", text="привет"))
        text = res.describe()
        self.assertIn("-1002194783850", text)
        self.assertIn("OFFICE_CHAT_ID", text)

    def test_new_id_reaches_office_logs(self):
        """Влад смотрит логи — значение должно быть там, а не только в ответе."""
        r = FakeRedis()
        with _Patched(fake_client(self.BODY)):
            run(gp.post_to_group(token="t", chat_id="-5194783850", text="привет",
                                 redis_client=r, bot="милли"))
        self.assertEqual(len(r.logs), 1)
        self.assertIn("-1002194783850", r.logs[0])

    def test_refusal_without_parameters_stays_plain_refused(self):
        """Без migrate_to_chat_id это обычный отказ — kind не подменяем."""
        body = {"ok": False, "error_code": 403,
                "description": "Forbidden: bot was kicked from the group chat"}
        with _Patched(fake_client(body)):
            res = run(gp.post_to_group(token="t", chat_id="-100", text="привет"))
        self.assertEqual(res.kind, gp.REFUSED)
        self.assertEqual(res.migrate_to, "")
        self.assertNotIn("OFFICE_CHAT_ID", res.describe())

    def test_lost_reply_still_logs_error_not_msg_out(self):
        """Миграция — тоже потеря: MSG_OUT здесь был бы ложью."""
        calls = []

        async def _log(event, msg, from_="", to_=""):
            calls.append({"event": event, "msg": msg})

        with _Patched(fake_client(self.BODY)):
            res = run(gp.post_to_group(token="t", chat_id="-5194783850", text="x"))
        run(gp.log_delivery(_log, res, text="Милли: работаем", agent="Милли"))
        self.assertEqual(calls[0]["event"], "ERROR")
        self.assertIn("-1002194783850", calls[0]["msg"])


class TestSuccess(unittest.TestCase):
    def test_ok_true_is_the_only_way_to_get_ok(self):
        with _Patched(fake_client(OK_BODY)):
            res = run(gp.post_to_group(token="t", chat_id="-100", text="привет"))
        self.assertTrue(res.ok)
        self.assertTrue(bool(res))
        self.assertEqual(res.kind, gp.SENT)
        self.assertEqual(res.message_id, 4242)
        self.assertEqual(res.chat_id, "-100")
        self.assertEqual(res.chat_id_int, -100)
        self.assertEqual(res.reason, "")

    def test_payload_carries_text_and_chat(self):
        seen = []
        with _Patched(fake_client(OK_BODY, seen=seen)):
            run(gp.post_to_group(token="TOKEN", chat_id="-100", text="привет",
                                 parse_mode="HTML"))
        self.assertEqual(len(seen), 1)
        self.assertIn("botTOKEN/sendMessage", seen[0]["url"])
        self.assertEqual(seen[0]["json"]["chat_id"], "-100")
        self.assertEqual(seen[0]["json"]["text"], "привет")
        self.assertEqual(seen[0]["json"]["parse_mode"], "HTML")

    def test_parse_mode_absent_unless_asked(self):
        seen = []
        with _Patched(fake_client(OK_BODY, seen=seen)):
            run(gp.post_to_group(token="t", chat_id="-100", text="привет"))
        self.assertNotIn("parse_mode", seen[0]["json"])

    def test_success_writes_no_failure_log(self):
        r = FakeRedis()
        with _Patched(fake_client(OK_BODY)):
            run(gp.post_to_group(token="t", chat_id="-100", text="привет",
                                 redis_client=r, bot="милли"))
        self.assertEqual(r.logs, [])


class TestGroupHistory(unittest.TestCase):
    """В ленту — только доставленное: иначе коллеги отвечают на призрак."""

    def test_delivered_reply_reaches_the_feed(self):
        r = FakeRedis()
        with _Patched(fake_client(OK_BODY)):
            run(gp.post_to_group(token="t", chat_id="-100", text="привет",
                                 sender_name="Милли", redis_client=r))
        self.assertEqual(len(r.history), 1)
        self.assertIn("Милли", r.history[0])

    def test_refused_reply_never_reaches_the_feed(self):
        r = FakeRedis()
        body = {"ok": False, "error_code": 403, "description": "Forbidden: bot was kicked"}
        with _Patched(fake_client(body)):
            run(gp.post_to_group(token="t", chat_id="-100", text="привет",
                                 sender_name="Милли", redis_client=r))
        self.assertEqual(r.history, [])

    def test_unconfigured_chat_never_reaches_the_feed(self):
        r = FakeRedis()
        run(gp.post_to_group(token="t", chat_id="", text="привет",
                             sender_name="Милли", redis_client=r))
        self.assertEqual(r.history, [])

    def test_no_sender_name_means_someone_else_keeps_the_feed(self):
        r = FakeRedis()
        with _Patched(fake_client(OK_BODY)):
            run(gp.post_to_group(token="t", chat_id="-100", text="привет",
                                 redis_client=r))
        self.assertEqual(r.history, [])


class TestLogDelivery(unittest.TestCase):
    """Тот самый выбор, которого в handle_task не было вовсе."""

    def setUp(self):
        self.calls = []

        async def _log(event, msg, from_="", to_=""):
            self.calls.append({"event": event, "msg": msg, "from": from_, "to": to_})

        self.log = _log

    def test_delivered_logs_msg_out(self):
        res = gp.PostResult(ok=True, kind=gp.SENT, message_id=7, chat_id="-100")
        run(gp.log_delivery(self.log, res, text="Милли: работаем", agent="Милли"))
        self.assertEqual(self.calls[0]["event"], "MSG_OUT")
        self.assertEqual(self.calls[0]["msg"], "Милли: работаем")

    def test_lost_reply_logs_error_not_msg_out(self):
        """Ровно то, что Влад увидел бы 10.09 вместо ложного MSG_OUT."""
        res = gp.PostResult(ok=False, kind=gp.NO_CHAT_ID, chat_id="",
                            reason="OFFICE_CHAT_ID не задан в env сервиса")
        run(gp.log_delivery(self.log, res, text="Милли: работаем", agent="Милли"))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["event"], "ERROR")
        self.assertNotEqual(self.calls[0]["event"], "MSG_OUT")
        self.assertIn("не доставлена", self.calls[0]["msg"])
        self.assertIn("OFFICE_CHAT_ID", self.calls[0]["msg"])

    def test_lost_reply_keeps_the_text_nobody_saw(self):
        res = gp.PostResult(ok=False, kind=gp.REFUSED, error_code=400,
                            reason="Bad Request: chat not found")
        run(gp.log_delivery(self.log, res, text="Милли: работаем", agent="Милли"))
        self.assertIn("Милли: работаем", self.calls[0]["msg"])
        self.assertIn("chat not found", self.calls[0]["msg"])

    def test_recipient_is_the_channel_not_the_interlocutor(self):
        """«Кому: Влад» на реплике в общую группу — вторая половина путаницы."""
        res = gp.PostResult(ok=True, kind=gp.SENT, message_id=7, chat_id="-100")
        run(gp.log_delivery(self.log, res, text="Милли: работаем", agent="Милли"))
        self.assertEqual(self.calls[0]["to"], "group")
        self.assertEqual(self.calls[0]["from"], "Милли")


class TestDescribe(unittest.TestCase):
    def test_success_names_where_it_landed(self):
        res = gp.PostResult(ok=True, kind=gp.SENT, message_id=7, chat_id="-100")
        self.assertIn("-100", res.describe())
        self.assertIn("7", res.describe())

    def test_failure_names_kind_code_and_words(self):
        res = gp.PostResult(ok=False, kind=gp.REFUSED, error_code=403,
                            reason="Forbidden: bot was kicked from the group chat")
        text = res.describe()
        self.assertIn(gp.REFUSED, text)
        self.assertIn("403", text)
        self.assertIn("bot was kicked", text)

    def test_non_numeric_chat_id_does_not_crash(self):
        res = gp.PostResult(ok=False, kind=gp.NO_CHAT_ID, chat_id="@officechat")
        self.assertIsNone(res.chat_id_int)


if __name__ == "__main__":
    unittest.main(verbosity=2)

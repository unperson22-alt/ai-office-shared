"""
Пустой ответ воркера при HTTP 200 — это провал, а не молчание.

23.08.2026 первая заявка из очереди (62ffa25b5e30) упала с диагнозом «Рикки не
ответил вообще», при том что все пять воркеров отвечали на /health кодом 200.
Разбираться было нечем: `_call_worker` возвращал `resp.json().get("response",
"")`, и воркер, ответивший 200 с пустым полем, отдавал ту же пустую строку, что
и «мне нечего сказать». Дальше по цепочке пустота выглядела как отсутствующий
блок кода.

Это форма урока #81: провал не должен возвращать то же значение, что штатный
пустой результат — вызывающий не может их различить и действует на пустоту как
на факт. Теперь пустой response поднимает исключение: попытка считается
неудачной, отрабатывают ретраи, а исчерпав их, воркер честно возвращает ERROR
со своим именем.
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared import dev_pipeline as dp  # noqa: E402


def run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeClient:
    """httpx-клиент, отвечающий заданным телом. Считает попытки."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def post(self, url, **kw):
        self.calls += 1
        return FakeResponse(self.payload)


class TestEmptyWorkerAnswer(unittest.TestCase):
    def setUp(self):
        self._retries = dp._MAX_RETRIES
        dp._MAX_RETRIES = 0          # без ожиданий backoff в тесте

    def tearDown(self):
        dp._MAX_RETRIES = self._retries

    def _call(self, payload):
        client = FakeClient(payload)
        out = run(dp._call_worker(client, dp.RICKY_URL, "сделай ревью",
                                  redis_client=None, task_id="t1"))
        return out, client

    def test_empty_response_becomes_an_explicit_error(self):
        out, _ = self._call({"response": ""})
        self.assertTrue(out.startswith("ERROR:"), out)
        self.assertIn("response", out)

    def test_whitespace_only_response_is_also_an_error(self):
        out, _ = self._call({"response": "   \n  "})
        self.assertTrue(out.startswith("ERROR:"), out)

    def test_missing_response_field_is_an_error(self):
        out, _ = self._call({})
        self.assertTrue(out.startswith("ERROR:"), out)

    def test_empty_answer_is_retried_not_accepted(self):
        # Пустота могла быть разовой — ретраи обязаны отработать, а не
        # засчитаться сразу.
        dp._MAX_RETRIES = 2
        client = FakeClient({"response": ""})
        out = run(dp._call_worker(client, dp.RICKY_URL, "ревью",
                                  redis_client=None, task_id="t1"))
        self.assertEqual(client.calls, 3)
        self.assertTrue(out.startswith("ERROR:"))

    def test_real_answer_passes_through_untouched(self):
        out, client = self._call({"response": "```python\nx = 1\n```"})
        self.assertEqual(out, "```python\nx = 1\n```")
        self.assertEqual(client.calls, 1)


if __name__ == "__main__":
    unittest.main()

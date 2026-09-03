"""
Какие маршруты офиса отвечают без секрета — и почему именно эти.

`/version` появился 02.09.2026, когда выяснилось, что «смёржено» и «работает» —
неразличимые состояния: фикс ретуши лежал в main, SHA пакета был поднят, обе
ветки смёржены, а единственным свидетельством того, что это дошло до человека,
были слова исполнителя деплоя «✅ задеплоен» (инвариант 5). `/health` отвечает
`{"status": "ok"}` одинаково до и после.

Смысл эндпоинта — независимая проверка, поэтому он обязан отвечать БЕЗ токена
офиса. Сегодня он ответил бы и так: OFFICE_RPC_TOKEN не выставлен ни на одном
сервисе, и middleware работает в Фазе A (пропускает всех). Именно поэтому тест
и нужен: включение enforcement (OFFICE_RPC_STRICT) — отдельная задача, и в тот
день `/version` молча начал бы отдавать 401, то есть перестал бы отвечать на
вопрос, ради которого написан, ровно тогда, когда офис станет строже.
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared import auth  # noqa: E402


class FakeRequest:
    def __init__(self, path, method="GET", headers=None):
        self.path = path
        self.method = method
        self.headers = headers or {}


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _ok(request):
    return "прошло"


class TestOpenPaths(unittest.TestCase):
    """С включённым enforcement и без токена: что проходит, что нет."""

    def setUp(self):
        self._token, self._strict = auth.OFFICE_RPC_TOKEN, auth.OFFICE_RPC_STRICT
        auth.OFFICE_RPC_TOKEN = "секрет-офиса"
        auth.OFFICE_RPC_STRICT = True

    def tearDown(self):
        auth.OFFICE_RPC_TOKEN, auth.OFFICE_RPC_STRICT = self._token, self._strict

    def _call(self, path, headers=None):
        return run(auth.office_auth_middleware(FakeRequest(path, headers=headers), _ok))

    def test_health_answers_without_a_token(self):
        self.assertEqual(self._call("/health"), "прошло")

    def test_version_answers_without_a_token(self):
        """
        Проверка, которой нужен секрет офиса, независимой не является: её не
        сделает ни сессия без токена, ни внешний watchdog — а именно для них
        эндпоинт и написан.
        """
        self.assertEqual(self._call("/version"), "прошло")

    def test_trailing_slash_does_not_close_the_path(self):
        self.assertEqual(self._call("/version/"), "прошло")

    def test_everything_else_still_needs_the_token(self):
        for path in ("/task", "/secrets", "/redis", "/logs", "/envcheck"):
            resp = self._call(path)
            self.assertNotEqual(resp, "прошло", f"{path} открыт без токена")
            self.assertEqual(getattr(resp, "status", None), 401, path)

    def test_a_valid_token_still_opens_the_rest(self):
        headers = {auth.OFFICE_AUTH_HEADER: "секрет-офиса"}
        self.assertEqual(self._call("/task", headers), "прошло")


if __name__ == "__main__":
    unittest.main()

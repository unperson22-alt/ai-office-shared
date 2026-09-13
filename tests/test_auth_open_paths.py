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


class TestLegacySecretHeader(unittest.TestCase):
    """
    Крисс молча выпадала из болталки, и это лежало в логах Филли с 10.09.2026:
    «banter_ping agents=нет failed=КРИС:401». Её /task проверяет
    `X-Secret-Token == HTTP_SECRET` — проверку она завела раньше офисного меша
    и не меняла, — а office_headers() слал только X-Office-Token, которого
    сегодня вообще нет (OFFICE_RPC_TOKEN не выставлен, Фаза A).

    Секрет у Филли есть — тот же HTTP_SECRET, что у Крисс. Она его не слала.
    """

    def setUp(self):
        self._saved = (auth.OFFICE_RPC_TOKEN, auth.LEGACY_SECRET)

    def tearDown(self):
        auth.OFFICE_RPC_TOKEN, auth.LEGACY_SECRET = self._saved

    def test_legacy_secret_is_sent_when_set(self):
        auth.OFFICE_RPC_TOKEN, auth.LEGACY_SECRET = "", "office-secret"
        h = auth.office_headers()
        self.assertEqual(h.get(auth.LEGACY_AUTH_HEADER), "office-secret")

    def test_both_secrets_travel_together(self):
        """Фаза B не должна выключить Крисс обратно."""
        auth.OFFICE_RPC_TOKEN, auth.LEGACY_SECRET = "mesh", "legacy"
        h = auth.office_headers()
        self.assertEqual(h.get(auth.OFFICE_AUTH_HEADER), "mesh")
        self.assertEqual(h.get(auth.LEGACY_AUTH_HEADER), "legacy")

    def test_nothing_sent_when_unset(self):
        """Пустой секрет — не заголовок с пустым значением, а его отсутствие."""
        auth.OFFICE_RPC_TOKEN, auth.LEGACY_SECRET = "", ""
        self.assertEqual(auth.office_headers(), {})

    def test_extra_headers_survive(self):
        auth.OFFICE_RPC_TOKEN, auth.LEGACY_SECRET = "", "legacy"
        h = auth.office_headers({"Content-Type": "application/json"})
        self.assertEqual(h["Content-Type"], "application/json")
        self.assertEqual(h[auth.LEGACY_AUTH_HEADER], "legacy")

    def test_caller_dict_is_not_mutated(self):
        auth.OFFICE_RPC_TOKEN, auth.LEGACY_SECRET = "", "legacy"
        extra = {"A": "1"}
        auth.office_headers(extra)
        self.assertEqual(extra, {"A": "1"})

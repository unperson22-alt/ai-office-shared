"""Секреты не доходят до обработчика логов, а чужие логи чистятся на входе.

Инцидент 23.08.2026: в логах villy-bot двенадцать подряд строк
`HTTP Request: POST https://api.telegram.org/bot<ТОКЕН>/getUpdates "200 OK"` —
их пишет httpx на INFO, и `basicConfig(level=INFO)` стоит в каждом боте офиса.
Токен бота печатался в логи Railway на каждый опрос Telegram.
"""
import ast
import io
import logging
import os
import pathlib
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared import log_redaction as lr   # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Токен из документации Telegram — хвост 34 символа, короче нынешних 35.
DOC_TOKEN = "110201543:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
LEAK_LINE = (
    "HTTP Request: POST https://api.telegram.org/bot"
    + DOC_TOKEN
    + '/getUpdates "HTTP/1.1 200 OK"'
)


class TestRedactTelegramToken(unittest.TestCase):
    def test_the_exact_line_from_the_incident_loses_its_token(self):
        out = lr.redact(LEAK_LINE)
        self.assertNotIn(DOC_TOKEN, out)
        self.assertIn(lr.MASK, out)

    def test_the_rest_of_the_line_survives(self):
        out = lr.redact(LEAK_LINE)
        for kept in ("getUpdates", "200 OK", "api.telegram.org"):
            self.assertIn(kept, out)

    def test_a_bare_token_outside_a_url_is_cut_too(self):
        self.assertNotIn(DOC_TOKEN, lr.redact(f"TELEGRAM_TOKEN={DOC_TOKEN}"))

    def test_new_style_token_with_a_longer_tail(self):
        tok = "8123456789:" + "A" * 35
        self.assertNotIn(tok, lr.redact(f"used {tok} here"))


class TestRedactDoesNotEatOrdinaryText(unittest.TestCase):
    """Гейт, съедающий полезные логи, был бы хуже болезни."""

    CLEAN = (
        "2026-08-23T18:16:44Z started",
        "12:30 heartbeat",
        "elapsed_ms=25000 updated_at=1755972000 id=97238517735",
        "deployment d34cd1a2-1f0e-4b7a-9f3e-2b7c5d6e8a90 SUCCESS",
        "Traceback (most recent call last):",
        "",
    )

    def test_clean_lines_are_returned_unchanged(self):
        for line in self.CLEAN:
            with self.subTest(line=line):
                self.assertEqual(lr.redact(line), line)


class TestRedactOtherProviders(unittest.TestCase):
    SECRETS = (
        "sk-ant-api03-" + "a" * 40,
        "ghp_" + "b" * 36,
        "xoxb-123456789012-abcdefghijkl",
    )

    def test_provider_keys_are_cut(self):
        for secret in self.SECRETS:
            with self.subTest(secret=secret[:12]):
                self.assertNotIn(secret, lr.redact(f"key={secret} tail"))


class TestRegisterSecret(unittest.TestCase):
    def setUp(self):
        self._saved = set(lr._registered)
        lr._registered.clear()

    def tearDown(self):
        lr._registered.clear()
        lr._registered.update(self._saved)

    def test_a_long_value_is_cut_verbatim(self):
        self.assertTrue(lr.register_secret("1BVtsOKgBu5fZ7HgSuPeRsEcReT"))
        self.assertNotIn("1BVtsOKgBu5fZ7HgSuPeRsEcReT",
                         lr.redact("session=1BVtsOKgBu5fZ7HgSuPeRsEcReT"))

    def test_a_short_value_is_refused_so_logs_stay_readable(self):
        self.assertFalse(lr.register_secret("abc"))
        self.assertEqual(lr.redact("abc def"), "abc def")

    def test_empty_and_none_are_refused(self):
        self.assertFalse(lr.register_secret(""))
        self.assertFalse(lr.register_secret(None))

    def test_env_names_are_read_by_name(self):
        os.environ["PROBE_TOKEN_FOR_TEST"] = DOC_TOKEN
        try:
            added = lr.register_env_secrets(["PROBE_TOKEN_FOR_TEST", "NO_SUCH_VAR_HERE"])
        finally:
            os.environ.pop("PROBE_TOKEN_FOR_TEST", None)
        self.assertEqual(added, 1)
        self.assertEqual(lr.registered_count(), 1)

    def test_redaction_is_idempotent(self):
        once = lr.redact(LEAK_LINE)
        self.assertEqual(lr.redact(once), once)


class TestInstalledFactory(unittest.TestCase):
    """Фабрика, а не фильтр: обработчик, добавленный позже, тоже накрыт."""

    @staticmethod
    def _capture(logger_name):
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(logging.Formatter("%(message)s"))
        log = logging.getLogger(logger_name)
        log.handlers = [handler]
        log.setLevel(logging.INFO)
        log.propagate = False
        return log, buf

    def setUp(self):
        lr.install_secret_redaction(from_env=False)

    def test_token_in_a_percent_argument_never_reaches_the_handler(self):
        log, buf = self._capture("probe.args")
        log.info("HTTP Request: %s %s", "POST",
                 f"https://api.telegram.org/bot{DOC_TOKEN}/getUpdates")
        self.assertNotIn(DOC_TOKEN, buf.getvalue())
        self.assertIn(lr.MASK, buf.getvalue())

    def test_token_inside_a_traceback_is_cut(self):
        log, buf = self._capture("probe.exc")
        try:
            raise ValueError(
                f"401 for url https://api.telegram.org/bot{DOC_TOKEN}/sendMessage")
        except ValueError:
            log.exception("send failed")
        out = buf.getvalue()
        self.assertNotIn(DOC_TOKEN, out)
        self.assertIn("ValueError", out)
        self.assertIn("send failed", out)

    def test_handler_added_after_install_is_covered(self):
        log, buf = self._capture("probe.late")
        log.info("token %s", DOC_TOKEN)
        self.assertNotIn(DOC_TOKEN, buf.getvalue())

    def test_second_install_reports_that_it_did_nothing(self):
        self.assertFalse(lr.install_secret_redaction(from_env=False))

    def test_message_without_secrets_is_left_alone(self):
        log, buf = self._capture("probe.clean")
        log.info("task %s finished in %d ms", "62ffa25b5e30", 1200)
        self.assertEqual(buf.getvalue().strip(), "task 62ffa25b5e30 finished in 1200 ms")


class TestQuietHttpClients(unittest.TestCase):
    def test_httpx_stops_writing_the_getupdates_line(self):
        logging.getLogger("httpx").setLevel(logging.NOTSET)
        lr.quiet_http_client_logs()
        self.assertEqual(logging.getLogger("httpx").level, logging.WARNING)

    def test_errors_are_still_visible(self):
        lr.quiet_http_client_logs()
        self.assertTrue(logging.getLogger("httpx").isEnabledFor(logging.ERROR))

    def test_every_named_client_is_raised(self):
        lr.quiet_http_client_logs()
        for name in lr.HTTP_CLIENT_LOGGERS:
            with self.subTest(logger=name):
                self.assertEqual(logging.getLogger(name).level, logging.WARNING)


class TestWiredIntoTheOffice(unittest.TestCase):
    """Хелпер без вызова — не защита. Проверяем, что он вызван везде, где есть
    свой logging.basicConfig и доступ к пакету."""

    WIRED = ("agents/coder.py",
             "ai_office_shared/shared/worker.py",
             "shared/new_bot_template.py")

    @staticmethod
    def _calls(path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        return {n.func.id for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}

    def test_both_helpers_are_called(self):
        for rel in self.WIRED:
            with self.subTest(file=rel):
                calls = self._calls(ROOT / rel)
                self.assertIn("install_secret_redaction", calls)
                self.assertIn("quiet_http_client_logs", calls)

    def test_generated_bot_template_quiets_httpx_without_the_package(self):
        """У бота по шаблону нет пина ai_office_shared — там голый logging."""
        src = (ROOT / "agents" / "coder.py").read_text(encoding="utf-8")
        body = src[src.index("BOT_TEMPLATE = "):src.index("REQUIREMENTS_TEMPLATE")]
        self.assertIn('logging.getLogger("httpx").setLevel(logging.WARNING)', body)

    def test_railway_logs_are_redacted_on_the_way_in(self):
        """Чужие логи чистятся при чтении: строка уезжает в промпт и в чат."""
        src = (ROOT / "agents" / "coder.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("redact("), 3,
                         "точек чтения логов Railway должно быть три")


if __name__ == "__main__":
    unittest.main()

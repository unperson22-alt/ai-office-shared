"""Секреты не доходят до обработчика логов, а чужие логи чистятся на входе.

Инцидент 23.08.2026: в логах villy-bot двенадцать подряд строк
`HTTP Request: POST https://api.telegram.org/bot<ТОКЕН>/getUpdates "200 OK"` —
их пишет httpx на INFO, и `basicConfig(level=INFO)` стоит в каждом боте.
"""
from __future__ import annotations

import ast
import io
import logging
import pathlib

import pytest

from ai_office_shared.shared import log_redaction as lr

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Токен из документации Telegram — хвост 34 символа, короче нынешних 35.
DOC_TOKEN = "110201543:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
LEAK_LINE = (
    'HTTP Request: POST https://api.telegram.org/bot'
    + DOC_TOKEN
    + '/getUpdates "HTTP/1.1 200 OK"'
)


class TestRedactTelegramToken:
    def test_the_exact_line_from_the_incident_loses_its_token(self):
        out = lr.redact(LEAK_LINE)
        assert DOC_TOKEN not in out
        assert lr.MASK in out

    def test_the_rest_of_the_line_survives(self):
        out = lr.redact(LEAK_LINE)
        assert "getUpdates" in out and "200 OK" in out and "api.telegram.org" in out

    def test_a_bare_token_outside_a_url_is_cut_too(self):
        assert DOC_TOKEN not in lr.redact(f"TELEGRAM_TOKEN={DOC_TOKEN}")

    def test_new_style_token_with_a_longer_tail(self):
        tok = "8123456789:" + "A" * 35
        assert tok not in lr.redact(f"used {tok} here")


class TestRedactDoesNotEatOrdinaryText:
    @pytest.mark.parametrize("line", [
        "2026-08-23T18:16:44Z started",
        "12:30 heartbeat",
        "elapsed_ms=25000 updated_at=1755972000 id=97238517735",
        "deployment d34cd1a2-1f0e-4b7a-9f3e-2b7c5d6e8a90 SUCCESS",
        "Traceback (most recent call last):",
        "",
    ])
    def test_clean_line_is_returned_unchanged(self, line):
        assert lr.redact(line) == line


class TestRedactOtherProviders:
    @pytest.mark.parametrize("secret", [
        "sk-ant-api03-" + "a" * 40,
        "ghp_" + "b" * 36,
        "xoxb-123456789012-abcdefghijkl",
    ])
    def test_provider_key_is_cut(self, secret):
        assert secret not in lr.redact(f"key={secret} tail")


class TestRegisterSecret:
    def test_a_long_value_is_cut_verbatim(self, monkeypatch):
        monkeypatch.setattr(lr, "_registered", set())
        assert lr.register_secret("1BVtsOKgBu5fZ7HgSuPeRsEcReT") is True
        assert "1BVtsOKgBu5fZ7HgSuPeRsEcReT" not in lr.redact("session=1BVtsOKgBu5fZ7HgSuPeRsEcReT")

    def test_a_short_value_is_refused_so_logs_stay_readable(self, monkeypatch):
        monkeypatch.setattr(lr, "_registered", set())
        assert lr.register_secret("abc") is False
        assert lr.redact("abc def") == "abc def"

    def test_empty_and_none_are_refused(self, monkeypatch):
        monkeypatch.setattr(lr, "_registered", set())
        assert lr.register_secret("") is False
        assert lr.register_secret(None) is False

    def test_env_names_are_read_by_name(self, monkeypatch):
        monkeypatch.setattr(lr, "_registered", set())
        monkeypatch.setenv("TELEGRAM_TOKEN", DOC_TOKEN)
        assert lr.register_env_secrets(["TELEGRAM_TOKEN", "NO_SUCH_VAR_HERE"]) == 1
        assert lr.registered_count() == 1

    def test_redaction_is_idempotent(self):
        once = lr.redact(LEAK_LINE)
        assert lr.redact(once) == once


class TestInstalledFactory:
    """Фабрика, а не фильтр: обработчик, добавленный позже, тоже накрыт."""

    @staticmethod
    def _capture(logger_name: str):
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(logging.Formatter("%(message)s"))
        log = logging.getLogger(logger_name)
        log.handlers = [handler]
        log.setLevel(logging.INFO)
        log.propagate = False
        return log, buf

    def test_token_in_a_percent_argument_never_reaches_the_handler(self):
        lr.install_secret_redaction()
        log, buf = self._capture("probe.args")
        log.info("HTTP Request: %s %s", "POST",
                 f"https://api.telegram.org/bot{DOC_TOKEN}/getUpdates")
        assert DOC_TOKEN not in buf.getvalue()
        assert lr.MASK in buf.getvalue()

    def test_token_inside_a_traceback_is_cut(self):
        lr.install_secret_redaction()
        log, buf = self._capture("probe.exc")
        try:
            raise ValueError(f"401 for url https://api.telegram.org/bot{DOC_TOKEN}/sendMessage")
        except ValueError:
            log.exception("send failed")
        out = buf.getvalue()
        assert DOC_TOKEN not in out
        assert "ValueError" in out and "send failed" in out

    def test_handler_added_after_install_is_covered(self):
        lr.install_secret_redaction()
        log, buf = self._capture("probe.late")
        log.info("token %s", DOC_TOKEN)
        assert DOC_TOKEN not in buf.getvalue()

    def test_second_install_reports_that_it_did_nothing(self):
        lr.install_secret_redaction()
        assert lr.install_secret_redaction() is False

    def test_message_without_secrets_is_left_alone(self):
        lr.install_secret_redaction()
        log, buf = self._capture("probe.clean")
        log.info("task %s finished in %d ms", "62ffa25b5e30", 1200)
        assert buf.getvalue().strip() == "task 62ffa25b5e30 finished in 1200 ms"


class TestQuietHttpClients:
    def test_httpx_stops_writing_the_getupdates_line(self):
        logging.getLogger("httpx").setLevel(logging.NOTSET)
        lr.quiet_http_client_logs()
        assert logging.getLogger("httpx").level == logging.WARNING

    def test_errors_are_still_visible(self):
        lr.quiet_http_client_logs()
        assert logging.getLogger("httpx").isEnabledFor(logging.ERROR)

    def test_every_named_client_is_raised(self):
        lr.quiet_http_client_logs()
        for name in lr.HTTP_CLIENT_LOGGERS:
            assert logging.getLogger(name).level == logging.WARNING


class TestWiredIntoTheOffice:
    """Хелпер без вызова — не защита. Проверяем, что он вызван везде, где есть
    свой logging.basicConfig и доступ к пакету."""

    @staticmethod
    def _calls(path: pathlib.Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        return {
            n.func.id for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }

    @pytest.mark.parametrize("rel", ["agents/coder.py",
                                     "ai_office_shared/shared/worker.py",
                                     "shared/new_bot_template.py"])
    def test_both_helpers_are_called(self, rel):
        calls = self._calls(ROOT / rel)
        assert "install_secret_redaction" in calls, rel
        assert "quiet_http_client_logs" in calls, rel

    def test_generated_bot_template_quiets_httpx_without_the_package(self):
        """У бота по шаблону нет пина ai_office_shared — там голый logging."""
        src = (ROOT / "agents" / "coder.py").read_text(encoding="utf-8")
        start = src.index("BOT_TEMPLATE = ")
        body = src[start:src.index('REQUIREMENTS_TEMPLATE')]
        assert 'logging.getLogger("httpx").setLevel(logging.WARNING)' in body

    def test_railway_logs_are_redacted_on_the_way_in(self):
        """Чужие логи чистятся при чтении: строка уезжает в промпт и в чат."""
        src = (ROOT / "agents" / "coder.py").read_text(encoding="utf-8")
        assert src.count("redact(") == 3, "точек чтения логов Railway должно быть три"

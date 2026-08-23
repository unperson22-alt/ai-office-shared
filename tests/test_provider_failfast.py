"""
Сбой доступа к модели — не провал отдела и не повод для ретраев.

23.08.2026 две заявки подряд (62ffa25b5e30 и aa573bddb6d7) упёрлись в одну и ту
же ошибку: HTTP 400 «Your credit balance is too low to access the Anthropic
API». На счету не было денег — и повтор запроса не мог это изменить ни при
каком числе попыток. Но пайплайн честно отработал полный бюджет: три попытки в
run_dev_chain, внутри каждой поход к Девви, около 70 минут и ~19 вызовов на
подтверждение одного и того же ответа. Слот исполнения всё это время был занят.

Владельцу это ушло формулировкой «команда не дала код, прошедший гейт» — то
есть как провал отдела. Отдел не смог даже начать: диагноз посылал искать баг
там, где его нет.

Отличать обязательно от 429 и 5xx: те как раз проходят по повтору, и глушить
их ретраи было бы прямым вредом.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared.dev_pipeline import unrecoverable_provider_error  # noqa: E402

# Ровно то, что вернул Девви 23.08 — без сокращений.
REAL_ERROR = (
    "ERROR: девви не смог обработать задачу: Error code: 400 - {'type': 'error', "
    "'error': {'type': 'invalid_request_error', 'message': 'Your credit balance "
    "is too low to access the Anthropic API. Please go to Plans & Billing to "
    "upgrade or purchase credits.'}}"
)


class TestUnrecoverableIsRecognised(unittest.TestCase):
    def test_the_real_failure_is_recognised_and_named_in_words(self):
        why = unrecoverable_provider_error(REAL_ERROR)
        self.assertTrue(why)
        self.assertIn("деньги", why)

    def test_auth_and_permission_failures_are_unrecoverable_too(self):
        for text, expect in (
            ("Error code: 401 - authentication_error", "ключ"),
            ("Error code: 403 - permission_error", "прав"),
            ("invalid x-api-key", "неверный"),
        ):
            self.assertIn(expect, unrecoverable_provider_error(text), text)

    def test_it_scans_every_worker_not_just_the_first(self):
        why = unrecoverable_provider_error("", "", REAL_ERROR, "", "")
        self.assertTrue(why)


class TestRetryableStaysRetryable(unittest.TestCase):
    """429 и 5xx проходят по повтору — заглушить их значило бы сломать рабочее."""

    def test_rate_limit_is_not_unrecoverable(self):
        self.assertEqual(
            unrecoverable_provider_error("Error code: 429 - rate_limit_error"), "")

    def test_server_errors_are_not_unrecoverable(self):
        for text in ("Error code: 500 - api_error",
                     "Error code: 502 Bad Gateway",
                     "Error code: 529 - overloaded_error"):
            self.assertEqual(unrecoverable_provider_error(text), "", text)

    def test_worker_code_failures_are_not_mistaken_for_infrastructure(self):
        # Отказ воркера по качеству кода обязан идти обычным путём с ретраями.
        for text in (
            "ERROR: рикки не смог получить проходящий проверку код за 3 попыт(ки). "
            "Последняя ошибка: unterminated triple-quoted string literal, строка 12",
            "NEEDS_FIX: обработка ошибок отсутствует",
            "SyntaxError (строка 4): invalid syntax",
        ):
            self.assertEqual(unrecoverable_provider_error(text), "", text)

    def test_empty_and_missing_input_is_not_an_infrastructure_verdict(self):
        self.assertEqual(unrecoverable_provider_error(), "")
        self.assertEqual(unrecoverable_provider_error("", None, "   "), "")


# ── Поведение самой цепочки: обрыв сразу и без сожжённого раунда ─────────────
# coder.py не импортируется — функции достаются из AST, как в test_ricky_code.

import ast        # noqa: E402
import asyncio    # noqa: E402

import ai_office_shared.shared.dev_pipeline as dp          # noqa: E402
from ai_office_shared.shared.verify import extract_code    # noqa: E402

CODER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "agents", "coder.py")
WANT = ("extract_ricky_code", "ricky_failure_reason", "dev_failure_reason",
        "file_shrink_guard", "record_dev_gate_evidence", "run_dev_chain")

DEV_ACCEPTANCE = ["компилируется", "без NEEDS_FIX", "не схлопнулся"]


def run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


class BoardStub:
    VERIFIER_GATE = "гейт"

    def __init__(self):
        self.attempts = 0
        self.statuses = []
        self.evidence = []

    async def incr_attempts(self, *a, **k):
        self.attempts += 1
        return self.attempts

    async def update_status(self, redis_client, task_id, status, **k):
        self.statuses.append(status)
        return True

    async def add_evidence(self, redis_client, task_id, criterion, *, passed,
                           proof="", checked_by=""):
        self.evidence.append((criterion, passed))
        return True, ""


class _Logger:
    def warning(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass


def load_chain(board):
    with open(CODER, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    picked = [n for n in tree.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name in WANT]
    missing = [w for w in WANT if w not in {n.name for n in picked}]
    if missing:
        raise AssertionError(f"не найдены функции в coder.py: {missing}")
    ns = {"os": os, "logger": _Logger(), "tb": board, "extract_code": extract_code,
          "DEV_ACCEPTANCE": DEV_ACCEPTANCE, "MAX_DEV_ATTEMPTS": 3,
          "SHRINK_GUARD_RATIO": 0.7, "SHRINK_GUARD_MIN_LINES": 50}
    exec(compile(ast.Module(body=picked, type_ignores=[]), CODER, "exec"), ns)
    return ns


class TestChainStopsOnProviderFailure(unittest.TestCase):
    def setUp(self):
        self.board = BoardStub()
        self.ns = load_chain(self.board)
        self.calls = 0
        self._real = dp.run_dev_pipeline

    def tearDown(self):
        dp.run_dev_pipeline = self._real

    def _arm(self, devvy_answer):
        async def fake(task, **kw):
            self.calls += 1
            return {"devvy": devvy_answer, "final_code_artifact": "", "ricky": ""}
        dp.run_dev_pipeline = fake

    def _run(self):
        return run(self.ns["run_dev_chain"](
            "сделай фичу", repo="billy-bot", file_path="bot.py",
            context="x = 1\n",
            board_id="b1", task_id="b1", redis_client=object()))

    def test_provider_failure_stops_after_the_first_attempt(self):
        # Было: 3 попытки подряд подтверждали одну и ту же 400-ку.
        self._arm(REAL_ERROR)
        out = self._run()
        self.assertEqual(self.calls, 1)
        self.assertEqual(out["attempts"], 1)

    def test_provider_failure_does_not_burn_a_round(self):
        # Попытка, которой не дали случиться, не попытка: иначе заявка сгорала бы
        # по потолку раундов из-за чужого счёта.
        self._arm(REAL_ERROR)
        self._run()
        self.assertEqual(self.board.attempts, 0)

    def test_the_reason_says_it_is_not_the_code(self):
        self._arm(REAL_ERROR)
        out = self._run()
        self.assertTrue(out["infra_error"])
        self.assertIn("не код", out["reason"])
        self.assertFalse(out["ok"])

    def test_an_ordinary_failure_still_uses_the_whole_budget(self):
        # Обычный провал качества обязан ретраиться как раньше.
        self._arm("тут нет блока кода")
        out = self._run()
        self.assertEqual(self.calls, 3)
        self.assertEqual(self.board.attempts, 3)
        self.assertEqual(out["infra_error"], "")

    def test_evidence_is_still_written_on_a_provider_failure(self):
        # Доска обязана показать, что проверок не было, а не молчать.
        self._arm(REAL_ERROR)
        self._run()
        self.assertEqual(len(self.board.evidence), 3)
        self.assertTrue(all(passed is False for _, passed in self.board.evidence))


if __name__ == "__main__":
    unittest.main()

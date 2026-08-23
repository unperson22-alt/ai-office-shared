"""
Отдел не занимают находкой без улики.

23.08.2026 в офис-группу пришло «⛔ баг в villy-bot — команда за 3 попыток не
дала код». Симптом, который аудит вписал сам: «Missing traceback content in
logs, BUT code analysis reveals critical issue: await log(event, msg) called
with only 2 args».

Проверка исходника: функция объявлена с двумя параметрами, все четыре вызова
передают два, файл компилируется, pyflakes чист. Логи сервиса — двенадцать
строк «getUpdates 200 OK» подряд и ни одного признака отказа. Бот был здоров.

Аудит не увидел ошибку — он её вообразил, прочитав исходник, и отдал выдумку
цепочке отдела на три попытки. Единственным гейтом было поле is_bug: суждение
той же модели, которой показали весь исходник.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared.fault_evidence import (  # noqa: E402
    admits_no_evidence, dispatch_refusal, failure_evidence, looks_like_arity_claim,
)

# Дословно то, что аудит написал про villy-bot 23.08.
REAL_CLAIM = ("Missing traceback content in logs, but code analysis reveals critical "
              "issue: `await log(event: str, msg: str)` function called with only 2 "
              "args in `handle_task(`")
HEALTHY_LOGS = ['INFO:httpx:HTTP Request: POST .../getUpdates "HTTP/1.1 200 OK"'] * 12
CRASH_LOGS = [
    "Traceback (most recent call last):",
    '  File "bot.py", line 12, in main',
    "NameError: name 'random' is not defined",
]


class TestTheIncidentItself(unittest.TestCase):
    def test_the_villy_bot_finding_is_refused(self):
        why = dispatch_refusal({"is_bug": True, "description": REAL_CLAIM}, HEALTHY_LOGS)
        self.assertTrue(why, "находка без улики обязана быть отклонена")

    def test_the_analyzer_confessing_is_taken_at_its_word(self):
        # Модель сама написала «missing traceback» — это сообщение о собственном
        # основании, и верить ему дешевле, чем спорить.
        self.assertEqual(admits_no_evidence({"description": REAL_CLAIM}), "missing traceback")

    def test_healthy_logs_carry_no_evidence(self):
        self.assertEqual(failure_evidence(HEALTHY_LOGS), "")

    def test_an_arity_claim_is_recognisable_as_compiler_checkable(self):
        self.assertTrue(looks_like_arity_claim(REAL_CLAIM))


class TestRealFailuresStillGoThrough(unittest.TestCase):
    """Гейт обязан молчать там, где отказ настоящий, иначе он ломает автофикс."""

    def test_a_traceback_is_evidence(self):
        self.assertIn("Traceback", failure_evidence(CRASH_LOGS))

    def test_a_real_bug_is_not_refused(self):
        self.assertEqual(
            dispatch_refusal({"is_bug": True, "description": "NameError на старте"}, CRASH_LOGS), "")

    def test_various_real_failures_are_recognised(self):
        for line in ("CRITICAL: worker died",
                     "container exited with code 137",
                     "ModuleNotFoundError: No module named 'httpx'",
                     "OOMKilled",
                     "SyntaxError: invalid syntax"):
            self.assertTrue(failure_evidence([line]), line)

    def test_evidence_is_the_observed_line_not_a_flag(self):
        # По улике должно быть видно, ЧТО именно посчитали отказом.
        self.assertEqual(failure_evidence(["ok", "CRITICAL: disk full", "ok"]),
                         "CRITICAL: disk full")


class TestGateOnlyActsWhenTheModelWantsToAct(unittest.TestCase):
    def test_no_bug_claimed_means_nothing_to_refuse(self):
        self.assertEqual(dispatch_refusal({"is_bug": False}, HEALTHY_LOGS), "")

    def test_garbage_analysis_does_not_crash_the_monitor(self):
        for bad in (None, "", [], {"is_bug": True}):
            dispatch_refusal(bad, HEALTHY_LOGS)

    def test_missing_logs_with_a_bug_claim_are_refused(self):
        self.assertTrue(dispatch_refusal({"is_bug": True, "description": "что-то не так"}, []))


class TestBothDispatchPathsAreGated(unittest.TestCase):
    """Гейт против отката: обе точки, зовущие отдел, обязаны его спрашивать."""

    def test_monitor_and_sweep_both_call_the_refusal(self):
        coder = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "agents", "coder.py")
        with open(coder, encoding="utf-8") as f:
            src = f.read()
        self.assertEqual(src.count("dispatch_refusal("), 2,
                         "обе точки вызова handle_bug должны стоять за гейтом")


if __name__ == "__main__":
    unittest.main()

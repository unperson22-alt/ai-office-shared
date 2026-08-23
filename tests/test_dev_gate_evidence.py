"""
Улики гейта: три галочки за пустоту — инцидент 2026-08-23.

Первая же заявка, прошедшая через новую входящую очередь (62ffa25b5e30,
ретушь от Крисса), упала — и прислала владельцу такую таблицу:

    | финальный код компилируется   | ✅ | compile() ок, 0 строк            |
    | ревью Рикки без NEEDS_FIX     | ✅ | NEEDS_FIX в вердикте не встречается |
    | файл не схлопнулся            | ✅ | размер файла в пределах гейта    |

Кода не было вообще. Механика: compile_ok инициализируется True и сбрасывается
только внутри `if final_code`, пустая строка не содержит NEEDS_FIX, а гейт
размера при пустом коде не вызывается вовсе. Ноль строк проходил все три.

Выстрелить не успело: отдельная проверка `chain["ok"]` требует непустой код, и
задача ушла в blocked. Но таблица улик — тот самый артефакт, который должен
говорить правду о проверках, и она отчиталась за непроверенное. Проверка,
которой не на чем было исполниться, — провал, а не пропуск (урок #93).

Второй дефект того же прогона — диагноз. «Рикки не ответил вообще» пришло при
пяти живых воркерах: при провале Девви пайплайн обрывает фан-аут и ключа
`ricky` в ответе не оставляет, а вызывающий звал `ricky_failure_reason("")`,
которая по пустой строке всегда винит Рикки. Отчёт называл воркера, которого
в прогоне не вызывали (урок #100: не выдумывай причину провала).

coder.py не импортируется — функции достаются из AST, как в test_ricky_code.
"""
import ast
import asyncio
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CODER = os.path.join(ROOT, "agents", "coder.py")
WANT = ("ricky_failure_reason", "dev_failure_reason", "record_dev_gate_evidence")

DEV_ACCEPTANCE = [
    "финальный код компилируется",
    "ревью Рикки без вердикта NEEDS_FIX",
    "файл не схлопнулся (гейт размера)",
]


class _Recorder:
    """Подменяет taskboard: запоминает, что именно записалось уликой."""

    VERIFIER_GATE = "гейт"

    def __init__(self):
        self.calls = []

    async def add_evidence(self, redis_client, task_id, criterion, *,
                           passed, proof="", checked_by=""):
        self.calls.append({"criterion": criterion, "passed": passed,
                           "proof": proof, "checked_by": checked_by})
        return True, ""


class _Logger:
    def warning(self, *a, **k):
        pass


def load():
    with open(CODER, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    picked = [n for n in tree.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name in WANT]
    missing = [w for w in WANT if w not in {n.name for n in picked}]
    if missing:
        raise AssertionError(f"не найдены функции в coder.py: {missing}")
    rec = _Recorder()
    ns = {"tb": rec, "DEV_ACCEPTANCE": DEV_ACCEPTANCE, "logger": _Logger()}
    exec(compile(ast.Module(body=picked, type_ignores=[]), CODER, "exec"), ns)
    return ns, rec


def run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


class TestEvidenceWithoutCode(unittest.TestCase):
    def setUp(self):
        self.ns, self.rec = load()

    def _record(self, **kw):
        base = dict(final_code="", compile_ok=True, se_info="",
                    review_ok=True, ricky_result="", shrink_info="")
        base.update(kw)
        run(self.ns["record_dev_gate_evidence"](object(), "task1", **base))
        return {c["criterion"]: c for c in self.rec.calls}

    def test_no_code_fails_every_criterion(self):
        # Ровно та таблица, что пришла владельцу 23.08 — теперь без галочек.
        got = self._record()
        self.assertEqual(len(got), 3)
        for criterion in DEV_ACCEPTANCE:
            self.assertFalse(got[criterion]["passed"],
                             f"{criterion} отчитался «пройдено» без кода")

    def test_no_code_says_there_was_nothing_to_check(self):
        got = self._record()
        for criterion in DEV_ACCEPTANCE:
            self.assertIn("проверять было нечего", got[criterion]["proof"],
                          criterion)

    def test_no_code_never_claims_zero_lines_compiled(self):
        # «compile() ок, 0 строк» — формулировка, которая и ввела в заблуждение.
        got = self._record()
        self.assertNotIn("0 строк", got[DEV_ACCEPTANCE[0]]["proof"])

    def test_real_code_still_passes(self):
        got = self._record(final_code="x = 1\ny = 2\n")
        for criterion in DEV_ACCEPTANCE:
            self.assertTrue(got[criterion]["passed"], criterion)
        self.assertIn("2 строк", got[DEV_ACCEPTANCE[0]]["proof"])

    def test_real_failures_are_still_reported_as_failures(self):
        got = self._record(final_code="def(", compile_ok=False,
                           se_info="invalid syntax, строка 1")
        self.assertFalse(got[DEV_ACCEPTANCE[0]]["passed"])
        self.assertIn("invalid syntax", got[DEV_ACCEPTANCE[0]]["proof"])

        self.rec.calls.clear()
        got = self._record(final_code="x = 1", shrink_info="файл схлопнулся: 900 → 5")
        self.assertFalse(got[DEV_ACCEPTANCE[2]]["passed"])
        self.assertIn("схлопнулся", got[DEV_ACCEPTANCE[2]]["proof"])

    def test_evidence_is_signed_by_the_gate(self):
        self._record()
        for call in self.rec.calls:
            self.assertEqual(call["checked_by"], "гейт")


class TestFailureNamesTheRightWorker(unittest.TestCase):
    def setUp(self):
        self.ns, _ = load()

    def why(self, pipe):
        return self.ns["dev_failure_reason"](pipe)

    def test_devvy_refusal_is_quoted_and_ricky_is_not_blamed(self):
        # Ровно случай 23.08: фан-аут не запускался, ключа ricky в ответе нет.
        why = self.why({"devvy": "ERROR: девви не смог получить проходящий код"})
        self.assertIn("Девви", why)
        self.assertIn("не смог получить проходящий код", why)
        self.assertNotIn("Рикки не ответил", why)

    def test_empty_devvy_names_devvy(self):
        why = self.why({"devvy": ""})
        self.assertIn("Девви", why)
        self.assertNotIn("Рикки не ответил", why)

    def test_ricky_is_blamed_only_when_devvy_actually_answered(self):
        why = self.why({"devvy": "```python\nx = 1\n```", "ricky": ""})
        self.assertIn("Рикки", why)

    def test_ricky_refusal_is_quoted_verbatim(self):
        why = self.why({"devvy": "код", "ricky": "ERROR: unterminated string, строка 12"})
        self.assertIn("unterminated string", why)

    def test_answer_without_a_code_block_asks_for_the_block(self):
        why = self.why({"devvy": "код", "ricky": "Замечаний нет, всё хорошо."})
        self.assertIn("```python", why)

    def test_missing_pipe_does_not_crash(self):
        self.assertIn("Девви", self.why({}))


if __name__ == "__main__":
    unittest.main()

"""
Фаза 2 оркестрации: исполнитель ≠ проверяющий, детерминированные гейты,
потолок раундов, отчёт таблицей улик.

Проверяем ровно те дыры, ради которых Фаза 2 написана:
  • исполнитель не может сам подписать свой успех;
  • признание провала при этом остаётся свободным;
  • гейт меряет наблюдаемое, а неисполнимый гейт — это провал, не пропуск;
  • петля «доработай» упирается в потолок и уходит к человеку;
  • отчёт показывает, кто и чем закрыл каждый критерий.
"""
import unittest

from ai_office_shared.shared import gates
from ai_office_shared.shared import models
from ai_office_shared.shared import taskboard as tb

from test_taskboard_acceptance import FakeRedis, run


class TestExecutorIsNotVerifier(unittest.TestCase):
    def setUp(self):
        self.r = FakeRedis()

    def _task(self, **kw):
        return run(tb.create_task(self.r, "задача", assignee="билли",
                                  acceptance=["A"], **kw))

    def test_executor_cannot_sign_off_own_success(self):
        tid = self._task()
        ok, why = run(tb.add_evidence(self.r, tid, "A", passed=True,
                                      proof="я проверил", checked_by="билли"))
        self.assertFalse(ok)
        self.assertIn("исполнитель", why)
        self.assertEqual(run(tb.get_task(self.r, tid))["evidence"], [])

    def test_anonymous_success_is_refused(self):
        tid = self._task()
        ok, why = run(tb.add_evidence(self.r, tid, "A", passed=True, proof="ok"))
        self.assertFalse(ok)
        self.assertIn("проверяющий", why)

    def test_third_party_success_is_accepted(self):
        tid = self._task()
        ok, why = run(tb.add_evidence(self.r, tid, "A", passed=True,
                                      proof="HTTP 200", checked_by="тести"))
        self.assertTrue(ok, why)

    def test_gate_may_sign_off_even_on_own_assignee_task(self):
        # У гейта нет интереса в исходе — правило на него не распространяется.
        tid = self._task()
        ok, why = run(tb.add_evidence(self.r, tid, "A", passed=True,
                                      proof="GET /health → 200",
                                      checked_by=tb.VERIFIER_GATE))
        self.assertTrue(ok, why)

    def test_executor_may_report_own_failure(self):
        # Признание провала не выгодно признающему — барьер здесь только
        # заставил бы молчать о поломке.
        tid = self._task()
        ok, why = run(tb.add_evidence(self.r, tid, "A", passed=False,
                                      proof="упало", checked_by="билли"))
        self.assertTrue(ok, why)

    def test_anonymous_failure_is_accepted(self):
        tid = self._task()
        ok, _ = run(tb.add_evidence(self.r, tid, "A", passed=False, proof="упало"))
        self.assertTrue(ok)

    def test_self_signed_success_cannot_unlock_done(self):
        # Сквозная проверка: обход через самоподпись не открывает done.
        tid = self._task()
        run(tb.add_evidence(self.r, tid, "A", passed=True, checked_by="билли"))
        self.assertFalse(run(tb.update_status(self.r, tid, "done")))


class TestGates(unittest.TestCase):
    def setUp(self):
        self.r = FakeRedis()

    def test_compile_gate_passes_on_valid_code(self):
        ok, proof = run(gates.run_gate({"kind": "python_compile", "code": "x = 1\n"}))
        self.assertTrue(ok)
        self.assertIn("compile()", proof)

    def test_compile_gate_catches_truncated_file(self):
        # Ровно инцидент «воркер обрезал код».
        ok, proof = run(gates.run_gate({"kind": "python_compile",
                                        "code": "def f(:\n"}))
        self.assertFalse(ok)
        self.assertIn("SyntaxError", proof)

    def test_unknown_gate_kind_fails_not_passes(self):
        ok, proof = run(gates.run_gate({"kind": "магия"}))
        self.assertFalse(ok)
        self.assertIn("неизвестный тип", proof)

    def test_redis_gate_reports_missing_key(self):
        ok, proof = run(gates.run_gate({"kind": "redis_key", "key": "office:нет"},
                                       redis_client=self.r))
        self.assertFalse(ok)
        self.assertIn("отсутствует", proof)

    def test_redis_gate_confirms_written_data(self):
        # Гейт «данные реально записались», а не «бот сказал, что записал».
        run(self.r.set("office:health:БИЛЛИ", "ok"))
        passed, proof = run(gates.run_gate(
            {"kind": "redis_key", "key": "office:health:БИЛЛИ", "contains": "ok"},
            redis_client=self.r))
        self.assertTrue(passed, proof)
        self.assertIn("ok", proof)

    def test_redis_gate_fails_when_value_differs(self):
        run(self.r.set("office:health:БИЛЛИ", "down"))
        passed, proof = run(gates.run_gate(
            {"kind": "redis_key", "key": "office:health:БИЛЛИ", "contains": "ok"},
            redis_client=self.r))
        self.assertFalse(passed)
        self.assertIn("down", proof)

    def test_redis_gate_without_client_fails(self):
        # Гейт, который не смог проверить, ничего не подтвердил.
        ok, proof = run(gates.run_gate({"kind": "redis_key", "key": "k"}))
        self.assertFalse(ok)
        self.assertIn("нет redis", proof)

    def test_text_absent_gate_catches_leftover(self):
        ok, proof = run(gates.run_gate({"kind": "text_absent",
                                        "text": "ALLOWED_USERS=1,675773302",
                                        "needle": "675773302"}))
        self.assertFalse(ok)
        self.assertIn("ВСТРЕЧАЕТСЯ", proof)

    def test_text_absent_gate_passes_when_clean(self):
        ok, _ = run(gates.run_gate({"kind": "text_absent",
                                    "text": "ALLOWED_USERS=1,2",
                                    "needle": "675773302"}))
        self.assertTrue(ok)

    def test_text_contains_gate(self):
        ok, _ = run(gates.run_gate({"kind": "text_contains",
                                    "text": "142 passed", "needle": "passed"}))
        self.assertTrue(ok)

    def test_criterion_helpers_accept_both_formats(self):
        self.assertEqual(gates.criterion_text("строка"), "строка")
        self.assertIsNone(gates.criterion_gate("строка"))
        item = {"text": "код компилируется",
                "gate": {"kind": "python_compile", "code": "x=1"}}
        self.assertEqual(gates.criterion_text(item), "код компилируется")
        self.assertEqual(gates.criterion_gate(item)["kind"], "python_compile")

    def test_malformed_gate_spec_is_ignored_not_trusted(self):
        self.assertIsNone(gates.criterion_gate({"text": "x", "gate": "не словарь"}))
        self.assertIsNone(gates.criterion_gate({"text": "x", "gate": {"kind": "ftp"}}))


class TestVerifyTask(unittest.TestCase):
    def setUp(self):
        self.r = FakeRedis()

    def test_gated_criteria_are_closed_automatically(self):
        tid = run(tb.create_task(self.r, "задача", assignee="девви", acceptance=[
            {"text": "код компилируется",
             "gate": {"kind": "python_compile", "code": "x = 1\n"}},
        ]))
        passed, failed, records = run(tb.verify_task(self.r, tid))
        self.assertEqual((passed, failed), (1, 0))
        self.assertEqual(len(records), 1)
        ev = run(tb.get_task(self.r, tid))["evidence"][0]
        self.assertEqual(ev["checked_by"], tb.VERIFIER_GATE)
        self.assertTrue(ev["passed"])

    def test_verified_task_can_reach_done(self):
        tid = run(tb.create_task(self.r, "задача", assignee="девви", acceptance=[
            {"text": "компилируется", "gate": {"kind": "python_compile", "code": "x=1"}},
        ]))
        run(tb.verify_task(self.r, tid))
        self.assertTrue(run(tb.update_status(self.r, tid, "done")))

    def test_failing_gate_blocks_done(self):
        tid = run(tb.create_task(self.r, "задача", assignee="девви", acceptance=[
            {"text": "компилируется", "gate": {"kind": "python_compile", "code": "def f(:"}},
        ]))
        passed, failed, _ = run(tb.verify_task(self.r, tid))
        self.assertEqual((passed, failed), (0, 1))
        self.assertFalse(run(tb.update_status(self.r, tid, "done")))

    def test_ungated_criteria_are_left_to_a_human(self):
        tid = run(tb.create_task(self.r, "задача", assignee="девви", acceptance=[
            "ответ звучит по-человечески",
            {"text": "компилируется", "gate": {"kind": "python_compile", "code": "x=1"}},
        ]))
        passed, failed, records = run(tb.verify_task(self.r, tid))
        self.assertEqual((passed, failed, len(records)), (1, 0, 1))
        _, missing, _ = tb.acceptance_verdict(run(tb.get_task(self.r, tid)))
        self.assertEqual(missing, ["ответ звучит по-человечески"])

    def test_verdict_reads_text_of_dict_criteria(self):
        # Критерий-словарь не должен считаться «непокрытым» из-за формата.
        task = {"acceptance": [{"text": "A", "gate": {"kind": "python_compile"}}],
                "evidence": [{"criterion": "A", "passed": True}]}
        self.assertEqual(tb.acceptance_verdict(task), (True, [], []))


class TestRoundCap(unittest.TestCase):
    def setUp(self):
        self.r = FakeRedis()

    def test_rounds_left_counts_down(self):
        self.assertEqual(tb.rounds_left({"attempts": 0}), tb.MAX_ROUNDS)
        self.assertEqual(tb.rounds_left({"attempts": tb.MAX_ROUNDS}), 0)
        self.assertEqual(tb.rounds_left({"attempts": 99}), 0)

    def test_escalates_when_cap_reached(self):
        self.assertTrue(tb.should_escalate({"attempts": tb.MAX_ROUNDS,
                                            "status": "needs_fix"}))

    def test_does_not_escalate_while_rounds_remain(self):
        self.assertFalse(tb.should_escalate({"attempts": 1, "status": "needs_fix"}))

    def test_does_not_escalate_finished_or_already_escalated(self):
        self.assertFalse(tb.should_escalate({"attempts": 9, "status": "done"}))
        self.assertFalse(tb.should_escalate({"attempts": 9, "status": "needs_fix",
                                             "escalated": True}))

    def test_escalate_blocks_task_and_explains_why(self):
        tid = run(tb.create_task(self.r, "задача"))
        for _ in range(tb.MAX_ROUNDS):
            run(tb.incr_attempts(self.r, tid))
        task = run(tb.get_task(self.r, tid))
        self.assertTrue(tb.should_escalate(task))
        run(tb.escalate(self.r, tid))
        task = run(tb.get_task(self.r, tid))
        self.assertEqual(task["status"], "blocked")
        self.assertTrue(task["escalated"])
        self.assertIn("потолок", task["result"])
        # Повторно эскалировать уже нечего — петля закрыта.
        self.assertFalse(tb.should_escalate(task))


class TestStageModels(unittest.TestCase):
    def test_heavy_model_only_on_plan_and_review(self):
        self.assertEqual(models.model_for_stage(models.STAGE_PLAN), models.MODEL_OPUS)
        self.assertEqual(models.model_for_stage(models.STAGE_REVIEW), models.MODEL_OPUS)
        self.assertEqual(models.model_for_stage(models.STAGE_EXECUTE), models.MODEL_SONNET)
        self.assertEqual(models.model_for_stage(models.STAGE_ROUTE), models.MODEL_HAIKU)

    def test_unknown_stage_falls_back_to_middle_not_to_the_priciest(self):
        self.assertEqual(models.model_for_stage("непонятно"), models.MODEL_SONNET)


class TestEvidenceReport(unittest.TestCase):
    def test_report_shows_proof_and_checker_per_criterion(self):
        task = {
            "id": "abc", "title": "починить Крисса",
            "acceptance": ["A", "B", "C"],
            "evidence": [
                {"criterion": "A", "passed": True, "proof": "GET /health → 200",
                 "checked_by": "гейт"},
                {"criterion": "B", "passed": False, "proof": "SyntaxError",
                 "checked_by": "тести"},
            ],
        }
        out = tb.format_evidence_report(task)
        self.assertIn("GET /health → 200", out)
        self.assertIn("гейт", out)
        self.assertIn("❌", out)
        self.assertIn("⬜ не проверен", out)   # C без улики
        self.assertIn("НЕ пройдена", out)

    def test_report_says_pass_only_when_everything_proven(self):
        task = {
            "id": "abc", "title": "t", "acceptance": ["A"],
            "evidence": [{"criterion": "A", "passed": True, "proof": "ok",
                          "checked_by": "гейт"}],
        }
        self.assertIn("приёмка пройдена", tb.format_evidence_report(task))

    def test_report_is_explicit_when_there_were_no_criteria(self):
        # Молчаливое «ок» на задаче без критериев — то же самое непроверяемое
        # утверждение, поэтому отчёт говорит об этом прямо.
        out = tb.format_evidence_report({"id": "x", "title": "t"})
        self.assertIn("проверить нечем", out)


if __name__ == "__main__":
    unittest.main()

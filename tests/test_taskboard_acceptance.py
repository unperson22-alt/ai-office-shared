"""
Приёмка на доске задач: критерии замораживаются ДО работы, улики — ПОСЛЕ.

Зачем: пока «выполнено» — это утверждение исполнителя, оно всегда истинно.
12.08 Милли считала задачу решённой, хотя её реплика молча выбрасывалась;
«баг починен» держалось ровно до момента, когда кто-то записал, что значит
«починен». Критерии, написанные после результата, подгоняются под результат.

Тесты идут на фейковом Redis — доска это HASH + ZSET, поведение важнее сети.
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared import taskboard as tb  # noqa: E402


def run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


class FakeRedis:
    """Минимальный Redis: HASH + ZSET, ровно то, что использует доска."""

    def __init__(self):
        self.h: dict = {}
        self.z: dict = {}
        self.s: dict = {}   # обычные строковые ключи — для гейта redis_key

    async def set(self, key, value):
        self.s[key] = value

    async def get(self, key):
        return self.s.get(key)

    async def exists(self, key):
        return 1 if (key in self.s or key in self.h) else 0

    async def hset(self, key, mapping=None, **kw):
        self.h.setdefault(key, {}).update(mapping or {})

    async def hgetall(self, key):
        return dict(self.h.get(key, {}))

    async def hincrby(self, key, field, amount=1):
        cur = int(self.h.setdefault(key, {}).get(field, 0) or 0) + amount
        self.h[key][field] = str(cur)
        return cur

    async def zadd(self, key, mapping):
        self.z.setdefault(key, {}).update(mapping)

    async def zremrangebyrank(self, *a, **k):
        pass

    async def expire(self, *a, **k):
        pass

    def pipeline(self, transaction=False):
        outer = self

        class _Pipe:
            def __init__(self):
                self.ops = []
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            def hset(self, key, mapping=None, **kw):
                self.ops.append(("hset", key, mapping))
            def zadd(self, key, mapping):
                self.ops.append(("zadd", key, mapping))
            def zremrangebyrank(self, *a):
                self.ops.append(("noop",))
            def expire(self, *a):
                self.ops.append(("noop",))
            async def execute(self):
                for op in self.ops:
                    if op[0] == "hset":
                        await outer.hset(op[1], mapping=op[2])
                    elif op[0] == "zadd":
                        await outer.zadd(op[1], op[2])
                self.ops = []
        return _Pipe()


class TestFrozenCriteria(unittest.TestCase):
    def setUp(self):
        self.r = FakeRedis()

    def test_criteria_set_at_creation_are_readable(self):
        tid = run(tb.create_task(self.r, "починить роутинг",
                                 acceptance=["реплай уходит в ГОСЛИНГ", "тесты зелёные"]))
        task = run(tb.get_task(self.r, tid))
        self.assertEqual(task["acceptance"],
                         ["реплай уходит в ГОСЛИНГ", "тесты зелёные"])

    def test_cannot_rewrite_criteria_once_set(self):
        tid = run(tb.create_task(self.r, "задача", acceptance=["A"]))
        ok, why = run(tb.set_acceptance(self.r, tid, ["что-нибудь попроще"]))
        self.assertFalse(ok)
        self.assertIn("заморожены", why)

    def test_cannot_set_criteria_after_work_started(self):
        tid = run(tb.create_task(self.r, "задача"))
        run(tb.update_status(self.r, tid, "in_progress"))
        ok, why = run(tb.set_acceptance(self.r, tid, ["А давайте так"]))
        self.assertFalse(ok)
        self.assertIn("работа уже началась", why)

    def test_can_set_criteria_before_work(self):
        tid = run(tb.create_task(self.r, "задача"))
        ok, why = run(tb.set_acceptance(self.r, tid, ["критерий"]))
        self.assertTrue(ok, why)
        self.assertEqual(run(tb.get_task(self.r, tid))["acceptance"], ["критерий"])


class TestDoneRequiresEvidence(unittest.TestCase):
    def setUp(self):
        self.r = FakeRedis()

    def test_done_refused_while_criteria_uncovered(self):
        tid = run(tb.create_task(self.r, "задача", acceptance=["A", "B"]))
        self.assertFalse(run(tb.update_status(self.r, tid, "done")))
        self.assertNotEqual(run(tb.get_task(self.r, tid))["status"], "done")

    def test_done_refused_when_a_criterion_failed(self):
        tid = run(tb.create_task(self.r, "задача", acceptance=["A"]))
        run(tb.add_evidence(self.r, tid, "A", passed=False, proof="exit 1"))
        self.assertFalse(run(tb.update_status(self.r, tid, "done")))

    def test_done_allowed_when_every_criterion_proven(self):
        # Проверяющий назван и это не исполнитель — только такая улика считается.
        tid = run(tb.create_task(self.r, "задача", assignee="билли",
                                 acceptance=["A", "B"]))
        run(tb.add_evidence(self.r, tid, "A", passed=True,
                            proof="pytest: 142 passed", checked_by="тести"))
        run(tb.add_evidence(self.r, tid, "B", passed=True,
                            proof="HTTP 200", checked_by=tb.VERIFIER_GATE))
        self.assertTrue(run(tb.update_status(self.r, tid, "done")))
        self.assertEqual(run(tb.get_task(self.r, tid))["status"], "done")

    def test_task_without_criteria_behaves_as_before(self):
        # Обратная совместимость: существующие вызовы не должны сломаться.
        tid = run(tb.create_task(self.r, "старая задача"))
        self.assertTrue(run(tb.update_status(self.r, tid, "done")))

    def test_other_statuses_are_not_gated(self):
        tid = run(tb.create_task(self.r, "задача", acceptance=["A"]))
        for st in ("in_progress", "needs_fix", "blocked"):
            self.assertTrue(run(tb.update_status(self.r, tid, st)), st)

    def test_evidence_for_one_criterion_is_replaced_not_duplicated(self):
        tid = run(tb.create_task(self.r, "задача", acceptance=["A"]))
        run(tb.add_evidence(self.r, tid, "A", passed=False, proof="сначала упало"))
        run(tb.add_evidence(self.r, tid, "A", passed=True,
                            proof="после фикса ок", checked_by="тести"))
        ev = run(tb.get_task(self.r, tid))["evidence"]
        self.assertEqual(len(ev), 1)
        self.assertTrue(ev[0]["passed"])

    def test_evidence_records_who_checked(self):
        # Известный бот приводится к canonical...
        tid = run(tb.create_task(self.r, "задача", acceptance=["A"]))
        run(tb.add_evidence(self.r, tid, "A", passed=True, proof="ok", checked_by="Крисс"))
        self.assertEqual(run(tb.get_task(self.r, tid))["evidence"][0]["checked_by"], "крисс")

    def test_unknown_checker_name_is_kept_verbatim(self):
        # ...а имя вне реестра ботов (Тести из dev-dept, отдел, человек)
        # сохраняется как есть — иначе проверяющий потерялся бы.
        tid = run(tb.create_task(self.r, "задача", acceptance=["A"]))
        run(tb.add_evidence(self.r, tid, "A", passed=True, proof="ok", checked_by="Тести"))
        self.assertEqual(run(tb.get_task(self.r, tid))["evidence"][0]["checked_by"], "Тести")


class TestVerdictIsPure(unittest.TestCase):
    def test_no_criteria_means_pass(self):
        self.assertEqual(tb.acceptance_verdict({}), (True, [], []))

    def test_reports_missing_and_failed_separately(self):
        task = {
            "acceptance": ["A", "B", "C"],
            "evidence": [
                {"criterion": "A", "passed": True},
                {"criterion": "B", "passed": False},
            ],
        }
        ok, missing, failed = tb.acceptance_verdict(task)
        self.assertFalse(ok)
        self.assertEqual(missing, ["C"])
        self.assertEqual(failed, ["B"])

    def test_broken_json_does_not_crash_the_board(self):
        self.assertEqual(tb._load_list("не json"), [])
        self.assertEqual(tb._load_list('{"не": "список"}'), [])
        self.assertEqual(tb._load_list(None), [])


if __name__ == "__main__":
    unittest.main()

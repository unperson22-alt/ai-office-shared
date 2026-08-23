"""
Входящая дверь отдела разработки: заявка на доске должна ДОЙТИ до работы.

До этого run_dev_pipeline звали только автофикс краша и интент dev_task по
явной просьбе человека, а management_tick смотрел на уже взятое в работу.
Заявка от бота ложилась со status="open" и лежала — 62ffa25b5e30 и aa573bddb6d7
провисели так до тех пор, пока обе фичи не сделали руками.

Проверяем ровно те свойства, ради которых очередь написана:
  • одна заявка берётся ровно один раз, параллельных цепочек не бывает;
  • подзадача не самостоятельна и в очередь не попадает;
  • «Отложить» — это не «отклонить»: заявка возвращается;
  • потолок раундов упирается в человека, а не в новый круг;
  • критерии заморожены ДО работы, а done без улики по CI невозможен;
  • свой собственный код не правится автоматически ни при каких условиях;
  • отсутствие CI — это провал проверки, а не её пропуск.

Идут на фейковом Redis: важно поведение, а не сеть.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared import dev_queue as dq  # noqa: E402
from ai_office_shared.shared import taskboard as tb  # noqa: E402
from shared.github_tools import parse_check_runs      # noqa: E402

from test_taskboard_acceptance import FakeRedis, run   # noqa: E402


class QueueRedis(FakeRedis):
    """FakeRedis + SET NX/EX и DELETE — ровно то, чем очередь берёт замок."""

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.s:
            return None
        self.s[key] = value
        return True

    async def delete(self, *keys):
        for k in keys:
            self.s.pop(k, None)
        return len(keys)


def make_task(tid, *, status="open", assignee=dq.DEV_DEPT_ASSIGNEE,
              parent_id="", created_at="2026-08-21T10:00:00Z", title="заявка"):
    return {"id": tid, "status": status, "assignee": assignee,
            "parent_id": parent_id, "created_at": created_at, "title": title}


class TestPickNext(unittest.TestCase):
    def test_oldest_open_dev_dept_task_wins(self):
        tasks = [make_task("new", created_at="2026-08-22T10:00:00Z"),
                 make_task("old", created_at="2026-08-20T10:00:00Z")]
        self.assertEqual(dq.pick_next(tasks)["id"], "old")

    def test_subtask_is_ignored(self):
        # Подзадачу ведёт родитель; взяв её отдельно, отдел писал бы код
        # по куску чужой задачи.
        tasks = [make_task("child", parent_id="parent")]
        self.assertIsNone(dq.pick_next(tasks))

    def test_other_assignee_and_other_status_are_ignored(self):
        tasks = [make_task("a", assignee="билли"),
                 make_task("b", status="in_progress")]
        self.assertIsNone(dq.pick_next(tasks))

    def test_blocked_ids_are_skipped(self):
        tasks = [make_task("x", created_at="2026-08-20T10:00:00Z"),
                 make_task("y", created_at="2026-08-21T10:00:00Z")]
        self.assertEqual(dq.pick_next(tasks, blocked={"x"})["id"], "y")

    def test_nothing_ready_is_none_not_error(self):
        self.assertIsNone(dq.pick_next([]))
        self.assertIsNone(dq.pick_next(None))


class TestClaim(unittest.TestCase):
    def setUp(self):
        self.r = QueueRedis()

    def test_task_is_claimed_exactly_once(self):
        self.assertTrue(run(dq.claim_task(self.r, "t1")))
        self.assertFalse(run(dq.claim_task(self.r, "t1")))

    def test_released_claim_can_be_taken_again(self):
        run(dq.claim_task(self.r, "t1"))
        run(dq.release_claim(self.r, "t1"))
        self.assertTrue(run(dq.claim_task(self.r, "t1")))

    def test_two_tasks_do_not_block_each_other(self):
        self.assertTrue(run(dq.claim_task(self.r, "t1")))
        self.assertTrue(run(dq.claim_task(self.r, "t2")))

    def test_without_redis_nothing_is_claimed(self):
        # Fail-closed: без замка две ветки напишут один файл разным кодом,
        # и победит порядок пушей, а не качество.
        self.assertFalse(run(dq.claim_task(None, "t1")))


class TestSnoozeAndAsked(unittest.TestCase):
    def setUp(self):
        self.r = QueueRedis()

    def test_snoozed_task_is_not_offered(self):
        run(dq.snooze(self.r, "t1"))
        tasks = [make_task("t1")]
        blocked = run(dq.blocked_ids(self.r, tasks))
        self.assertIn("t1", blocked)
        self.assertIsNone(dq.pick_next(tasks, blocked=blocked))

    def test_task_returns_after_snooze_expires(self):
        run(dq.snooze(self.r, "t1"))
        self.r.s.pop(dq.snooze_key("t1"))          # имитируем истечение TTL
        tasks = [make_task("t1")]
        blocked = run(dq.blocked_ids(self.r, tasks))
        self.assertEqual(dq.pick_next(tasks, blocked=blocked)["id"], "t1")

    def test_asked_task_is_not_asked_again(self):
        run(dq.mark_asked(self.r, "t1"))
        self.assertIn("t1", run(dq.blocked_ids(self.r, [make_task("t1")])))
        run(dq.clear_asked(self.r, "t1"))
        self.assertNotIn("t1", run(dq.blocked_ids(self.r, [make_task("t1")])))

    def test_without_redis_everything_is_blocked(self):
        self.assertEqual(run(dq.blocked_ids(None, [make_task("t1")])), {"t1"})


class TestRoundCeiling(unittest.TestCase):
    def setUp(self):
        self.r = QueueRedis()

    def test_exhausted_rounds_escalate_instead_of_looping(self):
        tid = run(tb.create_task(self.r, "заявка", assignee=dq.DEV_DEPT_ASSIGNEE))
        for _ in range(tb.MAX_ROUNDS):
            run(tb.incr_attempts(self.r, tid))
        task = run(tb.get_task(self.r, tid))
        self.assertTrue(tb.should_escalate(task))
        run(tb.escalate(self.r, tid, "потолок"))
        after = run(tb.get_task(self.r, tid))
        self.assertEqual(after["status"], "blocked")
        self.assertTrue(after["escalated"])
        self.assertFalse(tb.should_escalate(after))   # второй раз не эскалируем


class TestAcceptance(unittest.TestCase):
    def setUp(self):
        self.r = QueueRedis()

    def test_ci_criterion_is_part_of_acceptance(self):
        self.assertIn(dq.CI_ACCEPTANCE, dq.acceptance_for())
        self.assertEqual(len(dq.acceptance_for()), len(dq.GATE_ACCEPTANCE) + 1)

    def test_criteria_freeze_before_work_and_refuse_after(self):
        tid = run(tb.create_task(self.r, "заявка", assignee=dq.DEV_DEPT_ASSIGNEE))
        ok, why = run(tb.set_acceptance(self.r, tid, dq.acceptance_for()))
        self.assertTrue(ok, why)
        run(tb.update_status(self.r, tid, "in_progress"))
        ok2, why2 = run(tb.set_acceptance(self.r, tid, ["что попроще"]))
        self.assertFalse(ok2)
        self.assertIn("заморожены", why2)

    def test_done_is_refused_until_ci_evidence_exists(self):
        tid = run(tb.create_task(self.r, "заявка", assignee=dq.DEV_DEPT_ASSIGNEE,
                                 acceptance=dq.acceptance_for()))
        for criterion in dq.GATE_ACCEPTANCE:
            run(tb.add_evidence(self.r, tid, criterion, passed=True,
                                proof="наблюдённое значение", checked_by=tb.VERIFIER_GATE))
        self.assertFalse(run(tb.update_status(self.r, tid, "done")))
        run(tb.add_evidence(self.r, tid, dq.CI_ACCEPTANCE, passed=True,
                            proof="CI зелёный: https://github.com/…", checked_by=tb.VERIFIER_GATE))
        self.assertTrue(run(tb.update_status(self.r, tid, "done")))

    def test_red_ci_evidence_keeps_task_open(self):
        tid = run(tb.create_task(self.r, "заявка", assignee=dq.DEV_DEPT_ASSIGNEE,
                                 acceptance=dq.acceptance_for()))
        for criterion in dq.GATE_ACCEPTANCE:
            run(tb.add_evidence(self.r, tid, criterion, passed=True,
                                proof="ок", checked_by=tb.VERIFIER_GATE))
        run(tb.add_evidence(self.r, tid, dq.CI_ACCEPTANCE, passed=False,
                            proof="упали джобы: tests", checked_by=tb.VERIFIER_GATE))
        self.assertFalse(run(tb.update_status(self.r, tid, "done")))


class TestTaskWithoutCriteriaIsNotSilentlyClosable(unittest.TestCase):
    """Задача без критериев закрывается без единой улики — и это не теория.

    Именно поэтому очередь обязана либо заморозить критерии, либо не начинать
    работу: молча пропустив отказ set_acceptance, она получила бы задачу,
    для которой «готово» ничем не подтверждается.
    """

    def setUp(self):
        self.r = QueueRedis()

    def test_no_criteria_means_done_passes_unchecked(self):
        tid = run(tb.create_task(self.r, "заявка", assignee=dq.DEV_DEPT_ASSIGNEE))
        self.assertTrue(run(tb.update_status(self.r, tid, "done")))

    def test_criteria_cannot_be_added_once_work_started(self):
        tid = run(tb.create_task(self.r, "заявка", assignee=dq.DEV_DEPT_ASSIGNEE))
        run(tb.incr_attempts(self.r, tid))
        ok, _ = run(tb.set_acceptance(self.r, tid, dq.acceptance_for()))
        self.assertFalse(ok)
        self.assertFalse(run(tb.get_task(self.r, tid))["acceptance"])


class TestOneRunAtATime(unittest.TestCase):
    """«Одна за тик» ограничивает вопросы, а не запуски.

    Одобрив две заявки подряд, владелец запустил бы две цепочки одновременно:
    на одном файле это два PR с разошедшимися базами. Слот один на весь офис.
    """

    def setUp(self):
        self.r = QueueRedis()

    def test_second_run_cannot_start(self):
        self.assertTrue(run(dq.acquire_run_slot(self.r, "t1")))
        self.assertFalse(run(dq.acquire_run_slot(self.r, "t2")))

    def test_slot_names_the_task_that_holds_it(self):
        run(dq.acquire_run_slot(self.r, "t1"))
        self.assertEqual(run(dq.current_run(self.r)), "t1")

    def test_free_slot_reads_as_empty(self):
        self.assertEqual(run(dq.current_run(self.r)), "")

    def test_slot_is_released_and_reusable(self):
        run(dq.acquire_run_slot(self.r, "t1"))
        run(dq.release_run_slot(self.r, "t1"))
        self.assertEqual(run(dq.current_run(self.r)), "")
        self.assertTrue(run(dq.acquire_run_slot(self.r, "t2")))

    def test_a_stale_task_cannot_release_someone_elses_slot(self):
        # Задача, доработавшая после истечения своего TTL, не должна снести
        # слот у той, что идёт сейчас.
        run(dq.acquire_run_slot(self.r, "running"))
        run(dq.release_run_slot(self.r, "stale"))
        self.assertEqual(run(dq.current_run(self.r)), "running")

    def test_without_redis_nothing_runs(self):
        self.assertFalse(run(dq.acquire_run_slot(None, "t1")))


class TestSelfEditGuard(unittest.TestCase):
    def test_own_repo_is_refused(self):
        for repo in dq.SELF_EDIT_REPOS:
            self.assertTrue(dq.self_edit_refusal(repo, "agents/coder.py"), repo)

    def test_other_repos_pass(self):
        self.assertEqual(dq.self_edit_refusal("kriss-bot", "bot.py"), "")
        self.assertEqual(dq.self_edit_refusal("billy-bot", "bot.py"), "")


class TestResolveTarget(unittest.TestCase):
    def test_request_brought_by_kriss_goes_to_kriss_bot(self):
        repo, _ = dq.resolve_target({
            "title": "[крисс] доработка по запросу Яны: убрать дефекты на фото",
            "created_by": "крисс"})
        self.assertEqual(repo, "kriss-bot")

    def test_named_bot_wins_over_sender(self):
        # 15.08 репозиторий выбирала модель и уехала в billy-bot, где искомых
        # кнопок не было. Названный в тексте бот — факт, а не догадка.
        repo, _ = dq.resolve_target({
            "title": "[крисс] доработка: у Билли не сохраняются задачи",
            "created_by": "крисс"})
        self.assertEqual(repo, "billy-bot")

    def test_service_prefix_is_stripped_from_the_spec(self):
        self.assertEqual(
            dq.request_text("[крисс] доработка по запросу Яны: убрать дефекты на фото"),
            "убрать дефекты на фото")
        # Без приписки текст остаётся как есть, а не обрезается по первому ":".
        self.assertEqual(dq.request_text("добавь /ping"), "добавь /ping")

    def test_unknown_sender_gives_no_repo(self):
        repo, _ = dq.resolve_target({"title": "надо что-то доделать", "created_by": ""})
        self.assertIsNone(repo)


class TestParseCheckRuns(unittest.TestCase):
    def test_all_green(self):
        state, failed, _ = parse_check_runs({"check_runs": [
            {"name": "lint", "status": "completed", "conclusion": "success"},
            {"name": "tests", "status": "completed", "conclusion": "success"}]})
        self.assertEqual((state, failed), ("success", []))

    def test_one_red_names_the_job(self):
        state, failed, _ = parse_check_runs({"check_runs": [
            {"name": "lint", "status": "completed", "conclusion": "success"},
            {"name": "tests", "status": "completed", "conclusion": "failure"}]})
        self.assertEqual((state, failed), ("failure", ["tests"]))

    def test_unfinished_is_pending_not_success(self):
        state, _, _ = parse_check_runs({"check_runs": [
            {"name": "lint", "status": "completed", "conclusion": "success"},
            {"name": "tests", "status": "in_progress"}]})
        self.assertEqual(state, "pending")

    def test_no_checks_is_not_green(self):
        # Проверка, которая не смогла выполниться, — провал, а не пропуск:
        # иначе репозиторий без CI выдавал бы зелёный свет любому коду.
        self.assertEqual(parse_check_runs({"check_runs": []})[0], "empty")
        self.assertEqual(parse_check_runs({})[0], "empty")

    def test_skipped_job_is_not_a_failure(self):
        state, failed, _ = parse_check_runs({"check_runs": [
            {"name": "deploy", "status": "completed", "conclusion": "skipped"}]})
        self.assertEqual((state, failed), ("success", []))


if __name__ == "__main__":
    unittest.main()

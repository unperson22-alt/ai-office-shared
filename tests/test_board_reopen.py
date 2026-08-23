"""
Вернуть задачу доски в работу — названная операция вместо записи в Redis.

23.08.2026 две заявки dev-dept (62ffa25b5e30 и aa573bddb6d7) легли в blocked
из-за пустого счёта Anthropic — сбой инфраструктуры, а не работа команды.
Вернуть их в строй оказалось нечем:
  • `redis_query` у Силли только читает — это фиксированное меню из четырёх
    аудитов, HSET там нет ни в каком виде;
  • `/redis` сверяет X-Auth-Token с её RAILWAY_TOKEN_VLAD, которого у сессии
    Клода нет;
  • просьба «выполни HSET … updated_at …» вообще ушла в аудит качества, потому
    что ветку выбрала подстрока "up" внутри "updated_at".

Возможность, которую нельзя назвать, не существует (урок #80). Поэтому здесь
ровно одна названная операция, а не право писать в Redis что угодно.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared import dev_queue as dq  # noqa: E402
from ai_office_shared.shared import taskboard as tb  # noqa: E402

from test_taskboard_acceptance import FakeRedis, run  # noqa: E402


class IndexedRedis(FakeRedis):
    """FakeRedis + ZREVRANGE — доска ищет задачи через индекс-ZSET."""

    async def zrevrange(self, key, start, end):
        items = sorted(self.z.get(key, {}).items(), key=lambda kv: kv[1], reverse=True)
        stop = len(items) if end == -1 else end + 1
        return [k for k, _ in items[start:stop]]

    async def zrem(self, key, *members):
        for m in members:
            self.z.get(key, {}).pop(m, None)
        return len(members)


class TestReopenTask(unittest.TestCase):
    def setUp(self):
        self.r = IndexedRedis()

    def _blocked_task(self, attempts=3):
        tid = run(tb.create_task(self.r, "заявка", assignee=dq.DEV_DEPT_ASSIGNEE,
                                 acceptance=dq.acceptance_for()))
        for _ in range(attempts):
            run(tb.incr_attempts(self.r, tid))
        run(tb.escalate(self.r, tid, "потолок"))
        return tid

    def test_blocked_task_returns_to_the_queue(self):
        tid = self._blocked_task()
        ok, why = run(tb.reopen_task(self.r, tid, reason="баланс пополнен"))
        self.assertTrue(ok, why)
        t = run(tb.get_task(self.r, tid))
        self.assertEqual(t["status"], "open")
        self.assertEqual(t["attempts"], 0)
        self.assertFalse(t["escalated"])

    def test_reopened_task_is_picked_by_the_queue_again(self):
        # Смысл операции — не «поменять поле», а вернуть заявку в работу.
        tid = self._blocked_task()
        tasks = run(tb.list_tasks(self.r, status="open",
                                  assignee=dq.DEV_DEPT_ASSIGNEE, parent_id=""))
        self.assertIsNone(dq.pick_next(tasks))
        run(tb.reopen_task(self.r, tid))
        tasks = run(tb.list_tasks(self.r, status="open",
                                  assignee=dq.DEV_DEPT_ASSIGNEE, parent_id=""))
        self.assertEqual((dq.pick_next(tasks) or {}).get("id"), tid)

    def test_attempts_must_be_reset_or_the_queue_blocks_it_again(self):
        # Оставь счётчик — и очередь на первом же тике увидит исчерпанный потолок.
        tid = self._blocked_task()
        run(tb.reopen_task(self.r, tid))
        t = run(tb.get_task(self.r, tid))
        self.assertFalse(tb.should_escalate(t))

    def test_a_running_task_is_refused(self):
        # Сброс счётчика под работающим прогоном — гонка, а не починка.
        tid = run(tb.create_task(self.r, "заявка", assignee=dq.DEV_DEPT_ASSIGNEE))
        run(tb.update_status(self.r, tid, "in_progress"))
        ok, why = run(tb.reopen_task(self.r, tid))
        self.assertFalse(ok)
        self.assertIn("уже в работе", why)

    def test_unknown_task_is_refused_by_name(self):
        ok, why = run(tb.reopen_task(self.r, "deadbeefdead"))
        self.assertFalse(ok)
        self.assertIn("не найдена", why)

    def test_acceptance_survives_the_return(self):
        # Критерии заморожены до работы и возврат их не трогает — иначе
        # «готово» снова стало бы словом без содержания.
        tid = self._blocked_task()
        before = run(tb.get_task(self.r, tid))["acceptance"]
        run(tb.reopen_task(self.r, tid))
        self.assertEqual(run(tb.get_task(self.r, tid))["acceptance"], before)

    def test_done_still_requires_evidence_after_a_return(self):
        tid = self._blocked_task()
        run(tb.reopen_task(self.r, tid))
        self.assertFalse(run(tb.update_status(self.r, tid, "done")))

    def test_no_redis_is_refused_not_silently_ignored(self):
        ok, why = run(tb.reopen_task(None, "62ffa25b5e30"))
        self.assertFalse(ok)
        self.assertTrue(why)


# ── Разбор ветки redis_query: по слову, а не по подстроке ───────────────────
import ast   # noqa: E402

CODER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "agents", "coder.py")


def load_mentions():
    with open(CODER, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "_mentions"), None)
    if fn is None:
        raise AssertionError("_mentions не найдена в coder.py")
    ns = {"_re_words": __import__("re")}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), CODER, "exec"), ns)
    return ns["_mentions"]


class TestBranchPickedByWord(unittest.TestCase):
    def setUp(self):
        self.mentions = load_mentions()

    def test_the_exact_request_that_misrouted_no_longer_matches(self):
        # Это дословно то, что 23.08 ушло в аудит качества: "up" внутри
        # "updated_at" выбрало ветку quality вместо записи на доску.
        req = 'hset office:task:62ffa25b5e30 status open updated_at 2026-08-23t08:19:07z'
        self.assertFalse(
            self.mentions(req, ["quality", "реакци", "голос", "👍", "👎",
                                "up", "down", "аудит"]))

    def test_substring_traps_do_not_fire(self):
        for text, words in (("send it to the group", ["up"]),
                            ("нужен support", ["up"]),
                            ("покажи model", ["del"]),
                            ("сделай дубль", ["up"])):
            self.assertFalse(self.mentions(text, words), text)

    def test_genuine_requests_still_match(self):
        self.assertTrue(self.mentions("покажи up/down по ботам", ["up", "down"]))
        self.assertTrue(self.mentions("аудит качества", ["аудит"]))
        self.assertTrue(self.mentions("health ботов", ["health"]))
        self.assertTrue(self.mentions("удали ключ office:x", ["удали ключ"]))

    def test_emoji_have_no_word_boundary_and_still_match(self):
        self.assertTrue(self.mentions("поставь 👍 боту", ["👍"]))

    def test_empty_input_matches_nothing(self):
        self.assertFalse(self.mentions("", ["up"]))
        self.assertFalse(self.mentions("что-то", ["", None]))


if __name__ == "__main__":
    unittest.main()

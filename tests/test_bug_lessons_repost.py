"""Перезапись одного урока в Bug Lessons бьёт ровно по нему.

23.08.2026 урок #118 уехал в группу по-русски. Существующий механизм —
`/migrate_lessons_en confirm` — сносит ВСЕ сообщения-уроки и постит архив
заново: 118 удалений ради одной записи, да ещё и с перепостом #105/#106,
которые как были русскими, так и останутся.

Две ловушки адресного варианта: номер `#11` не должен цеплять `#118`
(инвариант офиса №7 — совпадение по слову, а не по подстроке), и длинный урок
уходит несколькими сообщениями, из которых заголовок несёт только первое.
"""
import ast
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared.bug_lessons import (                    # noqa: E402
    RESYNC_MAX_LESSONS, edit_plan, forget_messages, known_messages, msgids_key,
    remember_messages, resync_plan, select_lesson_parts, stale_link,
)
from ai_office_shared.shared.telegram_text import (                   # noqa: E402
    is_continuation_part, split_for_telegram,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODER = os.path.join(ROOT, "agents", "coder.py")

HEAD = "🐛 Lesson #{} — {}"


class TestNumberIsMatchedWhole(unittest.TestCase):
    """`#11` внутри `#118` — не одиннадцатый урок."""

    MSGS = [
        HEAD.format(11, "eleven"),
        HEAD.format(111, "one eleven"),
        HEAD.format(118, "one eighteen"),
    ]

    def test_short_number_does_not_catch_the_longer_one(self):
        self.assertEqual(select_lesson_parts(self.MSGS, 11), [0])

    def test_each_number_finds_only_itself(self):
        for want, lesson_id in ((0, 11), (1, 111), (2, 118)):
            with self.subTest(id=lesson_id):
                self.assertEqual(select_lesson_parts(self.MSGS, lesson_id), [want])

    def test_absent_lesson_selects_nothing(self):
        self.assertEqual(select_lesson_parts(self.MSGS, 9999), [])

    def test_russian_header_of_the_old_archive_is_found_too(self):
        self.assertEqual(select_lesson_parts(["Урок #118 — старый"], 118), [0])


class TestMultipartLesson(unittest.TestCase):
    """Удалить одну голову — оставить в группе хвост без начала."""

    MSGS = [
        HEAD.format(117, "before"),
        "[1/3]\n" + HEAD.format(118, "long one"),
        "[2/3]\nсередина",
        "[3/3]\nконец",
        HEAD.format(119, "after"),
    ]

    def test_head_and_all_tails_are_selected(self):
        self.assertEqual(select_lesson_parts(self.MSGS, 118), [1, 2, 3])

    def test_neighbours_are_untouched(self):
        self.assertEqual(select_lesson_parts(self.MSGS, 117), [0])
        self.assertEqual(select_lesson_parts(self.MSGS, 119), [4])

    def test_a_tail_belongs_to_the_head_it_follows(self):
        """Хвост чужого урока не прилипает к предыдущему."""
        msgs = ["[1/2]\n" + HEAD.format(118, "a"), "[2/2]\nхвост 118",
                "[1/2]\n" + HEAD.format(119, "b"), "[2/2]\nхвост 119"]
        self.assertEqual(select_lesson_parts(msgs, 118), [0, 1])
        self.assertEqual(select_lesson_parts(msgs, 119), [2, 3])

    def test_a_gap_stops_the_collection(self):
        msgs = [HEAD.format(118, "a"), "постороннее сообщение", "[2/2]\nсирота"]
        self.assertEqual(select_lesson_parts(msgs, 118), [0])

    def test_empty_and_none_texts_do_not_crash(self):
        self.assertEqual(select_lesson_parts(["", None, HEAD.format(118, "x")], 118), [2])
        self.assertEqual(select_lesson_parts([], 118), [])


class TestContinuationMarkerMatchesItsWriter(unittest.TestCase):
    """Читатель и писатель маркера обязаны совпадать — они в одном файле."""

    def test_real_split_output_is_classified_correctly(self):
        parts = split_for_telegram("\n\n".join(["x" * 900] * 8))
        self.assertGreater(len(parts), 1)
        self.assertFalse(is_continuation_part(parts[0]), "первая часть — не хвост")
        for p in parts[1:]:
            self.assertTrue(is_continuation_part(p))

    def test_unsplit_message_is_never_a_continuation(self):
        only = split_for_telegram("короткий урок")
        self.assertEqual(len(only), 1)
        self.assertFalse(is_continuation_part(only[0]))


class FakeRedis:
    """Достаточно для set/get одной строки."""

    def __init__(self, broken=False):
        self.data = {}
        self.broken = broken

    async def set(self, key, val):
        if self.broken:
            raise RuntimeError("redis down")
        self.data[key] = val

    async def get(self, key):
        if self.broken:
            raise RuntimeError("redis down")
        return self.data.get(key)

    async def delete(self, key):
        if self.broken:
            raise RuntimeError("redis down")
        self.data.pop(key, None)


def run(coro):
    return asyncio.run(coro)


class TestRememberMessages(unittest.TestCase):
    """Bot API истории не читает: id, не записанный при отправке, потерян навсегда."""

    def test_roundtrip_keeps_part_order(self):
        r = FakeRedis()
        self.assertTrue(run(remember_messages(r, 118, [41, 42, 43])))
        self.assertEqual(run(known_messages(r, 118)), [41, 42, 43])

    def test_key_is_namespaced_per_lesson(self):
        r = FakeRedis()
        run(remember_messages(r, 118, [41]))
        run(remember_messages(r, 119, [42]))
        self.assertEqual(run(known_messages(r, 118)), [41])
        self.assertEqual(run(known_messages(r, 119)), [42])
        self.assertIn("118", msgids_key(118))

    def test_unknown_lesson_is_empty_not_an_error(self):
        self.assertEqual(run(known_messages(FakeRedis(), 999)), [])

    def test_empty_list_is_refused(self):
        self.assertFalse(run(remember_messages(FakeRedis(), 118, [])))

    def test_broken_redis_never_raises(self):
        """Не запомнили — перепост уйдёт старым путём, но бот не упал."""
        self.assertFalse(run(remember_messages(FakeRedis(broken=True), 118, [1])))
        self.assertEqual(run(known_messages(FakeRedis(broken=True), 118)), [])

    def test_no_redis_at_all_is_survivable(self):
        self.assertFalse(run(remember_messages(None, 118, [1])))
        self.assertEqual(run(known_messages(None, 118)), [])

    def test_garbage_in_redis_does_not_crash(self):
        r = FakeRedis()
        r.data[msgids_key(118)] = "не json"
        self.assertEqual(run(known_messages(r, 118)), [])


class TestStaleLink(unittest.TestCase):
    """Записанный неверный id хуже незаписанного: уводит на обречённый путь.

    24.08.2026 `/relink_lesson 118` отправили в ЛИЧКЕ, ответом на собственное
    сообщение. id уехал в память, команда отчиталась «запомнено 1 сообщени(й)»,
    и только правка показала правду: «message to edit not found».
    """

    def test_the_exact_telegram_answer_from_the_incident(self):
        self.assertTrue(stale_link(
            "Telegram server says - Bad Request: message to edit not found"))

    def test_uneditable_message_counts_too(self):
        self.assertTrue(stale_link("Bad Request: message can't be edited"))

    def test_identical_text_is_not_a_stale_link(self):
        """Иначе сброс привязки случался бы на пустой правке."""
        self.assertFalse(stale_link("Bad Request: message is not modified"))

    def test_unrelated_errors_do_not_reset_the_link(self):
        for err in ("Too Many Requests: retry after 30", "Bad Request: text is too long", ""):
            with self.subTest(err=err):
                self.assertFalse(stale_link(err))

    def test_forget_clears_only_that_lesson(self):
        r = FakeRedis()
        run(remember_messages(r, 118, [1]))
        run(remember_messages(r, 119, [2]))
        self.assertTrue(run(forget_messages(r, 118)))
        self.assertEqual(run(known_messages(r, 118)), [])
        self.assertEqual(run(known_messages(r, 119)), [2])

    def test_forget_survives_broken_redis(self):
        self.assertFalse(run(forget_messages(FakeRedis(broken=True), 118)))
        self.assertFalse(run(forget_messages(None, 118)))


class TestEditPlan(unittest.TestCase):
    """Правка на месте — единственный путь, не двигающий урок в конец ленты."""

    def test_matching_counts_allow_the_edit(self):
        ok, why = edit_plan([41], ["один кусок"])
        self.assertTrue(ok)
        self.assertEqual(why, "")

    def test_unknown_ids_refuse(self):
        ok, why = edit_plan([], ["один кусок"])
        self.assertFalse(ok)
        self.assertIn("неизвестн", why)

    def test_mismatch_refuses_and_names_both_numbers(self):
        """#119 после правки стал двумя сообщениями, а в группе лежит одним."""
        ok, why = edit_plan([41], ["часть 1", "часть 2"])
        self.assertFalse(ok)
        self.assertIn("1 сообщени", why)
        self.assertIn("2", why)

    def test_shrinking_also_refuses(self):
        ok, _ = edit_plan([41, 42], ["теперь одна часть"])
        self.assertFalse(ok)


class TestResyncPlan(unittest.TestCase):
    """Дослать пропавший урок нельзя: Telegram не вставляет сообщение в середину
    истории, и #118 лёг бы после #122. Порядок восстанавливается только
    переизданием хвоста целиком."""

    LESSONS = [{"id": i} for i in range(115, 123)]

    def test_tail_is_published_in_id_order(self):
        plan = resync_plan(self.LESSONS, 118, {})
        self.assertEqual([s["id"] for s in plan["steps"]], [118, 119, 120, 121, 122])

    def test_lessons_before_the_start_are_untouched(self):
        plan = resync_plan(self.LESSONS, 118, {})
        self.assertNotIn(117, [s["id"] for s in plan["steps"]])

    def test_known_ids_become_deletions_in_one_flat_list(self):
        plan = resync_plan(self.LESSONS, 118, {120: [10], 122: [12, 13]})
        self.assertEqual(plan["deletable"], [10, 12, 13])

    def test_lessons_without_ids_are_named_before_anything_is_deleted(self):
        """Владелец должен узнать про будущий дубликат ДО запуска, а не после."""
        plan = resync_plan(self.LESSONS, 118, {120: [10], 121: [11], 122: [12]})
        self.assertEqual(plan["orphans"], [118, 119])

    def test_everything_known_means_no_orphans(self):
        known = {i: [i] for i in range(118, 123)}
        self.assertEqual(resync_plan(self.LESSONS, 118, known)["orphans"], [])

    def test_a_start_beyond_the_archive_is_refused(self):
        plan = resync_plan(self.LESSONS, 999, {})
        self.assertTrue(plan["refusal"])
        self.assertEqual(plan["steps"], [])

    def test_a_whole_archive_resync_is_refused_as_flood(self):
        """Урок #54: анти-флуд. Это была бы миграция, а не починка порядка."""
        big = [{"id": i} for i in range(1, 200)]
        plan = resync_plan(big, 1, {})
        self.assertIn(str(RESYNC_MAX_LESSONS), plan["refusal"])
        self.assertEqual(plan["deletable"], [])

    def test_the_cap_is_inclusive_at_the_boundary(self):
        exact = [{"id": i} for i in range(1, 1 + RESYNC_MAX_LESSONS)]
        self.assertEqual(resync_plan(exact, 1, {})["refusal"], "")

    def test_unsorted_input_still_yields_ordered_steps(self):
        shuffled = [{"id": i} for i in (122, 118, 120, 119, 121)]
        plan = resync_plan(shuffled, 118, {})
        self.assertEqual([s["id"] for s in plan["steps"]], [118, 119, 120, 121, 122])


class TestCommandIsWiredAndGuarded(unittest.TestCase):
    """coder.py не импортируется в тест — читаем его AST."""

    @classmethod
    def setUpClass(cls):
        with open(CODER, encoding="utf-8") as f:
            cls.src = f.read()
        cls.tree = ast.parse(cls.src)
        cls.funcs = {n.name: n for n in ast.walk(cls.tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def test_both_the_operation_and_the_handler_exist(self):
        self.assertIn("repost_lesson", self.funcs)
        self.assertIn("cmd_repost_lesson", self.funcs)

    def test_handler_is_owner_only(self):
        body = ast.get_source_segment(self.src, self.funcs["cmd_repost_lesson"])
        self.assertIn("YOUR_TELEGRAM_ID", body)

    def test_dry_run_is_the_default(self):
        body = ast.get_source_segment(self.src, self.funcs["cmd_repost_lesson"])
        self.assertIn('"confirm" in', body)

    def test_it_does_not_commit_to_git(self):
        """Текст урока правится через PR, «когда перепостили» — данные в Redis."""
        body = ast.get_source_segment(self.src, self.funcs["repost_lesson"])
        self.assertNotIn("push_file", body)

    def test_a_failed_send_after_delete_is_escalated(self):
        """Старое снято, новое не ушло — урок исчез бы из группы молча."""
        body = ast.get_source_segment(self.src, self.funcs["repost_lesson"])
        self.assertIn("_escalate_vlad", body)

    def test_messages_are_reversed_into_chronological_order(self):
        """Селектор считает хвост принадлежащим предыдущей голове."""
        body = ast.get_source_segment(self.src, self.funcs["repost_lesson"])
        self.assertIn("reversed(", body)


if __name__ == "__main__":
    unittest.main()

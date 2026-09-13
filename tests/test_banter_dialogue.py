"""
Болталка как разговор, а не как набор параллельных монологов.

Пересборка 13.09.2026 по решению Влада. До неё всплеск был устроен так:
кого звать — `random.shuffle` по восьми (тема не учитывалась никак), все
комментировали последнюю строку «со стороны» (BANTER_RULES прямо это велят),
длина всплеска была записана числом (ровно две волны), правило «не повторяй
сказанное» жило в промте, а транскрипт собирался окном по ВСЕЙ ленте офиса —
и склеивал реплики из разных всплесков.

Замер того дня: за 30 суток болталка запускалась дважды, а в окно из 4 строк
попадали три ответа на три разных вопроса.

Здесь закреплено пять свойств нового устройства:
  1. у всплеска есть нить, и транскрипт собирается по ней;
  2. кого звать — решает распорядитель по теме, жребий остаётся запасным;
  3. первая волна комментирует, дальше идёт АДРЕСНЫЙ разговор;
  4. длину решает реплика: зовёт ответить — продолжаем, нет — закрываем;
  5. повтор уже сказанного в разговор не попадает.

Запуск: cd ai-office-shared && python3 -m unittest discover -s tests -v
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared import banter as b  # noqa: E402


def run(coro):
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


class TestThreadIdentity(unittest.TestCase):
    """Всплеск — объект с началом, а не окно по общей ленте."""

    def test_ids_are_unique(self):
        ids = {b.new_thread_id() for _ in range(200)}
        self.assertEqual(len(ids), 200)

    def test_id_is_short_enough_to_travel(self):
        tid = b.new_thread_id()
        self.assertTrue(0 < len(tid) <= 16, tid)

    def test_thread_of_reads_the_payload(self):
        self.assertEqual(b.thread_of({"thread_id": "abc123"}), "abc123")

    def test_thread_of_is_empty_outside_banter(self):
        for payload in ({}, {"thread_id": ""}, {"thread_id": None}, None):
            with self.subTest(payload=payload):
                self.assertEqual(b.thread_of(payload), "")


class TestInvitesReply(unittest.TestCase):
    """
    Длину всплеска решает реплика, а не константа. Признаки — про форму:
    вопрос или обращение по имени. Утверждение закрывает нить, и это
    естественный конец разговора, а не сбой.
    """

    def test_question_invites(self):
        yes, who = b.invites_reply("а ты сам-то что думаешь?")
        self.assertTrue(yes)
        self.assertEqual(who, "")

    def test_address_by_name_invites_and_names(self):
        yes, who = b.invites_reply("Гослинг, ты где вообще был")
        self.assertTrue(yes)
        self.assertEqual(who, "ГОСЛИНГ")

    def test_address_with_colon_also_counts(self):
        yes, who = b.invites_reply("Тилли: рынок-то стоит")
        self.assertTrue(yes)
        self.assertEqual(who, "ТИЛЛИ")

    def test_plain_statement_closes_the_thread(self):
        for text in ("ага, и не говори",
                     "сервер лёг, я же говорил",
                     "работаем, деньги сами себя не считают"):
            with self.subTest(text=text):
                self.assertEqual(b.invites_reply(text), (False, ""))

    def test_address_in_the_middle_of_the_line_counts(self):
        """В живой речи по имени зовут не только с начала строки."""
        yes, who = b.invites_reply(
            "таблица из 2003-го — это комплимент, Тилли, скажи честно?")
        self.assertTrue(yes)
        self.assertEqual(who, "ТИЛЛИ")

    def test_mentioning_a_colleague_is_not_addressing_him(self):
        """
        «Я спросил Гослинга вчера» — рассказ О коллеге, а не вопрос К нему.
        Позвать по такому значит сделать ровно то, от чего уходим: реплику
        мимо разговора. Инвариант №7 — совпадение по слову, а не по подстроке.
        """
        yes, who = b.invites_reply("я спросил Гослинга вчера")
        self.assertFalse(yes)
        self.assertEqual(who, "")

    def test_stranger_name_is_not_an_addressee(self):
        """Имя не из пула — просто слово, а не адресат."""
        yes, who = b.invites_reply("Вася, ты серьёзно")
        self.assertFalse(yes)
        self.assertEqual(who, "")

    def test_empty_closes(self):
        self.assertEqual(b.invites_reply(""), (False, ""))


class TestRepeatGate(unittest.TestCase):
    """«На месте» трижды подряд — это хор, а не разговор."""

    LINES = [("Влад", "кто на месте?"), ("Гослинг", "На месте.")]

    def test_repeat_is_caught(self):
        self.assertTrue(b.repeats_existing("На месте.", self.LINES))

    def test_repeat_is_caught_regardless_of_case_and_spaces(self):
        self.assertTrue(b.repeats_existing("  на  месте.  ", self.LINES))

    def test_new_thought_passes(self):
        self.assertFalse(
            b.repeats_existing("На месте, но обед никто не отменял", self.LINES))

    def test_unrelated_passes(self):
        self.assertFalse(b.repeats_existing("рынок сегодня стоит", self.LINES))


class TestAddressing(unittest.TestCase):
    """Смешанный режим: первая волна со стороны, дальше — в лицо."""

    LINES = [("Влад", "что там с деплоем"), ("Милли", "лежит с утра, ты смотрел?")]

    def test_without_addressee_it_is_a_side_remark(self):
        msg = b.build_message(self.LINES, target="ТИЛЛИ")
        self.assertIn("вставь своё со стороны", msg)
        self.assertNotIn("обратись к нему НАПРЯМУЮ", msg)

    def test_with_addressee_it_is_a_direct_reply(self):
        msg = b.build_message(self.LINES, target="ТИЛЛИ", addressee="МИЛЛИ")
        self.assertIn("Тебе отвечает Милли", msg)
        self.assertIn("обратись к нему НАПРЯМУЮ", msg)

    def test_direct_reply_drops_the_rule_that_forbade_dialogue(self):
        """«Вставь со стороны» — ровно то правило, что запрещало диалог."""
        msg = b.build_message(self.LINES, target="ТИЛЛИ", addressee="МИЛЛИ")
        self.assertNotIn("вставь своё со стороны", msg)

    def test_direct_reply_allows_a_counter_question(self):
        """Встречный вопрос — единственное, чем нить продлевается."""
        msg = b.build_message(self.LINES, target="ТИЛЛИ", addressee="МИЛЛИ")
        self.assertIn("встречный вопрос", msg)

    def test_transcript_is_present_either_way(self):
        for addressee in ("", "МИЛЛИ"):
            with self.subTest(addressee=addressee):
                msg = b.build_message(self.LINES, target="ТИЛЛИ", addressee=addressee)
                self.assertIn("Влад: что там с деплоем", msg)


class FakeRank:
    """Распорядитель: отдаёт то, что положили, или падает по требованию."""

    def __init__(self, answer="", boom=False):
        self.answer, self.boom, self.calls = answer, boom, []
        self.messages = self

    async def create(self, **kw):
        self.calls.append(kw)
        if self.boom:
            raise RuntimeError("модель недоступна")
        text = self.answer

        class _C:
            def __init__(self, t): self.text = t
        class _R:
            def __init__(self, t): self.content = [_C(t)]
        return _R(text)


class TestRankSpeakers(unittest.TestCase):
    """Кого звать — по теме, а не жребием. Но жребий обязан остаться запасным."""

    POOL = ["МИЛЛИ", "ВИЛЛИ", "ТИЛЛИ", "ДИЛЛИ"]
    LINES = [("Влад", "дашборд выглядит как таблица из 2003-го")]

    def test_names_from_the_pool_are_returned(self):
        c = FakeRank("Вилли, Милли")
        got = run(b.rank_speakers(c, self.LINES, self.POOL))
        self.assertEqual(got, ["ВИЛЛИ", "МИЛЛИ"])

    def test_speciality_reaches_the_prompt(self):
        """Без специальностей распорядителю нечем отличать коллег."""
        c = FakeRank("Вилли")
        run(b.rank_speakers(c, self.LINES, self.POOL))
        sent = c.calls[0]["messages"][0]["content"]
        self.assertIn("дизайн", sent)
        self.assertIn("трейдинг", sent)

    def test_at_most_two(self):
        c = FakeRank("Милли, Вилли, Тилли, Дилли")
        self.assertEqual(len(run(b.rank_speakers(c, self.LINES, self.POOL))), 2)

    def test_unknown_names_are_dropped(self):
        c = FakeRank("Вася, Петя")
        self.assertEqual(run(b.rank_speakers(c, self.LINES, self.POOL)), [])

    def test_names_outside_the_candidate_list_are_dropped(self):
        """Распорядитель не имеет права позвать того, кого мы не предлагали."""
        c = FakeRank("Гослинг")
        self.assertEqual(run(b.rank_speakers(c, self.LINES, self.POOL)), [])

    def test_model_failure_falls_back_to_the_draw(self):
        c = FakeRank(boom=True)
        self.assertEqual(run(b.rank_speakers(c, self.LINES, self.POOL)), [])

    def test_no_client_means_no_call(self):
        self.assertEqual(run(b.rank_speakers(None, self.LINES, self.POOL)), [])

    def test_garbage_answer_falls_back(self):
        for answer in ("", "не знаю", "```json\n{}\n```"):
            with self.subTest(answer=answer):
                c = FakeRank(answer)
                self.assertEqual(run(b.rank_speakers(c, self.LINES, self.POOL)), [])


class FakeRedisSet:
    def __init__(self): self.m = set()
    async def smembers(self, k): return set(self.m)
    async def sadd(self, k, *v): self.m.update(v)
    async def expire(self, k, t): pass
    async def set(self, k, v, nx=False, ex=None): return True
    async def lrange(self, *a): return []
    async def get(self, k): return None


class TestPickUsesRanking(unittest.TestCase):
    def test_ranked_names_win_over_the_draw(self):
        c = FakeRank("Вилли")
        got = run(b.pick(FakeRedisSet(), "БИЛЛИ", pool=["МИЛЛИ", "ВИЛЛИ", "ТИЛЛИ"],
                         client=c, lines=[("Влад", "дашборд ужасен")]))
        self.assertEqual(got, ["ВИЛЛИ"])

    def test_draw_still_works_without_a_client(self):
        got = run(b.pick(FakeRedisSet(), "БИЛЛИ", pool=["МИЛЛИ", "ВИЛЛИ"]))
        self.assertTrue(set(got) <= {"МИЛЛИ", "ВИЛЛИ"})
        self.assertTrue(got)

    def test_named_addressee_may_speak_again(self):
        """
        Дедуп нити держит одного от того, чтобы говорить весь всплеск. Но
        диалог — это и есть повторные реплики тех же двоих, поэтому обращение
        по имени правило снимает. Без этого «Гослинг, ты где?» уходило бы
        кому угодно, только не Гослингу.
        """
        r = FakeRedisSet()
        run(b.pick(r, "БИЛЛИ", pool=["МИЛЛИ"], limit=1))
        r.m.add("МИЛЛИ")
        self.assertEqual(run(b.pick(r, "БИЛЛИ", pool=["МИЛЛИ"], limit=1)), [],
                         "без обращения повтор запрещён")
        self.assertEqual(
            run(b.pick(r, "БИЛЛИ", pool=["МИЛЛИ"], limit=1, allow_repeat=True)),
            ["МИЛЛИ"], "по обращению обязан ответить")


if __name__ == "__main__":
    unittest.main(verbosity=2)

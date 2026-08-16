"""
Один ответ на одно сообщение — общий замок.

Жил в двух копиях, в gosling-bot и prophet-bot: у обоих по два независимых
входа в группу (свой телеграм-хендлер с шансом 0.40 и HTTP /task от Филли), и
16.08 08:41 Гослинг ответил на одно «доброе утро» дважды — коротко в 08:42:01 и
монологом в 08:42:06.

Здесь закреплено то, из-за чего копии нельзя было просто «свести к одной»:
формат ключа, поведение при отсутствующем Redis и нормализация текста.

Запуск:
    cd ai-office-shared && python3 -m unittest discover -s tests -v
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared import dedup  # noqa: E402


def run(coro):
    """См. пояснение в test_inter_bot.run — чужой event loop убираем за собой."""
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


class FakeRedis:
    """Только SET NX EX — больше замку ничего не нужно."""

    def __init__(self, broken=False):
        self.keys, self.broken = {}, broken

    async def set(self, key, value, nx=False, ex=None):
        if self.broken:
            raise RuntimeError("redis лёг")
        if nx and key in self.keys:
            return None
        self.keys[key] = (value, ex)
        return True


class TestAnswerKey(unittest.TestCase):
    def test_format_matches_the_versions_that_lived_in_the_bots(self):
        # Формат сохранён намеренно: иначе выкат разъехался бы с замками,
        # уже стоящими в Redis.
        key = dedup.answer_key("гослинг", "привет")
        self.assertTrue(key.startswith("office:answered:гослинг:"), key)
        self.assertEqual(len(key.rsplit(":", 1)[1]), 16)

    def test_bot_name_is_normalised(self):
        # Вызывающий передаёт как умеет: ГОСЛИНГ / Гослинг / gosling.
        keys = {dedup.answer_key(n, "x")
                for n in ("гослинг", "ГОСЛИНГ", "Гослинг", "gosling")}
        self.assertEqual(len(keys), 1, keys)

    def test_unknown_bot_still_gets_a_key(self):
        # Воркеров dev-отдела в identity нет, но замок им ломаться не должен.
        self.assertTrue(dedup.answer_key("девви", "x").startswith(
            "office:answered:девви:"))

    def test_whitespace_and_case_do_not_split_the_lock(self):
        self.assertEqual(dedup.answer_key("пророк", "Ну как  там\nс деньгами?"),
                         dedup.answer_key("пророк", "ну как там с деньгами?"))

    def test_different_bots_do_not_share_a_lock(self):
        self.assertNotEqual(dedup.answer_key("гослинг", "привет"),
                            dedup.answer_key("пророк", "привет"))

    def test_different_texts_differ(self):
        self.assertNotEqual(dedup.answer_key("гослинг", "привет"),
                            dedup.answer_key("гослинг", "пока"))


class TestClaimAnswer(unittest.TestCase):
    def test_first_claim_wins_second_is_refused(self):
        r = FakeRedis()
        self.assertTrue(run(dedup.claim_answer(r, "гослинг", "доброе утро")))
        self.assertFalse(run(dedup.claim_answer(r, "гослинг", "доброе утро")))

    def test_different_messages_do_not_block_each_other(self):
        r = FakeRedis()
        self.assertTrue(run(dedup.claim_answer(r, "гослинг", "первое")))
        self.assertTrue(run(dedup.claim_answer(r, "гослинг", "второе")))

    def test_two_bots_do_not_block_each_other(self):
        # Одну и ту же реплику Влада могут забрать разные боты — это норма.
        r = FakeRedis()
        self.assertTrue(run(dedup.claim_answer(r, "гослинг", "привет")))
        self.assertTrue(run(dedup.claim_answer(r, "пророк", "привет")))

    def test_ttl_is_applied(self):
        r = FakeRedis()
        run(dedup.claim_answer(r, "гослинг", "x", ttl=42))
        self.assertEqual(list(r.keys.values())[0][1], 42)

    def test_no_redis_is_fail_open(self):
        # Лучше два ответа, чем ни одного.
        self.assertTrue(run(dedup.claim_answer(None, "гослинг", "текст")))
        self.assertTrue(run(dedup.claim_answer(None, "гослинг", "текст")))

    def test_broken_redis_is_fail_open(self):
        self.assertTrue(run(dedup.claim_answer(FakeRedis(broken=True),
                                               "гослинг", "текст")))

    def test_empty_text_is_not_locked(self):
        r = FakeRedis()
        self.assertTrue(run(dedup.claim_answer(r, "гослинг", "")))
        self.assertTrue(run(dedup.claim_answer(r, "гослинг", "   ")))
        self.assertEqual(r.keys, {})


if __name__ == "__main__":
    unittest.main()

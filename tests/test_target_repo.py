"""
Выбор репозитория для правки.

Инцидент 15.08: заявка «убрать быстрые кнопки из /menu» пришла через Крисса,
Силли ушла в billy-bot с уверенностью 0.92 и зациклилась, ничего там не найдя.
Кнопки — у Крисса. Репозиторий угадывала модель, хотя ответ был известен точно.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared.target_repo import (  # noqa: E402
    bots_named_in, repo_for_bot, target_repo,
)


class TestRepoForBot(unittest.TestCase):
    def test_resolves_through_aliases(self):
        for name in ("Крис", "КРИСС", "крисс", "kriss"):
            self.assertEqual(repo_for_bot(name), "kriss-bot", name)

    def test_unknown_bot_is_none(self):
        self.assertIsNone(repo_for_bot("Неизвестный"))
        self.assertIsNone(repo_for_bot(""))


class TestBotsNamedIn(unittest.TestCase):
    def test_finds_named_bot(self):
        self.assertIn("крисс", bots_named_in("Yodka через Крис: убери кнопки"))

    def test_word_boundary_not_substring(self):
        # «били» внутри «побили» не должно давать Билли: неверная цель дороже
        # пропущенной, из-за неё правят чужой файл.
        self.assertEqual(bots_named_in("нас побили в чате"), [])

    def test_no_bots_named(self):
        self.assertEqual(bots_named_in("убери кнопки из меню"), [])


class TestTargetRepo(unittest.TestCase):
    def test_the_actual_incident(self):
        # Ровно сообщение, на котором Силли ушла в billy-bot.
        repo, why = target_repo(
            "Yodka через Крис: Нужно удалить быстрые кнопки на каждом сообщении "
            "которые есть в /menu",
            sender="КРИС", llm_guess="billy-bot")
        self.assertEqual(repo, "kriss-bot")
        self.assertIn("назван", why)

    def test_sender_used_when_nobody_named(self):
        repo, why = target_repo("убери кнопки из меню", sender="КРИС",
                                llm_guess="billy-bot")
        self.assertEqual(repo, "kriss-bot")
        self.assertIn("принёс", why)

    def test_explicit_name_beats_sender(self):
        # Жалоба на Билли, принесённая Криссом, — про Билли.
        repo, _ = target_repo("у Билли сломались фото", sender="КРИС",
                              llm_guess="kriss-bot")
        self.assertEqual(repo, "billy-bot")

    def test_llm_guess_only_when_no_facts(self):
        repo, why = target_repo("почини это", sender="", llm_guess="tilly-bot")
        self.assertEqual(repo, "tilly-bot")
        self.assertIn("догадка", why)

    def test_several_named_bots_fall_back_to_model(self):
        # Спорный случай не разруливаем молча — но в логе он будет виден.
        repo, why = target_repo("Билли и Гослинг оба молчат", sender="",
                                llm_guess="billy-bot")
        self.assertEqual(repo, "billy-bot")
        self.assertIn("догадка", why)

    def test_unknown_sender_does_not_break(self):
        repo, _ = target_repo("почини", sender="кто-то", llm_guess="x-bot")
        self.assertEqual(repo, "x-bot")

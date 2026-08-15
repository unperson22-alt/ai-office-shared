"""
Извлечение ответа из блоков модели.

Инцидент 15.08: Крисс отвечал «заикаясь» — «Хороший вопрос... Давай
по-честному. Окей, данные есть. Погнали — это уже не про алгоритмы...».
Причина: при веб-поиске ответ состоит из нескольких блоков, а бот склеивал
их все, включая «сейчас поищу» — строительные леса, адресованные не читателю.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared.answer_text import extract_answer_text  # noqa: E402


class B:
    """Блок ответа: text-блок или инструментальный."""
    def __init__(self, type_, text=None):
        self.type = type_
        if text is not None:
            self.text = text


class TestExtractAnswerText(unittest.TestCase):
    def test_the_actual_incident(self):
        blocks = [
            B("text", "Хороший вопрос. Давай по-честному."),
            B("server_tool_use"),
            B("web_search_tool_result"),
            B("text", "Окей, данные есть. Погнали — вот что реально работает."),
        ]
        out = extract_answer_text(blocks)
        self.assertEqual(out, "Окей, данные есть. Погнали — вот что реально работает.")
        self.assertNotIn("Хороший вопрос", out)

    def test_without_tools_behaves_as_before(self):
        blocks = [B("text", "первый"), B("text", "второй")]
        self.assertEqual(extract_answer_text(blocks), "первый второй")

    def test_several_searches_take_text_after_the_last(self):
        blocks = [
            B("text", "ищу раз"), B("server_tool_use"), B("web_search_tool_result"),
            B("text", "ищу два"), B("server_tool_use"), B("web_search_tool_result"),
            B("text", "итоговый ответ"),
        ]
        self.assertEqual(extract_answer_text(blocks), "итоговый ответ")

    def test_nothing_after_tool_falls_back_to_everything(self):
        # Пустой ответ пользователь читает как «бот сломался».
        blocks = [B("text", "сейчас поищу"), B("server_tool_use")]
        self.assertEqual(extract_answer_text(blocks), "сейчас поищу")

    def test_plain_tool_use_also_counts(self):
        blocks = [B("text", "до"), B("tool_use"), B("text", "после")]
        self.assertEqual(extract_answer_text(blocks), "после")

    def test_empty_and_none_do_not_raise(self):
        self.assertEqual(extract_answer_text([]), "")
        self.assertEqual(extract_answer_text(None), "")

    def test_blocks_without_text_attribute_are_skipped(self):
        blocks = [B("thinking"), B("text", "ответ")]
        self.assertEqual(extract_answer_text(blocks), "ответ")

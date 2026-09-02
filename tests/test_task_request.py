"""
Тесты состава заявки на `POST /task`.

Сторожат ровно тот отказ, которого 02.09.2026 не было: заявка ушла полем
`task`, обработчик взял работу из `message`, получил пустую строку, отдал её в
LLM и отрапортовал `done` приветствием. Проверяется не только факт отказа, но и
ЕГО ТЕКСТ: отказ, который не называет поле, оставляет вызывающего там же, где
он был.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared.task_request import (  # noqa: E402
    EmptyTask, TEXT_FIELD, task_text,
)


class TestTaskText(unittest.TestCase):
    """Работа есть — отдаём её; работы нет — говорим об этом громко."""

    def test_message_is_returned_trimmed(self):
        self.assertEqual(task_text({"message": "  Задеплой kriss-bot  "}),
                         "Задеплой kriss-bot")

    def test_other_fields_do_not_matter(self):
        data = {"message": "привет", "agent": "Claude", "source": "CLAUDE"}
        self.assertEqual(task_text(data), "привет")

    def test_missing_field_raises_instead_of_returning_empty(self):
        """
        Пустая строка была бы худшим из ответов: она выглядит как валидная
        заявка и уезжает в LLM, а та на пустой вопрос отвечает приветствием.
        """
        with self.assertRaises(EmptyTask):
            task_text({"agent": "Claude"})

    def test_blank_and_whitespace_are_empty(self):
        for value in ("", "   ", "\n\t "):
            with self.assertRaises(EmptyTask):
                task_text({"message": value})

    def test_non_string_message_is_refused(self):
        for value in (42, None, ["сделай"], {"text": "сделай"}):
            with self.assertRaises(EmptyTask):
                task_text({"message": value})

    def test_body_that_is_not_an_object_is_refused(self):
        for body in ([], "сделай", None, 7):
            with self.assertRaises(EmptyTask):
                task_text(body)


class TestRefusalNamesTheField(unittest.TestCase):
    """
    Текст отказа — рабочая часть, а не оформление.

    Полчаса разбора 02.09 ушло именно потому, что дальняя сторона ответила
    приветствием, а не «работа берётся из message, а пришло task».
    """

    def _detail(self, data):
        with self.assertRaises(EmptyTask) as caught:
            task_text(data)
        return caught.exception.detail

    def test_names_the_expected_field(self):
        for data in ({"agent": "Claude"}, {"message": ""}, {"message": 5}, []):
            self.assertIn(TEXT_FIELD, self._detail(data))

    def test_names_the_field_the_work_actually_arrived_in(self):
        """Тот самый промах: текст пришёл, но под чужим именем."""
        detail = self._detail({"task": "Задеплой kriss-bot", "agent": "Claude"})
        self.assertIn("task", detail)
        self.assertIn(TEXT_FIELD, detail)

    def test_every_common_misspelling_is_pointed_at(self):
        for wrong in ("task", "text", "prompt", "query", "content", "msg",
                      "input", "request", "command", "body"):
            detail = self._detail({wrong: "сделай дело"})
            self.assertIn(wrong, detail,
                          f"отказ не назвал поле {wrong!r}, в котором пришла работа")

    def test_an_empty_near_miss_is_not_reported_as_the_work(self):
        """Пустой `task` работой не является — не надо звать чинить его."""
        detail = self._detail({"task": "   ", "agent": "Claude"})
        self.assertNotIn("переименуй", detail.lower())

    def test_refusal_says_the_task_was_not_performed(self):
        """
        Главное, что обязан унести вызывающий: работа НЕ делалась. Иначе он
        продолжит считать, что запрос исполнен, — как и вышло 02.09.
        """
        for data in ({"agent": "Claude"}, {"task": "Задеплой kriss-bot"}):
            self.assertIn("не выполнял", self._detail(data))

    def test_received_keys_are_listed(self):
        detail = self._detail({"agent": "Claude", "source": "CLAUDE"})
        self.assertIn("agent", detail)
        self.assertIn("source", detail)


if __name__ == "__main__":
    unittest.main()

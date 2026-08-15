"""
Разбор JSON из ответа модели.

Инцидент 15.08: агентная петля Силли умерла на десятом шаге с «Extra data:
line 3 column 1» — модель вернула два объекта подряд, а срез «от первой
скобки до последней» захватил оба. Задача Влада (баг-репорт через Крисса)
не выполнена.

Каждый тест ниже — форма ответа, на которой старый срез ломался или врал.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared.json_extract import (  # noqa: E402
    first_json_object, first_json_value,
)


def old_way(raw):
    """Как было до фикса — чтобы тесты показывали разницу, а не только итог."""
    import json
    start, end = raw.find("{"), raw.rfind("}") + 1
    return json.loads(raw[start:end])


class TestFirstJsonObject(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(first_json_object('{"action": "done"}'),
                         {"action": "done"})

    def test_two_objects_takes_the_first(self):
        # РОВНО инцидент 15.08: старый способ здесь падает.
        raw = '{"action": "read_file"}\n{"action": "push_file"}'
        self.assertEqual(first_json_object(raw), {"action": "read_file"})
        with self.assertRaises(Exception):
            old_way(raw)

    def test_prose_with_stray_brace_after_json(self):
        raw = 'Вот действие: {"action": "done"} — надеюсь подойдёт {sic}'
        self.assertEqual(first_json_object(raw), {"action": "done"})
        with self.assertRaises(Exception):
            old_way(raw)

    def test_stray_brace_before_json(self):
        raw = 'Формат {такой}: {"action": "done"}'
        self.assertEqual(first_json_object(raw), {"action": "done"})

    def test_nested_object_is_not_truncated(self):
        raw = '{"action": "set_var", "payload": {"a": {"b": 1}}} лишнее'
        self.assertEqual(first_json_object(raw)["payload"], {"a": {"b": 1}})

    def test_markdown_fence(self):
        raw = '```json\n{"action": "done", "result": "ок"}\n```'
        self.assertEqual(first_json_object(raw)["action"], "done")

    def test_no_json_returns_none_not_raises(self):
        self.assertIsNone(first_json_object("никакого json тут нет"))
        self.assertIsNone(first_json_object(""))
        self.assertIsNone(first_json_object(None))

    def test_truncated_object_returns_none(self):
        # Обрезанный ответ модели — не результат, а провал. Врать нельзя.
        self.assertIsNone(first_json_object('{"action": "read_fi'))

    def test_top_level_array_is_not_an_object(self):
        # Просили объект действия — массив им не является.
        self.assertIsNone(first_json_object('[1, 2, 3]'))

    def test_cyrillic_values_survive(self):
        raw = '{"action":"send_message","text":"Записала и передала"}'
        self.assertEqual(first_json_object(raw)["text"], "Записала и передала")


class TestFirstJsonValue(unittest.TestCase):
    def test_accepts_array(self):
        self.assertEqual(first_json_value('[{"a": 1}]'), [{"a": 1}])

    def test_accepts_object(self):
        self.assertEqual(first_json_value('{"a": 1}'), {"a": 1})

    def test_none_when_nothing_parses(self):
        self.assertIsNone(first_json_value("просто текст"))


if __name__ == "__main__":
    unittest.main()

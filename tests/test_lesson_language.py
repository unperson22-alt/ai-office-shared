"""Заголовок урока — по-английски.

23.08.2026 запись #118 приехала в архив целиком на русском, включая title, —
правило про английский заголовок стояло в SCHEMA.md с самого начала, но нигде
не проверялось. Автор писал в тот момент коммит и тело PR, а они в этом
репозитории русские, и язык переехал вместе с ним.

Тело записи тестом НЕ проверяется намеренно: #105 и #106 написаны по-русски
целиком, и порог по доле кириллицы отделил бы их от цитат только вместе с
половиной живых записей. Соглашение про тело записано в SCHEMA.md.
"""
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LESSONS = os.path.join(ROOT, "lessons", "lessons.json")

CYRILLIC = re.compile(r"[А-Яа-яЁё]")


class TestLessonStatus(unittest.TestCase):
    """Урок без status уходит в группу строкой «❓ Status: ?».

    24.08.2026 так уехал #119: поле просто не написали, заводя запись, а
    schema-проверок на него не было. В группе это читается как сбой публикатора,
    хотя публикатор честно отрисовал то, что лежало в файле.
    """

    @classmethod
    def setUpClass(cls):
        with open(LESSONS, encoding="utf-8") as f:
            cls.lessons = [r for r in json.load(f)
                           if r.get("kind", "lesson") == "lesson"]

    def test_every_lesson_carries_a_status(self):
        missing = [r.get("id") for r in self.lessons
                   if not str(r.get("status", "")).strip()]
        self.assertEqual(missing, [], f"уроки без status: {missing}")

    def test_the_rendered_line_never_says_question_mark(self):
        """Ровно то, что увидел бы читатель в Bug Lessons."""
        for r in self.lessons:
            with self.subTest(id=r.get("id")):
                self.assertNotEqual(str(r.get("status", "?")).strip(), "?")


class TestLessonTitleLanguage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(LESSONS, encoding="utf-8") as f:
            cls.records = json.load(f)

    def test_no_title_contains_cyrillic(self):
        offenders = [r["id"] for r in self.records if CYRILLIC.search(r.get("title", ""))]
        self.assertEqual(offenders, [], f"заголовки не по-английски: {offenders}")

    def test_every_record_has_a_title(self):
        missing = [r.get("id") for r in self.records if not str(r.get("title", "")).strip()]
        self.assertEqual(missing, [], f"записи без заголовка: {missing}")

    def test_the_regression_is_actually_caught(self):
        """Гейт, который не ловит свой инцидент, — не гейт."""
        self.assertTrue(
            CYRILLIC.search("Токен каждого бота печатался в логи Railway"))
        self.assertIsNone(
            CYRILLIC.search("Every bot printed its own token into Railway logs"))


if __name__ == "__main__":
    unittest.main()

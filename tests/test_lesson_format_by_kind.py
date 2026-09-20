"""Запись публикуется шаблоном СВОЕГО вида.

20.09.2026 Влад увидел в Bug Lessons сообщение «🐛 Lesson #140» с пятью «?»
подряд: Symptom, Root cause, Fix, Prevention, Status — все пустые. Запись #140
не сломана, она просто другого вида: `kind: "decision"`, и в lessons/SCHEMA.md
про это сказано прямо — «Полей symptom/root_cause/fix у решения НЕТ».

Дефект был в публикаторе: `_format_lesson` имел ОДИН шаблон, с полями урока, и
`.get(..., "?")` подставлял вопрос в каждое отсутствующее. Так ушли ВСЕ пять
решений файла — #111, #112, #113, #132, #140. Заметить это мог только человек:
публикация при этом отрабатывала успешно и ставила `posted_to_group`.

🔴 Отсюда второе правило, которое держит этот файл: пустое тело НЕ публикуется
и флагом НЕ закрывается. Сообщение из одних «?» хуже отсутствующего — оно
выглядит как состоявшаяся публикация (инвариант офиса №4: гейт не отчитывается
«пройдено», не исполнившись).

Функции достаём из agents/coder.py через AST: импортировать его нельзя —
на уровне модуля читается os.environ и поднимается aiogram.
"""
import ast
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODER = os.path.join(ROOT, "agents", "coder.py")
LESSONS = os.path.join(ROOT, "lessons", "lessons.json")

WANT = ("_LESSON_BODY", "_DECISION_BODY", "_entry_kind", "_body_is_empty",
        "_format_lesson")


def _load():
    """Достать нужные символы из coder.py, не импортируя его."""
    with open(CODER, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    keep = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in WANT:
            keep.append(node)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in WANT:
                    keep.append(node)
    ns = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), CODER, "exec"), ns)
    missing = [w for w in WANT if w not in ns]
    assert not missing, f"не найдено в coder.py: {missing}"
    return ns


NS = _load()

DECISION = {"id": 140, "kind": "decision", "bot": "cilly-bot",
            "layer": "railway service resolution", "title": "T",
            "decision": "D", "why": "W", "rejected": "R", "revisit_if": "V"}
LESSON = {"id": 139, "kind": "lesson", "bot": "marty", "layer": "auth",
          "title": "T", "symptom": "S", "root_cause": "RC", "fix": "F",
          "prevention": "P", "status": "fixed"}


class TestDecisionIsNotPrintedAsLesson(unittest.TestCase):
    def test_no_question_marks_in_body(self):
        """Тот самый симптом: пять «?» подряд."""
        out = NS["_format_lesson"](DECISION)
        for label in ("Symptom", "Root cause", "Fix", "Prevention", "Status"):
            self.assertNotIn(label, out, f"у решения не должно быть поля {label}: {out}")
        self.assertNotIn("?", out, out)

    def test_header_says_decision(self):
        self.assertIn("📌 Decision #140", NS["_format_lesson"](DECISION))
        self.assertNotIn("🐛 Lesson", NS["_format_lesson"](DECISION))

    def test_all_decision_fields_present(self):
        out = NS["_format_lesson"](DECISION)
        for v in ("D", "W", "R", "V"):
            self.assertIn(v, out)

    def test_decision_has_no_status_line(self):
        """Решение не «починено» — оно принято. Статуса у него нет."""
        self.assertNotIn("Status", NS["_format_lesson"](DECISION))


class TestLessonUnchanged(unittest.TestCase):
    def test_lesson_keeps_its_shape(self):
        out = NS["_format_lesson"](LESSON)
        self.assertIn("🐛 Lesson #139", out)
        for label in ("👁 Symptom", "🔍 Root cause", "✅ Fix", "🛡 Prevention"):
            self.assertIn(label, out)
        self.assertIn("✅ Status: fixed", out)

    def test_missing_kind_is_a_lesson(self):
        """Записи #1–110 живут без поля kind — они уроки."""
        l = dict(LESSON)
        l.pop("kind")
        self.assertIn("🐛 Lesson", NS["_format_lesson"](l))
        self.assertEqual(NS["_entry_kind"](l), "lesson")

    def test_legacy_cause_field_still_read(self):
        """Старые записи несут `cause` вместо `root_cause`."""
        l = dict(LESSON)
        l["cause"] = "LEGACY"
        l.pop("root_cause")
        self.assertIn("LEGACY", NS["_format_lesson"](l))


class TestEmptyBodyIsNotPublished(unittest.TestCase):
    def test_empty_decision_detected(self):
        self.assertTrue(NS["_body_is_empty"]({"id": 1, "kind": "decision", "title": "T"}))

    def test_empty_lesson_detected(self):
        self.assertTrue(NS["_body_is_empty"]({"id": 1, "title": "T"}))

    def test_whitespace_only_counts_as_empty(self):
        self.assertTrue(NS["_body_is_empty"]({"id": 1, "kind": "decision", "why": "   "}))

    def test_filled_entries_pass(self):
        self.assertFalse(NS["_body_is_empty"](DECISION))
        self.assertFalse(NS["_body_is_empty"](LESSON))

    def test_one_filled_field_is_enough(self):
        self.assertFalse(NS["_body_is_empty"]({"id": 1, "kind": "decision", "why": "W"}))


class TestEveryRealEntryRenders(unittest.TestCase):
    """Гейт на живом файле: ни одна запись не должна печататься вопросами."""

    @classmethod
    def setUpClass(cls):
        with open(LESSONS, encoding="utf-8") as f:
            data = json.load(f)
        cls.entries = data if isinstance(data, list) else data.get("lessons", [])

    def test_file_has_both_kinds(self):
        kinds = {NS["_entry_kind"](e) for e in self.entries}
        self.assertEqual(kinds, {"lesson", "decision"},
                         "тест потерял смысл: в файле остался один вид записей")

    def test_no_field_renders_as_a_bare_question_mark(self):
        """Ни одно ПОЛЕ не печатается вопросом.

        Считать символы «?» нельзя — они законно встречаются в тексте записей
        (первый заход теста поймал так #34, у которой тело заполнено целиком).
        Проверяем ровно то, что было дефектом: строка вида «Label:» с одним «?»
        под ней, то есть отрисованное пустое поле.
        """
        labels = [lbl for lbl, _ in NS["_LESSON_BODY"] + NS["_DECISION_BODY"]]
        bad = []
        for e in self.entries:
            if NS["_body_is_empty"](e):
                continue
            lines = NS["_format_lesson"](e).split("\n")
            for i, ln in enumerate(lines[:-1]):
                if any(ln.startswith(l + ":") for l in labels) and lines[i + 1].strip() == "?":
                    bad.append((e.get("id"), ln))
        self.assertEqual(bad, [], f"поля печатаются вопросом: {bad}")

    def test_entries_without_bot_or_layer_are_known_and_few(self):
        """Отдельная, меньшая дыра: у старых записей нет `bot`/`layer`, и шапка
        печатает «📍 ? | ?». Тело при этом целое, поэтому это не тот дефект —
        но и молча терпеть его не надо: тест держит счёт, чтобы он не рос.

        Сейчас таких десять — #31–40, один блок мая 2026. Порог стоит по факту:
        новая запись без bot/layer уронит тест, старые чинить не обязательно."""
        KNOWN = 10
        bad = [e.get("id") for e in self.entries
               if not (e.get("bot") or "").strip() or not (e.get("layer") or "").strip()]
        self.assertLessEqual(len(bad), KNOWN,
                             f"записей без bot/layer стало больше {KNOWN}: {bad}")

    def test_the_five_decisions_render_as_decisions(self):
        dec = [e for e in self.entries if NS["_entry_kind"](e) == "decision"]
        self.assertGreaterEqual(len(dec), 5)
        for e in dec:
            self.assertIn("📌 Decision", NS["_format_lesson"](e))


if __name__ == "__main__":
    unittest.main(verbosity=2)

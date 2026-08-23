"""
Якоря записей обязаны указывать на живой код.

Правило, которое никто не проверяет, врёт. Это уже стоило времени: на
CLAUDE — СТАРТ месяцами висело описание office-токена, которого в проде не было,
и сессии искали механизм, не существовавший ни дня. Архив, разошедшийся с
кодом, опаснее отсутствующего — его читают и ему верят.

Поэтому проверяем НЕ «написал ли ты запись» (такое правило вырождается в
турникет: двести записей «см. коммит-мессадж»), а «не сгнила ли написанная».
Переименовали функцию, о которой есть запись, — CI краснеет и заставляет либо
поправить запись, либо осознанно её закрыть.

Схема — `lessons/SCHEMA.md`.
"""
import ast
import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LESSONS = os.path.join(ROOT, "lessons", "lessons.json")

KINDS = ("lesson", "decision")
# Поля, без которых запись нечитаема. Для решения намеренно НЕ требуем
# symptom/root_cause/fix: у решения нет симптома, в этом и смысл разделения.
# Кортеж внутри = «любое из этих полей». Записи #31–40 писались по ранней схеме
# и несут `cause` вместо `root_cause`; переписывать десять исторических записей
# ради формы теста — подгонка архива под проверку, а не проверка.
REQUIRED = {
    "lesson":   (("symptom",), ("root_cause", "cause"), ("fix",)),
    "decision": (("decision",), ("why",), ("rejected",)),
}


def load():
    with open(LESSONS, encoding="utf-8") as f:
        return json.load(f)


def defined_symbols(path: str) -> set:
    """Символы верхнего уровня файла: def / async def / class / КОНСТАНТА = ..."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    out = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
    return out


class TestSchema(unittest.TestCase):
    def setUp(self):
        self.records = load()

    def test_ids_are_unique_and_ordered(self):
        ids = [r["id"] for r in self.records]
        self.assertEqual(len(ids), len(set(ids)), "дублирующиеся id")
        self.assertEqual(ids, sorted(ids), "записи должны идти по возрастанию id")

    def test_kind_is_known_and_absent_means_lesson(self):
        # Записи #1–110 живут без поля kind — они уроки по умолчанию,
        # переписывать сто десять штук ради поля незачем.
        for r in self.records:
            kind = r.get("kind", "lesson")
            self.assertIn(kind, KINDS, f"#{r['id']}: неизвестный kind={kind!r}")

    def test_each_record_carries_the_fields_its_kind_needs(self):
        for r in self.records:
            kind = r.get("kind", "lesson")
            for alternatives in REQUIRED[kind]:
                self.assertTrue(
                    any(str(r.get(f, "")).strip() for f in alternatives),
                    f"#{r['id']} ({kind}): пусто во всех полях {alternatives}")

    def test_a_decision_does_not_pretend_something_broke(self):
        # Разделение «почему так» и «что сломалось» — по полям, а не по файлам.
        for r in self.records:
            if r.get("kind") != "decision":
                continue
            for field in ("symptom", "root_cause", "fix"):
                self.assertNotIn(field, r,
                                 f"#{r['id']}: у решения не должно быть {field!r}")


class TestAnchorsPointAtLiveCode(unittest.TestCase):
    def setUp(self):
        self.records = load()

    def test_anchor_format(self):
        for r in self.records:
            for a in r.get("anchors", []):
                self.assertIn(":", a, f"#{r['id']}: якорь {a!r} без символа")
                self.assertEqual(a.count(":"), 1, f"#{r['id']}: якорь {a!r}")

    def test_anchored_files_exist(self):
        for r in self.records:
            for a in r.get("anchors", []):
                path = os.path.join(ROOT, a.split(":", 1)[0])
                self.assertTrue(os.path.isfile(path),
                                f"#{r['id']}: нет файла {a.split(':', 1)[0]}")

    def test_anchored_symbols_still_exist(self):
        cache: dict = {}
        for r in self.records:
            for a in r.get("anchors", []):
                rel, symbol = a.split(":", 1)
                path = os.path.join(ROOT, rel)
                if not rel.endswith(".py"):
                    with open(path, encoding="utf-8") as f:
                        self.assertIn(symbol, f.read(), f"#{r['id']}: {a}")
                    continue
                if rel not in cache:
                    cache[rel] = defined_symbols(path)
                self.assertIn(
                    symbol, cache[rel],
                    f"#{r['id']}: якорь {a} больше никуда не указывает — "
                    f"символ переименован или удалён. Поправь запись или закрой её.")

    def test_at_least_the_load_bearing_lessons_are_anchored(self):
        # Механизм без применения — мёртвый механизм. Уроки, ради которых стоят
        # живые гварды, обязаны на них указывать.
        anchored = {r["id"] for r in self.records if r.get("anchors")}
        for lesson_id in (70, 93, 100, 108, 109):
            self.assertIn(lesson_id, anchored,
                          f"урок #{lesson_id} должен быть привязан к своему гварду")


if __name__ == "__main__":
    unittest.main()

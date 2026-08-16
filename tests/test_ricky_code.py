"""
Извлечение финального кода из ответа Рикки — инцидент 2026-08-16.

В офис-группу пришло «⛔ Cilly: баг в villy-bot — команда за 3 попыток не дала
код, прошедший ревью», а в office:logs Девви получал ту же задачу каждые ~2
минуты подряд. Причина провала во всех попытках одна: «Рикки не вернул
финальный код (блок ```python отсутствует/пуст)».

Код Рикки возвращал. Промахивался разбор: в coder.py стоял срез от литерала
«```python» до ближайшего «```».

  • «```py» и голый «```» не совпадали с литералом вовсе;
  • забор ``` не вложенный, поэтому файл, который сам содержит тройные
    кавычки, обрезался на внутреннем заборе и отдавал обрубок.

Обидно то, что стойкий разбор в офисе уже был: shared/verify.extract_code
написан 01.08.2026 ровно после трёх попыток «unterminated triple-quoted
string». В coder.py его просто не принесли, и наивный срез дожил в двух
местах до 16.08.

Второй дефект — диагноз. Ответ вида «ERROR: …» воркер отдаёт, когда сам не
смог довести код до проверки; кода там нет намеренно. Пайплайн отвечал на это
«ты забыл забор», то есть слал Рикки чинить не то, и так три раза.

coder.py не импортируется (5800 строк с побочными эффектами, правится только
вручную) — функции достаются из AST.

Запуск: cd ai-office-shared && python3 -m unittest discover -s tests -v
"""
import ast
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ai_office_shared.shared.verify import extract_code  # noqa: E402

CODER = os.path.join(ROOT, "agents", "coder.py")
WANT = ("extract_ricky_code", "ricky_failure_reason")


def old_way(ricky_result):
    """Как было до фикса — чтобы тесты показывали разницу, а не только итог."""
    if "```python" in ricky_result:
        cs = ricky_result.find("```python") + 9
        ce = ricky_result.find("```", cs)
        if ce > cs:
            return ricky_result[cs:ce].strip()
    return ""


def load():
    with open(CODER, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    picked = [n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name in WANT]
    missing = WANT if not picked else [
        n for n in WANT if n not in {p.name for p in picked}]
    if missing:
        raise AssertionError(f"в coder.py не найдены: {missing}")
    ns = {"extract_code": extract_code}
    exec(compile(ast.Module(body=picked, type_ignores=[]), CODER, "exec"), ns)
    return ns, src


GOOD = 'import os\n\n\ndef main():\n    return os.getpid()\n'


class TestExtractRickyCode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns, cls.src = load()

    def x(self, text):
        return self.ns["extract_ricky_code"](text)

    def test_plain_python_fence(self):
        self.assertEqual(self.x(f"Ревью ок.\n```python\n{GOOD}```"), GOOD.strip())

    def test_py_fence(self):
        # Старый срез не видел «```py» вовсе — а модели пишут так постоянно.
        raw = f"FINAL_CODE:\n```py\n{GOOD}```"
        self.assertEqual(self.x(raw), GOOD.strip())
        self.assertEqual(old_way(raw), "")

    def test_bare_fence(self):
        raw = f"FINAL_CODE:\n```\n{GOOD}```"
        self.assertEqual(self.x(raw), GOOD.strip())
        self.assertEqual(old_way(raw), "")

    def test_file_containing_triple_quotes_is_not_truncated(self):
        # РОВНО дефект 01.08: код с примером в тройных кавычках внутри. Старый
        # срез резал по внутреннему забору и отдавал не компилирующийся обрубок.
        inner = (
            'SYSTEM_PROMPT = """Отвечай так:\n'
            '```python\n'
            'print(1)\n'
            '```\n'
            '"""\n\n'
            'def main():\n'
            '    return SYSTEM_PROMPT\n'
        )
        raw = f"Ревью ок.\n```python\n{inner}```"
        got = self.x(raw)
        self.assertIn("def main():", got, "хвост файла потерян")
        compile(got, "<t>", "exec")           # старый вариант здесь падал
        with self.assertRaises(SyntaxError):
            compile(old_way(raw), "<t>", "exec")

    def test_prose_around_the_block(self):
        raw = (f"Нашёл две проблемы, обе поправил.\n\n```python\n{GOOD}```\n\n"
               f"Отдельно советую покрыть тестами.")
        self.assertEqual(self.x(raw), GOOD.strip())

    def test_worker_error_is_not_code(self):
        # «ERROR: …» — это отказ воркера, а не файл. Пытаться достать оттуда
        # код нельзя: получится текст ошибки, отправленный в репозиторий.
        raw = ("ERROR: рикки не смог получить проходящий проверку код за 3 "
               "попыт(ки). Последняя ошибка: invalid syntax, строка 12\n\n"
               f"```python\n{GOOD}```")
        self.assertEqual(self.x(raw), "")

    def test_empty_and_none(self):
        self.assertEqual(self.x(""), "")
        self.assertEqual(self.x(None), "")

    def test_review_text_without_code_is_empty(self):
        # У ревьюеров ответ текстовый — это не сбой, просто кода нет.
        self.assertEqual(self.x("Выглядит нормально, замечаний нет."), "")


class TestFailureReason(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns, cls.src = load()

    def why(self, text):
        return self.ns["ricky_failure_reason"](text)

    def test_worker_error_is_quoted_not_guessed(self):
        # Главный смысл правки: не слать Рикки чинить забор, когда он сообщил
        # о совсем другой беде. Три попытки подряд уходили именно на это.
        raw = ("ERROR: рикки не смог получить проходящий проверку код за 3 "
               "попыт(ки). Последняя ошибка: invalid syntax, строка 12")
        why = self.why(raw)
        self.assertIn("отказ", why)
        self.assertIn("invalid syntax", why)
        self.assertNotIn("забор", why)

    def test_empty_answer_is_named_as_such(self):
        self.assertIn("не ответил", self.why(""))

    def test_text_without_code_asks_for_the_block(self):
        why = self.why("Всё хорошо, замечаний нет.")
        self.assertIn("```python", why)


class TestNoNaiveSliceLeft(unittest.TestCase):
    def test_both_pipelines_use_the_shared_extractor(self):
        # Гейт против отката и против третьей копии наивного среза.
        with open(CODER, encoding="utf-8") as f:
            src = f.read()
        self.assertEqual(src.count("extract_ricky_code(ricky_result)"), 2,
                         "оба пайплайна должны звать общий разбор")
        self.assertNotIn('ricky_result.find("```python")', src,
                         "наивный срез вернулся в coder.py")


if __name__ == "__main__":
    unittest.main()

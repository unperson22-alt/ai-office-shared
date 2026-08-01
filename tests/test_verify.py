"""Гейт проверки кода воркера.

Сторожит то, чего в dev-dept не было вообще: результат ни разу не встречался с
компилятором. Оба дефекта, которые реально выдал Девви 31.07.2026, обязаны
ловиться — на них здесь отдельные тесты.
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared import verify  # noqa: E402


def run(c):
    return asyncio.get_event_loop().run_until_complete(c)


class TestExtractCode(unittest.TestCase):

    def test_takes_python_block(self):
        self.assertEqual(verify.extract_code("бла\n```python\nx = 1\n```\nбла"), "x = 1")

    def test_takes_longest_block(self):
        t = "```python\nx=1\n```\ntext\n```python\ndef f():\n    return 2\n```"
        self.assertIn("def f()", verify.extract_code(t))

    def test_file_containing_fences_survives(self):
        """🔴 Файл сам содержит ``` — наивный разбор обрезал его на внутреннем заборе.

        Ровно форма devvy-bot/bot.py: в SYSTEM_PROMPT лежит пример ответа в
        ```python-блоке. 01.08.2026 это три попытки подряд давало
        «unterminated triple-quoted string literal», и выглядело как слабая
        модель, хотя виноват транспорт.
        """
        inner = '```python\n# полный код файла\n```'
        payload = f'BOT_NAME = "девви"\n\nPROMPT = """формат:\n{inner}\nSUMMARY: что сделал"""\n'
        answer = f"Вот файл:\n```python\n{payload}```\nSUMMARY: добавил эндпоинт"
        got = verify.extract_code(answer)
        self.assertIn("SUMMARY: что сделал", got, "хвост файла обрезан на внутреннем заборе")
        ok, rep = verify.verify_code(got)
        self.assertTrue(ok, f"извлечённый файл не компилируется: {rep}")

    def test_no_block_is_empty_not_error(self):
        """У ревьюеров ответ текстовый — это не повод падать."""
        self.assertEqual(verify.extract_code("VERDICT: APPROVED, всё хорошо"), "")
        self.assertEqual(verify.extract_code(""), "")


class TestVerifyCode(unittest.TestCase):

    def test_good_code_passes(self):
        ok, rep = verify.verify_code("import os\n\n\ndef f():\n    return os.getcwd()\n")
        self.assertTrue(ok, rep)
        self.assertEqual(rep, "")

    def test_empty_code_passes(self):
        self.assertEqual(verify.verify_code(""), (True, ""))
        self.assertEqual(verify.verify_code("   \n "), (True, ""))

    def test_catches_unterminated_triple_quote(self):
        """🔴 Ровно то, что выдал Девви 31.07 после починки чтения файла."""
        ok, rep = verify.verify_code('SYSTEM_PROMPT = """не закрыта\nx = 1\n')
        self.assertFalse(ok)
        self.assertIn("SyntaxError", rep)

    def test_catches_undefined_name(self):
        """🔴 Второй реальный дефект Девви: undefined name 'web'.

        Тот же класс, что дал 30 живых крашей 25.07 (lessons #74-76): код
        компилируется, но имя не определено — компилятор молчит, pyflakes нет.
        """
        ok, rep = verify.verify_code("async def h(r):\n    return web.json_response({})\n")
        self.assertFalse(ok, "undefined name прошёл гейт")
        self.assertIn("pyflakes", rep)
        self.assertIn("web", rep)

    def test_report_names_the_line(self):
        ok, rep = verify.verify_code("def f(:\n    pass\n")
        self.assertFalse(ok)
        self.assertIn("строка", rep, "в отчёте нет номера строки — модели нечего чинить")

    def test_does_not_execute_the_code(self):
        """Код проверяется, а НЕ запускается: compile() разбирает, не исполняет."""
        marker = "/tmp/verify_must_not_run"
        if os.path.exists(marker):
            os.remove(marker)
        ok, _ = verify.verify_code(f"open({marker!r}, 'w').write('boom')\n")
        self.assertTrue(ok, "синтаксис валиден")
        self.assertFalse(os.path.exists(marker), "ГЕЙТ ВЫПОЛНИЛ КОД — это дыра")


class TestRetryPrompt(unittest.TestCase):

    def test_contains_error_and_demands_full_file(self):
        p = verify.retry_prompt("SyntaxError (строка 4): boom", 1, 3)
        self.assertIn("SyntaxError (строка 4): boom", p)
        self.assertIn("ПОЛНЫЙ файл", p)
        self.assertIn("1 из 3", p)


class TestWorkerLoop(unittest.TestCase):
    """Цикл самопроверки внутри воркера."""

    def setUp(self):
        from ai_office_shared.shared import worker
        self.worker = worker
        self._orig = worker.ask_claude

    def tearDown(self):
        self.worker.ask_claude = self._orig

    def test_bad_code_is_retried_with_the_real_error(self):
        seen = []

        async def fake(system_prompt, message, context=""):
            # Сломанный ответ приходит в _verified аргументом, а не отсюда:
            # первый же вызов модели — это уже ПЕРЕДЕЛКА, и она чинит код.
            seen.append(message)
            return "```python\nx = 1\n```"

        self.worker.ask_claude = fake
        out = run(self.worker._verified("девви", "S", "задача", "", "```python\nx = (\n```"))
        self.assertIn("x = 1", out, "исправленный ответ не вернулся")
        self.assertEqual(len(seen), 1, "должна быть ровно одна переделка")
        self.assertIn("SyntaxError", seen[0], "модели не передали реальную ошибку")

    def test_gives_up_loudly_after_attempts(self):
        async def always_broken(system_prompt, message, context=""):
            return "```python\nx = (\n```"

        self.worker.ask_claude = always_broken
        out = run(self.worker._verified("девви", "S", "задача", "", "```python\nx = (\n```"))
        self.assertTrue(out.startswith("ERROR:"), out[:80])
        self.assertIn("SyntaxError", out)

    def test_text_only_answer_is_untouched(self):
        """Ревьюер вернул прозу — гейт не должен вмешиваться и звать модель."""
        called = []

        async def must_not_run(*a, **k):
            called.append(1)
            return "не должно случиться"

        self.worker.ask_claude = must_not_run
        text = "VERDICT: APPROVED\nSUMMARY: замечаний нет"
        out = run(self.worker._verified("рикки", "S", "задача", "", text))
        self.assertEqual(out, text)
        self.assertEqual(called, [])

    def test_good_code_passes_without_extra_calls(self):
        called = []

        async def must_not_run(*a, **k):
            called.append(1)
            return "не должно случиться"

        self.worker.ask_claude = must_not_run
        good = "```python\nimport os\nprint(os.getcwd())\n```"
        out = run(self.worker._verified("девви", "S", "задача", "", good))
        self.assertEqual(out, good)
        self.assertEqual(called, [], "модель вызвана на исправном коде")


if __name__ == "__main__":
    unittest.main()

"""Чей это отказ — и что делать, когда судить не на чем.

Регрессия на инцидент 27–29.08.2026: 11 сообщений «повтор известной проблемы»
за трое суток по httpx-таймаутам, про которые монитор обязан молчать. Гейт
тишины открывался на ОБРЕЗАННОМ трейсбеке: внешних паттернов в огрызке нет
(все они — имена исключений, а именно строки исключения там и не было), и
classify возвращала "internal" по умолчанию.

Ключевой тест здесь — test_truncated_stack_is_unknown_not_ours. Он проверяет
не формулировку вердикта, а то, что отсутствие улики не выдаётся за улику.
"""

import unittest

from ai_office_shared.shared.fault_class import (
    EXTERNAL, INTERNAL, UNKNOWN, classify, is_ours,
)

# Тот самый лог molly-trader, снятый с Railway 29.08.2026.
TIMEOUT = [
    "Traceback (most recent call last):",
    '  File "/app/molly_trader/trader.py", line 214, in fetch_market_data',
    "    response = await client.get(url, timeout=25.0)",
    '  File "/usr/local/lib/python3.11/site-packages/httpx/_client.py", line 1774, in get',
    '    return await self.request("GET", url, **kwargs)',
    "httpx.ReadTimeout: timed out while reading response after 25.0 seconds",
]

OUR_BUG = [
    "Traceback (most recent call last):",
    '  File "/app/bot.py", line 88, in handle',
    '    return payload["text"]',
    "KeyError: 'text'",
]


class TestVerdicts(unittest.TestCase):
    def test_full_timeout_is_external(self):
        v = classify(TIMEOUT)
        self.assertEqual(v, EXTERNAL)
        self.assertIn("httpx.ReadTimeout", v.reason)

    def test_our_bug_is_internal(self):
        self.assertEqual(classify(OUR_BUG), INTERNAL)
        self.assertTrue(is_ours(OUR_BUG))

    def test_truncated_stack_is_unknown_not_ours(self):
        """Сердце инцидента: стек без строки исключения — НЕ «наш баг».

        Водяной знак в get_service_logs режет выгрузку Railway по метке
        времени, а каждая строка стека приезжает отдельной записью. Цикл,
        попавший в середину, забирает заголовок с кадрами без исключения.
        Раньше отсюда выходило "internal", открывался гейт тишины, и офис
        получал сообщение по сбою, о котором договорились молчать.
        """
        truncated = TIMEOUT[:-1]
        v = classify(truncated)
        self.assertEqual(v, UNKNOWN)
        self.assertNotEqual(v, INTERNAL)
        self.assertFalse(is_ours(truncated))

    def test_truncated_our_bug_is_also_unknown(self):
        """Симметрично: обрезанный стек НАШЕГО бага тоже не диагностируется.

        Иначе «unknown» превратилось бы в способ угадывать в другую сторону.
        """
        self.assertEqual(classify(OUR_BUG[:-1]), UNKNOWN)

    def test_verdict_names_what_fired(self):
        """Инвариант №8: вердикт называет паттерн, а не пересказывает лог."""
        self.assertIn("httpx.ReadTimeout", classify(TIMEOUT).reason)
        self.assertIn("KeyError", classify(OUR_BUG).reason)
        self.assertIn("оборван", classify(TIMEOUT[:-1]).reason)


class TestMatchingRules(unittest.TestCase):
    def test_marker_in_frame_does_not_make_it_ours(self):
        """`except KeyError` в кадре чужой библиотеки — не наше падение.

        Маркер ищется на строке исключения. Раньше он искался по всему тексту
        окна, и одно слово в кадре переводило внешний сбой в «наш баг».
        """
        lines = [
            "Traceback (most recent call last):",
            '  File "/app/x.py", line 9, in get',
            "    except KeyError: pass",
            "httpx.ConnectTimeout: timed out",
        ]
        self.assertEqual(classify(lines), EXTERNAL)

    def test_word_boundary_not_substring(self):
        """Инвариант №7: совпадение по слову, а не по подстроке."""
        lines = [
            "Traceback (most recent call last):",
            '  File "/app/x.py", line 3, in run',
            "    raise Boom()",
            "MyTypeErrorHandlerFault: не имя из списка маркеров",
        ]
        # `TypeError` лежит внутри `MyTypeErrorHandlerFault`, но это не он.
        self.assertNotIn("TypeError", classify(lines).reason)

    def test_framework_exception_without_error_suffix(self):
        """TelegramBadRequest — полный стек, внешним не притворяется."""
        lines = [
            "Traceback (most recent call last):",
            '  File "/app/bot.py", line 12, in f',
            "    await bot.send(x)",
            "aiogram.exceptions.TelegramBadRequest: file is too big",
        ]
        self.assertEqual(classify(lines), INTERNAL)

    def test_chained_exception_keeps_external_root(self):
        """Цепочка httpx → telegram: корень внешний, вердикт внешний."""
        lines = [
            "Traceback (most recent call last):",
            '  File "/usr/lib/httpx/_client.py", line 1, in send',
            "    raise exc",
            "httpx.ConnectTimeout: timed out",
            "",
            "The above exception was the direct cause of the following exception:",
            "",
            "Traceback (most recent call last):",
            '  File "/app/bot.py", line 40, in poll',
            "    await bot.get_updates()",
            "telegram.error.NetworkError: httpx.ConnectTimeout: timed out",
        ]
        self.assertEqual(classify(lines), EXTERNAL)

    def test_no_traceback_falls_back_to_whole_text(self):
        """Синтетические строки из Redis-фолбэка трейсбека не имеют."""
        self.assertEqual(classify(["api_error: Bad Gateway from Telegram"]), EXTERNAL)
        self.assertEqual(classify(["container crashed with exit code 1"]), INTERNAL)

    def test_empty_log_is_unknown(self):
        """Инвариант №4: проверке не на чем было запуститься — это не «чисто»."""
        self.assertEqual(classify([]), UNKNOWN)
        self.assertEqual(classify(["", "   "]), UNKNOWN)
        self.assertFalse(is_ours([]))


class TestCallSites(unittest.TestCase):
    """coder.py не импортируется — контракт вызова проверяем по тексту.

    Вердикта три, поэтому `== "external"` перестало быть полным условием:
    "unknown" попадал бы в ветку нашего бага и открывал ровно тот же гейт.
    """

    def setUp(self):
        with open("agents/coder.py", encoding="utf-8") as fh:
            self.code = fh.read()

    def test_no_call_site_compares_to_external(self):
        self.assertNotIn('classify_fault(', self.code.split(
            'def classify_fault')[0].split('EXTERNAL_FAULT_PATTERNS')[0])
        bad = [ln.strip() for ln in self.code.splitlines()
               if 'classify_fault(' in ln and '== "external"' in ln]
        self.assertEqual(bad, [], f"вызовы, теряющие 'unknown': {bad}")

    def test_known_lesson_repeat_does_not_notify_office(self):
        """Сообщение «новых действий не требуется» в офис не уходит.

        Ищем шаблон сообщения, а не слова: разбор инцидента в докстринге
        цитирует ту же фразу, и он там нужен.
        """
        self.assertNotIn("📚 Cilly:", self.code)
        branch = self.code.split('_les_key = f"lesson_applied:', 1)
        self.assertEqual(len(branch), 2, "ветка известного урока не найдена")
        # Тело ветки — до её собственного `continue` (20 пробелов отступа).
        body = branch[1].split("\n" + " " * 20 + "continue", 1)[0]
        # Комментарии выкидываем: разбор инцидента в них называет notify_office
        # именно потому, что вызова там больше нет.
        code = "\n".join(ln for ln in body.splitlines()
                         if not ln.lstrip().startswith("#"))
        self.assertNotIn("notify_office", code)
        self.assertIn("log_event", code)

    def test_classify_fault_delegates_to_package(self):
        self.assertIn("return _fault_class.classify(error_logs)", self.code)


if __name__ == "__main__":
    unittest.main()

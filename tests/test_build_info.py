"""
`GET /version` отвечает на вопрос, на который `GET /health` не отвечает.

02.09.2026: фикс ретуши смёржен, SHA пакета поднят, обе ветки в main — и ни
одного способа узнать, что именно крутится у Крисс. `/health` отвечал
`{"status": "ok"}` и до деплоя, и после; единственной «уликой» был текст Силли
«✅ kriss-bot задеплоен», то есть подпись исполнителя под собственной работой
(инвариант 5).

Сторожим здесь ровно одно свойство, и оно важнее полноты: НЕИЗВЕСТНАЯ ВЕРСИЯ
ОБЯЗАНА ВЫГЛЯДЕТЬ КАК НЕИЗВЕСТНАЯ. Выдуманный SHA хуже отсутствующего — по
нему решают «фикс раскатан», и следующий разбор стартует с ложной посылки.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared import build_info as bi  # noqa: E402


class TestServiceCommit(unittest.TestCase):

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in bi._COMMIT_ENV}
        for k in bi._COMMIT_ENV:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_reads_the_platform_variable(self):
        os.environ["RAILWAY_GIT_COMMIT_SHA"] = "ff372c4ffca2aad9"
        self.assertEqual(bi.service_commit(), "ff372c4ffca2aad9")

    def test_absent_is_none_not_a_plausible_string(self):
        self.assertIsNone(bi.service_commit())

    def test_blank_variable_counts_as_absent(self):
        os.environ["RAILWAY_GIT_COMMIT_SHA"] = "   "
        self.assertIsNone(bi.service_commit())


class TestSharedCommit(unittest.TestCase):

    def test_never_raises_and_never_invents(self):
        """
        В этом окружении пакет лежит на sys.path, а не поставлен из git, —
        значит правильный ответ None. Функция обязана его дать, а не упасть и
        не выдумать: она вызывается из HTTP-обработчика.
        """
        value = bi.shared_commit()
        self.assertTrue(value is None or (isinstance(value, str) and value))


class TestBuildInfo(unittest.TestCase):

    def test_keys_are_always_present(self):
        """
        Отсутствие ключа читалось бы как «эта версия не умеет отвечать», а
        нужно обратное: «версия неизвестна» обязано быть видно.
        """
        info = bi.build_info("крисс")
        self.assertEqual(set(info), {"bot", "service_commit", "shared_commit"})
        self.assertEqual(info["bot"], "крисс")

    def test_unknown_is_null_not_a_word_that_reads_as_a_version(self):
        for key in ("service_commit", "shared_commit"):
            value = bi.build_info()[key]
            self.assertTrue(value is None or isinstance(value, str))
            if isinstance(value, str):
                self.assertNotIn(value.lower(), ("unknown", "none", "n/a", "-"),
                                 "неизвестность выдана строкой, которую примут "
                                 "за версию")


if __name__ == "__main__":
    unittest.main()

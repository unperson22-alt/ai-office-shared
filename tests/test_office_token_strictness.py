"""
Разрешительность Фазы A не должна попадать в чужие гейты.

ИНЦИДЕНТ 26.09.2026. У Эллис четыре HTTP-входа (/send, /send_scheduled,
/generate, /mention) проверяли легаси-секрет офиса. Объединяя их в один гейт, я
поставил первым условием

    if check_office_token(request):
        return True

а `check_office_token` при невыставленном OFFICE_RPC_TOKEN возвращает True ВСЕМ.
Итог: все четыре входа принимали запрос без единого заголовка, а `/send`
отправляет сообщение от имени Эллис в произвольный chat_id. Измерено на проде:
`POST /send` без заголовков отвечал 500 (дошёл до хендлера), а не 401; после
починки — 401.

ПОЧЕМУ ЭТО ВОПРОС ЭТОГО ПАКЕТА, А НЕ ВНИМАТЕЛЬНОСТИ ЭЛЛИС. Функция называется
`check_...`, возвращает bool и на вопрос «свой ли это запрос» отвечает «да» —
хотя отвечает она на другой вопрос: «блокировать ли выкат». Такое имя читается
как разрешение, и прочитано так будет снова: тот же паттерн уже стоит у Марти.
Поэтому в пакете появилась `has_office_token`, которая при невыставленном
секрете говорит «нет», — а этот тест стережёт разницу между ними.

Запуск: cd ai-office-shared && python3 -m pytest tests/test_office_token_strictness.py -q
"""
import importlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeRequest:
    def __init__(self, headers=None, method="POST", path="/send"):
        self.headers = headers or {}
        self.method = method
        self.path = path


def auth_with(token: str):
    """Модуль auth, перечитанный с заданным OFFICE_RPC_TOKEN.

    Константы читаются на импорте, поэтому окружение подменяем ДО reload —
    иначе тест проверял бы не то состояние, в котором живёт офис.
    """
    if token:
        os.environ["OFFICE_RPC_TOKEN"] = token
    else:
        os.environ.pop("OFFICE_RPC_TOKEN", None)
    from ai_office_shared.shared import auth
    return importlib.reload(auth)


class TestPhaseA(unittest.TestCase):
    """OFFICE_RPC_TOKEN не выставлен — состояние офиса на 26.09.2026."""

    def setUp(self):
        self.auth = auth_with("")

    def tearDown(self):
        auth_with("")

    def test_check_office_token_admits_everyone(self):
        # Это НЕ дефект, а смысл Фазы A: выкат кода не должен ронять трафик.
        # Утверждение зафиксировано, потому что на него опираются гейты ботов.
        self.assertTrue(self.auth.check_office_token(FakeRequest()))

    def test_has_office_token_refuses_everyone(self):
        # Ровно эта разница и есть починка инцидента: учётки не существует —
        # значит по ней никто не свой.
        self.assertFalse(self.auth.has_office_token(FakeRequest()))
        self.assertFalse(
            self.auth.has_office_token(FakeRequest({"X-Office-Token": "что угодно"})))


class TestPhaseB(unittest.TestCase):
    """Секрет выставлен — обе функции обязаны сойтись."""

    def setUp(self):
        self.auth = auth_with("mesh-secret")

    def tearDown(self):
        auth_with("")

    def test_valid_token_passes_both(self):
        req = FakeRequest({"X-Office-Token": "mesh-secret"})
        self.assertTrue(self.auth.check_office_token(req))
        self.assertTrue(self.auth.has_office_token(req))

    def test_wrong_token_fails_both(self):
        for headers in ({}, {"X-Office-Token": "чужой"}, {"X-Office-Token": ""}):
            with self.subTest(headers=headers):
                req = FakeRequest(headers)
                self.assertFalse(self.auth.check_office_token(req))
                self.assertFalse(self.auth.has_office_token(req))


class TestTheWarningStaysWhereItIsRead(unittest.TestCase):
    """Докстринг здесь — не украшение: он единственное, что видит следующий.

    Предупреждение сорвалось один раз именно потому, что стояло в скобках как
    деталь раскатки. Если его снова уберут в скобки или сотрут, тест покажет
    это до мёрджа, а не после инцидента.
    """

    def test_check_office_token_warns_against_using_it_as_a_gate(self):
        doc = auth_with("").check_office_token.__doc__ or ""
        self.assertIn("НЕ АУТЕНТИФИКАЦИЯ", doc)
        self.assertIn("has_office_token", doc,
                      "предупреждение обязано называть, чем пользоваться вместо")

    def test_has_office_token_says_what_it_refuses(self):
        doc = auth_with("").has_office_token.__doc__ or ""
        self.assertIn("OFFICE_RPC_TOKEN", doc)


if __name__ == "__main__":
    unittest.main()

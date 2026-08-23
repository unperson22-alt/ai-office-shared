"""
Сырой проброс в Railway GraphQL не отдаётся по открытому запросу.

23.08.2026: `POST /task` у Силли не требует никакой аутентификации. Так
задумано не было — `office_auth_middleware` написан как ПОЭТАПНАЯ раскатка, и
`check_office_token` возвращает True, пока `OFFICE_RPC_TOKEN` не выставлен. Он
не выставлен ни на одном сервисе (проверено через Railway GraphQL 21.08 и 23.08),
поэтому middleware был no-op.

Внутри `/task` жил префикс `/railway` — сырой проброс произвольного GraphQL от
имени рабочего токена офиса: чтение переменных ВСЕХ сервисов, то есть всех
секретов, и мутации. В этот день сессия Клода без единого токена вытащила все
44 переменные сервиса ai-office-shared, включая TELETHON_SESSION и ключи ботов.

Поэтапность годится для обычных эндпоинтов, но не для канала в облачный API:
привилегия не имеет права наследовать позу «пока пускаем всех».

coder.py не импортируется — проверяем структуру через AST, как test_ricky_code.
"""
import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODER = os.path.join(ROOT, "agents", "coder.py")


def source() -> str:
    with open(CODER, encoding="utf-8") as f:
        return f.read()


def func(name: str):
    for node in ast.walk(ast.parse(source())):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} не найдена в coder.py")


class TestRailwayPassthroughIsGated(unittest.TestCase):
    def test_inner_handler_accepts_a_privilege_flag(self):
        node = func("_handle_cilly_task_inner")
        names = [a.arg for a in node.args.kwonlyargs] + [a.arg for a in node.args.args]
        self.assertIn("privileged", names)

    def test_privilege_comes_from_the_header_not_the_body(self):
        # user_id и прочие поля тела шлёт вызывающий — подделываются тривиально.
        body = ast.unparse(func("handle_cilly_task"))
        self.assertIn("headers", body)
        self.assertIn("X-Auth-Token", body)
        self.assertIn("RAILWAY_SECRET", body)

    def test_an_empty_secret_does_not_grant_privilege(self):
        # Ровно та ловушка, из-за которой middleware был no-op: пустой секрет
        # не должен означать «пускаем всех».
        body = ast.unparse(func("handle_cilly_task"))
        self.assertIn("bool(RAILWAY_SECRET)", body.replace(" ", ""))

    def test_the_railway_branch_refuses_before_it_queries(self):
        body = ast.unparse(func("_handle_cilly_task_inner"))
        i_guard = body.find("not privileged")
        i_call = body.find("railway_query")
        self.assertNotEqual(i_guard, -1, "проверка привилегии исчезла")
        self.assertNotEqual(i_call, -1, "вызов railway_query исчез")
        self.assertLess(i_guard, i_call, "гейт обязан стоять ДО обращения к Railway")

    def test_the_refusal_is_a_401_not_a_polite_note(self):
        body = ast.unparse(func("_handle_cilly_task_inner"))
        self.assertIn("401", body)


if __name__ == "__main__":
    unittest.main()

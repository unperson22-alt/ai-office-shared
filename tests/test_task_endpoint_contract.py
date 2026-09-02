"""
Все `POST /task` в офисе отказывают заявке без работы — и делают это одинаково.

02.09.2026 сессия Клода дважды попросила Силли задеплоить kriss-bot и дважды
получила `{"status": "done"}` с текстом её приветствия. Заявка ушла полем
`task`, работа берётся из `message`, пустая строка доехала до модели, та
ответила на пустой вопрос, а обработчик отрапортовал успех. Разбор занял
полчаса и закончился неверным обвинением: дефект записали офису, а упал
вызывающий (инвариант 8 — «провал называет того, кто упал»).

Правило одно на офис, поэтому и тест один. Проверяются ТРИ обработчика, и
каждый — своим способом, потому что живут они по-разному:

  • `shared/task_request.task_text` — сама функция (test_task_request.py);
  • `shared/worker.handle_task` — вызовом хендлера (test_worker.py);
  • `agents/coder._handle_cilly_task_inner` — здесь, через AST: coder.py не
    импортируется (на уровне модуля читается os.environ и поднимается aiogram)
    и правится только вручную, поэтому дотянуться до него можно лишь разбором
    исходника — образец в test_ricky_code.py.

Смысл AST-проверки не в форме кода, а в том, что дыру нельзя вернуть незаметно:
`data.get("message")` в этом обработчике снова означает «пустая заявка доедет
до LLM».
"""
import ast
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CODER = os.path.join(ROOT, "agents", "coder.py")
HANDLER = "_handle_cilly_task_inner"


def _handler_node():
    tree = ast.parse(open(CODER, encoding="utf-8").read(), filename=CODER)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == HANDLER:
            return node
    raise AssertionError(f"{HANDLER} не найден в agents/coder.py")


class TestSillyTaskEndpoint(unittest.TestCase):
    """Обработчик Силли — тот самый, что ответил приветствием на команду."""

    def setUp(self):
        self.node = _handler_node()
        self.calls = [n for n in ast.walk(self.node) if isinstance(n, ast.Call)]

    def test_work_is_taken_through_the_shared_rule(self):
        named = {n.func.id for n in self.calls if isinstance(n.func, ast.Name)}
        self.assertIn("task_text", named,
                      "заявка разбирается мимо общего правила task_text — "
                      "значит правило снова живёт в трёх местах по-разному")

    def test_the_bare_message_lookup_is_gone(self):
        """
        `data.get("message", "")` здесь — это ровно тот дефект: пустая строка
        выглядит как валидная заявка и уезжает в модель.
        """
        for call in self.calls:
            if not (isinstance(call.func, ast.Attribute) and call.func.attr == "get"):
                continue
            if not (isinstance(call.func.value, ast.Name) and call.func.value.id == "data"):
                continue
            if call.args and isinstance(call.args[0], ast.Constant) \
                    and call.args[0].value == "message":
                self.fail("работа снова берётся напрямую из data.get(\"message\") — "
                          "пустая заявка доедет до LLM и вернётся приветствием")

    def test_empty_task_is_caught_and_answered(self):
        handled = set()
        for node in ast.walk(self.node):
            if isinstance(node, ast.ExceptHandler) and node.type is not None:
                for name in ast.walk(node.type):
                    if isinstance(name, ast.Name):
                        handled.add(name.id)
        self.assertIn("EmptyTask", handled,
                      "EmptyTask не перехвачен: отказ утечёт в общий except и "
                      "вернётся как 200 с трейсбеком, а не как внятное 400")

    def test_the_refusal_is_a_client_error_not_a_success(self):
        """
        Главное следствие инцидента: «нечего делать» обязано отличаться от
        «сделано» по СТАТУСУ, а не только по тексту. Вызывающий читает статус.
        """
        statuses = []
        for node in ast.walk(self.node):
            if isinstance(node, ast.ExceptHandler) and node.type is not None \
                    and any(isinstance(n, ast.Name) and n.id == "EmptyTask"
                            for n in ast.walk(node.type)):
                for call in ast.walk(node):
                    if isinstance(call, ast.Call):
                        for kw in call.keywords:
                            if kw.arg == "status" and isinstance(kw.value, ast.Constant):
                                statuses.append(kw.value.value)
        self.assertIn(400, statuses,
                      f"отказ отдаётся не как 400, а как {statuses or 'без статуса'}")


class TestRuleLivesInOnePlace(unittest.TestCase):
    """
    Правило про состав заявки не имеет права разъехаться снова.

    До фикса тот же вопрос решался тремя способами: kriss-bot отвергал пустую
    заявку, но не называл поле; worker.py и Силли принимали её молча.
    """

    def test_shared_rule_is_importable_and_names_the_field(self):
        from ai_office_shared.shared.task_request import EmptyTask, task_text
        with self.assertRaises(EmptyTask) as caught:
            task_text({"task": "Задеплой kriss-bot"})
        detail = caught.exception.detail
        self.assertIn("message", detail)
        self.assertIn("task", detail)

    def test_worker_uses_the_same_rule(self):
        import inspect
        from ai_office_shared.shared import worker
        source = inspect.getsource(worker)
        self.assertIn("task_text(data)", source,
                      "worker.py разбирает заявку мимо общего правила")


if __name__ == "__main__":
    unittest.main()

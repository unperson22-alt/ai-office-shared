"""
Передеплой собственного сервиса Силли — инцидент 2026-08-16.

Силли упала, автохил Филли попросил её подняться и получил дважды подряд:
«❌ Сервис ai-office-shared не найден в SERVICES». Офис не смог перезапустить
собственного лидера.

Причин оказалось две, и первая правка закрыла только одну:

  1. интент deploy искал service_id ТОЛЬКО в SERVICES, а своего сервиса там
     нет намеренно — иначе аудит принялся бы править собственный coder.py
     (запрещено после 01.07, урок #70);
  2. фолбэк «спросить Railway по имени репозитория» здесь не работает в
     принципе: сервис на Railway называется не ai-office-shared, а
     автогенерённым «cilly-bot-<uuid>-<uuid>-<uuid>» — совпадения не будет
     никогда, сколько ни спрашивай.

Отсюда явный SELF_SERVICE_ID и resolve_service_id(). Тесты ниже стерегут
границу: своё имя резолвится, а список автопочинки при этом НЕ пополняется.

coder.py не импортируется: модуль на 5800 строк с побочными эффектами при
импорте, и по правилу CLAUDE-СТАРТ правится только вручную. Нужные объявления
достаём из AST и исполняем в отдельном пространстве имён — так тест проверяет
настоящий код файла, не требуя, чтобы весь бот поднялся.

Запуск: cd ai-office-shared && python3 -m unittest discover -s tests -v
"""
import ast
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CODER = os.path.join(ROOT, "agents", "coder.py")
WANT = ("SERVICES", "SELF_REPO", "SELF_SERVICE_ID", "resolve_service_id")


def load_from_coder():
    """Вытащить из coder.py только нужные объявления и исполнить их."""
    src = open(CODER, encoding="utf-8").read()
    tree = ast.parse(src)
    picked = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in WANT:
            picked.append(node)
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(n in WANT for n in names):
                picked.append(node)
    ns = {"os": os}
    exec(compile(ast.Module(body=picked, type_ignores=[]), CODER, "exec"), ns)
    missing = [n for n in WANT if n not in ns]
    if missing:
        raise AssertionError(f"в coder.py не найдены: {missing}")
    return ns, src


class TestResolveServiceId(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns, cls.src = load_from_coder()

    def test_own_repo_resolves(self):
        # РОВНО инцидент: до правки здесь был None и отказ в ответ Филли.
        sid = self.ns["resolve_service_id"]("ai-office-shared")
        self.assertTrue(sid, "свой сервис обязан резолвиться по прямой просьбе")

    def test_own_service_id_is_a_uuid(self):
        # Выдуманные хвосты UUID уже давали «Not Authorized» (см. коммент в
        # _render_railway_ids_block). Форма — минимум, который можно проверить
        # без сети.
        self.assertRegex(
            self.ns["SELF_SERVICE_ID"],
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

    def test_ordinary_bot_resolves_through_services(self):
        services = self.ns["SERVICES"]
        for sid, (repo, _) in list(services.items())[:5]:
            self.assertEqual(self.ns["resolve_service_id"](repo), sid)

    def test_unknown_repo_is_none(self):
        # Честный None → вызывающий пойдёт спрашивать Railway и, если и там
        # пусто, откажет с внятным текстом. Выдумывать id нельзя.
        self.assertIsNone(self.ns["resolve_service_id"]("нет-такого-репо"))
        self.assertIsNone(self.ns["resolve_service_id"](""))

    def test_self_is_still_absent_from_services(self):
        # Главный инвариант. Соблазн «просто добавить себя в SERVICES» решает
        # симптом и возвращает запрет, ради которого список и урезали: аудит
        # начнёт править собственный coder.py Силли (урок #70 — заглушка на
        # 8 строк вместо 5766, офис лёг целиком).
        repos = {repo for repo, _ in self.ns["SERVICES"].values()}
        self.assertNotIn(self.ns["SELF_REPO"], repos,
                         "свой сервис не должен попадать в список автопочинки")

    def test_self_id_is_not_a_duplicate_of_another_service(self):
        self.assertNotIn(self.ns["SELF_SERVICE_ID"], self.ns["SERVICES"],
                         "id своего сервиса совпал с чужим — опечатка в UUID")

    def test_deploy_branch_uses_the_resolver(self):
        # Гейт против отката: если ветку deploy снова напишут через прямой
        # обход SERVICES, свой сервис снова станет ненаходимым.
        m = re.search(r"elif intent == \"deploy\":(.{0,700})", self.src, re.S)
        self.assertIsNotNone(m, "ветка deploy не найдена — тест устарел")
        body = m.group(1)
        self.assertIn("resolve_service_id(repo)", body)
        self.assertNotIn("SERVICES.items()", body,
                         "deploy снова ходит в SERVICES напрямую")


if __name__ == "__main__":
    unittest.main()

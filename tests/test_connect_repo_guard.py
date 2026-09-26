"""
Кого агентной петле позволено привязывать к сервису Railway.

ЗАЧЕМ ДЕЙСТВИЕ ПОЯВИЛОСЬ (26.09.2026). connect_repo написан 14.06 и с тех пор
не вызывался ниоткуда: ни интента, ни действия — мёртвый код. За один день офис
трижды упёрся в «сервис не деплоится», и каждый раз чинили руками в Railway UI:
у marty-bot окружение не было связано с веткой (восемь дней мёрджи не
доезжали), у nelli-bot источник не подключён вовсе — она собирается чьей-то
ручной выкаткой. Дыру закрывает инструмент, а не промпт (это уже записано про
redeploy после 13.08).

ЗАЧЕМ ГВАРД. serviceConnect ПЕРЕПИСЫВАЕТ источник сервиса, и ошибка здесь тише
и дороже, чем у set_var: сервис молча начнёт собирать чужой код, а узнают об
этом на следующем деплое — то есть в проде и не сразу. Поэтому репозиторий
обязан быть назван полностью (owner/name), принадлежать владельцу офиса и быть
офису известен.

Запуск: cd ai-office-shared && python3 -m pytest tests/test_connect_repo_guard.py -q
"""
import ast
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ai_office_shared.shared.railway_vars import (  # noqa: E402
    GITHUB_OWNER, connect_repo_allowed,
)

CODER = os.path.join(ROOT, "agents", "coder.py")
KNOWN = {"family-dept", "marketing-dept", "filly-bot", "ai-office-shared"}


class TestGuard(unittest.TestCase):
    def test_office_repo_is_allowed(self):
        ok, why = connect_repo_allowed(f"{GITHUB_OWNER}/family-dept", KNOWN)
        self.assertTrue(ok, why)
        self.assertEqual(why, "")

    def test_foreign_owner_is_refused(self):
        # Привязать сервис офиса к чужому репозиторию — это выполнить чужой код
        # под нашими переменными окружения.
        ok, why = connect_repo_allowed("someone-else/family-dept", KNOWN)
        self.assertFalse(ok)
        self.assertIn("someone-else", why)

    def test_bare_name_is_refused_not_guessed(self):
        # Догадаться о владельце — значит однажды догадаться неверно и молча
        # подключить чужой репозиторий. Просим назвать полностью.
        ok, why = connect_repo_allowed("family-dept", KNOWN)
        self.assertFalse(ok)
        self.assertIn("owner/name", why)

    def test_unknown_repo_is_refused_and_lists_what_is_known(self):
        ok, why = connect_repo_allowed(f"{GITHUB_OWNER}/who-knows", KNOWN)
        self.assertFalse(ok)
        self.assertIn("who-knows", why)
        self.assertIn("family-dept", why, "отказ обязан показать, что известно")

    def test_empty_and_malformed(self):
        for bad in ("", "   ", None, "a/b/c", "/", "unperson22-alt/"):
            with self.subTest(repo=bad):
                ok, _ = connect_repo_allowed(bad, KNOWN)
                self.assertFalse(ok, f"{bad!r} не должен проходить")

    def test_empty_known_set_refuses_everything(self):
        # Пустой реестр означает «не смог узнать, что наше». Это НЕ повод
        # разрешить всё (инвариант 4: не проверил ≠ прошёл).
        ok, _ = connect_repo_allowed(f"{GITHUB_OWNER}/family-dept", set())
        self.assertFalse(ok)


class TestActionWiring(unittest.TestCase):
    """coder.py не импортируется (урок #70) — читаем текст."""

    @classmethod
    def setUpClass(cls):
        with open(CODER, encoding="utf-8") as fh:
            cls.src = fh.read()
        ast.parse(cls.src)          # файл обязан оставаться разбираемым

    def test_action_exists(self):
        self.assertIn('elif action == "connect_repo":', self.src)

    def test_action_is_listed_for_the_model(self):
        # Действие, которого нет в AGENTIC_SYSTEM, модель не вызовет никогда —
        # ровно так connect_repo и пролежал мёртвым с 14.06.
        self.assertIn('- connect_repo: {"action":"connect_repo"', self.src)

    def test_action_goes_through_the_guard(self):
        m = re.search(r'elif action == "connect_repo":(.{0,3000})', self.src, re.S)
        self.assertIsNotNone(m, "ветка connect_repo не найдена — тест устарел")
        body = m.group(1)
        self.assertIn("connect_repo_allowed(", body,
                      "привязка в обход гварда — сервис можно увести на чужой репозиторий")

    def test_action_does_not_claim_a_deploy_happened(self):
        # Привязка ничего не деплоит. Отчёт, читаемый как «починено», — это
        # нарушение инварианта 5, за которое офис уже платил.
        m = re.search(r'elif action == "connect_repo":(.{0,3000})', self.src, re.S)
        body = m.group(1)
        self.assertIn("/version", body,
                      "результат обязан называть улику, а не объявлять успех")


if __name__ == "__main__":
    unittest.main()

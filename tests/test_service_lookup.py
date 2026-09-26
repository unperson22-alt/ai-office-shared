"""
Репозиторий ≠ сервис — инцидент 18.09.2026.

Филли не достучалась до Марти и попросила Силли: «передеплой марти». Силли
перевела имя бота в репозиторий (target_repo: марти → marketing-dept) и стала
искать в Railway сервис с ИМЕНЕМ «marketing-dept». Такого нет и быть не может:
в этой монорепе живут четыре бота — Марти, Лекс, Нэлли, Копи — и у каждого свой
сервис. Влад увидел: «❌ Сервис marketing-dept не найден ни в SERVICES, ни в
Railway. Проверь название репозитория». Название было верным; неверным был сам
вопрос.

Две посылки, обе ложные:
  1. «один репозиторий — один сервис» — держалось, пока боты жили по одному
     на репо, и ломает любого бота монорепы;
  2. «сервис лежит в PROJECT_ID» — аудит при этом уже ходит по ВСЕМ проектам
     (_all_office_services), то есть офис одновременно видел чужие отделы в
     отчётах и не находил их при деплое.

Тесты ниже стерегут кандидатов на имя сервиса и оба гейта в coder.py.

Запуск: cd ai-office-shared && python3 -m pytest tests/test_service_lookup.py -q
"""
import ast
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ai_office_shared.shared.identity import (  # noqa: E402
    BOTS, railway_service_names, service_id,
)

CODER = os.path.join(ROOT, "agents", "coder.py")


class TestServiceNameCandidates(unittest.TestCase):
    def test_marty_resolves_to_his_own_service_not_the_monorepo(self):
        names = railway_service_names("марти")
        self.assertEqual(names[0], "marty-bot",
                         "первым кандидатом обязан быть сервис, а не репозиторий")
        self.assertIn("marketing-dept", names,
                      "репозиторий остаётся последним вариантом, а не единственным")

    def test_any_spelling_of_the_name_works(self):
        for spelling in ("марти", "МАРТИ", "Марти", "marty", "MARTY"):
            self.assertEqual(railway_service_names(spelling)[0], "marty-bot", spelling)

    def test_monorepo_name_yields_its_own_bots_only(self):
        # Имя монорепы даёт сервисы ИМЕННО этой монорепы. 26.09.2026 отсюда
        # ушла Нэлли: её каталог лежал в marketing-dept, но деплоился сервис
        # nelli-bot из family-dept, копии разошлись, и дубль удалили. Пока
        # реестр называл её репозиторием marketing-dept, «передеплой марти» мог
        # получить в кандидаты nelli-bot — то есть чужой живой сервис.
        names = railway_service_names("marketing-dept")
        self.assertIn("marty-bot", names)
        self.assertNotIn("nelli-bot", names,
                         "Нэлли больше не в marketing-dept — её сервис не должен "
                         "попадать в кандидаты этой монорепы")

    def test_nelli_resolves_through_her_real_repo(self):
        self.assertEqual(railway_service_names("нэлли")[0], "nelli-bot")
        self.assertIn("nelli-bot", railway_service_names("family-dept"))

    def test_collision_suffix_is_stripped(self):
        # ray-bot-production-d754.up.railway.app → ray-bot, а не ray-bot-production-d754.
        self.assertEqual(railway_service_names("рэй")[0], "ray-bot")
        self.assertEqual(railway_service_names("пророк")[0], "prophet-bot")
        self.assertEqual(railway_service_names("доктор")[0], "dilly-bot")

    def test_one_repo_one_service_bots_still_work(self):
        self.assertEqual(railway_service_names("билли")[0], "billy-bot")
        self.assertEqual(railway_service_names("филли")[0], "filly-bot")

    def test_nothing_is_invented(self):
        # Каждый кандидат — записанный факт о боте: имя из URL либо репозиторий.
        # Угаданное имя может совпасть с ЧУЖИМ живым сервисом, и тогда
        # «передеплой Марти» передеплоит кого-то другого, отчитавшись успехом.
        for canon, meta in BOTS.items():
            host = (meta.get("url") or "").split("://")[-1].split(".")[0]
            for name in railway_service_names(canon):
                self.assertTrue(
                    name == meta.get("repo") or host.startswith(name),
                    f"{canon}: кандидат {name!r} ниоткуда не следует")

    def test_unknown_name_returns_itself(self):
        self.assertEqual(railway_service_names("нет-такого"), ["нет-такого"])
        self.assertEqual(railway_service_names(""), [])

    def test_no_truncated_ids_in_the_registry(self):
        # «8fb51207» у Марти — восемь hex вместо UUID. Правдоподобный неверный id
        # хуже пустого: операция уходит в никуда и возвращает «ок».
        uuid = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        for canon in BOTS:
            sid = service_id(canon)
            if sid is not None:
                self.assertRegex(sid, uuid, f"{canon}: service_id не UUID")


class TestCoderGates(unittest.TestCase):
    """coder.py не импортируется (урок #70 — правится только вручную); читаем текст."""

    @classmethod
    def setUpClass(cls):
        cls.src = open(CODER, encoding="utf-8").read()
        cls.tree = ast.parse(cls.src)

    def _func(self, name: str) -> str:
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return ast.get_source_segment(self.src, node) or ""
        self.fail(f"{name} не найдена в coder.py")

    def test_lookup_asks_all_projects_not_one(self):
        body = self._func("railway_get_service_id")
        self.assertIn("projects {", body,
                      "поиск сервиса снова ограничен одним проектом — "
                      "боты отделов станут ненаходимыми")

    def test_lookup_uses_the_registry_candidates(self):
        self.assertIn("railway_service_names", self._func("railway_get_service_id"))

    def test_deploy_resolves_by_bot_not_only_by_repo(self):
        m = re.search(r'elif intent == "deploy":(.{0,1600})', self.src, re.S)
        self.assertIsNotNone(m, "ветка deploy не найдена — тест устарел")
        self.assertIn("bots_named_in", m.group(1),
                      "имя бота снова теряется по дороге в репозиторий")

    def test_failure_names_what_was_tried(self):
        # Инвариант 8: провал называет того, кто упал. Прежний текст советовал
        # искать опечатку в названии репозитория — там, где её не было.
        m = re.search(r'elif intent == "deploy":(.{0,3200})', self.src, re.S)
        body = m.group(1)
        self.assertIn("Искал по именам", body)
        self.assertNotIn("Проверь название репозитория", body)


if __name__ == "__main__":
    unittest.main()

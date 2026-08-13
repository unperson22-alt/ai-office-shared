"""
Гвард на запись production-переменных Railway из агентной петли Силли.

Появился 2026-08-12: у Силли не было действия «записать переменную», и на его
месте модель выдумывала нехватку токена (урок #92 / Active Vulnerabilities).
Действие добавили — вместе с запретом трогать секреты, потому что модель не
может проверить валидность нового токена, а неверный кладёт сервис молча.

Запуск: cd ai-office-shared && python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared.railway_vars import (  # noqa: E402
    SET_VAR_FORBIDDEN, set_var_allowed,
)


class TestSetVarGuard(unittest.TestCase):
    def test_ordinary_config_is_writable(self):
        # Ровно тот случай, ради которого действие и заводилось.
        for name in ("ALLOWED_USERS", "OFFICE_CHAT_ID", "OLLAMA_MODEL",
                     "BANTER_CHANCE", "PORT", "SCREENER_GATE"):
            ok, why = set_var_allowed(name)
            self.assertTrue(ok, f"{name} должна быть разрешена, отказ: {why}")

    def test_secrets_are_refused(self):
        for name in ("TELEGRAM_TOKEN", "ANTHROPIC_API_KEY", "GH_PAT",
                     "HTTP_SECRET", "WEBHOOK_SECRET", "REDIS_URL",
                     "DATABASE_URL", "OFFICE_RPC_TOKEN", "RAILWAY_TOKEN_VLAD"):
            ok, _ = set_var_allowed(name)
            self.assertFalse(ok, f"{name} — секрет, писать нельзя")

    def test_refusal_explains_itself(self):
        ok, why = set_var_allowed("TELEGRAM_TOKEN")
        self.assertFalse(ok)
        self.assertIn("TOKEN", why)
        self.assertTrue(why.strip(), "отказ обязан объяснять причину")

    def test_case_and_whitespace_do_not_bypass(self):
        for variant in ("telegram_token", " TELEGRAM_TOKEN ", "Telegram_Token"):
            ok, _ = set_var_allowed(variant)
            self.assertFalse(ok, f"{variant!r} не должен обходить гвард")

    def test_substring_match_catches_prefixed_and_suffixed(self):
        for name in ("MY_TOKEN_V2", "LEGACY_API_KEY_OLD", "X_SECRET"):
            ok, _ = set_var_allowed(name)
            self.assertFalse(ok, f"{name} содержит запретную подстроку")

    def test_empty_name_refused(self):
        for bad in ("", "   ", None):
            ok, why = set_var_allowed(bad)
            self.assertFalse(ok)
            self.assertIn("пуст", why.lower())

    def test_forbidden_list_is_uppercase(self):
        # Сравнение идёт с UPPERCASE-именем: строчная запись в списке молча
        # перестала бы срабатывать.
        for entry in SET_VAR_FORBIDDEN:
            self.assertEqual(entry, entry.upper(), f"{entry!r} должен быть в UPPERCASE")


if __name__ == "__main__":
    unittest.main()

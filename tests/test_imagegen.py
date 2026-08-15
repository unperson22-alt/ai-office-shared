"""
Тесты генерации картинок (shared/imagegen.py).

Проверяется то, из-за чего фича не работала в проде: мусор в промпте,
мёртвая цепочка провайдеров и «оба провайдера недоступны» вместо причины.
Сеть не трогаем — провайдеры подменяются.
"""
import asyncio
import base64
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared import imagegen  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4000


class TestCleanRequest(unittest.TestCase):
    """Обращение к боту и командные слова не должны уезжать в модель."""

    def test_strips_address_and_command(self):
        cases = {
            "Сыш, сгенерируй мне жирную мамашу которая жрёт бургер":
                "жирную мамашу которая жрёт бургер",
            "нарисуй кота в скафандре": "кота в скафандре",
            "Можешь сделать картинку заката над морем, пожалуйста":
                "заката над морем",
            "сгенерируй картинку жирной женщины которая поедает бургеры":
                "жирной женщины которая поедает бургеры",
        }
        for text, expected in cases.items():
            self.assertEqual(imagegen.clean_request(text).lower(), expected, text)

    def test_plain_subject_survives(self):
        self.assertEqual(imagegen.clean_request("кот на подоконнике"),
                         "кот на подоконнике")

    def test_never_returns_empty(self):
        # Из «нарисуй» после чистки не остаётся ничего — отдаём исходник,
        # иначе в модель уйдёт пустая строка и вернётся случайная картинка.
        self.assertTrue(imagegen.clean_request("нарисуй"))
        self.assertEqual(imagegen.clean_request(""), "")


def _http(status=200, json_body=None, content=b"", content_type="application/json"):
    r = MagicMock()
    r.status_code = status
    r.headers = {"content-type": content_type}
    r.json.return_value = json_body or {}
    r.content = content
    r.text = str(json_body or content[:50])
    return r


def _client(responses):
    """AsyncClient-заглушка: отдаёт ответы по очереди."""
    client = MagicMock()
    seq = list(responses)
    client.post = AsyncMock(side_effect=lambda *a, **k: seq.pop(0))
    client.get = AsyncMock(side_effect=lambda *a, **k: seq.pop(0))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


class TestBuildPrompt(unittest.TestCase):

    def test_without_api_key_falls_back_to_cleaned_text(self):
        saved = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            out = asyncio.run(imagegen.build_prompt("Сыш, нарисуй кота в скафандре"))
        finally:
            if saved is not None:
                os.environ["ANTHROPIC_API_KEY"] = saved
        self.assertEqual(out, "кота в скафандре")

    def test_model_answer_becomes_the_prompt(self):
        os.environ["ANTHROPIC_API_KEY"] = "test"
        try:
            client = _client([_http(200, {"content": [
                {"type": "text", "text": "a cat in a spacesuit, studio light"}]})])
            with patch("httpx.AsyncClient", return_value=client):
                out = asyncio.run(imagegen.build_prompt("нарисуй кота в скафандре"))
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        self.assertEqual(out, "a cat in a spacesuit, studio light")

    def test_model_failure_falls_back_not_crashes(self):
        os.environ["ANTHROPIC_API_KEY"] = "test"
        try:
            client = _client([_http(500, {"error": "boom"})])
            with patch("httpx.AsyncClient", return_value=client):
                out = asyncio.run(imagegen.build_prompt("нарисуй кота"))
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        self.assertEqual(out, "кота")


class TestProviders(unittest.TestCase):

    def setUp(self):
        self.env = {k: os.environ.pop(k, None) for k in
                    ("CF_ACCOUNT_ID", "CF_API_TOKEN", "REPLICATE_API_TOKEN",
                     "ANTHROPIC_API_KEY", "POLLINATIONS_TOKEN")}

    def tearDown(self):
        for k, v in self.env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def test_cloudflare_base64_json_is_decoded(self):
        os.environ["CF_ACCOUNT_ID"] = "acc"
        os.environ["CF_API_TOKEN"] = "tok"
        body = {"success": True, "result": {"image": base64.b64encode(PNG).decode()}}
        client = _client([_http(200, body)])
        with patch("httpx.AsyncClient", return_value=client):
            data, err = asyncio.run(imagegen._cf_generate("a cat"))
        self.assertEqual(data, PNG)
        self.assertEqual(err, "")

    def test_cloudflare_binary_response_also_works(self):
        os.environ["CF_ACCOUNT_ID"] = "acc"
        os.environ["CF_API_TOKEN"] = "tok"
        client = _client([_http(200, content=PNG, content_type="image/png")])
        with patch("httpx.AsyncClient", return_value=client):
            data, err = asyncio.run(imagegen._cf_generate("a cat"))
        self.assertEqual(data, PNG)

    def test_cloudflare_daily_limit_is_reported_as_limit(self):
        os.environ["CF_ACCOUNT_ID"] = "acc"
        os.environ["CF_API_TOKEN"] = "tok"
        client = _client([_http(429, {})])
        with patch("httpx.AsyncClient", return_value=client):
            _, err = asyncio.run(imagegen._cf_generate("a cat"))
        self.assertIn("лимит", err)

    def test_pollinations_returns_bytes_not_url(self):
        # Раньше боту отдавалась ссылка, и за картинкой шёл сам Telegram —
        # на медленном ответе это падало уже вне нашего кода.
        client = _client([_http(200, content=PNG, content_type="image/jpeg")])
        with patch("httpx.AsyncClient", return_value=client):
            data, err = asyncio.run(imagegen._pollinations_generate("a cat"))
        self.assertEqual(data, PNG)
        self.assertEqual(err, "")

    def test_pollinations_rejects_empty_image(self):
        client = _client([_http(200, content=b"x" * 100, content_type="image/jpeg")])
        with patch("httpx.AsyncClient", return_value=client):
            data, err = asyncio.run(imagegen._pollinations_generate("a cat"))
        self.assertIsNone(data)
        self.assertIn("пуст", err)

    def test_replicate_without_token_is_skipped_quietly(self):
        data, err = asyncio.run(imagegen._replicate_generate("a cat"))
        self.assertIsNone(data)
        self.assertIn("токен", err)


class TestGenerateImageChain(unittest.TestCase):

    def setUp(self):
        self.env = {k: os.environ.pop(k, None) for k in
                    ("CF_ACCOUNT_ID", "CF_API_TOKEN", "ANTHROPIC_API_KEY")}

    def tearDown(self):
        for k, v in self.env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def _run(self, cf, poll, rep, request="нарисуй кота"):
        with patch.object(imagegen, "_PROVIDERS", (
                ("cloudflare", AsyncMock(return_value=cf)),
                ("pollinations", AsyncMock(return_value=poll)),
                ("replicate", AsyncMock(return_value=rep)))):
            return asyncio.run(imagegen.generate_image(request))

    def test_first_working_provider_wins(self):
        res = self._run((PNG, ""), (None, "не должен вызываться"), (None, ""))
        self.assertTrue(res.ok)
        self.assertEqual(res.provider, "cloudflare")
        self.assertEqual(res.prompt, "кота")

    def test_falls_through_to_next_provider(self):
        res = self._run((None, "Cloudflare HTTP 500"), (PNG, ""), (None, ""))
        self.assertTrue(res.ok)
        self.assertEqual(res.provider, "pollinations")

    def test_all_down_gives_reason_not_silence(self):
        res = self._run((None, "Cloudflare HTTP 500"), (None, "Pollinations HTTP 502"),
                        (None, "нет токена Replicate"))
        self.assertFalse(res.ok)
        self.assertIn("Не получилось нарисовать", res.error)
        self.assertIn("попробуй", res.error.lower())

    def test_limit_reached_says_so_explicitly(self):
        res = self._run((None, "дневной лимит Cloudflare исчерпан"),
                        (None, "Pollinations HTTP 429"), (None, "нет токена Replicate"))
        self.assertIn("лимит", res.error.lower())

    def test_missing_keys_tell_what_to_set(self):
        res = self._run((None, "нет ключей Cloudflare"), (None, "Pollinations HTTP 500"),
                        (None, "нет токена Replicate"))
        self.assertIn("CF_ACCOUNT_ID", res.error)

    def test_empty_request_is_rejected_politely(self):
        res = asyncio.run(imagegen.generate_image("   "))
        self.assertFalse(res.ok)
        self.assertIn("опиши", res.error)

    def test_result_is_ready_to_send(self):
        res = self._run((PNG, ""), (None, ""), (None, ""))
        stream = res.as_file()
        self.assertEqual(stream.getvalue(), PNG)
        self.assertIn("кота", res.caption)


if __name__ == "__main__":
    unittest.main()

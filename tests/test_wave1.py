"""
Регресс-тесты волны 2026-07-25: enhance-guard, приём картинок, dev-эскалация.

Запуск (pytest в окружении нет — только stdlib):
    cd ai-office-shared && python3 -m unittest discover -s tests -v

Каждый кейс в TestEnhanceGuard взят из реальных логов инцидента, а не выдуман.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared.prompt import (  # noqa: E402
    EnhanceResult, _is_safe_enhancement, enhance_prompt, enhance_prompt_ex, intent_hint,
)
from ai_office_shared.shared.dev_escalation import (  # noqa: E402
    parse_dev_feature_tag, strip_dev_feature_tag, request_dev_feature,
)
from ai_office_shared.shared.media import (  # noqa: E402
    ImageError, ImagePayload, MAX_IMAGE_BYTES, addressed_in_group,
    extract_image, has_image,
)


def run(coro):
    return asyncio.run(coro)


# ── Тестовые дублёры ──────────────────────────────────────────────────────────

class _Block:
    def __init__(self, text):
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]


class FakeClient:
    """AsyncAnthropic-совместимый заглушка: всегда отдаёт заранее заданный текст."""

    def __init__(self, reply):
        self._reply = reply
        self.calls = 0
        self.messages = self

    async def create(self, **kwargs):
        self.calls += 1
        if isinstance(self._reply, Exception):
            raise self._reply
        return _Resp(self._reply)


class FakeFile:
    def __init__(self, data):
        self._data = data

    async def download_as_bytearray(self):
        return bytearray(self._data)


class FakeBot:
    def __init__(self, data=b"\xff\xd8\xff\xe0binary", fail=False):
        self._data = data
        self._fail = fail

    async def get_file(self, file_id):
        if self._fail:
            raise RuntimeError("telegram down")
        return FakeFile(self._data)


class FakePhotoSize:
    def __init__(self, file_id="fid", file_size=1000):
        self.file_id = file_id
        self.file_size = file_size


class FakeDocument:
    def __init__(self, mime_type="image/png", file_name="shot.png", file_size=1000):
        self.file_id = "doc-fid"
        self.mime_type = mime_type
        self.file_name = file_name
        self.file_size = file_size


class FakeUser:
    def __init__(self, username="", is_bot=False):
        self.username = username
        self.is_bot = is_bot


class FakeMessage:
    def __init__(self, photo=None, document=None, caption=None, text=None,
                 reply_to_message=None):
        self.photo = photo or []
        self.document = document
        self.caption = caption
        self.text = text
        self.reply_to_message = reply_to_message


class FakeRedis:
    """Достаточно для taskboard.create_task: pipeline + hset/zadd/zremrangebyrank."""

    def __init__(self):
        self.hashes = {}
        self.zsets = {}
        self._ops = []

    def pipeline(self, transaction=False):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def hset(self, key, mapping=None):
        self._ops.append(("hset", key, mapping))

    def zadd(self, key, mapping):
        self._ops.append(("zadd", key, mapping))

    def zremrangebyrank(self, key, start, end):
        self._ops.append(("trim", key, start, end))

    async def execute(self):
        for op in self._ops:
            if op[0] == "hset":
                self.hashes[op[1]] = dict(op[2])
            elif op[0] == "zadd":
                self.zsets.setdefault(op[1], {}).update(op[2])
        self._ops = []
        return True


# ── 1. enhance-guard: инцидент 2026-07-25 ─────────────────────────────────────

class TestEnhanceGuard(unittest.TestCase):

    def test_rejects_question_added_to_short_address(self):
        """«Кріс» → Haiku дописал вопрос. Именно так бот «сам себе задал вопрос»."""
        ok, reason = _is_safe_enhancement("Кріс", "Кріс, що ти хотіла запитати?")
        self.assertFalse(ok)
        self.assertTrue(
            reason == "question_added" or reason.startswith("length_ratio"),
            f"unexpected reason: {reason}",
        )

    def test_rejects_question_added_at_same_length(self):
        """Изолированно: вопрос дописан, а длина в коридоре — ловит именно guard на «?»."""
        original = "посмотри что там по курсу доллара и евро на сегодня"
        ok, reason = _is_safe_enhancement(original, "Какой курс доллара и евро сегодня?")
        self.assertFalse(ok)
        self.assertEqual(reason, "question_added")

    def test_rejects_translation_of_ukrainian_task(self):
        """Реальное сообщение Яны: украинский → русский = потеря языка пользователя."""
        original = ("Є догляд за волоссям numero, зараз у них ребрендинг, "
                    "можеш порівняти стару версію шампуню з вівсом і нову")
        enhanced = ("Есть уход за волосами numero, сейчас у них ребрендинг, "
                    "можешь сравнить старую версию шампуня с овсом и новую")
        ok, reason = _is_safe_enhancement(original, enhanced)
        self.assertFalse(ok)
        self.assertEqual(reason, "uk_markers_lost")

    def test_rejects_meta_commentary(self):
        original = "сравни две версии шампуня с овсом до и после ребрендинга бренда"
        enhanced = "Вот улучшенный запрос: сравни две версии шампуня с овсом"
        ok, reason = _is_safe_enhancement(original, enhanced)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("meta_marker"))

    def test_rejects_script_switch_to_latin(self):
        original = "сравни две версии шампуня с овсом до и после ребрендинга бренда"
        enhanced = "compare two versions of the oat shampoo before and after rebranding"
        ok, _ = _is_safe_enhancement(original, enhanced)
        self.assertFalse(ok)

    def test_rejects_length_blowup(self):
        original = "сравни две версии шампуня с овсом до и после ребрендинга бренда"
        enhanced = original + " " + ("подробно и по пунктам, " * 20)
        ok, reason = _is_safe_enhancement(original, enhanced)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("length_ratio"))

    def test_accepts_legitimate_russian_refinement(self):
        """Регрессия: нормальное уточнение на том же языке должно проходить."""
        original = "посмотри что там по курсу доллара и евро на сегодня примерно"
        enhanced = "Найди актуальный курс доллара и евро к гривне на сегодня"
        ok, reason = _is_safe_enhancement(original, enhanced)
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "ok")

    def test_accepts_ukrainian_refinement_keeping_language(self):
        original = ("Є догляд за волоссям numero, зараз у них ребрендинг, "
                    "можеш порівняти стару версію шампуню з вівсом і нову")
        enhanced = ("Порівняй стару та нову версію шампуню з вівсом бренду numero "
                    "після ребрендингу")
        ok, reason = _is_safe_enhancement(original, enhanced)
        self.assertTrue(ok, reason)

    def test_short_message_never_calls_model(self):
        client = FakeClient("что угодно")
        res = run(enhance_prompt_ex("Кріс", client))
        self.assertFalse(res.used)
        self.assertEqual(res.reason, "too_short")
        self.assertEqual(res.enhanced, "Кріс")
        self.assertEqual(client.calls, 0)

    def test_long_message_untouched(self):
        client = FakeClient("что угодно")
        long_text = "а" * 500
        res = run(enhance_prompt_ex(long_text, client))
        self.assertFalse(res.used)
        self.assertEqual(res.reason, "too_long")
        self.assertEqual(client.calls, 0)

    def test_string_api_returns_original_on_bad_enhancement(self):
        """Ключевое: боты, не тронувшие call-site, тоже защищены."""
        original = ("Є догляд за волоссям numero, зараз у них ребрендинг, "
                    "можеш порівняти стару версію шампуню з вівсом і нову")
        client = FakeClient("Вот улучшенный запрос: сравни шампуни numero")
        out = run(enhance_prompt(original, client))
        self.assertEqual(out, original)

    def test_api_error_returns_original(self):
        original = "посмотри что там по курсу доллара и евро на сегодня примерно"
        client = FakeClient(RuntimeError("429"))
        res = run(enhance_prompt_ex(original, client))
        self.assertFalse(res.used)
        self.assertEqual(res.reason, "api_error")
        self.assertEqual(res.enhanced, original)

    def test_intent_hint_empty_when_unused(self):
        self.assertEqual(intent_hint(EnhanceResult("a", "a", False, "too_short")), "")
        hint = intent_hint(EnhanceResult("a", "уточнение", True, "ok"))
        self.assertIn("уточнение", hint)
        self.assertIn("приоритет всегда у дословного сообщения", hint)


# ── 2. Приём картинок: дефект «молчит на картинку» ────────────────────────────

class TestMedia(unittest.TestCase):

    def test_has_image_detects_document_image(self):
        self.assertTrue(has_image(FakeMessage(document=FakeDocument("image/png"))))
        self.assertTrue(has_image(FakeMessage(photo=[FakePhotoSize()])))
        self.assertFalse(has_image(FakeMessage(document=FakeDocument("application/pdf"))))
        self.assertFalse(has_image(FakeMessage(text="привет")))

    def test_photo_extracted_as_jpeg(self):
        msg = FakeMessage(photo=[FakePhotoSize()], caption="что тут?")
        res = run(extract_image(msg, FakeBot()))
        self.assertIsInstance(res, ImagePayload)
        self.assertEqual(res.media_type, "image/jpeg")
        self.assertEqual(res.source, "photo")
        self.assertEqual(res.caption, "что тут?")

    def test_document_png_extracted(self):
        """Скриншот файлом — раньше молча пропадал у всех ботов."""
        msg = FakeMessage(document=FakeDocument("image/png", "screenshot.png"))
        res = run(extract_image(msg, FakeBot(b"\x89PNG\r\n")))
        self.assertIsInstance(res, ImagePayload)
        self.assertEqual(res.media_type, "image/png")
        self.assertEqual(res.source, "document")

    def test_document_mime_falls_back_to_extension(self):
        msg = FakeMessage(document=FakeDocument(mime_type="", file_name="pic.WEBP"))
        res = run(extract_image(msg, FakeBot()))
        self.assertIsInstance(res, ImagePayload)
        self.assertEqual(res.media_type, "image/webp")

    def test_heic_rejected_with_user_message(self):
        msg = FakeMessage(document=FakeDocument("image/heic", "IMG_1.heic"))
        res = run(extract_image(msg, FakeBot()))
        self.assertIsInstance(res, ImageError)
        self.assertIn("heic", res.reason)
        self.assertTrue(res.user_message)

    def test_non_image_document_rejected(self):
        msg = FakeMessage(document=FakeDocument("application/pdf", "doc.pdf"))
        res = run(extract_image(msg, FakeBot()))
        self.assertIsInstance(res, ImageError)
        self.assertEqual(res.reason, "not_an_image")

    def test_oversized_rejected_before_download(self):
        doc = FakeDocument("image/png", "big.png", file_size=MAX_IMAGE_BYTES + 1)
        res = run(extract_image(FakeMessage(document=doc), FakeBot()))
        self.assertIsInstance(res, ImageError)
        self.assertTrue(res.reason.startswith("too_large"))

    def test_download_failure_returns_user_message_not_silence(self):
        msg = FakeMessage(photo=[FakePhotoSize()])
        res = run(extract_image(msg, FakeBot(fail=True)))
        self.assertIsInstance(res, ImageError)
        self.assertEqual(res.reason, "download_failed")
        self.assertTrue(res.user_message)

    def test_empty_message_never_raises(self):
        res = run(extract_image(None, FakeBot()))
        self.assertIsInstance(res, ImageError)


class TestAddressedInGroup(unittest.TestCase):

    def test_reply_to_bot(self):
        reply = FakeMessage(text="я тут")
        reply.from_user = FakeUser(username="kriss_bot", is_bot=True)
        msg = FakeMessage(photo=[FakePhotoSize()], reply_to_message=reply)
        self.assertTrue(addressed_in_group(msg, "kriss_bot", "Крис"))

    def test_username_mention(self):
        msg = FakeMessage(text="@kriss_bot глянь")
        self.assertTrue(addressed_in_group(msg, "kriss_bot", "Крис"))

    def test_name_in_caption(self):
        msg = FakeMessage(photo=[FakePhotoSize()], caption="Крис, что тут написано?")
        self.assertTrue(addressed_in_group(msg, "kriss_bot", "Крис"))

    def test_unrelated_message(self):
        msg = FakeMessage(text="ребята, кто пойдёт обедать")
        self.assertFalse(addressed_in_group(msg, "kriss_bot", "Крис"))

    def test_no_text_no_address(self):
        msg = FakeMessage(photo=[FakePhotoSize()])
        self.assertFalse(addressed_in_group(msg, "kriss_bot", "Крис"))


# ── 3. Dev-эскалация ──────────────────────────────────────────────────────────

class TestDevEscalation(unittest.TestCase):

    def test_parse_valid_tag(self):
        text = "Передал в разработку. [DEV_FEATURE:напоминания по утрам через cron]"
        self.assertEqual(parse_dev_feature_tag(text),
                         "напоминания по утрам через cron")

    def test_parse_ignores_empty_body(self):
        self.assertIsNone(parse_dev_feature_tag("[DEV_FEATURE: ]"))
        self.assertIsNone(parse_dev_feature_tag("нет тега"))
        self.assertIsNone(parse_dev_feature_tag(""))

    def test_parse_ignores_unclosed_tag(self):
        self.assertIsNone(parse_dev_feature_tag("[DEV_FEATURE:без закрывающей"))

    def test_strip_removes_tag_and_trims(self):
        text = "Передал в разработку. [DEV_FEATURE:утренние напоминания]"
        self.assertEqual(strip_dev_feature_tag(text), "Передал в разработку.")

    def test_strip_handles_multiple_tags(self):
        text = "[DEV_FEATURE:раз] ответ [DEV_FEATURE:два]"
        self.assertEqual(strip_dev_feature_tag(text), "ответ")

    def test_request_creates_taskboard_task_for_dev_dept(self):
        redis = FakeRedis()
        task_id = run(request_dev_feature(
            redis, "Билли", "Yodka", "утренние напоминания по расписанию"))
        self.assertIsNotNone(task_id)
        stored = list(redis.hashes.values())
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["assignee"], "dev-dept")
        self.assertEqual(stored[0]["status"], "open")
        self.assertIn("утренние напоминания", stored[0]["title"])

    def test_request_calls_notify(self):
        redis = FakeRedis()
        seen = []

        async def notify(text):
            seen.append(text)

        run(request_dev_feature(redis, "Билли", "Yodka", "что-то", notify=notify))
        self.assertEqual(len(seen), 1)
        self.assertIn("Билли", seen[0])

    def test_notify_failure_does_not_raise(self):
        redis = FakeRedis()

        async def notify(text):
            raise RuntimeError("telegram down")

        task_id = run(request_dev_feature(redis, "Билли", "Yodka", "что-то", notify=notify))
        self.assertIsNotNone(task_id)

    def test_no_redis_does_not_raise(self):
        task_id = run(request_dev_feature(None, "Билли", "Yodka", "что-то"))
        self.assertIsNone(task_id)

    def test_empty_description_is_noop(self):
        self.assertIsNone(run(request_dev_feature(FakeRedis(), "Билли", "Yodka", "   ")))


# ── 4. spawn: удержание фоновых задач ────────────────────────────────────────

class TestSpawn(unittest.TestCase):

    def test_spawn_keeps_reference_until_done(self):
        from ai_office_shared.shared.tasks import _BACKGROUND_TASKS, spawn

        async def scenario():
            done = asyncio.Event()

            async def work():
                await asyncio.sleep(0)
                done.set()

            task = spawn(work(), name="test-loop")
            self.assertIn(task, _BACKGROUND_TASKS)
            await done.wait()
            await task
            await asyncio.sleep(0)
            self.assertNotIn(task, _BACKGROUND_TASKS)

        run(scenario())

    def test_spawn_logs_and_survives_failure(self):
        from ai_office_shared.shared.tasks import spawn

        async def scenario():
            async def boom():
                raise RuntimeError("loop died")

            task = spawn(boom(), name="dying-loop")
            with self.assertRaises(RuntimeError):
                await task

        run(scenario())


if __name__ == "__main__":
    unittest.main()

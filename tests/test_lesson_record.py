"""
Аудит записал в вечный архив собственный сломанный вывод — 25.08.2026.

Вечерний аудит впервые нашёл «новые паттерны багов» и записал два урока сам:

  #123 «Truncated traceback ... buffer limits in Railway logging», дата
       2025-04-10 — это была НЕ проблема Railway: аудит прочитал собственную
       строку Силли из её же логов (обезглавленный трейсбек, урок #123) и
       записал сбой офиса как чужую платформенную беду;
  #124 `telegram.error.NetworkError: httpx.ConnectError:` у molly-trader, дата
       2025-01-10 — ровно тот класс сбоя, о котором монитор нарочно молчит:
       обе строки дословно лежат в EXTERNAL_FAULT_PATTERNS.

Оба ушли в группу ПО ДВА РАЗА с разницей в четыре секунды.

История коммитов читается буквально и приложена к тестам как улика:
lesson(123) в 20:01:27 → lesson(124) в 20:01:33, где #123 ВСЁ ЕЩЁ pending
(пометки не случилось — публикация не увидела собственной записи) → два
отдельных «mark 2 posted_to_group» в 20:01:38 и 20:01:42 с разными posted_at.

Запуск: cd ai-office-shared && python3 -m pytest tests/test_lesson_record.py -q
"""
import ast
import asyncio
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ai_office_shared.shared.lesson_record import (  # noqa: E402
    REQUIRED_TEXT, claim_publish, documented_fix, invented_date, is_closed,
    lesson_by_id, normalize, publish_claim_key, release_publish, today_iso,
)

CODER = os.path.join(ROOT, "agents", "coder.py")

# Дословно то, что модель вернула 25.08 (даты — её выдумка).
CILLY_123 = {
    "id": 123, "date": "2025-04-10", "ts": "2025-04-10",
    "bot": "deep_diagnose", "layer": "railway",
    "title": "Truncated traceback in logs prevents root cause diagnosis",
    "symptom": "deep_diagnose reports 'Traceback fully truncated' with no actionable error details",
    "root_cause": "Log output is cut off mid-traceback due to buffer limits in Railway logging",
    "fix": "Increase Railway log buffer size, stream full traceback to stderr directly",
    "prevention": "Always write error logs to both stderr and a persistent file",
    "posted_to_group": True,
}


class FakeRedis:
    """SET NX c TTL и DELETE — ровно то, на чём держится замок."""

    def __init__(self, broken=False):
        self.store, self.broken = {}, broken

    async def set(self, key, val, nx=False, ex=None):
        if self.broken:
            raise RuntimeError("redis down")
        if nx and key in self.store:
            return None
        self.store[key] = val
        return True

    async def delete(self, key):
        if self.broken:
            raise RuntimeError("redis down")
        self.store.pop(key, None)


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class TestDateIsNeverAskedOfTheModel(unittest.TestCase):
    def test_the_invented_date_is_overwritten_by_the_clock(self):
        out = normalize(CILLY_123, lesson_id=123)
        self.assertEqual(out["date"], today_iso())
        self.assertNotEqual(out["date"], "2025-04-10")

    def test_the_ts_field_the_prompt_asked_for_is_dropped(self):
        # Промпт просил поле `ts`, схема архива его не знает — и он приехал.
        self.assertNotIn("ts", normalize(CILLY_123, lesson_id=123))

    def test_the_model_cannot_set_id_or_the_published_flag(self):
        out = normalize(CILLY_123, lesson_id=200)
        self.assertEqual(out["id"], 200)          # не 123 из ответа модели
        self.assertFalse(out["posted_to_group"])  # не True из ответа модели

    def test_kind_is_stamped_so_the_schema_holds(self):
        self.assertEqual(normalize(CILLY_123, lesson_id=1)["kind"], "lesson")

    def test_invented_date_names_the_offender(self):
        # Для записей, приехавших мимо normalize — а #123 и #124 приехали так.
        self.assertEqual(invented_date(CILLY_123), "2025-04-10")
        self.assertEqual(invented_date({"date": today_iso()}), "")


class TestEmptyLessonIsRefused(unittest.TestCase):
    def test_a_lesson_without_content_never_reaches_the_file(self):
        with self.assertRaises(ValueError):
            normalize({"title": "x"}, lesson_id=1)

    def test_placeholder_text_counts_as_empty(self):
        thin = dict(CILLY_123, symptom="n/a")
        with self.assertRaises(ValueError) as cm:
            normalize(thin, lesson_id=1)
        self.assertIn("symptom", str(cm.exception))

    def test_the_refusal_names_every_missing_field(self):
        with self.assertRaises(ValueError) as cm:
            normalize({}, lesson_id=1)
        for field in REQUIRED_TEXT:
            self.assertIn(field, str(cm.exception))

    def test_a_non_object_is_refused_by_type(self):
        with self.assertRaises(ValueError):
            normalize("не JSON", lesson_id=1)


class TestPublishLock(unittest.TestCase):
    """Замок, а не флаг в git: гарантия нужна ЧТЕНИЮ, а не файлу."""

    def test_second_attempt_on_the_same_lesson_is_refused(self):
        r = FakeRedis()
        self.assertTrue(run(claim_publish(r, 123)))
        self.assertFalse(run(claim_publish(r, 123)))

    def test_the_real_double_publish_would_now_be_blocked(self):
        # Тот самый заход: два вызова публикации подряд, каждый прочитал файл,
        # где #123 и #124 ещё pending.
        r = FakeRedis()
        first = [lid for lid in (123, 124) if run(claim_publish(r, lid))]
        second = [lid for lid in (123, 124) if run(claim_publish(r, lid))]
        self.assertEqual(first, [123, 124])
        self.assertEqual(second, [], "второй заход не имеет права отправить те же уроки")

    def test_different_lessons_do_not_block_each_other(self):
        r = FakeRedis()
        self.assertTrue(run(claim_publish(r, 123)))
        self.assertTrue(run(claim_publish(r, 124)))

    def test_lock_is_fail_closed_without_redis(self):
        # Намеренно НЕ как dedup.claim_answer: там молчание дороже дубля,
        # здесь дубль в вечном архиве дороже отложенной записи.
        self.assertFalse(run(claim_publish(None, 123)))

    def test_lock_is_fail_closed_when_redis_errors(self):
        self.assertFalse(run(claim_publish(FakeRedis(broken=True), 123)))

    def test_release_lets_a_failed_send_retry(self):
        r = FakeRedis()
        self.assertTrue(run(claim_publish(r, 123)))
        run(release_publish(r, 123))
        self.assertTrue(run(claim_publish(r, 123)))

    def test_key_is_namespaced_per_lesson(self):
        self.assertNotEqual(publish_claim_key(118), publish_claim_key(119))
        self.assertTrue(publish_claim_key(118).startswith("office:lesson:"))


class TestCoderGates(unittest.TestCase):
    """coder.py не импортируется — правки проверяем по AST."""

    @classmethod
    def setUpClass(cls):
        with open(CODER, encoding="utf-8") as f:
            cls.src = f.read()
        cls.tree = ast.parse(cls.src)

    def _fn(self, name):
        node = next((n for n in ast.walk(self.tree)
                     if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                     and n.name == name), None)
        self.assertIsNotNone(node, f"{name} не найдена в coder.py")
        return ast.unparse(node)      # комментарии отброшены — остаётся код

    def test_the_prompt_no_longer_asks_the_model_for_a_date(self):
        code = self._fn("append_lesson_ai")
        self.assertNotIn("with today's date", code)
        self.assertIn("normalize_lesson(", code)

    def test_the_audit_scan_refuses_external_faults(self):
        code = self._fn("run_daily_audit")
        self.assertIn("classify_fault(errs)", code)

    def test_the_audit_scan_requires_evidence(self):
        self.assertIn("failure_evidence(errs)", self._fn("run_daily_audit"))

    def test_the_audit_scan_skips_cillys_own_logs(self):
        # Её вывод не имеет права стать её же входом.
        self.assertIn("SELF_REPOS", self._fn("run_daily_audit"))

    def test_publication_claims_the_lock_before_sending(self):
        code = self._fn("publish_pending_lessons")
        self.assertIn("claim_publish(", code)
        self.assertIn("release_publish(", code)

    def test_resync_is_reachable_without_the_telegram_command(self):
        # #118 и #119 пролежали сломанными двое суток: переиздание было
        # написано, но запустить его могла ровно одна пара рук.
        self.assertIn("'resync_lessons'", self._fn("handle_natural_language"))
        self.assertIn("resync_lessons=переиздать", self.src)

    def test_resync_over_http_is_dry_run_unless_confirmed(self):
        code = self._fn("handle_natural_language")
        i = code.index("'resync_lessons'")
        self.assertIn("confirm", code[i:i + 900])


if __name__ == "__main__":
    unittest.main()


class TestLessonStatusGate(unittest.TestCase):
    """Совпадение с уроком — улика, что проблема ИЗВЕСТНА, а не что починена.

    26.08.2026 монитор нашёл у villy-bot урок #8 и ответил «Новых действий не
    требуется (фикс уже задокументирован)». #8 несёт статус `still_relevant`,
    названные рядом #64 и #95 — `open`, а в самом villy-bot фикса не было
    вовсе: голый `Application.builder().token(...).build()`, ни таймаутов, ни
    ретрая, — при том что office-dashboard тот же сбой переживал благодаря
    ровно этому фиксу.
    """

    ARCHIVE = [
        {"id": 8, "status": "still_relevant", "fix": "Raise timeouts to 30s. Add exponential backoff"},
        {"id": 43, "status": "fixed", "fix": "get_me in a retry loop"},
        {"id": 64, "status": "open", "fix": "Exponential backoff with a max ceiling"},
        {"id": 118, "status": "fixed", "fix": "whole-file window"},
    ]

    def test_lesson_8_does_not_silence_the_office(self):
        self.assertFalse(is_closed(lesson_by_id(self.ARCHIVE, 8)))

    def test_a_genuinely_fixed_lesson_still_silences_it(self):
        self.assertTrue(is_closed(lesson_by_id(self.ARCHIVE, 43)))

    def test_a_missing_status_is_not_closed(self):
        # Молчание — не «починено».
        self.assertFalse(is_closed({"id": 999}))
        self.assertFalse(is_closed(None))

    def test_lookup_matches_the_whole_number(self):
        # Инвариант офиса №7: #11 не живёт внутри #118.
        self.assertIsNone(lesson_by_id(self.ARCHIVE, 11))
        self.assertEqual(lesson_by_id(self.ARCHIVE, 118)["id"], 118)

    def test_lookup_survives_a_junk_id(self):
        self.assertIsNone(lesson_by_id(self.ARCHIVE, None))
        self.assertIsNone(lesson_by_id(self.ARCHIVE, "восемь"))

    def test_the_documented_fix_is_readable_as_a_spec(self):
        # До 26.08 это поле не читал НИКТО: ветка писала заметку и делала
        # continue, поэтому архив умел объяснять баг человеку и ничего не
        # умел сообщить конвейеру починки.
        self.assertIn("backoff", documented_fix(lesson_by_id(self.ARCHIVE, 8)))
        self.assertEqual(documented_fix({}), "")

    def test_the_real_archive_would_not_have_silenced_villy(self):
        # Не выдумка теста: читаем настоящий lessons.json офиса.
        with open(os.path.join(ROOT, "lessons", "lessons.json"), encoding="utf-8") as f:
            real = json.load(f)
        for lid in (8, 64, 95):
            lesson = lesson_by_id(real, lid)
            self.assertIsNotNone(lesson, f"урок #{lid} пропал из архива")
            self.assertFalse(
                is_closed(lesson),
                f"#{lid} числится закрытым — тест инцидента больше не про то",
            )


class TestCoderLessonGate(unittest.TestCase):
    """Проверки coder.py по AST — импортировать его нельзя."""

    def _monitor(self):
        with open(CODER, encoding="utf-8") as f:
            src = f.read()
        node = next(n for n in ast.walk(ast.parse(src))
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "monitor_loop")
        return ast.unparse(node)

    def test_the_gate_reads_the_lesson_status(self):
        self.assertIn("lesson_is_closed(lesson)", self._monitor())

    def test_the_redis_key_no_longer_claims_the_fix_was_applied(self):
        code = self._monitor()
        self.assertIn("lesson_seen", code)
        self.assertNotIn("lesson_applied", code)

    def test_the_documented_fix_reaches_the_analyzer(self):
        self.assertIn("source_code + known_fix_hint", self._monitor())

    def test_the_hint_is_reset_per_service(self):
        # Иначе подсказка от урока предыдущего бота уехала бы в спеку следующего.
        code = self._monitor()
        self.assertLess(code.index("for service_id"), code.index("known_fix_hint = ''"))
        self.assertLess(code.index("known_fix_hint = ''"),
                        code.index("source_code + known_fix_hint"))

    def test_search_lessons_looks_the_record_up_in_the_file(self):
        with open(CODER, encoding="utf-8") as f:
            src = f.read()
        node = next(n for n in ast.walk(ast.parse(src))
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "search_lessons")
        # Статус берётся из файла, а не из пересказа модели, назвавшей номер.
        self.assertIn("lesson_by_id(lessons", ast.unparse(node))

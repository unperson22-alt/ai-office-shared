"""
Болталка: качество реплик, а не проводка.

Проводка (кого зовём, дошёл ли пинг, потолок глубины) закреплена в
test_inter_bot.py и test_orchestration_phase2.py. Здесь — то, что определяет,
получится ли из пингов разговор:

  1. Званый видит ЧАТ, а не только сообщение Влада. Раньше в промт уезжал
     голый trigger_text — реплика человека. Ответ основного агента, ради
     которого болталку и запускают, до званых не доходил вовсе, и первая волна
     отвечала Владу хором.
  2. Внутри одной волны второй бот видит, что сказал первый. Правило «не
     повторяй уже сказанное» стояло в промте с самого начала, но выполнить его
     было нечем: обоим уходил один и тот же текст, собранный до цикла.
  3. Обрезка по границе слова. `text[:200]` рубил реплику на полуслове, и
     коллега реагировал на огрызок.
  4. Чужое имя. 16.08 Милли получила адресованное Билли «Билли, ты спиздел» и
     ответила так, будто Билли — это она.
  5. Потолок частоты. BANTER_CHANCE отвечает «шуметь ли», но не «не шумели ли
     мы только что»: на 0.9 три сообщения подряд давали три всплеска подряд.
  6. «Ага» не затравка для второй волны.

Запуск:
    cd ai-office-shared && python3 -m unittest discover -s tests -v
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared import banter as b  # noqa: E402


def run(coro):
    """См. пояснение в test_inter_bot.run — чужой event loop мы за собой убираем."""
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


class FakeRedis:
    """
    Минимум, который трогает болталка: дедуп нити, замок потолка и лента группы.
    Лента лежит списком JSON-строк в том же формате, что пишет group_history.
    """

    def __init__(self, history=None):
        self.members: set[str] = set()
        self.locks: dict[str, str] = {}
        self._history = list(history or [])   # [(кто, что)], старые → новые

    # ── дедуп нити ────────────────────────────────────────────────────────────
    async def smembers(self, key):
        return set(self.members)

    async def sadd(self, key, *values):
        self.members.update(values)

    async def expire(self, key, ttl):
        pass

    # ── замок потолка частоты ─────────────────────────────────────────────────
    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.locks:
            return None
        self.locks[key] = value
        return True

    # ── лента группы ──────────────────────────────────────────────────────────
    async def lrange(self, key, start, stop):
        import json
        newest_first = [
            json.dumps({"from": who, "text": text}, ensure_ascii=False)
            for who, text in reversed(self._history)
        ]
        return newest_first[start:stop + 1]

    async def get(self, key):
        return None          # сводки нет — get_context здесь не используется


def fake_client(replies, seen):
    """
    Подменяет httpx.AsyncClient. `replies` — что боты «отвечают» по порядку
    вызовов, `seen` — сюда складываем отправленные payload'ы.
    """
    class _Resp:
        status_code = 200

        def __init__(self, text):
            self._text = text

        def json(self):
            return {"response": self._text}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            payload = kw.get("json") or {}
            seen.append(payload)
            idx = len(seen) - 1
            return _Resp(replies[idx] if idx < len(replies) else replies[-1])

    return _Client


class _Patched:
    """Подмена httpx/sleep/кулдауна на время одного прогона."""

    def __init__(self, client, cooldown=0):
        self.client = client
        self.cooldown = cooldown

    def __enter__(self):
        self._orig = (b.httpx.AsyncClient, b.asyncio.sleep, b.BANTER_COOLDOWN)
        b.httpx.AsyncClient = self.client

        async def _nosleep(_):
            pass

        b.asyncio.sleep = _nosleep
        b.BANTER_COOLDOWN = self.cooldown
        return self

    def __exit__(self, *a):
        b.httpx.AsyncClient, b.asyncio.sleep, b.BANTER_COOLDOWN = self._orig
        return False


class TestClip(unittest.TestCase):
    """Обрезка по границе слова, а не по счётчику байт."""

    def test_short_text_is_untouched(self):
        self.assertEqual(b.clip("коротко", 100), "коротко")

    def test_does_not_cut_mid_word(self):
        text = "я же говорил что деплой упадёт " * 20
        out = b.clip(text, 60)
        self.assertLessEqual(len(out), 61)          # +символ многоточия
        self.assertTrue(out.endswith("…"))
        self.assertNotIn("упа…", out)               # ровно тот огрызок из логов

    def test_collapses_newlines(self):
        # Транскрипт — «Кто: что» построчно; перенос внутри реплики ломает разметку.
        self.assertEqual(b.clip("первая\nвторая\n\nтретья"), "первая вторая третья")

    def test_monster_word_is_still_cut(self):
        out = b.clip("я" * 300, 50)
        self.assertLessEqual(len(out), 51)


class TestStripSelfPrefix(unittest.TestCase):
    """Имя автора подставляем мы; подпись модели — задвоение."""

    def test_removes_own_name(self):
        self.assertEqual(b.strip_self_prefix("Милли: ага, и не говори", "МИЛЛИ"),
                         "ага, и не говори")

    def test_removes_decorated_name(self):
        self.assertEqual(b.strip_self_prefix("**Милли**: ну да", "МИЛЛИ"), "ну да")

    def test_keeps_somebody_elses_name(self):
        # «Билли, ты спиздел» от Милли — это обращение, а не подпись.
        self.assertEqual(b.strip_self_prefix("Билли: ты опять всё сломал", "МИЛЛИ"),
                         "Билли: ты опять всё сломал")

    def test_keeps_plain_reply(self):
        self.assertEqual(b.strip_self_prefix("ну и ладно", "МИЛЛИ"), "ну и ладно")


class TestBuildMessage(unittest.TestCase):
    """Промт: транскрипт + правила, и никто не отвечает сам себе."""

    def test_transcript_is_in_chronological_order(self):
        msg = b.build_message([("Влад", "деплой упал"), ("Билли", "уже чиню")])
        self.assertLess(msg.index("Влад: деплой упал"), msg.index("Билли: уже чиню"))

    def test_names_the_last_speaker(self):
        msg = b.build_message([("Влад", "деплой упал"), ("Билли", "уже чиню")])
        self.assertIn("автор: Билли", msg)

    def test_rules_survive(self):
        msg = b.build_message([("Влад", "привет")])
        self.assertIn("ОДНА строка", msg)
        self.assertIn("не отвечай за того, кому она адресована", msg)

    def test_drops_targets_own_trailing_line(self):
        # pick() ленту не читает и может позвать того, кто минуту назад писал сам.
        msg = b.build_message([("Влад", "деплой упал"), ("Милли", "я предупреждала")],
                              target="МИЛЛИ")
        self.assertIn("автор: Влад", msg)
        self.assertNotIn("я предупреждала", msg)

    def test_empty_transcript_still_gives_a_prompt(self):
        msg = b.build_message([])
        self.assertIn("Болталка", msg)
        self.assertIn("ОДНА строка", msg)


class TestFirstWaveSeesTheChat(unittest.TestCase):
    """
    Главный дефект качества: званые видели реплику Влада и не видели ответа
    основного агента. Отсюда «хор в одну сторону» уже на первой волне.
    """

    def _fanout(self, history, trigger="а нормально вообще?"):
        seen = []
        with _Patched(fake_client(["ну такое"], seen)):
            run(b.fanout(FakeRedis(history), primary_agent="БИЛЛИ",
                         trigger_text=trigger, sender="Влад", chance=1.0,
                         pool=["МИЛЛИ"]))
        return seen

    def test_primary_answer_reaches_the_invited_bot(self):
        seen = self._fanout([("Влад", "а нормально вообще?"),
                             ("Билли", "нормально, я проверил")])
        self.assertIn("Билли: нормально, я проверил", seen[0]["message"])

    def test_trigger_text_is_not_duplicated_when_already_in_history(self):
        # Филли пишет сообщение человека в ленту сама — без дедупа строка задваивалась.
        seen = self._fanout([("Влад", "а нормально вообще?")])
        self.assertEqual(seen[0]["message"].count("а нормально вообще?"), 1)

    def test_trigger_text_is_used_when_history_is_empty(self):
        seen = self._fanout([])
        self.assertIn("Влад: а нормально вообще?", seen[0]["message"])


class TestSenderMatchesTheTranscript(unittest.TestCase):
    """
    `sender` принимающий бот кладёт в свой промт как «[от X]». Если X — Влад, а
    последняя строка транскрипта от Билли, бот получает указание говорить с
    человеком поверх реплики коллеги. Это и есть «хор в сторону Влада».
    """

    def test_sender_is_the_last_speaker_not_the_one_who_started_the_burst(self):
        seen = []
        with _Patched(fake_client(["ну такое"], seen)):
            run(b.fanout(FakeRedis([("Влад", "деплой упал?"),
                                    ("Билли", "упал, уже поднимаю")]),
                         primary_agent="БИЛЛИ", trigger_text="деплой упал?",
                         sender="Влад", chance=1.0, pool=["МИЛЛИ"]))
        self.assertEqual(seen[0]["sender"], "Билли")

    def test_falls_back_to_the_human_when_nobody_answered_yet(self):
        seen = []
        with _Patched(fake_client(["ну такое"], seen)):
            run(b.fanout(FakeRedis(), primary_agent="БИЛЛИ",
                         trigger_text="деплой упал?", sender="Влад",
                         chance=1.0, pool=["МИЛЛИ"]))
        self.assertEqual(seen[0]["sender"], "Влад")

    def test_sender_and_transcript_never_disagree(self):
        seen = []
        with _Patched(fake_client(["да он с утра лежит, я в логи смотрела"], seen)):
            run(b.fanout(FakeRedis([("Влад", "что там"), ("Билли", "смотрю логи")]),
                         primary_agent="БИЛЛИ", trigger_text="что там",
                         sender="Влад", chance=1.0,
                         pool=["МИЛЛИ", "ВИЛЛИ", "ТИЛЛИ"]))
        for payload in seen:
            self.assertIn(f"автор: {payload['sender']}", payload["message"])


class TestSecondBotInTheWaveSeesTheFirst(unittest.TestCase):
    """
    «Не повторяй уже сказанное» было невыполнимо: промт собирался один раз до
    цикла, и оба бота получали идентичный текст.
    """

    def test_reply_of_the_first_is_in_the_prompt_of_the_second(self):
        seen = []
        with _Patched(fake_client(["сервер лёг, я же говорил", "и правда лёг"], seen)):
            run(b.fanout(FakeRedis(), primary_agent="БИЛЛИ", trigger_text="что там",
                         sender="Влад", chance=1.0, pool=["МИЛЛИ", "ВИЛЛИ"],
                         health_check=None))
        self.assertGreaterEqual(len(seen), 2)
        self.assertNotIn("сервер лёг", seen[0]["message"])
        self.assertIn("сервер лёг, я же говорил", seen[1]["message"])

    def test_prompts_are_not_identical(self):
        seen = []
        with _Patched(fake_client(["первая содержательная реплика"], seen)):
            run(b.fanout(FakeRedis(), primary_agent="БИЛЛИ", trigger_text="что там",
                         sender="Влад", chance=1.0, pool=["МИЛЛИ", "ВИЛЛИ"]))
        if len(seen) >= 2:
            self.assertNotEqual(seen[0]["message"], seen[1]["message"])


class TestCooldownIsACeiling(unittest.TestCase):
    """
    Класс ошибки «порог без потолка»: условие «шуметь ли» есть, ограничения
    «не шумели ли только что» нет. Лечится SET NX с TTL.
    """

    def _burst(self, redis, seen):
        return run(b.fanout(redis, primary_agent="БИЛЛИ", trigger_text="раз",
                            sender="Влад", chance=1.0, pool=["МИЛЛИ", "ВИЛЛИ"]))

    def test_second_burst_within_the_window_is_silent(self):
        redis, seen = FakeRedis(), []
        with _Patched(fake_client(["ага"], seen), cooldown=60):
            first = self._burst(redis, seen)
            second = self._burst(redis, seen)
        self.assertTrue(first)
        self.assertEqual(second, [])

    def test_zero_disables_the_ceiling(self):
        redis, seen = FakeRedis(), []
        with _Patched(fake_client(["ага"], seen), cooldown=0):
            self.assertTrue(self._burst(redis, seen))
            redis.members.clear()      # чтобы мешал только потолок, не дедуп нити
            self.assertTrue(self._burst(redis, seen))

    def test_failed_roll_does_not_burn_the_window(self):
        # Иначе на 0.9 каждый десятый промах глушил бы следующее сообщение.
        redis, seen = FakeRedis(), []
        with _Patched(fake_client(["ага"], seen), cooldown=60):
            run(b.fanout(redis, primary_agent="БИЛЛИ", trigger_text="раз",
                         chance=0.0, pool=["МИЛЛИ"]))
            self.assertTrue(self._burst(redis, seen))

    def test_second_wave_is_not_blocked_by_its_own_lock(self):
        # Замок ставит первая волна; вторая идёт внутри того же всплеска.
        seen = []
        with _Patched(fake_client(["сервер лёг, я же говорил"], seen), cooldown=60):
            run(b.fanout(FakeRedis(), primary_agent="БИЛЛИ", trigger_text="что там",
                         sender="Влад", chance=1.0, pool=["МИЛЛИ", "ВИЛЛИ", "ТИЛЛИ"]))
        self.assertIn(2, [p.get("depth") for p in seen], seen)

    def test_reason_is_logged(self):
        # Отказ без следа не отлаживается — у каждого выхода есть причина.
        events = []

        async def fake_log_event(redis_client, bot, event, **kw):
            events.append((event, kw.get("reason")))

        import ai_office_shared.shared.logging as _lg
        orig, redis, seen = _lg.log_event, FakeRedis(), []
        _lg.log_event = fake_log_event
        try:
            with _Patched(fake_client(["ага"], seen), cooldown=60):
                self._burst(redis, seen)
                self._burst(redis, seen)
        finally:
            _lg.log_event = orig
        self.assertIn(("banter_skip", "cooldown"), events)


class TestSecondWaveNeedsSomethingToAnswer(unittest.TestCase):
    """На «ага» вторая волна отвечает «ага» — это шум, а не разговор."""

    def _depths(self, reply):
        seen = []
        with _Patched(fake_client([reply], seen)):
            run(b.fanout(FakeRedis(), primary_agent="БИЛЛИ", trigger_text="что там",
                         sender="Влад", chance=1.0, pool=["МИЛЛИ", "ВИЛЛИ", "ТИЛЛИ"]))
        return [p.get("depth") for p in seen]

    def test_one_word_reply_does_not_start_a_second_wave(self):
        self.assertNotIn(2, self._depths("ага"))

    def test_substantive_reply_does(self):
        self.assertIn(2, self._depths("да он с утра лежит, я в логи смотрела"))

    def test_second_wave_sees_the_first_even_with_an_empty_history(self):
        # Лента наполняется, только когда позванный бот сам постит в группу.
        # Полагаться на то, что он успел это сделать до HTTP-ответа, нельзя —
        # первую волну вторая получает списком, а не через Redis.
        seen = []
        with _Patched(fake_client(["да он с утра лежит, я в логи смотрела"], seen)):
            run(b.fanout(FakeRedis(), primary_agent="БИЛЛИ",
                         trigger_text="что там с деплоем", sender="Влад",
                         chance=1.0, pool=["МИЛЛИ", "ВИЛЛИ", "ТИЛЛИ"]))
        wave2 = [p for p in seen if p.get("depth") == 2]
        self.assertTrue(wave2, seen)
        self.assertIn("Влад: что там с деплоем", wave2[0]["message"])

    def test_second_wave_gets_the_reply_without_the_speaker_prefix(self):
        # Иначе в транскрипте выходит «Милли: Милли: ...».
        seen = []
        with _Patched(fake_client(["Милли: да он с утра лежит, я смотрела"], seen)):
            run(b.fanout(FakeRedis(), primary_agent="БИЛЛИ", trigger_text="что там",
                         sender="Влад", chance=1.0, pool=["МИЛЛИ", "ВИЛЛИ", "ТИЛЛИ"]))
        wave2 = [p for p in seen if p.get("depth") == 2]
        self.assertTrue(wave2, seen)
        self.assertNotIn("Милли: Милли:", wave2[0]["message"])
        self.assertIn("Милли: да он с утра лежит", wave2[0]["message"])


if __name__ == "__main__":
    unittest.main()

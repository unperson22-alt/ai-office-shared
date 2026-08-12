"""
Регресс-тесты инцидента 2026-08-07 — межботовая коммуникация.

Три бага из офис-чата, каждый закреплён здесь кейсом из реальной переписки:
  1. Гослинг обратился к Билли как к «Йодке» — /task не нёс отправителя, а Влад
     и Yodka были для ботов разными людьми.
  2. Филли роутила всё на БИЛЛИ, не глядя кто кому отвечает.
  3. Болталка: Милли и Тилли реплику генерировали и молча выбрасывали, потолка
     глубины не было вовсе.

Запуск (pytest в окружении нет — только stdlib):
    cd ai-office-shared && python3 -m unittest discover -s tests -v
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared import banter  # noqa: E402
from ai_office_shared.shared import identity as ident  # noqa: E402
from ai_office_shared.shared.office import OFFICE_AGENTS  # noqa: E402


def run(coro):
    """
    asyncio.run() оставляет поток без текущего event loop, а test_verify.py
    зовёт get_event_loop() и падает, если запустился следом за нами. Свой мусор
    убираем сами, чтобы порядок тестов в discover ничего не решал.
    """
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


class TestBotIdentity(unittest.TestCase):
    """Реестр ботов: одно имя — один бот, как его ни напиши."""

    def test_every_bot_resolves_from_all_its_aliases(self):
        for canon, meta in ident.BOTS.items():
            for alias in [canon, canon.upper(), *meta["aliases"]]:
                self.assertEqual(
                    ident.canonical(alias), canon,
                    f"alias {alias!r} должен вести к {canon!r}")

    def test_two_spellings_collapse_to_one_bot(self):
        # Историческое расхождение BOT_URLS (ДИЛЛИ/КРИС) и OFFICE_AGENTS
        # (ДОКТОР/КРИСС) — именно оно и было «рассинхроном между инстансами».
        self.assertEqual(ident.canonical("ДОКТОР"), ident.canonical("ДИЛЛИ"))
        self.assertEqual(ident.canonical("КРИСС"), ident.canonical("КРИС"))
        self.assertEqual(ident.route_key("ДОКТОР"), "ДИЛЛИ")
        self.assertEqual(ident.route_key("КРИСС"), "КРИС")

    def test_unknown_name_is_none_not_a_guess(self):
        self.assertIsNone(ident.canonical("мусор"))
        self.assertIsNone(ident.route_key("мусор"))
        self.assertIsNone(ident.url("мусор"))

    def test_every_bot_has_url_and_persona(self):
        for canon, meta in ident.BOTS.items():
            self.assertTrue(meta.get("url"), f"{canon} без url")
            self.assertTrue(meta.get("persona"), f"{canon} без persona")
            self.assertTrue(meta.get("route_key"), f"{canon} без route_key")

    def test_route_keys_are_unique(self):
        keys = [m["route_key"] for m in ident.BOTS.values()]
        self.assertEqual(len(keys), len(set(keys)))

    def test_lookup_by_telegram_username(self):
        # Филли определяет адресата реплая по username автора сообщения.
        self.assertEqual(ident.bot_by_username("@gosling_friend_bot"), "гослинг")
        self.assertEqual(ident.bot_by_username("billy_vlad_bot"), "билли")
        self.assertIsNone(ident.bot_by_username("@random_stranger_bot"))


class TestPeopleIdentity(unittest.TestCase):
    """Влад и Йодка — один человек. Ровно этого бот и не знал."""

    def test_yodka_and_vlad_are_the_same_person(self):
        self.assertEqual(ident.person_by_alias("Yodka"), "влад")
        self.assertEqual(ident.person_by_alias("Йодка"), "влад")
        self.assertEqual(ident.person_by_alias("Влад"), "влад")
        self.assertEqual(ident.person_by_alias("yodka"),
                         ident.person_by_alias("влад"))

    def test_person_resolves_by_telegram_id(self):
        self.assertEqual(ident.person(391077101), "влад")
        self.assertEqual(ident.person(331989769), "лук")
        self.assertEqual(ident.person(7354462052), "ангелина")
        self.assertIsNone(ident.person(1))

    def test_luk_and_angelina_are_not_the_same_id(self):
        # Подтверждено Владом 2026-08-12: Лук 331989769, Ангелина 7354462052.
        self.assertNotEqual(ident.PEOPLE["лук"]["user_id"],
                            ident.PEOPLE["ангелина"]["user_id"])

    def test_who_is_covers_people_and_bots(self):
        self.assertEqual(ident.who_is("Yodka"), "Влад")
        self.assertEqual(ident.who_is("gosling"), "Гослинг")
        self.assertIsNone(ident.who_is("никто"))


class TestRosterPrompt(unittest.TestCase):
    """Ростер — детерминированный текст, а не сочинение Haiku по чату."""

    def test_lists_every_bot(self):
        text = ident.roster_prompt("Гослинг")
        for meta in ident.BOTS.values():
            self.assertIn(meta["display"], text)

    def test_marks_the_bot_itself(self):
        self.assertIn("← это ТЫ", ident.roster_prompt("Гослинг"))
        gosling_line = [l for l in ident.roster_prompt("Гослинг").splitlines()
                        if "← это ТЫ" in l][0]
        self.assertIn("Гослинг", gosling_line)

    def test_states_vlad_yodka_equivalence_explicitly(self):
        text = ident.roster_prompt("Билли")
        self.assertIn("Yodka", text)
        self.assertIn("ОДИН человек", text)

    def test_works_without_a_bot_name(self):
        self.assertIn("КТО ЕСТЬ КТО", ident.roster_prompt())


class TestOfficeAgentsRegistry(unittest.TestCase):
    """OFFICE_AGENTS выводится из identity — второй список больше не заведётся."""

    def test_iteration_has_one_entry_per_bot(self):
        # Промты Нэлли/Рэя печатают этот список — дубли раздули бы его.
        self.assertEqual(len(list(OFFICE_AGENTS)), len(set(OFFICE_AGENTS)))

    def test_urls_match_identity(self):
        for key, info in OFFICE_AGENTS.items():
            self.assertEqual(info["url"], ident.url(key))

    def test_legacy_spellings_still_resolve(self):
        # В других репо код делает OFFICE_AGENTS.get(name.upper()).
        self.assertIsNotNone(OFFICE_AGENTS.get("ДОКТОР"))
        self.assertIsNotNone(OFFICE_AGENTS.get("КРИСС"))
        self.assertIn("ДОКТОР", OFFICE_AGENTS)

    def test_router_is_not_callable_as_a_specialist(self):
        self.assertNotIn("ФИЛЛИ", list(OFFICE_AGENTS))

    def test_unknown_agent_is_none(self):
        self.assertIsNone(OFFICE_AGENTS.get("МУСОР"))


class TestBanter(unittest.TestCase):
    """Болталка: 1-2 реплики и затихнуть."""

    def test_stops_at_max_depth(self):
        # chance=1.0 — шанс гарантированно срабатывает, останавливает только глубина.
        pinged = run(banter.fanout(None, "БИЛЛИ", "текст",
                                   depth=banter.BANTER_MAX_DEPTH, chance=1.0))
        self.assertEqual(pinged, [])

    def test_never_pings_the_bot_that_just_answered(self):
        chosen = run(banter.pick(None, "БИЛЛИ", limit=99))
        self.assertNotIn("БИЛЛИ", chosen)

    def test_primary_excluded_by_any_spelling(self):
        # Филли отдаёт «ДИЛЛИ», болталка не должна звать Доктора отвечать себе.
        self.assertNotIn("ДИЛЛИ", run(banter.pick(None, "ДОКТОР", limit=99)))

    def test_skips_bots_already_in_this_thread(self):
        class FakeRedis:
            def __init__(self, members):
                self._m = members
            async def smembers(self, key):
                return self._m
        chosen = run(banter.pick(FakeRedis({"ГОСЛИНГ", "КРИС"}), "БИЛЛИ", limit=99))
        self.assertNotIn("ГОСЛИНГ", chosen)
        self.assertNotIn("КРИС", chosen)

    def test_skips_bots_marked_down(self):
        async def health(agent):
            return "down" if agent == "ГОСЛИНГ" else "up"
        chosen = run(banter.pick(None, "БИЛЛИ", limit=99, health_check=health))
        self.assertNotIn("ГОСЛИНГ", chosen)

    def test_pool_is_the_agreed_one(self):
        # Решение Влада: офисное ядро + Пророк, без Эллис и Силли.
        self.assertIn("ПРОРОК", banter.BANTER_POOL)
        self.assertNotIn("ЭЛЛИС", banter.BANTER_POOL)
        self.assertNotIn("СИЛЛИ", banter.BANTER_POOL)

    def test_every_pool_member_is_a_real_addressable_bot(self):
        for name in banter.BANTER_POOL:
            self.assertIsNotNone(ident.canonical(name), f"{name} нет в identity")
            self.assertIsNotNone(ident.url(name), f"{name} без url")

    def test_zero_chance_never_fires(self):
        self.assertEqual(run(banter.fanout(None, "БИЛЛИ", "t", chance=0.0)), [])

    def test_task_payload_helpers(self):
        self.assertTrue(banter.is_banter({"source": "BANTER"}))
        self.assertFalse(banter.is_banter({"source": "ФИЛЛИ"}))
        self.assertEqual(banter.depth_of({"depth": "2"}), 2)
        self.assertEqual(banter.depth_of({}), 0)
        self.assertEqual(banter.depth_of({"depth": "мусор"}), 0)

    def test_sender_is_normalised_to_a_known_name(self):
        # Ровно то поле, отсутствие которого сделало Билли «Йодкой».
        self.assertEqual(banter.sender_of({"sender": "Yodka"}), "Влад")
        self.assertEqual(banter.sender_of({"sender": "БИЛЛИ"}), "Билли")
        self.assertEqual(banter.sender_of({"sender": "HTTP"}), "")
        self.assertEqual(banter.sender_of({}), "")


if __name__ == "__main__":
    unittest.main()

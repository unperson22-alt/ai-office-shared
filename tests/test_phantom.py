"""
Детектор фантомных «отписок» (Ollama gemma3:4b отдаёт офф-персонажные заглушки).

Кейсы взяты из жалобы «Филли опять бредит»: в офис-группе появлялись сообщения
про технические работы, хотя аудит был зелёный и сервисы живы.

Главный риск здесь не пропустить заглушку, а СЪЕСТЬ живой ответ — Силли по
нескольку раз в день пишет осмысленные отчёты про деплои и техработы. Половина
тестов ниже именно про это.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared.phantom import looks_like_phantom  # noqa: E402


class TestPhantomCaught(unittest.TestCase):
    """Заглушки, которые обязаны гаситься."""

    def test_the_actual_reported_stub(self):
        self.assertTrue(looks_like_phantom(
            "🔧 Сейчас технические работы — скоро вернусь. "
            "Обычно это занимает несколько минут."))

    def test_self_unavailability_variants(self):
        for t in ("Я временно недоступен, попробуй позже",
                  "Сейчас недоступна, попробуй чуть позже",
                  "я недоступен",
                  "Скоро вернусь!"):
            self.assertTrue(looks_like_phantom(t), t)

    def test_english_variants(self):
        for t in ("Sorry, under maintenance", "Please try again later",
                  "The service is temporarily unavailable"):
            self.assertTrue(looks_like_phantom(t), t)

    def test_bare_topic_in_a_very_short_message(self):
        # Кроме отписки в сообщении ничего нет — это заглушка.
        self.assertTrue(looks_like_phantom("Ведутся технические работы"))


class TestRealAnswersSurvive(unittest.TestCase):
    """Живые ответы, которые НЕЛЬЗЯ съесть."""

    def test_silly_deploy_report_is_not_phantom(self):
        # Регрессия на первую версию детектора: она резала это как заглушку,
        # потому что смотрела только на длину и слово «техработы».
        self.assertFalse(looks_like_phantom(
            "На Railway сегодня были технические работы: два сервиса "
            "передеплоились, я проверил health у всех одиннадцати — везде 200. "
            "Ничего не упало. Напоминаю, что коммит в shared даёт около "
            "90 секунд даунтайма."))

    def test_ordinary_short_reply(self):
        for t in ("Живём, всё норм", "Готово", "Ок, сделаю"):
            self.assertFalse(looks_like_phantom(t), t)

    def test_market_analysis(self):
        self.assertFalse(looks_like_phantom(
            "По битку: сопротивление 68к, если пробьём — цель 72к. "
            "Ниже 64к сетап отменяется."))

    def test_empty_and_none(self):
        for t in ("", "   ", None):
            self.assertFalse(looks_like_phantom(t))

    def test_long_text_never_phantom(self):
        # Даже с сильным маркером: настоящая заглушка короткая.
        self.assertFalse(looks_like_phantom("скоро вернусь. " + "детали. " * 60))


if __name__ == "__main__":
    unittest.main()

"""Команда, отправленная в группе, доходит до своего обработчика.

20.09.2026 Влад ответил в Bug Lessons на сообщение урока командой
`/relink_lesson 111` — и не получил НИЧЕГО. Ни ответа, ни ошибки.

Причина: `monitor_group_responses` объявлен с фильтром
`F.chat.type.in_({"group","supergroup"})` и стоит на ~1100 строк раньше всех
команд. aiogram отдаёт событие ПЕРВОМУ подошедшему обработчику и дальше не
несёт, а монитор для человеческого сообщения делал обычный `return` — то есть
съедал его. До 20.09 ни одна команда в группе не работала.

Дороже всего это стоило `/relink_lesson`: она ЕДИНСТВЕННАЯ обязана
отправляться в группе (`if message.chat.id != BUG_LESSONS_CHAT` — id сообщения
существует только внутри своего чата), и оказалась недостижима по построению:
в группе её съедал монитор, в личке она сама отвечала «надо в группе». Вместе
с ней не работал `repost_lesson` для всего архива уроков до 18.09.

🔴 Чего тест НЕ разрешает: пропускать дальше всё подряд. Ниже монитора стоит
`@dp.message(F.text & ~F.text.startswith("/"))` — разговорный обработчик, и
пропусти монитор обычную болтовню, Силли отвечала бы на каждую реплику в
каждой группе. Поэтому второй случай здесь так же обязателен, как первый.

Функцию достаём из agents/coder.py через AST: импортировать его нельзя —
на уровне модуля читается os.environ и поднимается aiogram.
"""
import ast
import asyncio
import collections
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiogram import Dispatcher, F                                    # noqa: E402
from aiogram.dispatcher.event.bases import SkipHandler               # noqa: E402
from aiogram.types import Chat, Message, Update, User                # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODER = os.path.join(ROOT, "agents", "coder.py")
OWNER = 391077101


class _FakeBot:
    id = 1
    token = "stub"


def _monitor():
    """Настоящий monitor_group_responses из coder.py с подставленными глобалями."""
    with open(CODER, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    node = next((n for n in tree.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == "monitor_group_responses"), None)
    assert node is not None, "monitor_group_responses не найден в coder.py"
    node.decorator_list = []          # регистрируем сами, своим Dispatcher
    ns = {"recent_group_msgs": collections.deque(maxlen=50),
          "SkipHandler": SkipHandler, "bot": _FakeBot(),
          "BOT_SYSTEMS_WEB": {}, "BOT_REPOS": {},
          "Message": Message}      # аннотация вычисляется при def
    exec(compile(ast.Module(body=[node], type_ignores=[]), CODER, "exec"), ns)
    return ns["monitor_group_responses"], ns


def _dispatch(text: str, is_bot: bool = False, user_id: int = OWNER) -> list:
    """Прогнать сообщение группы через реальный Dispatcher. Вернуть, что сработало."""
    monitor, _ns = _monitor()
    dp = Dispatcher()
    hit = []

    @dp.message(F.chat.type.in_({"group", "supergroup"}))
    async def _monitor_handler(message: Message):
        hit.append("monitor")
        await monitor(message)

    @dp.message(F.text.startswith("/relink_lesson"))
    async def _command(message: Message):
        hit.append("command")

    @dp.message(F.text & ~F.text.startswith("/"))
    async def _chat(message: Message):
        hit.append("chat")

    msg = Message(message_id=1, date=datetime.datetime.now(),
                  chat=Chat(id=-1002194783850, type="supergroup"),
                  from_user=User(id=user_id, is_bot=is_bot, first_name="X"),
                  text=text)
    asyncio.run(dp.feed_update(bot=_FakeBot(),
                               update=Update(update_id=1, message=msg)))
    return hit


class TestCommandReachesHandler(unittest.TestCase):
    def test_group_command_reaches_its_handler(self):
        """Тот самый случай: /relink_lesson в группе."""
        self.assertEqual(_dispatch("/relink_lesson 111"), ["monitor", "command"])

    def test_any_human_command_gets_through(self):
        hit = _dispatch("/repost_lesson 140 confirm")
        self.assertIn("monitor", hit)
        self.assertNotIn("chat", hit)

    def test_monitor_still_sees_the_message(self):
        """Пропуск дальше не отменяет наблюдения: буфер нужен, чтобы найти
        вопрос человека перед ответом бота."""
        self.assertEqual(_dispatch("/relink_lesson 111")[0], "monitor")


class TestChatterIsStillSwallowed(unittest.TestCase):
    """🔴 Обратная сторона, без которой починка опаснее дефекта."""

    def test_plain_talk_does_not_reach_the_chat_handler(self):
        hit = _dispatch("привет, как дела")
        self.assertEqual(hit, ["monitor"],
                         "болтовня ушла дальше — Силли начнёт отвечать всем в группе")

    def test_message_that_merely_mentions_a_slash(self):
        self.assertEqual(_dispatch("посмотри в папке a/b"), ["monitor"])

    def test_empty_ish_text_is_not_a_command(self):
        self.assertEqual(_dispatch("   /не-команда-с-пробелами"), ["monitor"])


class TestBotMessagesUnchanged(unittest.TestCase):
    def test_bot_message_is_not_skipped(self):
        """Сообщения ботов монитор разбирает сам — их пропускать некуда."""
        self.assertEqual(_dispatch("готово", is_bot=True, user_id=777), ["monitor"])

    def test_bot_command_like_text_is_not_skipped(self):
        self.assertEqual(_dispatch("/status ok", is_bot=True, user_id=777), ["monitor"])


class TestSourceGuards(unittest.TestCase):
    """Структурные проверки — чтобы починку не откатили тихо."""

    @classmethod
    def setUpClass(cls):
        with open(CODER, encoding="utf-8") as f:
            cls.src = f.read()

    def test_skiphandler_is_imported(self):
        self.assertIn("from aiogram.dispatcher.event.bases import SkipHandler", self.src)

    def test_monitor_is_still_registered_before_commands(self):
        """Если монитор когда-нибудь переедет ПОСЛЕ команд, пропуск станет не
        нужен — но и тест должен об этом узнать, а не молча остаться зелёным."""
        mon = self.src.index('async def monitor_group_responses')
        cmd = self.src.index('async def cmd_relink_lesson')
        self.assertLess(mon, cmd,
                        "монитор больше не первый — пересмотреть необходимость SkipHandler")


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""
coder.py — агент Кодер
Генерирует код через Claude API и кладёт на GitHub
"""

import asyncio
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import CommandStart
from anthropic import AsyncAnthropic

from shared.github_tools import push_file, read_file, list_files

# --- Config ---
BOT_TOKEN = os.getenv("CODER_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
LESSONS_CHAT_ID = os.getenv("LESSONS_CHAT_ID")
DEFAULT_REPO = "ai-office-shared"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


async def post_lesson(title: str, symptom: str, cause: str, context: str, fix: str, how_to_avoid: str):
    """Отправить урок по багу в Bug Lessons группу."""
    if not LESSONS_CHAT_ID:
        return
    text = (
        f"📚 Урок — {title}\n\n"
        f"🔴 Симптом\n{symptom}\n\n"
        f"🔍 Причина\n{cause}\n\n"
        f"📍 Контекст\n{context}\n\n"
        f"🔧 Фикс\n{fix}\n\n"
        f"🛡️ Как избежать\n{how_to_avoid}"
    )
    try:
        await bot.send_message(chat_id=LESSONS_CHAT_ID, text=text)
    except Exception as e:
        print(f"[post_lesson] failed: {e}")

SYSTEM_PROMPT = """Ты — Кодер, агент AI-офиса. Твоя задача — писать чистый, рабочий Python код.

Когда тебя просят написать код:
- Возвращай ТОЛЬКО код, без объяснений и markdown-блоков
- Код должен быть готов к запуску
- Добавляй комментарии внутри кода где нужно

Когда тебя просят объяснить что-то — отвечай кратко и по делу.
"""


async def ask_claude(prompt: str) -> str:
    response = await claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


def parse_command(text: str) -> dict:
    """
    Парсит команду от пользователя.
    Форматы:
      /code <задача>                        → просто генерация кода
      /push <repo> <path> <задача>          → генерация + push на GitHub
      /read <repo> <path>                   → прочитать файл
      /ls <repo> [path]                     → список файлов
    """
    parts = text.strip().split(None, 1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    return {"cmd": cmd, "args": args}


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "👨‍💻 Кодер онлайн.\n\n"
        "Команды:\n"
        "/code <задача> — написать код\n"
        "/push <repo> <path> <задача> — написать и залить на GitHub\n"
        "/read <repo> <path> — прочитать файл из репо\n"
        "/ls <repo> [path] — список файлов в репо"
    )


@dp.message(F.text.startswith("/code"))
async def cmd_code(message: Message):
    task = message.text[5:].strip()
    if not task:
        await message.answer("Укажи задачу. Пример: /code скрипт для парсинга CSV")
        return

    await message.answer("⏳ Генерирую...")
    code = await ask_claude(task)
    await message.answer(f"```python\n{code}\n```", parse_mode="Markdown")


@dp.message(F.text.startswith("/push"))
async def cmd_push(message: Message):
    """
    Формат: /push <repo> <path> <задача>
    Пример: /push ai-office-shared scripts/parser.py скрипт для парсинга CSV
    """
    args = message.text[5:].strip().split(None, 2)
    if len(args) < 3:
        await message.answer(
            "Формат: /push <repo> <path> <задача>\n"
            "Пример: /push ai-office-shared scripts/parser.py скрипт для парсинга CSV"
        )
        return

    repo, path, task = args[0], args[1], args[2]

    await message.answer(f"⏳ Генерирую код для `{path}`...", parse_mode="Markdown")
    code = await ask_claude(task)

    await message.answer("📤 Загружаю на GitHub...")
    try:
        result = await push_file(repo, path, code, f"Coder: {task[:60]}")
        await message.answer(
            f"✅ {'Обновлён' if result['action'] == 'updated' else 'Создан'}: {result['url']}"
        )
    except EnvironmentError as e:
        await message.answer(f"❌ Ошибка конфигурации: {e}")
    except PermissionError as e:
        await message.answer(f"❌ Нет доступа к GitHub: {e}")
    except Exception as e:
        await message.answer(f"❌ Не удалось загрузить на GitHub: {type(e).__name__}: {e}")


@dp.message(F.text.startswith("/lesson"))
async def cmd_lesson(message: Message):
    """
    Опубликовать урок по багу в Bug Lessons группу.
    Формат: /lesson <заголовок> | <симптом> | <причина> | <контекст> | <фикс> | <как избежать>
    """
    args = message.text[7:].strip()
    parts = [p.strip() for p in args.split("|")]
    if len(parts) < 6:
        await message.answer(
            "Формат:\n/lesson Заголовок | Симптом | Причина | Контекст | Фикс | Как избежать"
        )
        return

    title, symptom, cause, context, fix, how_to_avoid = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
    await post_lesson(title, symptom, cause, context, fix, how_to_avoid)
    await message.answer("📚 Урок отправлен в Bug Lessons")


@dp.message(F.text.startswith("/read"))
async def cmd_read(message: Message):
    args = message.text[5:].strip().split(None, 1)
    if len(args) < 2:
        await message.answer("Формат: /read <repo> <path>")
        return

    repo, path = args[0], args[1]
    content = await read_file(repo, path)
    # Обрезаем если слишком длинный
    if len(content) > 3000:
        content = content[:3000] + "\n\n... (обрезано)"
    await message.answer(f"📄 `{path}`:\n```\n{content}\n```", parse_mode="Markdown")


@dp.message(F.text.startswith("/ls"))
async def cmd_ls(message: Message):
    args = message.text[3:].strip().split(None, 1)
    if not args:
        await message.answer("Формат: /ls <repo> [path]")
        return

    repo = args[0]
    path = args[1] if len(args) > 1 else ""
    files = await list_files(repo, path)

    lines = []
    for f in files:
        icon = "📁" if f["type"] == "dir" else "📄"
        lines.append(f"{icon} {f['name']}")

    await message.answer(
        f"📂 `{repo}/{path}`:\n" + "\n".join(lines),
        parse_mode="Markdown"
    )


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

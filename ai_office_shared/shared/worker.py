"""
ai_office_shared.shared.worker — рантайм воркера dev-dept.

Зачем: девви/рикки/скрибби/секки/тести были пятью почти дословными копиями одного
файла (~235 строк каждый). Различались только BOT_NAME и SYSTEM_PROMPT — 66 строк из
235, и почти все внутри промпта. Всё остальное — эфир активности, чтение файла из
GitHub, вызов Claude, три HTTP-эндпоинта, main() — совпадало символ в символ, включая
инлайн-копию `dev_activity` с комментарием «контракт = shared.dev_activity».

Копипаст такого масштаба — это не только объём: любой фикс приходилось накатывать
пять раз вручную, и он расходился (см. lessons #74–76, где один и тот же дефект нашёлся
дословно в четырёх ботах).

ИСПОЛЬЗОВАНИЕ (весь bot.py воркера):

    import asyncio
    from ai_office_shared.shared.worker import run_worker

    SYSTEM_PROMPT = \"\"\"Ты Рикки — senior code reviewer...\"\"\"

    if __name__ == "__main__":
        asyncio.run(run_worker("рикки", SYSTEM_PROMPT))

Поведение и контракты сохранены дословно: те же маршруты (/task, /health, /reply),
тот же формат ответа `{"response": ...}`, те же события log_event
(`task_received` / `response_sent`), тот же эфир `dev-dept:activity:{task_id}`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import urllib.error
import urllib.request
from urllib.parse import quote

from aiohttp import web

from .auth import office_auth_middleware
from .dev_activity import format_activity_for_prompt, publish_activity, read_activity
from .logging import log_event

logger = logging.getLogger("ai_office_shared.worker")

GH_ORG = os.getenv("GH_ORG", "unperson22-alt")
WORKER_MODEL = os.getenv("WORKER_MODEL", "claude-haiku-4-5-20251001")
# FINAL_CODE — полный файл целиком, иначе ответ обрезается на середине.
WORKER_MAX_TOKENS = int(os.getenv("WORKER_MAX_TOKENS", "8192"))

MAX_CONTEXT_CHARS = 8000
MAX_ARTIFACT_CHARS = 4000


def summarize_result(text: str, limit: int = 160) -> str:
    """Строка SUMMARY: из ответа воркера, иначе — начало ответа."""
    if not text:
        return ""
    for line in text.splitlines():
        s = line.strip()
        if s.upper().startswith("SUMMARY:"):
            return s[8:].strip()[:limit]
    return text.strip().replace("\n", " ")[:limit]


# 🔴 User-Agent ОБЯЗАН быть ASCII. urllib кодирует HTTP-заголовки в latin-1, а
# имена воркеров кириллические («девви», «рикки», «тести», «секки», «скрибби»).
# Подстановка bot_name в заголовок роняла КАЖДОЕ чтение файла с
# UnicodeEncodeError('latin-1', position 0-4) — ровно пять букв имени, — а
# fail-silent превращал падение в пустой контекст. В результате весь отдел
# писал код, ни разу не увидев ни одного файла (диагностика 31.07.2026: Девви на
# прямой вопрос о содержимом devvy-bot/bot.py отвечал «ФАЙЛА НЕТ»).
# Имя бота остаётся в логах, где кириллица безопасна.
GH_USER_AGENT = "ai-office-worker"


def gh_fetch_file(repo: str, path: str, *, bot_name: str = "worker") -> tuple:
    """(содержимое, ошибка). Пустая ошибка = успех.

    Ошибка возвращается ОТДЕЛЬНО, потому что вызывающему нужно различать два
    случая, которые раньше были неотличимы (оба давали ''):
      • файла ещё нет (404) — штатно для задачи «создай новый файл»;
      • файл есть, но прочитать не смогли — работать вслепую нельзя.
    """
    token = os.getenv("GH_PAT", "")
    if not token:
        return "", "GH_PAT не задан"
    if not repo or not path:
        return "", ""                      # нечего читать — это не ошибка
    # Путь ОБЯЗАН быть процентно-закодирован: urllib требует ASCII и в URL тоже,
    # так что нелатинское имя файла роняло бы чтение той же UnicodeEncodeError,
    # что и кириллица в заголовке (найдено при проверке фикса против живого API).
    url = (f"https://api.github.com/repos/{quote(GH_ORG, safe='')}/"
           f"{quote(repo, safe='')}/contents/{quote(path, safe='/')}")
    try:
        req = urllib.request.Request(url, headers={
            "Authorization": f"token {token}",
            "User-Agent": GH_USER_AGENT,
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        return base64.b64decode(data["content"]).decode(), ""
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "", ""                  # нового файла ещё нет — не ошибка
        logger.warning("gh_fetch_file %s/%s: HTTP %s (бот %s)", repo, path, e.code, bot_name)
        return "", f"HTTP {e.code}"
    except Exception as e:
        logger.warning("gh_fetch_file %s/%s: %s (бот %s)", repo, path, e, bot_name)
        return "", f"{type(e).__name__}: {e}"


def gh_read_file(repo: str, path: str, *, bot_name: str = "worker") -> str:
    """Совместимость со старыми вызовами: только содержимое, ошибка теряется.
    Новый код должен звать gh_fetch_file и обрабатывать ошибку явно."""
    return gh_fetch_file(repo, path, bot_name=bot_name)[0]


async def ask_claude(system_prompt: str, message: str, context: str = "") -> str:
    """Один вызов модели воркера. Контекст файла подмешивается блоком."""
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    content = message
    if context:
        content = (f"[КОНТЕКСТ КОДА]\n```python\n{context[:MAX_CONTEXT_CHARS]}\n```\n\n"
                   f"[ЗАДАЧА]\n{message}")
    resp = await client.messages.create(
        model=WORKER_MODEL,
        max_tokens=WORKER_MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": content}],
    )
    return resp.content[0].text


def build_app(bot_name: str, system_prompt: str, state: dict) -> web.Application:
    """aiohttp-приложение воркера: /task, /health, /reply."""

    async def get_redis():
        if state.get("redis") is None and os.getenv("REDIS_URL", ""):
            import redis.asyncio as aioredis
            state["redis"] = aioredis.from_url(os.environ["REDIS_URL"],
                                               decode_responses=True)
        return state.get("redis")

    async def handle_task(request):
        data = await request.json()
        message = data.get("message", "")
        user_id = data.get("user_id", 0)
        repo = data.get("repo", "")
        file_path = data.get("file_path", "")
        artifact = data.get("artifact", "")
        task_id = data.get("task_id", "")

        r = await get_redis()
        await log_event(r, bot_name, "task_received", user_id=user_id, repo=repo)
        await publish_activity(r, task_id, bot_name, "start", message[:80])

        context = data.get("context", "")
        if not context and repo:
            target = file_path or "bot.py"
            context, ctx_err = gh_fetch_file(repo, target, bot_name=bot_name)
            if ctx_err:
                # 🔴 Правка вслепую ЗАПРЕЩЕНА. Раньше воркер молча получал пустой
                # контекст и переписывал файл «по памяти». Замер 31.07: просьба
                # точечно добавить эндпоинт в devvy-bot/bot.py вернула
                # 474-символьный обрубок — без run_worker, без SYSTEM_PROMPT, с
                # неопределённым именем 'web'. Тот же механизм 01.07 подменил
                # 5766-строчный файл 8-строчным стабом и уронил Силли.
                # Отказ — единственный безопасный исход: пусть задача упадёт
                # громко, чем тихо уничтожит файл.
                msg = (f"ERROR: {bot_name} не смог прочитать {repo}/{target}: {ctx_err}. "
                       f"Правка файла вслепую запрещена — задача не выполнена.")
                logger.error("[%s] %s", bot_name, msg)
                await log_event(r, bot_name, "response_sent", user_id=user_id)
                await publish_activity(r, task_id, bot_name, "error", msg[:160])
                return web.json_response({"response": msg})

        full_message = message
        if artifact:
            full_message += (f"\n\n[РЕЗУЛЬТАТ ПРЕДЫДУЩЕГО ЭТАПА]\n"
                             f"{artifact[:MAX_ARTIFACT_CHARS]}")

        # Что делает остальная команда над той же задачей (параллельная работа).
        team = format_activity_for_prompt(await read_activity(r, task_id, limit=40),
                                          exclude_bot=bot_name)
        if team:
            full_message += f"\n\n{team}"

        try:
            response = await ask_claude(system_prompt, full_message, context=context)
        except Exception as e:
            logger.warning("[%s] ask_claude failed: %s", bot_name, e)
            response = f"ERROR: {bot_name} не смог обработать задачу: {e}"

        await log_event(r, bot_name, "response_sent", user_id=user_id)
        await publish_activity(r, task_id, bot_name, "done", summarize_result(response))
        return web.json_response({"response": response})

    async def handle_health(request):
        return web.json_response({"status": "ok", "bot": bot_name})

    async def handle_reply(request):
        data = await request.json()
        chat_id = data.get("chat_id", 0)
        text = data.get("text", "")
        if chat_id and state.get("bot"):
            await state["bot"].send_message(chat_id=chat_id, text=text)
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[office_auth_middleware])
    app.router.add_post("/task", handle_task)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/reply", handle_reply)
    return app


async def run_worker(bot_name: str, system_prompt: str) -> None:
    """Поднимает воркера и блокируется навсегда. Весь main() бота — этот вызов."""
    from telegram.ext import ApplicationBuilder

    logging.basicConfig(level=logging.INFO)
    state: dict = {"redis": None, "bot": None}

    app_tg = ApplicationBuilder().token(os.getenv("TELEGRAM_TOKEN", "")).build()
    state["bot"] = app_tg.bot

    runner = web.AppRunner(build_app(bot_name, system_prompt, state))
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)

    async with app_tg:
        await app_tg.start()
        await site.start()
        logger.info("%s запущен на порту %s", bot_name, port)
        await asyncio.Event().wait()

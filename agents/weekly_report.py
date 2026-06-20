# ─────────────────────────────────────────────────────────────────────────
# ai-office-shared/agents/weekly_report.py
#
# Еженедельный отчёт качества офиса.
# Интегрируется в agents/coder.py:
#   from agents.weekly_report import register_weekly_handlers
#   register_weekly_handlers(router, redis_client, anthropic_client)
#
# Команды:
#   /weekly  — сгенерировать и отправить отчёт (группа и личка)
#   /approve — принять предложение по промпту и запушить в GitHub
#   /skip    — пропустить предложение этой недели
#
# Требует env: GITHUB_TOKEN, YOUR_TELEGRAM_ID
# ─────────────────────────────────────────────────────────────────────────

import os
import json
import base64
import datetime
import asyncio
import httpx

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

# ── Константы ────────────────────────────────────────────────────────────────

QUALITY_THRESHOLD = 0.25      # 25%+ 👎 → "хуже нормы"
PENDING_KEY       = "office:pending_proposal"   # Redis: текущее предложение
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "")
YOUR_ID           = int(os.environ.get("YOUR_TELEGRAM_ID", "0"))

# Все боты офиса (lowercase = Redis-ключи)
OFFICE_BOTS = [
    "фили", "крисс", "эллис", "гослинг", "билли",
    "тилли", "милли", "доктор", "вилли", "силли",
    "нэлли", "рэй", "марти",
]

# Где живёт системный промпт каждого бота (репо → файл → имя константы)
# Силли дополняет этот словарь когда узнаёт структуру нового бота.
BOT_PROMPT_LOCATION = {
    "билли":   ("unperson22-alt/billy-bot",        "bot.py",          "SYSTEM"),
    "тилли":   ("unperson22-alt/tilly-bot",        "bot.py",          "SYSTEM"),
    "милли":   ("unperson22-alt/milly-bot",        "bot.py",          "SYSTEM"),
    "вилли":   ("unperson22-alt/villy-bot",        "bot.py",          "SYSTEM"),
    "крисс":   ("unperson22-alt/kriss-bot",        "bot.py",          "SYSTEM_BASE"),
    "доктор":  ("unperson22-alt/doctor-bot",       "bot.py",          "SYSTEM"),
    "гослинг": ("unperson22-alt/gosling-bot",      "bot.py",          "GOSLING_SYSTEM"),
    "эллис":   ("unperson22-alt/mama-bot",         "bot.py",          "SYSTEM_BASE"),
    "силли":   ("unperson22-alt/ai-office-shared", "agents/coder.py", "CHAT_PROMPT"),
}

# ── GitHub helpers ────────────────────────────────────────────────────────────

async def github_get(repo: str, path: str) -> tuple[str, str] | None:
    """Возвращает (содержимое файла, sha) или None."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(url, headers=headers)
        if r.status_code != 200:
            return None
        data = r.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, data["sha"]

async def github_put(repo: str, path: str, content: str, sha: str, message: str) -> bool:
    """Пушит файл в GitHub. Возвращает True при успехе."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "sha": sha,
    }
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.put(url, headers=headers, json=payload)
        return r.status_code in (200, 201)

# ── Генерация предложения по промпту ─────────────────────────────────────────

async def generate_suggestion(bot: str, ratio: float, miss_words: list[str],
                               anthropic_client) -> str | None:
    """Генерирует конкретное предложение по улучшению промпта через Haiku."""
    context = (
        f"Бот: {bot}\n"
        f"Плохих реакций (👎): {int(ratio * 100)}%\n"
        f"Частые слова в промахах роутинга: {', '.join(miss_words) if miss_words else 'нет данных'}\n"
    )
    system = (
        "Ты анализируешь качество AI-бота и предлагаешь конкретное улучшение системного промпта. "
        "Ответ строго в формате двух строк:\n"
        "Строка 1: одно предложение — в чём проблема (без воды).\n"
        "Строка 2: 'Промпт → добавить: \"<конкретная фраза для вставки в промпт>\"'\n"
        "Не пиши ничего кроме этих двух строк."
    )
    try:
        r = await anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=system,
            messages=[{"role": "user", "content": context}],
        )
        return r.content[0].text.strip()
    except Exception:
        return None

# ── Сборка отчёта ─────────────────────────────────────────────────────────────

async def build_report(redis, anthropic_client) -> str:
    week = datetime.datetime.utcnow().strftime("%G-W%V")
    lines = [f"📊 Качество офиса — неделя {week}", ""]

    # ── Quality ──────────────────────────────────────────────────────────────
    bots_data = []
    for bot in OFFICE_BOTS:
        try:
            q = await asyncio.wait_for(redis.hgetall(f"office:quality:{bot}"), timeout=2.0)
            up   = int(q.get("up",   0) or 0)
            down = int(q.get("down", 0) or 0)
        except Exception:
            up, down = 0, 0
        if up + down == 0:
            continue
        bots_data.append({"name": bot, "up": up, "down": down,
                           "ratio": down / (up + down)})

    bots_data.sort(key=lambda x: x["ratio"], reverse=True)

    lines.append("РЕАКЦИИ (только с активностью)")
    if bots_data:
        for b in bots_data:
            pct  = int(b["ratio"] * 100)
            icon = "👎" if b["ratio"] >= QUALITY_THRESHOLD else "✅"
            note = "  ← хуже нормы" if b["ratio"] >= QUALITY_THRESHOLD else ""
            lines.append(f"{icon} {b['name'].capitalize():<10} {b['down']}/{b['up']+b['down']}  ({pct}%){note}")
    else:
        lines.append("— нет данных")

    # ── Routing misses ────────────────────────────────────────────────────────
    try:
        raw = await asyncio.wait_for(redis.lrange("office:routing:misses", 0, -1), timeout=2.0)
    except Exception:
        raw = []

    miss_counts: dict[str, int]        = {}
    miss_words:  dict[str, dict[str, int]] = {}
    for m in raw:
        try:
            entry = json.loads(m)
            ag  = entry.get("agent", "").upper()
            msg = entry.get("message", "")
            miss_counts[ag] = miss_counts.get(ag, 0) + 1
            for w in msg.lower().split():
                if len(w) > 3:
                    miss_words.setdefault(ag, {})
                    miss_words[ag][w] = miss_words[ag].get(w, 0) + 1
        except Exception:
            pass

    top_misses = sorted(miss_counts.items(), key=lambda x: x[1], reverse=True)[:2]
    if top_misses:
        lines.append("")
        lines.append("ПРОМАХИ РОУТИНГА")
        for ag, cnt in top_misses:
            words = miss_words.get(ag, {})
            top3  = [w for w, _ in sorted(words.items(), key=lambda x: x[1], reverse=True)[:3]]
            wstr  = ", ".join(f'"{w}"' for w in top3) if top3 else "—"
            lines.append(f"⚠️ {ag.capitalize():<10} — {cnt} промахов  (топ: {wstr})")

    # ── Предложение по промпту ────────────────────────────────────────────────
    bad = [b for b in bots_data if b["ratio"] >= QUALITY_THRESHOLD]
    if bad:
        worst     = bad[0]
        ag_upper  = worst["name"].upper()
        top_words = [w for w, _ in sorted(
            miss_words.get(ag_upper, {}).items(), key=lambda x: x[1], reverse=True
        )[:3]]

        suggestion = await generate_suggestion(
            worst["name"], worst["ratio"], top_words, anthropic_client
        )

        if suggestion:
            lines += ["", "ПРЕДЛОЖЕНИЕ", suggestion, "",
                      "/approve — принять и запушить",
                      "/skip    — пропустить на эту неделю"]
            # Сохраняем в Redis до /approve
            await redis.set(PENDING_KEY, json.dumps({
                "bot":        worst["name"],
                "suggestion": suggestion,
                "week":       week,
            }))

    return "\n".join(lines)

# ── /approve — пушит предложение в GitHub ────────────────────────────────────

async def apply_proposal(redis) -> str:
    """Читает pending proposal, добавляет в промпт бота, пушит. Возвращает статус."""
    raw = await redis.get(PENDING_KEY)
    if not raw:
        return "⚠️ Нет активного предложения."

    proposal = json.loads(raw)
    bot        = proposal["bot"]
    suggestion = proposal["suggestion"]

    loc = BOT_PROMPT_LOCATION.get(bot)
    if not loc:
        return f"⚠️ Не знаю где живёт промпт {bot}. Добавь в BOT_PROMPT_LOCATION."

    repo, filepath, const_name = loc
    result = await github_get(repo, filepath)
    if not result:
        return f"❌ Не удалось прочитать {repo}/{filepath}"

    content, sha = result

    # Ищем конец строковой константы промпта и вставляем перед закрывающими """
    marker = f'{const_name} = """'
    if marker not in content:
        return f"❌ Константа {const_name} не найдена в файле."

    # Добавляем suggestion перед последними """ в блоке константы
    idx = content.find(marker)
    end = content.find('"""', idx + len(marker))
    if end == -1:
        return "❌ Не найден конец строки промпта."

    insert = f"\n\n{suggestion}"
    new_content = content[:end] + insert + content[end:]

    ok = await github_put(
        repo, filepath, new_content, sha,
        f"fix({bot}): weekly prompt improvement — {proposal['week']}"
    )

    if not ok:
        return "❌ Пуш не удался."

    await redis.delete(PENDING_KEY)
    return f"✅ Промпт {bot.capitalize()} обновлён. Railway задеплоит автоматически."

# ── Aiogram handlers ──────────────────────────────────────────────────────────

def register_weekly_handlers(router: Router, redis, anthropic_client):
    """Регистрирует /weekly, /approve, /skip в переданном роутере."""

    _both = F.chat.type.in_({"private", "group", "supergroup"})
    _owner = F.from_user.id == YOUR_ID

    @router.message(Command("weekly"), _both, _owner)
    async def cmd_weekly(msg: Message):
        await msg.answer("⏳ Собираю отчёт...")
        try:
            report = await build_report(redis, anthropic_client)
            # Если есть активное предложение — прикрепляем кнопки ✅/⏭
            # (callback обрабатывается единым cb_approval в coder.py, домен wk).
            kb = None
            try:
                if await redis.get(PENDING_KEY):
                    kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="✅ Применить", callback_data="wk:appr"),
                        InlineKeyboardButton(text="⏭ Пропустить", callback_data="wk:decl"),
                    ]])
            except Exception:
                kb = None
            await msg.answer(report, reply_markup=kb)
        except Exception as e:
            await msg.answer(f"❌ Ошибка: {e}")

    @router.message(Command("approve"), _both, _owner)
    async def cmd_approve(msg: Message):
        await msg.answer("⏳ Применяю...")
        result = await apply_proposal(redis)
        await msg.answer(result)

    @router.message(Command("skip"), _both, _owner)
    async def cmd_skip(msg: Message):
        await redis.delete(PENDING_KEY)
        await msg.answer("↩️ Предложение пропущено.")

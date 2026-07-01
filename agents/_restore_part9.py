        )
        return

    # Парсим упомянутых ботов из задачи
    import re as _re
    bot_mentions = _re.findall(r'@(\S+)', text)
    task_clean = _re.sub(r'@\S+', '', text).strip()

    file_specs = []
    for mention in bot_mentions:
        canon = canonical(mention.strip("@,.!?"))
        if canon and canon in BOTS:
            repo = BOTS[canon]["repo"]
            file_specs.append({"repo": repo, "path": "bot.py"})

    if not file_specs:
        await message.answer(
            f"⚠️ Не нашёл ботов в задаче. Укажи через @: `/cc {task_clean} @билли @тилли`",
            parse_mode="Markdown"
        )
        return

    repos_list = ", ".join(f"`{s['repo']}`" for s in file_specs)
    await message.answer(
        f"🤖 **CC-subagent запущен**\n\n"
        f"**Задача:** {task_clean}\n"
        f"**Файлы:** {repos_list}\n\n"
        f"⏳ Читаю файлы и генерирую изменения...",
        parse_mode="Markdown"
    )

    result = await multi_file_refactor(task_clean, file_specs,
                                        branch_suffix=bot_mentions[0] if bot_mentions else "")

    if "error" in result:
        await message.answer(f"❌ Ошибка: {result['error']}")
        return

    prs = result.get("prs", [])
    errors = result.get("errors", [])

    if not prs:
        await message.answer(f"⚠️ PR-ы не созданы.\nОшибки: {'; '.join(errors) if errors else 'нет изменений'}")
        return

    # Регистрируем PR-ы для /approve_pr + строим кнопки (по строке на PR)
    pr_lines = []
    kb_rows = []
    for item in prs:
        pr = item["pr"]
        pr_id = f"pr_{item['repo']}_{pr['number']}"
        pending_prs[pr_id] = {
            "repo": item["repo"],
            "pr_number": pr["number"],
            "branch": result["branch"],
            "html_url": pr["html_url"],
        }
        pr_lines.append(f"• [{item['repo']} #{pr['number']}]({pr['html_url']}) — {item['files']} файл(ов)")
        kb_rows.append([
            InlineKeyboardButton(text=f"✅ Мержить {item['repo']} #{pr['number']}",
                                 callback_data=f"pr:appr:{pr_id}"),
            InlineKeyboardButton(text="⏭", callback_data=f"pr:decl:{pr_id}"),
        ])

    errors_text = f"\n\n⚠️ Ошибки: {'; '.join(errors)}" if errors else ""
    await message.answer(
        f"✅ **Готово!** {result['changed_files']} файл(ов) изменено\n\n"
        f"**PR-ы:**\n" + "\n".join(pr_lines) +
        f"\n\n**Summary:** {result.get('summary','')}\n\n"
        f"Мержи кнопкой ниже (или текстом `/approve_pr {list(pending_prs.keys())[-1]}`)" +
        errors_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None,
    )


@dp.message(F.text.startswith("/approve_pr"))
async def cmd_approve_pr(message: Message):
    """
    /approve_pr <id>  — мержит PR созданный через /cc
    /approve_pr all   — мержит все pending PR-ы
    """
    arg = message.text[11:].strip()

    if arg == "all":
        targets = list(pending_prs.items())
    elif arg in pending_prs:
        targets = [(arg, pending_prs[arg])]
    else:
        await message.answer(
            f"❌ PR `{arg}` не найден.\n"
            f"Pending PR-ы: {', '.join(pending_prs.keys()) or 'нет'}",
            parse_mode="Markdown"
        )
        return

    for pr_id, pr_data in targets:
        pending_prs.pop(pr_id, None)
        try:
            ok = await merge_pull_request(pr_data["repo"], pr_data["pr_number"],
                                           commit_msg=f"cc: approved by Влад")
            status = "✅ смержен" if ok else "⚠️ не смержен (проверь конфликты)"
            await message.answer(
                f"{status}: [{pr_data['repo']} #{pr_data['pr_number']}]({pr_data['html_url']})",
                parse_mode="Markdown"
            )
        except Exception as e:
            await message.answer(f"❌ Ошибка мержа {pr_data['repo']} #{pr_data['pr_number']}: {e}")


@dp.message(F.text.startswith("/skip"))
async def cmd_skip(message: Message):
    action_id = message.text[5:].strip()
    entry = await pop_pending(action_id)
    if entry:
        task_id = entry.get("task_id", "") if isinstance(entry, dict) else ""
        if task_id:
            r = await get_redis()
            await tb.update_status(r, task_id, "rejected", result="пропущено Владом")
        await message.answer(f"⏭️ Действие `{action_id}` пропущено.")
    else:
        await message.answer(f"❌ Действие `{action_id}` не найдено.")


@dp.callback_query(F.data.startswith("pg:") | F.data.startswith("pr:") | F.data.startswith("wk:"))
async def cb_approval(cb: CallbackQuery):
    """Единый обработчик кнопок ✅/⏭ для всех предложений Силли. Только Влад."""
    owner = int(os.getenv("YOUR_TELEGRAM_ID", "0") or "0")
    if owner and cb.from_user and cb.from_user.id != owner:
        await cb.answer("Только Влад может подтверждать", show_alert=True)
        return

    parts = (cb.data or "").split(":")
    domain = parts[0] if parts else ""
    verb = parts[1] if len(parts) > 1 else ""
    ident = parts[2] if len(parts) > 2 else ""

    # ── office:pending (deploy_fix / deploy_devtask / update_instruction / delegate) ──
    if domain == "pg":
        entry = await pop_pending(ident)
        if not entry:
            await cb.answer("Уже применено или истекло", show_alert=True)
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            return
        if verb == "decl":
            task_id = entry.get("task_id", "") if isinstance(entry, dict) else ""
            if task_id:
                await tb.update_status(await get_redis(), task_id, "rejected",
                                       result="отклонено кнопкой")
            await cb.answer("Отклонено")
            await _finish_cb(cb, "⏭ Отклонено Владом")
            return
        await cb.answer("Применяю…")
        status = await _apply_pending_action(entry)
        await _finish_cb(cb, status)
        return

    # ── PR-мерж из /cc (pending_prs) ──
    if domain == "pr":
        pr_data = pending_prs.pop(ident, None)
        if not pr_data:
            await cb.answer("PR не найден или уже обработан", show_alert=True)
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            return
        if verb == "decl":
            await cb.answer("Отклонено")
            await _finish_cb(cb, f"⏭ PR {pr_data['repo']} #{pr_data['pr_number']} отклонён")
            return
        await cb.answer("Мержу…")
        try:
            ok = await merge_pull_request(pr_data["repo"], pr_data["pr_number"],
                                          commit_msg="cc: approved by Влад")
            line = (f"✅ Смержен: {pr_data['repo']} #{pr_data['pr_number']}" if ok
                    else f"⚠️ Не смержен (конфликты?): {pr_data['repo']} #{pr_data['pr_number']}")
        except Exception as e:
            line = f"❌ Ошибка мержа {pr_data['repo']} #{pr_data['pr_number']}: {e}"
        await _finish_cb(cb, line)
        return

    # ── weekly proposal (single, PENDING_KEY) ──
    if domain == "wk":
        from agents.weekly_report import apply_proposal, PENDING_KEY
        r = await get_redis()
        if verb == "decl":
            if r:
                await r.delete(PENDING_KEY)
            await cb.answer("Пропущено")
            await _finish_cb(cb, "↩️ Предложение пропущено")
            return
        await cb.answer("Применяю…")
        result = await apply_proposal(r) if r else "⚠️ Redis недоступен"
        await _finish_cb(cb, result)
        return


@dp.message(F.text.startswith("/pause"))
async def cmd_pause(message: Message):
    """Мгновенно заглушить исходящие сообщения Силли в группу (kill-switch)."""
    r = await get_redis()
    if r:
        await r.set("cilly:paused", "1")
        await message.answer("⏸ Силли поставлена на паузу: исходящие в группу подавлены. /resume — снять.")
    else:
        await message.answer("⚠️ Redis недоступен. Поставь env CILLY_PAUSED=1 в Railway для остановки.")


@dp.message(F.text.startswith("/resume"))
async def cmd_resume(message: Message):
    """Снять паузу."""
    r = await get_redis()
    if r:
        await r.delete("cilly:paused")
        await message.answer("▶️ Пауза снята. (Если стоит env CILLY_PAUSED — убери его в Railway.)")
    else:
        await message.answer("⚠️ Redis недоступен.")


@dp.message(F.text.startswith("/update_all"))
async def cmd_update_all(message: Message):
    """Обновить все template-боты по текущему BOT_TEMPLATE."""
    await message.answer("🔄 Запускаю обновление всех template-ботов...")
    async def progress(msg: str):
        await message.answer(msg)
    result = await update_all_template_bots(notify_func=progress)
    await message.answer(result)


@dp.message(F.text.startswith("/lesson"))
async def cmd_lesson(message: Message):
    args = message.text[7:].strip()
    parts = [p.strip() for p in args.split("|")]
    if len(parts) < 6:
        await message.answer("Формат:\n/lesson Title|Symptom|Cause|Context|Fix|Avoid")
        return
    await post_lesson(*parts[:6])
    await message.answer("📚 Урок отправлен в Bug Lessons")


async def migrate_lessons_to_english(reply_func, confirm: bool) -> None:
    """Контролируемый перепост Bug Lessons на английском.

    dry-run (confirm=False): только считает существующие сообщения-уроки в группе,
    НИЧЕГО не трогает. Реальный прогон (confirm=True): удаляет старые сообщения-уроки
    (русские «Урок #» и английские «Lesson #»), сбрасывает posted_to_group у всех
    уроков в lessons.json и перепубликовывает английские версии через
    publish_pending_lessons. Идемпотентно, лимит на партию — анти-флуд (урок #54).
    НЕ автозапуск: только по явной команде владельца.
    """
    import re as _re
    try:
        tg_cl = await get_telethon_client()
        messages = await tg_cl.get_messages(BUG_LESSONS_CHAT, limit=3000)
        lesson_msg_ids = [
            m.id for m in messages
            if m and m.text and _re.search(r'(?:Урок|Lesson) #\S+', m.text)
        ]
        if not confirm:
            await tg_cl.disconnect()
            await reply_func(
                f"🔎 Dry-run: в Bug Lessons найдено {len(lesson_msg_ids)} сообщений-уроков.\n"
                f"Будут удалены и перепощены на английском. Подтверди: "
                f"`/migrate_lessons_en confirm`"
            )
            return

        # 1. Удаляем существующие сообщения-уроки (батчами по 100)
        deleted = 0
        for i in range(0, len(lesson_msg_ids), 100):
            await tg_cl.delete_messages(BUG_LESSONS_CHAT, lesson_msg_ids[i:i + 100])
            deleted += len(lesson_msg_ids[i:i + 100])
            await asyncio.sleep(0.5)
        await tg_cl.disconnect()
        await reply_func(f"🧹 Удалено {deleted} старых сообщений-уроков. Перепубликую на английском...")

        # 2. Сбрасываем posted_to_group у всех уроков (durable, в git) + коммит
        raw = await read_file("ai-office-shared", LESSONS_FILE)
        lessons = json.loads(raw)
        for l in lessons:
            l["posted_to_group"] = False
            l["posted_at"] = None
        await push_file(
            "ai-office-shared", LESSONS_FILE,
            json.dumps(lessons, ensure_ascii=False, indent=2),
            "chore(lessons): reset posted flags for English re-post",
        )

        # 3. Перепост английских версий (publish_pending_lessons постит непомеченные)
        posted = await publish_pending_lessons(reply_func=reply_func)
        await reply_func(f"✅ Миграция завершена: перепощено {posted} уроков на английском.")
    except Exception as e:
        await reply_func(f"❌ Ошибка миграции уроков: {e}")


async def _lessons_en_migration_once():
    """Одноразовый авто-перепост Bug Lessons на английском (явная авторизация владельца).

    At-most-once: ставит Redis-флаг `cilly:lessons_en_migrated` ДО удаления, поэтому
    деструктивный шаг НЕ повторится при рестартах/редеплоях (защита от флуда, урок #54).
    При частичном сбое владелец дозапустит `/migrate_lessons_en confirm` (idempotent).
    """
    try:
        await asyncio.sleep(40)  # дать боту, HTTP и Telethon подняться
        r = await get_redis()
        if not r:
            logger.warning("[lessons_en_migration] Redis недоступен — пропускаю one-shot")
            return
        if await r.get("cilly:lessons_en_migrated"):
            return  # уже выполнено ранее
        await r.set("cilly:lessons_en_migrated", "1")  # фиксируем ДО удаления (at-most-once)
        logger.info("[lessons_en_migration] одноразовый перепост уроков на английском")

        async def _log(msg: str):
            try:
                await notify_office(msg)
            except Exception:
                pass

        await migrate_lessons_to_english(_log, confirm=True)
    except Exception as e:
        logger.error(f"[lessons_en_migration] failed: {e}")


@dp.message(F.text.startswith("/migrate_lessons_en"))
async def cmd_migrate_lessons_en(message: Message):
    """Перепост Bug Lessons на английском. По умолчанию dry-run; `confirm` — выполнить.
    Только владелец (YOUR_TELEGRAM_ID)."""
    owner = int(os.getenv("YOUR_TELEGRAM_ID", "0") or "0")
    if owner and message.from_user and message.from_user.id != owner:
        await message.answer("⛔ Только владелец может запускать миграцию уроков.")
        return
    confirm = "confirm" in (message.text or "").lower()
    await migrate_lessons_to_english(message.answer, confirm)



@dp.message(F.text & ~F.text.startswith("/"))
async def cmd_natural_language(message: Message):
    """Handle any non-command message as a natural language request."""
    is_dm = message.chat.type == "private"
    # Перехват GROQ API ключа
    _msg_text = message.text or ""
    if _msg_text.strip().startswith("gsk_") and len(_msg_text.strip()) > 20:
        groq_key = _msg_text.strip()
        r = await get_redis()
        if r:
            await r.set("office:secrets:groq_api_key", groq_key, ex=86400*365)
        await message.reply("✅ GROQ_API_KEY сохранён")
        try:
            tg_cl = await get_telethon_client()
            # Userbot (аккаунт Влада) может удалять свои сообщения в любом диалоге
            # В личке с ботом — ищем диалог и удаляем сообщение с ключом
            bot_entity = await tg_cl.get_entity(f"@{bot_name}")
            msgs = await tg_cl.get_messages(bot_entity, limit=10)
            to_delete = [m.id for m in msgs if m.text and groq_key in m.text]
            if to_delete:
                await tg_cl.delete_messages(bot_entity, to_delete)
            # Также удаляем ответное сообщение бота "✅ GROQ_API_KEY сохранён"
            bot_msgs = await tg_cl.get_messages(bot_entity, limit=5, from_user="me")
            # from_user="me" не работает в личке — берём последние и фильтруем
            all_msgs = await tg_cl.get_messages(bot_entity, limit=5)
            bot_replies = [m.id for m in all_msgs if m.out and "GROQ" in (m.text or "")]
            if bot_replies:
                await tg_cl.delete_messages(bot_entity, bot_replies)
            await tg_cl.disconnect()
        except Exception:
            pass
        return

    # В группе — ТОЛЬКО если сообщение начинается с имени или явного тега
    # Игнорируем если просто упоминается в середине текста (чтобы не хватать чужие разговоры)
    if not is_dm:
        txt_lower = (message.text or "").lower().strip()
        is_direct = (
            txt_lower.startswith("силли") or
            txt_lower.startswith("cilly") or
            txt_lower.startswith("@cilly")
        )
        if not is_direct:
            return

    text = message.text
    for mention in ["силли,", "силли", "cilly,", "cilly", "@cilly_bot"]:
        text = text.replace(mention, "").strip()

    user_id = message.from_user.id

    # Сохраняем сообщение в историю
    if user_id not in dm_history:
        dm_history[user_id] = []
    dm_history[user_id].append({"role": "user", "content": text})
    if len(dm_history[user_id]) > DM_HISTORY_MAX:
        dm_history[user_id] = dm_history[user_id][-DM_HISTORY_MAX:]

    _reply_buffer = []

    async def reply(msg: str):
        # Буферизуем — шлём только финальный ответ, не промежуточные статусы
        _reply_buffer.append(msg)

    await handle_natural_language(text, message.chat.id, reply, history=dm_history[user_id],
                                  proposal_chat_id=message.chat.id)

    # Шлём только последний (финальный) ответ
    if _reply_buffer:
        final = _reply_buffer[-1]
        dm_history[user_id].append({"role": "assistant", "content": final})
        await message.answer(final, parse_mode=None)


# ── HTTP endpoint for Filly routing (family bots → Cilly) ────────────────────
async def handle_cilly_task(request):
    """Filly routes natural language requests here from any bot."""
    try:
        data = await request.json()
    except Exception as parse_err:
        return web.json_response({"status": "error", "detail": f"json parse: {parse_err}"}, status=400)
    try:
        return await _handle_cilly_task_inner(data)
    except Exception as e:
        import traceback
        return web.json_response({"status": "error", "detail": str(e), "trace": traceback.format_exc()[-1000:]}, status=200)

async def _handle_cilly_task_inner(data):
    text    = data.get("message", "")
    agent   = data.get("agent", "Unknown")
    source  = data.get("source", "")
    # source=CLAUDE → полная тишина: не пишем промежуточные шаги ни в группу ни в личку
    silent  = data.get("silent", False) or source.upper() == "CLAUDE"
    # chat_id: куда слать промежуточные reply_func ответы (только если НЕ silent)
    # target_chat: явный параметр для операций (cleanup_group, post_lessons) — не зависит от silent
    chat_id     = data.get("chat_id", "") if not silent else ""
    target_chat = data.get("target_chat", "") or data.get("chat_id", "")
    # Явные repo/file_path из payload — прокидываем в планировщик (приоритетнее догадки интента)
    payload_repo = data.get("repo", "")
    payload_file = data.get("file_path", "")

    responses = []

    # /railway <gql> — ПЕРВЫЙ перехват, до LLM, не требует ANTHROPIC_API_KEY
    if text.strip().startswith("/railway"):
        gql_q = text.strip()[8:].strip()
        if not gql_q:
            return web.json_response({"status": "ok", "responses": ["Использование: /railway <graphql query>"]})
        try:
            rw_result = await railway_query(gql_q)
            out = json.dumps(rw_result.get("data") or rw_result, ensure_ascii=False, indent=2)
            if len(out) > 3000:
                out = out[:3000] + "\n...(обрезано)"
            return web.json_response({"status": "ok", "responses": [out]})
        except Exception as rw_e:
            return web.json_response({"status": "ok", "responses": [f"❌ Railway error: {rw_e}"]})
    async def collect(msg: str):
        responses.append(msg)
        # Шлём в чат ТОЛЬКО если chat_id явно передан И не silent
        if chat_id and not silent:
            try:
                await bot.send_message(chat_id=int(chat_id), text=msg, parse_mode=None)
            except Exception as e:
                logger.error(f"collect send_message failed: {e}")

    # Перехватываем GROQ API ключ — сохраняем в Redis
    if text.strip().startswith("gsk_") and len(text.strip()) > 20:
        groq_key = text.strip()
        # Сохраняем в Redis
        try:
            r_client = await get_redis()
            await r_client.set("office:config:GROQ_API_KEY", groq_key)
            redis_ok = True
        except Exception:
            redis_ok = False
        # Удаляем сообщение с ключом через Telethon
        deleted = False
        if chat_id:
            try:
                tg_cl = await get_telethon_client()
                msg_history = await tg_cl.get_messages(int(chat_id), limit=5)
                for msg in msg_history:
                    if msg.text and groq_key in msg.text:
                        await tg_cl.delete_messages(int(chat_id), [msg.id])
                        deleted = True
                        break
                await tg_cl.disconnect()
            except Exception:
                pass
        status = f"🔑 GROQ_API_KEY {'сохранён в Redis ✅' if redis_ok else '❌ Redis недоступен'}. Сообщение {'удалено 🗑' if deleted else 'не найдено'}."
        collect(status)
        responses.append(status)
        return web.json_response({"status": "ok", "responses": responses})

    await handle_natural_language(f"[{agent}] {text}", int(chat_id) if chat_id else 0, collect, silent=silent,
                                  repo_override=payload_repo, file_path_override=payload_file)
    return web.json_response({"status": "ok", "responses": responses})




async def handle_promote_bots(request):
    """Выдать права администратора списку ботов в группе."""
    data = await request.json()
    group_id = int(data.get("group_id", -5194783850))
    bots = data.get("bots", [])
    results = {}
    for username in bots:
        ok = await tg_promote_bot_admin(username, group_id)
        results[username] = "✅" if ok else "❌"
    return web.json_response({"results": results})

# ── Secrets endpoint (for Claude to read GH token without exposing in chat) ──
RAILWAY_SECRET = os.getenv("RAILWAY_TOKEN_VLAD", "") or os.getenv("RAILWAY_TOKEN", "")  # reuse existing Railway token as auth

async def handle_secrets(request):
    """Returns GH token to authenticated callers (Claude uses Railway token as key)."""
    auth = request.headers.get("X-Auth-Token", "")
    if not auth or auth != RAILWAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    return web.json_response({
        "GITHUB_TOKEN": os.getenv("GITHUB_TOKEN", ""),
        "GH_PAT": os.getenv("GH_PAT", ""),
        "RAILWAY_TOKEN_VLAD": os.getenv("RAILWAY_TOKEN_VLAD", ""),
    })

# ── Main ───────────────────────────────────────────────────────────────────────

async def handle_get_bot_token(request):
    """Get token for existing bot via BotFather."""
    data = await request.json()
    bot_username = data.get("bot_username", "").lstrip("@")
    try:
        client = await get_telethon_client()
        botfather = await client.get_entity("@BotFather")
        await client.send_message(botfather, "/mybots")
        await asyncio.sleep(2)
        msgs = await client.get_messages(botfather, limit=5)
        # Find the message with bot buttons
        import re
        for msg in msgs:
            if msg.reply_markup:
                for row in msg.reply_markup.rows:
                    for btn in row:
                        if bot_username.lower() in btn.text.lower():
                            await client.send_message(botfather, f"@{bot_username}")
                            await asyncio.sleep(2)
                            # Click API Token
                            msgs2 = await client.get_messages(botfather, limit=3)
                            for m2 in msgs2:
                                if m2.reply_markup:
                                    for row2 in m2.reply_markup.rows:
                                        for btn2 in row2:
                                            if "api token" in btn2.text.lower() or "token" in btn2.text.lower():
                                                await client.send_message(botfather, "API Token")
                                                await asyncio.sleep(2)
                                                final = await client.get_messages(botfather, limit=1)
                                                if final:
                                                    token_match = re.search(r"(\d+:[A-Za-z0-9_-]{35,})", final[0].text or "")
                                                    if token_match:
                                                        await client.disconnect()
                                                        return web.json_response({"token": token_match.group(1)})
        # Fallback: check recent BotFather messages for token pattern
        all_msgs = await client.get_messages(botfather, limit=20)
        for m in all_msgs:
            token_match = re.search(r"(\d+:[A-Za-z0-9_-]{35,})", m.text or "")
            if token_match:
                await client.disconnect()
                return web.json_response({"token": token_match.group(1), "note": "from recent history"})
        await client.disconnect()
        return web.json_response({"error": "token not found"})
    except Exception as e:
        return web.json_response({"error": str(e)})

async def handle_health(request):
    """Simple health check endpoint for external monitoring (Cloudflare Watchdog etc.)."""
    return web.json_response({"status": "ok", "service": "cilly-bot"})


# ── REACTIONS HANDLER: 👍/👎 на сообщения Силли → office:quality:силли ──────
@dp.message_reaction()
async def handle_reaction(reaction: MessageReactionUpdated):
    """Реакции на сообщения Силли — HASH up/down. Источник для feedback loop."""
    chat_id = reaction.chat.id
    msg_id  = reaction.message_id

    r = await get_redis()
    if r is None:
        return

    try:
        owner = await r.get(f"office:msg:{chat_id}:{msg_id}")
    except Exception as e:
        logger.warning(f"reaction owner lookup failed: {e}")
        return
    if owner != BOT_NAME_LOWER:
        return

    old_emojis = {x.emoji for x in (reaction.old_reaction or []) if getattr(x, "emoji", None)}

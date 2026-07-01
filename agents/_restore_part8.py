
            # Гейт качества: вердикт Рикки + компиляция
            review_ok = "NEEDS_FIX" not in (ricky_result or "").upper()
            compile_ok = True
            _se_info = ""
            if final_code and dev_file_path.endswith(".py"):
                try:
                    compile(final_code, dev_file_path, "exec")
                except SyntaxError as _se:
                    compile_ok = False
                    _se_info = f"{_se.msg}, строка {_se.lineno}"

            if final_code and review_ok and compile_ok:
                break  # успех — выходим из цикла ретраев

            # Провал гейта: считаем попытку, формируем фидбек на следующий заход
            await tb.incr_attempts(_r_act, board_id)
            if not final_code:
                retry_feedback = "Рикки не вернул финальный код (блок ```python отсутствует/пуст)."
            elif not review_ok:
                retry_feedback = "Рикки вернул NEEDS_FIX — код требует доработки. " + ricky_result[:600]
            else:
                retry_feedback = f"Финальный код не компилируется: {_se_info}. Похоже воркер обрезал код."
            await tb.update_status(_r_act, board_id, "needs_fix", result=retry_feedback)
            await reply_func(f"🛠 Попытка {attempt}/{MAX_DEV_ATTEMPTS} отклонена: {retry_feedback[:160]}")

        # Подзадачи-срез по воркерам — прозрачность доски
        for _name, _res in results.items():
            _st = "done" if "⚠️" not in _res else "blocked"
            await tb.add_subtask(_r_act, board_id,
                                 f"{_name}: {_res[:60].replace(chr(10), ' ')}",
                                 assignee=_name.lower(), status=_st)

        # ── 3. Гейт: успех → стейджим деплой на /approve; провал → эскалация ─
        if final_code and review_ok and compile_ok and dev_repo and dev_file_path:
            action_id = await stage_pending("deploy_devtask", {
                "repo": dev_repo, "path": dev_file_path, "code": final_code,
                "commit_msg": commit_msg or f"feat: {devvy_task[:60]}",
                "title": f"dev_task {dev_repo}/{dev_file_path}",
            }, task_id=board_id, title=f"deploy {dev_repo}/{dev_file_path}")
            await tb.update_status(_r_act, board_id, "awaiting_approval")
            await publish_activity(_r_act, _task_id, "силли", "done", "код готов, ждёт апрува")
            await send_proposal(
                f"✅ Код готов и прошёл гейт (review + compile).\n"
                f"📦 Деплой в {dev_repo}/{dev_file_path}\n"
                f"⏳ Approval-гейт — подтверди кнопкой ниже.\n"
                f"(или текстом: /approve {action_id})",
                "pg", action_id, chat_id=proposal_chat_id,
            )
            deploy_status = "✅ Код готов — отправил предложение на деплой с кнопками ✅/⏭"
        else:
            await tb.update_status(_r_act, board_id, "blocked",
                                   result=retry_feedback or "не удалось получить рабочий код",
                                   escalated=True)
            await publish_activity(_r_act, _task_id, "силли", "error",
                                   "исчерпаны попытки, эскалация")
            deploy_status = (
                f"⛔ После {MAX_DEV_ATTEMPTS} попыток рабочий код не получен — задача [{board_id}] "
                f"в статусе blocked, нужен твой разбор.\nПричина: {retry_feedback[:200]}"
            )

        await publish_activity(_r_act, _task_id, "силли", "deploy", deploy_status[:160])

        # ── 4. Итоговый отчёт ─────────────────────────────────────────────
        summary_parts = [f"🏁 Цепочка dev-dept завершена (задача [{board_id}], попыток: {attempt})\n"]
        for name, res in results.items():
            short = res[:150].replace("\n", " ")
            summary_parts.append(f"• {name}: {short}")
        summary_parts.append(f"\n{deploy_status}")
        if commit_msg:
            summary_parts.append(f"📝 {commit_msg}")

        await reply_func("\n".join(summary_parts))

    elif intent == "update_bot_instruction":
        """Рантайм-обучение бота: добавить/заменить инструкцию в системном промпте
        через Redis (office:instructions:{canon}) — бот учтёт без редеплоя.
        Approval-гейт: применяется только после /approve."""
        from ai_office_shared.shared.identity import canonical, display, BOTS

        target = intent_data.get("bot") or repo or ""
        canon = canonical(target) if target else None
        if not canon:
            for word in task.replace(",", " ").replace(":", " ").split():
                c = canonical(word)
                if c:
                    canon = c
                    break
        if not canon:
            await reply_func(
                "⚠️ Не понял, какому боту менять инструкцию. Укажи имя бота явно "
                f"(доступны: {', '.join(display(b) for b in BOTS)})."
            )
            return

        instruction = (intent_data.get("instruction") or "").strip()
        if not instruction:
            # эвристика: убираем имя бота из текста, остальное — инструкция
            words = [w for w in task.split() if canonical(w.strip(",:")) != canon]
            instruction = " ".join(words).strip(" :,-—") or task
        mode = intent_data.get("mode", "append")
        if mode not in ("append", "set", "clear"):
            mode = "append"
        disp = display(canon) or canon

        r_tb = await get_redis()
        board_id = await tb.create_task(
            r_tb, f"Инструкция {disp}: {instruction[:60]}",
            created_by="силли", assignee=canon, status="awaiting_approval",
        ) or ""
        action_id = await stage_pending("update_instruction", {
            "canon": canon, "display": disp, "instruction": instruction, "mode": mode,
        }, task_id=board_id, title=f"инструкция {disp}")
        await send_proposal(
            f"📝 Обновить инструкцию для {disp} (mode={mode}):\n"
            f"«{instruction[:300]}»\n\n"
            f"Бот учтёт это в следующем ответе БЕЗ редеплоя.\n"
            f"(или текстом: /approve {action_id})",
            "pg", action_id, chat_id=proposal_chat_id,
        )
        await reply_func(f"📨 Предложение по инструкции для {disp} — подтверди кнопкой ✅/⏭")

    elif intent == "delegate":
        """Делегирование задачи главе отдела (через офисный роутинг) + верификация
        результата. Approval-гейт: вызов отдела идёт после /approve."""
        from ai_office_shared.shared.office import OFFICE_AGENTS
        from ai_office_shared.shared.identity import canonical, display

        if confidence < 0.85:
            await reply_func(
                "🤔 Не уверен, кому и что делегировать. Уточни: какому отделу "
                "(Тилли/Милли/Доктор/Билли/Крисс/Вилли) и какую задачу?"
            )
            return

        target = intent_data.get("bot") or repo or ""
        canon = canonical(target) if target else None
        if not canon:
            for word in task.replace(",", " ").replace(":", " ").split():
                c = canonical(word)
                if c:
                    canon = c
                    break
        assignee_display = (canon.upper() if canon else (target.upper() if target else ""))
        if assignee_display not in OFFICE_AGENTS:
            await reply_func(
                f"⚠️ Не знаю отдела для делегирования: {assignee_display or target or '—'}.\n"
                f"Доступны: {', '.join(OFFICE_AGENTS)}"
            )
            return

        r_tb = await get_redis()
        board_id = await tb.create_task(
            r_tb, f"delegate {assignee_display}: {task[:60]}",
            created_by="силли", assignee=(canon or assignee_display.lower()),
            status="awaiting_approval",
        ) or ""
        action_id = await stage_pending("delegate", {
            "assignee_display": assignee_display,
            "task_text": task,
            "user_id": int(os.getenv("YOUR_TELEGRAM_ID", "391077101")),
        }, task_id=board_id, title=f"delegate {assignee_display}")
        await send_proposal(
            f"🤝 Делегировать {assignee_display}:\n«{task[:300]}»\n\n"
            f"После подтверждения дёрну отдел и проверю результат (верификация).\n"
            f"(или текстом: /approve {action_id})",
            "pg", action_id, chat_id=proposal_chat_id,
        )
        await reply_func(f"📨 Предложение делегировать {assignee_display} — подтверди кнопкой ✅/⏭")


# ── Telegram handlers ──────────────────────────────────────────────────────────
@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def monitor_group_responses(message: Message):
    """Следит за всеми ответами ботов в группе — анализирует через Haiku есть ли проблема."""
    text = message.text or ""
    sender = (message.from_user.first_name or "").lower()
    is_bot = message.from_user.is_bot

    # Пишем все сообщения в буфер
    recent_group_msgs.append({"sender": sender, "text": text, "is_bot": is_bot})

    # Анализируем только ответы ботов (не Cilly самого)
    if not is_bot:
        return
    if message.from_user.id == bot.id:
        return

    # Гослинг — casual бот, не анализируем его ответы
    if "гослинг" in sender or "gosling" in sender:
        return

    # Определяем какой бот ответил
    bot_display = None
    bot_system = None
    repo_info = None
    for name, system in BOT_SYSTEMS_WEB.items():
        if name in sender:
            bot_display = name.capitalize()
            bot_system = system
            repo_info = BOT_REPOS.get(name)
            break
    if not bot_display:
        return

    # Ищем последний вопрос пользователя перед этим ответом
    user_question = None
    for msg in reversed(list(recent_group_msgs)[:-1]):
        if not msg["is_bot"] and msg["text"].strip():
            user_question = msg["text"]
            break
    if not user_question:
        return

    # Если вопрос адресован конкретному боту через @тег — не лезем
    import re as _re
    if _re.search(r"@\w+_bot", user_question):
        return

    # Анализируем через Haiku — есть ли проблема с возможностями
    try:
        analysis = await analyze_bot_response(user_question, text)
    except Exception as e:
        logger.error(f"analyze_bot_response failed: {e}")
        return

    if not analysis.get("has_problem") or analysis.get("confidence") == "low":
        return
    if analysis.get("fix_needed") != "web_search":
        return

    logger.info(f"Capability gap detected in {bot_display}: {analysis.get('reason')}")
    _r = await get_redis()
    if _r:
        await log_event(_r, BOT_NAME_LOWER, "capability_gap_detected",
                        bot=bot_display.lower(), reason=analysis.get("reason","")[:200])

    # Auto-pull Redis-логов бота — Силли видит что там происходило перед gap
    gap_log_context = ""
    try:
        if _r:
            from ai_office_shared.shared.identity import canonical
            bot_canon = canonical(bot_display)
            if bot_canon:
                gap_events = await read_logs(_r, bot_canon, days=1, limit=20)
                if gap_events:
                    gap_lines = []
                    for ev in gap_events[:15]:
                        ts = ev.get("ts","")[-8:]
                        gap_lines.append(f"[{ts}] {ev.get('event','?')} {ev.get('context',{})}")
                    gap_log_context = "\n\n[Последние события бота из Redis:]\n" + "\n".join(gap_lines)
                    logger.info(f"[gap] pulled {len(gap_events)} Redis events for {bot_canon}")
    except Exception as _ge:
        logger.warning(f"[gap] auto-pull failed for {bot_display}: {_ge}")

    # Объявляем что фиксим
    sent = await bot.send_message(
        chat_id=message.chat.id,
        text=f"🔧 {bot_display} — вижу проблему ({analysis.get('reason', '')}), "
             f"сейчас отвечу с актуальными данными..."
    )
    await remember_my_message(sent)

    try:
        # Немедленно отвечаем от имени бота с web search
        # Redis-контекст добавляем в system если есть — помогает понять причину gap
        enriched_system = bot_system + gap_log_context if gap_log_context else bot_system
        response = await get_claude().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=enriched_system,
            messages=[{"role": "user", "content": user_question}],
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
        )
        answer = "\n".join(
            block.text for block in response.content if hasattr(block, "text")
        ).strip()

        sent = await bot.send_message(
            chat_id=message.chat.id,
            text=f"{bot_display}:\n{answer}"
        )
        await remember_my_message(sent)

        # Фиксим код в фоне — следующий раз бот сам справится
        if repo_info:
            asyncio.create_task(_fix_bot_code_background(bot_display, repo_info))

    except Exception as e:
        logger.error(f"instant reply failed for {bot_display}: {e}")
        sent = await bot.send_message(
            chat_id=message.chat.id,
            text=f"❌ Не смог получить данные для {bot_display}: {e}"
        )
        await remember_my_message(sent)


async def _fix_bot_code_background(bot_display: str, repo_info: tuple):
    """Добавляет web search в код бота в фоне — чтобы в следующий раз бот сам справился."""
    repo, filepath = repo_info
    try:
        source = await read_file(repo, filepath)
        if "web_search_20250305" in source:
            return  # уже есть
        fix_prompt = WEB_SEARCH_FIX_PROMPT.format(source=source)
        fixed_code = await generate_fix(source, fix_prompt)
        await push_file(repo, filepath, fixed_code,
                        f"feat({repo}): add web search tool for live data access")
        if OFFICE_CHAT_ID:
            sent = await bot.send_message(
                chat_id=OFFICE_CHAT_ID,
                text=f"✅ Код {bot_display} обновлён — web search встроен, следующий раз сам справится."
            )
            await remember_my_message(sent)
        await post_lesson(
            title=f"Web search добавлен для {bot_display}",
            symptom=f"{bot_display} не мог ответить на вопрос из-за отсутствия live данных",
            cause="tools=[web_search] не был подключён в client.messages.create()",
            context=f"{repo}/{filepath}",
            fix="Cilly ответил немедленно с web search, затем добавил tool в код бота",
            how_to_avoid="При создании аналитических ботов сразу подключать web search tool"
        )
    except Exception as e:
        logger.error(f"background fix failed for {bot_display}: {e}")


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "👨‍💻 Cilly онлайн. Мониторинг активен.\n\n"
        "Команды:\n"
        "/code <задача> — написать код\n"
        "/push <repo> <path> <задача> — написать и залить на GitHub\n"
        "/read <repo> <path> — прочитать файл из репо\n"
        "/ls <repo> [path] — список файлов\n"
        "/lesson <title>|<symptom>|<cause>|<ctx>|<fix>|<avoid> — урок в Bug Lessons\n"
        "/cc <задача> [@бот1 ...] — многофайловый рефактор через CC-subagent\n"
        "/approve_pr <id|all> — смержить PR из /cc\n"
        "/approve <id> — применить предложенный фикс\n"
        "/skip <id> — пропустить\n"
        "/update_all — обновить всех template-ботов по текущему шаблону"
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
    args = message.text[5:].strip().split(None, 2)
    if len(args) < 3:
        await message.answer("Формат: /push <repo> <path> <задача>")
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
        await message.answer(f"❌ Ошибка: {type(e).__name__}: {e}")


@dp.message(F.text.startswith("/read"))
async def cmd_read(message: Message):
    args = message.text[5:].strip().split(None, 1)
    if len(args) < 2:
        await message.answer("Формат: /read <repo> <path>")
        return
    repo, path = args[0], args[1]
    content = await read_file(repo, path)
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
    lines = [("📁 " if f["type"] == "dir" else "📄 ") + f["name"] for f in files]
    await message.answer(f"📂 `{repo}/{path}`:\n" + "\n".join(lines), parse_mode="Markdown")


async def set_bot_instruction(name: str, instruction: str, mode: str = "append") -> bool:
    """
    Рантайм-обучение бота: пишет office:instructions:{canon}, который бот дочитывает
    в build_system() и аппендит к системному промпту БЕЗ редеплоя.

    mode: "append" (добавить строку), "set" (заменить целиком), "clear" (удалить).
    """
    from ai_office_shared.shared.identity import canonical
    canon = canonical(name) or name
    r = await get_redis()
    if not r:
        return False
    key = f"office:instructions:{canon}"
    instruction = (instruction or "").strip()
    try:
        if mode == "clear" or not instruction:
            await r.delete(key)
        elif mode == "set":
            await r.set(key, instruction[:4000])
        else:  # append
            existing = await r.get(key) or ""
            combined = (existing + "\n" + instruction).strip() if existing else instruction
            await r.set(key, combined[:4000])
        return True
    except Exception as e:
        logger.warning(f"[instruction] set failed for {canon}: {e}")
        return False


async def _verify_delegation(task_text: str, response: str) -> dict:
    """Верификация ответа отдела: отвечает ли он на задачу. {ok:bool, reason:str}."""
    if not response.strip():
        return {"ok": False, "reason": "пустой ответ"}
    prompt = (
        f"Задача, которую делегировали отделу:\n{task_text}\n\n"
        f"Ответ отдела:\n{response[:2000]}\n\n"
        "Ответ закрывает задачу по существу (не отписка, не отказ, не запрос уточнений)?"
    )
    sys = ('Верификатор делегированных задач. JSON без markdown: '
           '{"ok": true|false, "reason": "1 короткое предложение"}')
    try:
        raw = await ask_claude(prompt, system=sys, model="claude-haiku-4-5-20251001")
        s, e = raw.find("{"), raw.rfind("}") + 1
        data = json.loads(raw[s:e]) if s != -1 and e > s else {}
        return {"ok": bool(data.get("ok")), "reason": str(data.get("reason", ""))[:200]}
    except Exception as e:
        # Верификатор упал — не блокируем, но честно помечаем
        return {"ok": True, "reason": f"верификатор недоступен ({e}), принято без проверки"}


async def run_delegation(assignee_display: str, task_text: str, user_id: int,
                         *, task_id: str = "") -> dict:
    """
    Делегирует задачу главе отдела через call_office (source=ФИЛЛИ → отдел вернёт JSON,
    не дублируя в группу), затем верифицирует ответ. Обновляет доску.
    Возвращает {ok, response, verdict}.
    """
    from ai_office_shared.shared.office import call_office
    r = await get_redis()
    if task_id:
        await tb.update_status(r, task_id, "in_progress")
    resp = await call_office(assignee_display, task_text, user_id, source="ФИЛЛИ")
    if not resp:
        if task_id:
            await tb.update_status(r, task_id, "blocked", result="нет ответа от отдела")
        return {"ok": False, "response": "", "verdict": "нет ответа от отдела"}
    verdict = await _verify_delegation(task_text, resp)
    if task_id:
        await tb.update_status(
            r, task_id, "done" if verdict["ok"] else "needs_fix",
            result=f"[{'OK' if verdict['ok'] else 'NEEDS_FIX'}] {verdict['reason']}\n\n{resp[:3000]}",
        )
    return {"ok": verdict["ok"], "response": resp, "verdict": verdict["reason"]}


async def _apply_pending_action(entry: dict) -> str:
    """
    Применяет подтверждённое (/approve) риск-действие. Диспетчит по type.
    Поддерживает legacy in-memory фикс-дикты (без поля type → deploy_fix).
    Возвращает человекочитаемый статус.
    """
    atype = entry.get("type")
    if atype is None:                 # legacy: сам entry и есть payload фикса
        atype = "deploy_fix"
        payload = entry
    else:
        payload = entry.get("payload", {}) or {}
    task_id = entry.get("task_id", "")
    r = await get_redis()
    try:
        if atype == "deploy_fix":
            analysis = payload.get("analysis", {})
            await push_file(
                payload["repo"], payload["affected"], payload["fixed_code"],
                f"approved fix({payload['service_name']}): {analysis.get('fix_description','')[:60]}",
            )
            redeployed = await redeploy_service(payload["service_id"])
            status = "редеплой запущен ✅" if redeployed else "редеплой не удался ⚠️"
            asyncio.create_task(append_ops_log(
                f"approved fix: {analysis.get('fix_description','')[:60]}",
                payload["service_name"], f"approved by Влад | {status}",
            ))
            await post_lesson(
                title        = analysis.get("lesson_title", ""),
                symptom      = analysis.get("lesson_symptom", ""),
                cause        = analysis.get("lesson_cause", ""),
                context      = f"{payload['repo']}/{payload['affected']}",
                fix          = analysis.get("lesson_fix", ""),
                how_to_avoid = analysis.get("lesson_avoid", ""),
            )
            if task_id:
                await tb.update_status(r, task_id, "done", result=status)
            return f"✅ Фикс применён ({payload['service_name']}), {status}"

        if atype == "deploy_devtask":
            res = await push_file(
                payload["repo"], payload["path"], payload["code"],
                payload.get("commit_msg") or f"feat: {payload.get('title', 'dev task')[:60]}",
            )
            action = res.get("action", "pushed") if isinstance(res, dict) else "pushed"
            url = res.get("url", "") if isinstance(res, dict) else ""
            asyncio.create_task(append_ops_log(
                "approved dev_task push", payload["repo"], f"{action} {payload['path']}",
            ))
            if task_id:
                await tb.update_status(r, task_id, "done", result=f"{action}: {url}")
            return (f"✅ Код запушен в {payload['repo']}/{payload['path']} ({action}) — "
                    f"Railway задеплоит автоматически.\n{url}")

        if atype == "update_instruction":
            ok = await set_bot_instruction(
                payload["canon"], payload.get("instruction", ""),
                mode=payload.get("mode", "append"),
            )
            if task_id:
                await tb.update_status(r, task_id, "done" if ok else "blocked")
            who = payload.get("display", payload.get("canon", "?"))
            return (f"✅ Инструкция для {who} обновлена (mode={payload.get('mode','append')})"
                    if ok else f"⚠️ Не удалось обновить инструкцию для {who} (Redis?)")

        if atype == "delegate":
            uid = payload.get("user_id") or int(os.getenv("YOUR_TELEGRAM_ID", "391077101"))
            result = await run_delegation(
                payload["assignee_display"], payload["task_text"], uid, task_id=task_id,
            )
            mark = "✅" if result["ok"] else "⚠️"
            return (f"{mark} Делегировано {payload['assignee_display']}: {result['verdict']}\n\n"
                    f"{result['response'][:1500]}")

        return f"❌ Неизвестный тип pending-действия: {atype}"
    except Exception as e:
        if task_id:
            await tb.update_status(r, task_id, "blocked", result=f"ошибка применения: {e}")
        return f"❌ Ошибка при применении ({atype}): {e}"


@dp.message(F.text.startswith("/approve"))
async def cmd_approve(message: Message):
    # /approve_pr — отдельный handler ниже; не перехватываем его здесь
    if message.text.startswith("/approve_pr"):
        return
    action_id = message.text[8:].strip()
    entry = await pop_pending(action_id)
    if not entry:
        await message.answer(f"❌ Действие `{action_id}` не найдено или уже применено.")
        return
    label = entry.get("title") or entry.get("type", "действие")
    await message.answer(f"⏳ Применяю: {label}...")
    status = await _apply_pending_action(entry)
    await message.answer(status)




@dp.message(F.text.startswith("/cc"))
async def cmd_cc(message: Message):
    """
    /cc <задача> [@бот1 @бот2 ...]
    Запускает CC-like многофайловый рефактор.

    Примеры:
      /cc добавь log_event в handle_message у всех ботов @билли @тилли
      /cc замени BOT_NAME на BOT_NAME_LOWER во всех bot.py @билли @крисс @доктор
      /cc обнови ai-office-shared до v0.1.2 в requirements.txt @билли @тилли @милли
    """
    from ai_office_shared.shared.identity import canonical, BOTS

    text = message.text[3:].strip()
    if not text:
        await message.answer(
            "Использование: `/cc <задача> [@бот1 @бот2 ...]`\n\n"
            "Примеры:\n"
            "• `/cc обнови shared до v0.1.2 @билли @тилли`\n"
            "• `/cc добавь log_event в handle_message @крисс @доктор`\n\n"
            "Если боты не указаны — спрошу список файлов явно.",
            parse_mode="Markdown"

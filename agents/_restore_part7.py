            from telethon.tl.types import PeerUser
            try:
                cilly = await tg_cl.get_input_entity(PeerUser(7779587562))
            except Exception:
                await tg_cl.disconnect()
                await reply_func('❌ Не могу найти диалог с Силли')
                return
            msgs = await tg_cl.get_messages(cilly, limit=50)
            to_delete = [
                m.id for m in msgs
                if m.text and any(s in m.text.lower() for s in SENSITIVE)
            ]
            if to_delete:
                await tg_cl.delete_messages(cilly, to_delete)
                await tg_cl.disconnect()
                await reply_func(f"✅ Удалено {len(to_delete)} сообщений с секретами из лички")
            else:
                await tg_cl.disconnect()
                await reply_func("✅ Секретных сообщений не найдено")
        except Exception as e:
            await reply_func(f"❌ {e}")


    elif intent == "create_cron":
        extract_prompt = f"""Из запроса извлеки параметры cron. JSON без markdown:
{{"bot":"kriss","chat_id":391077101,"schedule":"0 1 * * *","message":"текст","generate":false,"name":"kriss-daily-reminder"}}
schedule — UTC (Дананг UTC+7). Запрос: {message_text}"""
        raw = await ask_claude(extract_prompt, system="Верни только валидный JSON без markdown.", model="claude-haiku-4-5-20251001")
        try:
            raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            params = json.loads(raw)
        except Exception as e:
            await reply_func(f"❌ Не смог разобрать параметры: {e}")
            return

        bot_name_cron = params.get("bot", "kriss")
        chat_id_cron  = int(params.get("chat_id", 391077101))
        schedule      = params.get("schedule", "0 9 * * *")
        msg_cron      = params.get("message", "Напоминание")
        generate      = params.get("generate", False)
        _ts           = int(__import__('time').time()) % 100000
        cron_name     = params.get("name", f"{bot_name_cron}-cron-{_ts}")

        bot_url = f"https://{bot_name_cron}-bot-production.up.railway.app/send_scheduled"
        payload_data  = json.dumps({"chat_id": chat_id_cron, "message": msg_cron, "generate": generate})

        await reply_func(f"⏰ Создаю cron *{cron_name}*...\nРасписание: `{schedule}`\nСообщение: {msg_cron}")

        try:
            import urllib.request as _ur

            RAILWAY_TOKEN = os.getenv("RAILWAY_TOKEN_VLAD") or os.getenv("RAILWAY_TOKEN") or ""
            if not RAILWAY_TOKEN:
                await reply_func("❌ RAILWAY_TOKEN не задан в окружении.")
                return
            PROJECT_ID    = "271b40b7-199a-429a-88ef-ca417f26a638"
            ENV_ID        = "2efaaf60-ba39-492c-bf86-007fd505493f"

            def _rql(q, variables=None):
                body = {"query": q}
                if variables:
                    body["variables"] = variables
                req = _ur.Request(
                    "https://backboard.railway.app/graphql/v2",
                    data=json.dumps(body).encode(),
                    method="POST",
                    headers={"Authorization": f"Bearer {RAILWAY_TOKEN}",
                             "Content-Type": "application/json",
                             "User-Agent": "Mozilla/5.0 (compatible; railway-cli/3.0)"}
                )
                with _ur.urlopen(req) as r:
                    return json.loads(r.read())

            # 1. Создаём сервис
            d1 = _rql(f'mutation {{ serviceCreate(input: {{ projectId: "{PROJECT_ID}", name: "{cron_name}" }}) {{ id name }} }}')
            if not d1.get("data") or not d1["data"].get("serviceCreate"):
                err = d1.get("errors", [{}])[0].get("message", str(d1))
                await reply_func(f"❌ Railway serviceCreate failed: {err}")
                return
            svc_id = d1["data"]["serviceCreate"]["id"]

            # 2. Image
            _rql(f'mutation {{ serviceInstanceUpdate(serviceId: "{svc_id}", environmentId: "{ENV_ID}", input: {{ source: {{ image: "curlimages/curl:latest" }} }}) }}')

            # 3. Cron schedule
            _rql(f'mutation {{ serviceInstanceUpdate(serviceId: "{svc_id}", environmentId: "{ENV_ID}", input: {{ cronSchedule: "{schedule}" }}) }}')

            # 4. startCommand без кавычек внутри — payload через env var $P
            start_cmd = f"curl -sf -X POST {bot_url} -H Content-Type:application/json -d $P"
            _rql(f'mutation {{ serviceInstanceUpdate(serviceId: "{svc_id}", environmentId: "{ENV_ID}", input: {{ startCommand: "{start_cmd}" }}) }}')

            # 5. Env var P = payload JSON
            _rql(
                'mutation Upsert($input: VariableUpsertInput!) { variableUpsert(input: $input) }',
                variables={"input": {
                    "projectId": PROJECT_ID,
                    "environmentId": ENV_ID,
                    "serviceId": svc_id,
                    "name": "P",
                    "value": payload_data
                }}
            )

            await reply_func(f"✅ Cron создан!\n*{cron_name}*\n`{svc_id}`\nРасписание: `{schedule}` UTC\n{bot_name_cron} → {chat_id_cron}")
        except Exception as e:
            await reply_func(f"❌ Ошибка Railway: {e}")


    elif intent == "cleanup_group":
        """Удаляет старые сообщения от ботов в указанной группе через Telethon."""
        import asyncio as _asyncio
        from datetime import datetime, timezone

        # Параметры из task
        # chat_id по умолчанию — Bug Lessons
        target_chat = -5197140411
        # Удаляем всё что старше сегодняшнего дня (до 13:30 UTC 28.05.2026)
        cutoff = datetime(2026, 5, 28, 13, 30, tzinfo=timezone.utc)

        await reply_func("🧹 Чищу старые сообщения от ботов...")

        # Паттерны служебных сообщений которые всегда удаляем
        SERVICE_PATTERNS = [
            "⏸", "▶️ Силли", "🤖 Запускаю agentic", "📚 Постю",
            "✅ Завершено за", "⚠️ Достигнут лимит шагов",
            "🧹 Чищу", "✅ Удалено", "✅ Все 28",
            "🔧 *", "упал — пробую", "редеплой запущен",
            "передеплоить", "редеплой", "Запускаю agentic",
            "agentic mode", "Постю уроков", "опубликованы в Bug",
        ]

        # Определяем chat из task
        if "-5194783850" in task or "офис" in task.lower() or "office" in task.lower():
            target_chat = -5194783850
            # Проверяем указана ли дата начала ("с 29 мая", "начиная с", "from_date")
            import re as _re
            date_match = _re.search(r"(\d{4}-\d{2}-\d{2}|29.?мая|29 мая)", task)
            if date_match or "29" in task or "начиная с" in task:
                from datetime import datetime, timezone
                cutoff_mode = "from_date"
                cutoff = datetime(2026, 5, 29, 0, 0, tzinfo=timezone.utc)
            else:
                cutoff_mode = "today_patterns"
        elif "-5197140411" in task or "баг" in task.lower() or "bug" in task.lower() or "logs" in task.lower():
            target_chat = -5197140411
            if any(w in task.lower() for w in ["дубл", "dedup", "дублир", "повтор"]):
                cutoff_mode = "dedup_lessons"
            else:
                cutoff_mode = "old_bots"
        else:
            cutoff_mode = "today_patterns"  # дефолт — паттерны за сегодня

        try:
            tg_cl = await get_telethon_client()
            messages = await tg_cl.get_messages(target_chat, limit=3000)
            to_delete = []
            _lesson_map = {}  # для dedup_lessons: lesson_key -> [msg_ids]
            for msg in messages:
                if not msg or not msg.date:
                    continue
                if not msg.from_id:
                    continue

                if cutoff_mode == "today_patterns":
                    # Удаляем ВСЕ сообщения от ботов за сегодня из офисной группы
                    from datetime import date as _date, datetime as _dt, timezone as _tz
                    if msg.date.date() < _date.today():
                        continue
                    sender_id = getattr(msg.from_id, 'user_id', None)
                    if not sender_id:
                        continue
                    try:
                        user = await tg_cl.get_entity(sender_id)
                        if getattr(user, 'bot', False):
                            to_delete.append(msg.id)
                    except Exception:
                        # Если не можем получить entity — проверяем по паттернам
                        if msg.text and any(p in msg.text for p in SERVICE_PATTERNS):
                            to_delete.append(msg.id)
                elif cutoff_mode == "from_date":
                    # Удаляем всё от ботов начиная с cutoff даты
                    if msg.date < cutoff:
                        continue
                    sender_id = getattr(msg.from_id, 'user_id', None)
                    if not sender_id:
                        continue
                    try:
                        user = await tg_cl.get_entity(sender_id)
                        if getattr(user, 'bot', False):
                            to_delete.append(msg.id)
                    except Exception:
                        if msg.text and any(p in msg.text for p in SERVICE_PATTERNS):
                            to_delete.append(msg.id)
                elif cutoff_mode == "dedup_lessons":
                    import re as _re
                    if msg.text:
                        _m = _re.search(r'(?:Урок|Lesson) #(\S+)', msg.text)
                        if _m:
                            lesson_key = _m.group(1)
                            if lesson_key not in _lesson_map:
                                _lesson_map[lesson_key] = []
                            _lesson_map[lesson_key].append(msg.id)
                else:
                    # Старый режим: удаляем старые сообщения от ботов
                    if msg.date >= cutoff:
                        continue
                    sender_id = getattr(msg.from_id, 'user_id', None)
                    if not sender_id:
                        continue
                    try:
                        user = await tg_cl.get_entity(sender_id)
                        if getattr(user, 'bot', False):
                            to_delete.append(msg.id)
                    except Exception:
                        continue

            if cutoff_mode == "dedup_lessons":
                for lesson_key, msg_ids in _lesson_map.items():
                    if len(msg_ids) > 1:
                        msg_ids.sort()
                        to_delete.extend(msg_ids[:-1])
                if to_delete:
                    for i in range(0, len(to_delete), 100):
                        await tg_cl.delete_messages(target_chat, to_delete[i:i+100])
                        await _asyncio.sleep(0.5)
                    await tg_cl.disconnect()
                    await reply_func(f"✅ Удалено {len(to_delete)} дублей уроков")
                else:
                    await tg_cl.disconnect()
                    await reply_func("✅ Дублей уроков не найдено")
            elif to_delete:
                for i in range(0, len(to_delete), 100):
                    await tg_cl.delete_messages(target_chat, to_delete[i:i+100])
                    await _asyncio.sleep(0.5)
                await tg_cl.disconnect()
                await reply_func(f"✅ Удалено {len(to_delete)} старых сообщений от ботов")
            else:
                await tg_cl.disconnect()
                await reply_func("✅ Старых сообщений от ботов не найдено")
        except Exception as e:
            await reply_func(f"❌ Ошибка: {e}")


    elif intent == "post_lessons":
        """Публикует в Bug Lessons только НОВЫЕ уроки через единый durable-механизм.
        Состояние «опубликован» хранится флагом posted_to_group в lessons.json (git) —
        переживает сброс Redis и НЕ может зафлудить. Чтобы перепостить конкретный урок,
        снимите ему posted_to_group в lessons.json."""
        await publish_pending_lessons(reply_func)


    elif intent == "edit_file":
        """Точечное редактирование файла: old → new, с ast.parse для .py"""
        if not repo or not path:
            await reply_func("❌ Укажи repo и path")
            return
        old_text = intent_data.get("old", "")
        new_text = intent_data.get("new", "")
        if not old_text:
            await reply_func("❌ Укажи old (что заменить)")
            return
        try:
            file_content = await read_file(repo, path)
            if old_text not in file_content:
                await reply_func(f"❌ Строка не найдена в {repo}/{path}")
                return
            updated = file_content.replace(old_text, new_text, 1)
            if path.endswith(".py"):
                import ast as _ast
                try:
                    _ast.parse(updated)
                except SyntaxError as e:
                    await reply_func(f"❌ SyntaxError после замены: {e}")
                    return
            commit_msg = intent_data.get("message", f"edit: patch {path}")
            await push_file(repo, path, updated, commit_msg)
            await reply_func(f"✅ {repo}/{path} обновлён")
        except Exception as e:
            await reply_func(f"❌ Ошибка: {e}")


    elif intent == "agentic_task":
        """Agentic execution loop для многошаговых задач.
        ReAct pattern: think → act → observe → repeat.
        """
        AGENTIC_SYSTEM = """Ты — Силли, исполнитель задач AI-офиса.
Ты в agentic loop. На каждом шаге выбирай ОДНО действие и возвращай JSON.

Доступные действия:
- read_file: {"action":"read_file","repo":"...","path":"..."}
- push_file: {"action":"push_file","repo":"...","path":"...","content":"...","message":"..."}
- check_var: {"action":"check_var","service":"billy-bot","name":"REDIS_PROXY_TOKEN","expected":"опц. ожидаемая строка"} — читает переменную окружения сервиса в Railway (значение вернётся ЗАМАСКИРОВАННЫМ)
- send_message: {"action":"send_message","chat_id":-5194783850,"text":"..."} — в ОФИС ГРУППУ (-5194783850)
- send_messages: {"action":"send_messages","chat_id":-5194783850,"texts":["msg1","msg2",...]} — батч до 5
- done: {"action":"done","result":"итог для пользователя"}

Правила:
- Один JSON на шаг, без лишнего текста
- НИКОГДА не проси у людей токены/секреты/пароли/ключи в чат. Нужно значение переменной сервиса — используй check_var. Не хватает данных — заверши done с кратким запросом и НЕ повторяй одно и то же действие.
- Не больше 2 сообщений в группу за всю задачу.
- Если нужно прочитать несколько файлов — читай по одному
- done — когда задача выполнена. Максимум 12 шагов."""

        steps_log = []
        context = task
        max_steps = 12
        consecutive_failures = 0
        last_error = None
        group_sends = 0           # анти-спам: сообщений в группу за задачу
        sent_texts = set()        # дедуп одинаковых сообщений
        _last_action_sig = None   # стоп-гард против зацикливания
        _action_repeat = 0

        # agentic_task НЕ шлёт промежуточные шаги в чат — только финальный результат
        # silent_collect накапливает шаги в лог без отправки в группу
        agentic_log = []
        async def silent_collect(msg: str):
            agentic_log.append(msg)

        for step_num in range(max_steps):
            # Формируем prompt с историей шагов
            history_text = ""
            if steps_log:
                history_text = "\n\nУже выполнено:\n" + "\n".join(
                    f"  Шаг {i+1}: {s['action']} → {s['result'][:200]}"
                    for i, s in enumerate(steps_log)
                )

            step_prompt = f"Задача: {context}{history_text}\n\nСледующее действие:"

            raw_action = await ask_claude(step_prompt, system=AGENTIC_SYSTEM, model="claude-sonnet-4-6")
            raw_action = raw_action.strip()

            # Извлекаем JSON
            start_j = raw_action.find("{")
            end_j = raw_action.rfind("}") + 1
            if start_j == -1:
                await reply_func(f"❌ Шаг {step_num+1}: не получил JSON")
                break

            try:
                action_data = json.loads(raw_action[start_j:end_j])
            except Exception as e:
                await reply_func(f"❌ Шаг {step_num+1}: ошибка парсинга: {e}")
                break

            action = action_data.get("action", "")

            # Стоп-гард: одно и то же действие 3 раза подряд → зацикливание
            _sig = json.dumps(action_data, sort_keys=True, ensure_ascii=False)[:300]
            if _sig == _last_action_sig:
                _action_repeat += 1
            else:
                _action_repeat = 0
                _last_action_sig = _sig
            if _action_repeat >= 2:
                await reply_func("⚠️ Остановлено: повторяющееся действие (зацикливание).")
                break

            # Выполняем действие
            if action == "done":
                result_text = action_data.get("result", "✅ Готово")
                await reply_func(f"✅ {result_text}")  # только финал идёт в чат
                break

            elif action == "read_file":
                a_repo = action_data.get("repo", "")
                a_path = action_data.get("path", "")
                try:
                    file_content = await read_file(a_repo, a_path)
                    result = file_content[:4000]
                    steps_log.append({"action": f"read_file({a_repo}/{a_path})", "result": result})
                    # Добавляем содержимое в контекст
                    context += f"\n\n[Файл {a_repo}/{a_path}]:\n{result}"
                    consecutive_failures = 0
                    last_error = None
                except Exception as e:
                    err_str = str(e)
                    steps_log.append({"action": f"read_file({a_repo}/{a_path})", "result": f"ERROR: {err_str}"})
                    if err_str == last_error:
                        consecutive_failures += 1
                    else:
                        consecutive_failures = 1
                        last_error = err_str
                    if consecutive_failures >= 3:
                        await reply_func(f"❌ Задача остановлена: повторяющаяся ошибка — {err_str}")
                        break

            elif action == "push_file":
                a_repo = action_data.get("repo", "")
                a_path = action_data.get("path", "")
                a_content = action_data.get("content", "")
                a_message = action_data.get("message", "agentic update")
                try:
                    await push_file(a_repo, a_path, a_content, a_message)
                    steps_log.append({"action": f"push_file({a_repo}/{a_path})", "result": "OK"})
                except Exception as e:
                    steps_log.append({"action": f"push_file({a_repo}/{a_path})", "result": f"ERROR: {e}"})

            elif action == "send_messages":
                # silent (source=CLAUDE) / пауза / лимит 2 за задачу / дедуп
                if silent or await outbound_paused():
                    steps_log.append({"action": "send_messages", "result": "SKIPPED (silent/paused)"})
                elif group_sends >= 2:
                    steps_log.append({"action": "send_messages", "result": "SUPPRESSED (лимит сообщений в группу)"})
                else:
                    a_chat = action_data.get("chat_id", -5194783850)
                    texts = action_data.get("texts", [])
                    sent = 0
                    import asyncio as _asyncio
                    for t in texts[:5]:
                        if group_sends >= 2 or str(t) in sent_texts:
                            continue
                        try:
                            await _GLOBAL_BOT.send_message(chat_id=int(a_chat), text=str(t))
                            sent += 1
                            group_sends += 1
                            sent_texts.add(str(t))
                            await _asyncio.sleep(0.5)
                        except Exception:
                            pass
                    steps_log.append({"action": f"send_messages({a_chat})", "result": f"sent {sent}/{len(texts)}"})

            elif action == "send_message":
                # silent (source=CLAUDE) / пауза / лимит 2 за задачу / дедуп
                if silent or await outbound_paused():
                    steps_log.append({"action": "send_message", "result": "SKIPPED (silent/paused)"})
                elif group_sends >= 2 or action_data.get("text", "") in sent_texts:
                    steps_log.append({"action": "send_message", "result": "SUPPRESSED (дубль/лимит)"})
                else:
                    a_chat = action_data.get("chat_id", -5194783850)
                    a_text = action_data.get("text", "")
                    try:
                        await _GLOBAL_BOT.send_message(chat_id=int(a_chat), text=a_text)
                        group_sends += 1
                        sent_texts.add(a_text)
                        steps_log.append({"action": f"send_message({a_chat})", "result": "OK"})
                    except Exception as e:
                        steps_log.append({"action": f"send_message({a_chat})", "result": f"ERROR: {e}"})

            elif action == "check_var":
                a_service  = action_data.get("service", "")
                a_name     = action_data.get("name", "")
                a_expected = action_data.get("expected")
                try:
                    svc_id = next((sid for sid, (r, _) in SERVICES.items() if r == a_service), None)
                    if not svc_id:
                        svc_id = await railway_get_service_id(a_service)
                    if not svc_id:
                        raise Exception(f"сервис '{a_service}' не найден")
                    vars_map = await railway_get_variables(svc_id)
                    val = vars_map.get(a_name)
                    if val is None:
                        res = f"{a_name} НЕ задан на {a_service}"
                    else:
                        masked = (val[:4] + "…" + val[-4:]) if len(str(val)) > 8 else "***"
                        res = f"{a_name} на {a_service} = {masked} (len={len(str(val))})"
                        if a_expected is not None:
                            res += f"; equals_expected={str(val) == str(a_expected)}"
                    steps_log.append({"action": f"check_var({a_service}/{a_name})", "result": res})
                    context += f"\n\n[check_var] {res}"
                    consecutive_failures = 0
                    last_error = None
                except Exception as e:
                    err_str = str(e)
                    steps_log.append({"action": f"check_var({a_service}/{a_name})", "result": f"ERROR: {err_str}"})
                    if err_str == last_error:
                        consecutive_failures += 1
                    else:
                        consecutive_failures = 1
                        last_error = err_str
                    if consecutive_failures >= 3:
                        await reply_func(f"❌ Задача остановлена: повторяющаяся ошибка — {err_str}")
                        break

            else:
                steps_log.append({"action": action, "result": "UNKNOWN ACTION"})
                await reply_func(f"⚠️ Неизвестное действие: {action}")
                break

        else:
            await reply_func(f"⚠️ Не смог завершить за {max_steps} шагов")


    elif intent == "dev_task":
        """Делегирование задачи команде dev-dept по цепочке:
        Силли составляет план → Девви пишет код → Рикки review →
        Тести QA → Секки security → Скрибби docs → Силли деплоит.
        Каждый бот читает GitHub сам, получает артефакт предыдущего."""
        # ── 1. Силли составляет план ───────────────────────────────────────
        # Канонический список репо из SERVICES
        known_repos = sorted(set(repo_name for _, (repo_name, _) in SERVICES.items()))
        known_repos_str = ", ".join(known_repos)

        plan_prompt = (
            f"Задача: {task}\n\n"
            "Составь краткий план реализации для команды разработки.\n"
            f"Доступные репозитории (выбирай ТОЛЬКО из этого списка): {known_repos_str}\n\n"
            "Определи:\n"
            "1. repo — ТОЧНОЕ имя репо из списка выше (только имя, без org), или null\n"
            "2. file_path — путь к файлу (обычно bot.py)\n"
            "3. devvy_task — конкретное ТЗ для разработчика (что именно написать/изменить)\n\n"
            "Ответь ТОЛЬКО JSON без пояснений: {\"repo\": \"...\", \"file_path\": \"bot.py\", \"devvy_task\": \"...\"}"
        )
        plan_raw = await ask_claude(plan_prompt, system=CODER_PROMPT, model="claude-haiku-4-5-20251001")
        try:
            ps, pe = plan_raw.find("{"), plan_raw.rfind("}") + 1
            plan = json.loads(plan_raw[ps:pe]) if ps != -1 and pe > ps else {}
        except Exception:
            plan = {}

        dev_repo      = repo or plan.get("repo") or ""
        dev_file_path = file_path_override or plan.get("file_path") or "bot.py"
        # ТЗ для Девви = ОРИГИНАЛ задачи (авторитетно) + уточнение плана.
        # Не даём перефразу планировщика (Haiku/Ollama) затереть детали запроса —
        # из-за этого ТЗ искажалось ("подними порог до 9" вместо реальных правок).
        _hint = (plan.get("devvy_task") or "").strip()
        devvy_task    = task if not _hint else f"{task}\n\nУточнение плана: {_hint}"

        # Силли сама читает файл и передаёт контекст команде — надёжнее чем доверять Девви
        file_context = ""
        if dev_repo:
            try:
                gh_pat = os.getenv("GH_PAT", "")
                _url = f"https://api.github.com/repos/{GITHUB_USER}/{dev_repo}/contents/{dev_file_path}"
                _req = __import__("urllib.request", fromlist=["Request"]).Request(
                    _url, headers={"Authorization": f"token {gh_pat}", "User-Agent": "cilly-planner"}
                )
                import urllib.request as _ur
                with _ur.urlopen(_req, timeout=15) as _r:
                    _d = json.load(_r)
                file_context = __import__("base64").b64decode(_d["content"]).decode()
                logger.info(f"[dev_task] read {dev_repo}/{dev_file_path}: {len(file_context)} chars")
            except Exception as e:
                logger.warning(f"[dev_task] failed to read {dev_repo}/{dev_file_path}: {e}")

        await reply_func(
            f"🧠 План готов\n"
            f"📦 Репо: {dev_repo or 'не указано'}\n"
            f"📄 Файл: {dev_file_path}\n"
            f"📋 ТЗ: {devvy_task[:200]}"
        )

        # ── 2. Доска задач + параллельный пайплайн с ретраями ──────────────
        # Девви → [Рикки ‖ Тести ‖ Секки] → Скрибби. При провале гейта
        # (NEEDS_FIX / не компилируется / нет кода) — авто-повтор с фидбеком,
        # до MAX_DEV_ATTEMPTS. Исчерпали — blocked + эскалация (как fix_count>=3).
        from ai_office_shared.shared.dev_pipeline import run_dev_pipeline
        from ai_office_shared.shared.dev_activity import publish_activity
        import uuid as _uuid

        uid      = int(os.getenv("YOUR_TELEGRAM_ID", "391077101"))
        _r_act   = await get_redis()
        _task_id = _uuid.uuid4().hex[:12]

        # Заводим задачу на доске тем же id, что у эфира — связка board ↔ activity
        board_id = await tb.create_task(
            _r_act, f"dev_task: {devvy_task[:80]}",
            created_by="силли", assignee="dev-dept",
            status="in_progress", task_id=_task_id,
        ) or _task_id

        MAX_DEV_ATTEMPTS = 3
        final_code = ""
        review_ok = compile_ok = False
        commit_msg = ""
        results: dict = {}
        retry_feedback = ""
        attempt = 0

        while attempt < MAX_DEV_ATTEMPTS:
            attempt += 1
            cur_task = devvy_task if not retry_feedback else (
                f"{devvy_task}\n\n[ПОВТОР #{attempt}] Предыдущая попытка отклонена:\n{retry_feedback}"
            )
            await publish_activity(_r_act, _task_id, "силли", "plan",
                                   f"попытка {attempt}/{MAX_DEV_ATTEMPTS}: {dev_repo or '—'}/{dev_file_path}")

            pipe = await run_dev_pipeline(
                cur_task, repo=dev_repo, file_path=dev_file_path,
                context=file_context, user_id=uid,
                redis_client=_r_act, task_id=_task_id,
            )
            results = {
                "Девви":   pipe.get("devvy", "")   or "⚠️ нет ответа",
                "Рикки":   pipe.get("ricky", "")   or "⚠️ нет ответа",
                "Тести":   pipe.get("testi", "")   or "⚠️ нет ответа",
                "Секки":   pipe.get("sekky", "")   or "⚠️ нет ответа",
                "Скрибби": pipe.get("scribbi", "") or "⚠️ нет ответа",
            }
            commit_msg = pipe.get("commit_msg", "")

            # Финальный код — из ревью Рикки (FINAL_CODE блок), иначе из кода Девви
            ricky_result = pipe.get("final_code_artifact", "") or pipe.get("ricky", "")
            final_code = ""
            if "```python" in ricky_result:
                cs = ricky_result.find("```python") + 9
                ce = ricky_result.find("```", cs)
                if ce > cs:
                    final_code = ricky_result[cs:ce].strip()

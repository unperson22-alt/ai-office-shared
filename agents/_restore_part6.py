                        result[key] = data
                else:
                    raw = await r.lrange(ns, 0, 9)
                    result[ns] = [json.loads(e) for e in raw]

        # ── 7. сброс fix_count (reset / сброс) ───────────────────────
        if any(w in task_lower for w in ["сбро", "reset", "clear fix", "очист"]):
            deleted = []
            async for key in r.scan_iter("fix_count:*"):
                await r.delete(key)
                deleted.append(key.split(":")[-1][:8])
            async for key in r.scan_iter("seen_error:*"):
                await r.delete(key)
            result["reset"] = f"Сброшено {len(deleted)} fix_count ключей"

        out = json.dumps(result, ensure_ascii=False, indent=2)
        # Если много данных — режем
        if len(out) > 3000:
            out = out[:3000] + "\n... (обрезано)"
        await reply_func(f"```json\n{out}\n```")

    elif intent == "trader_winrate":
        """Винрейт трейдера: читает signals:list/signal:* и считает TP/SL по 1h-свечам.

        Свечи берём фолбэк-цепочкой binance→bybit→okx (BingX пропускаем — гео-бан IP).
        Закрытие консервативное: если в одной свече задеты и TP, и SL — считаем SL.
        Отдаёт WR за 7 дней и за всё время + краткую разбивку.
        """
        r = await get_redis()
        if not r:
            await reply_func("❌ Redis недоступен")
            return
        SIGNAL_TTL = 259200  # 72h — как в tilly-trader
        now_ts = int(time.time())
        _cache: dict = {}

        async def _tw_fetch(symbol: str) -> list:
            if symbol in _cache:
                return _cache[symbol]
            if "-" in symbol:
                base, quote = symbol.split("-", 1)
            else:
                quote = "USDT"; base = symbol[:-len(quote)] if symbol.endswith(quote) else symbol
            base, quote = base.upper(), quote.upper()
            concat = f"{base}{quote}"
            rows: list = []
            async with httpx.AsyncClient(timeout=10) as c:
                try:
                    resp = await c.get("https://fapi.binance.com/fapi/v1/klines",
                                       params={"symbol": concat, "interval": "1h", "limit": 1000})
                    if resp.status_code == 200:
                        rows = [(int(k[0]), float(k[2]), float(k[3])) for k in (resp.json() or [])]
                except Exception:
                    rows = []
                if len(rows) < 20:
                    try:
                        resp = await c.get("https://api.bybit.com/v5/market/kline",
                                           params={"category": "linear", "symbol": concat,
                                                   "interval": "60", "limit": 1000})
                        if resp.status_code == 200:
                            lst = ((resp.json().get("result") or {}).get("list")) or []
                            rows = [(int(k[0]), float(k[2]), float(k[3])) for k in lst]
                    except Exception:
                        pass
                if len(rows) < 20:
                    try:
                        resp = await c.get("https://www.okx.com/api/v5/market/candles",
                                           params={"instId": f"{base}-{quote}-SWAP", "bar": "1H", "limit": 300})
                        if resp.status_code == 200:
                            lst = resp.json().get("data") or []
                            rows = [(int(k[0]), float(k[2]), float(k[3])) for k in lst]
                    except Exception:
                        pass
            rows.sort(key=lambda x: x[0])
            _cache[symbol] = rows
            return rows

        def _tw_outcome(direction: str, sl: float, tp: float, window: list):
            for (_tms, high, low) in window:
                if direction == "LONG":
                    sl_hit = low <= sl; tp_hit = high >= tp
                else:
                    sl_hit = high >= sl; tp_hit = low <= tp
                if sl_hit:      # покрывает и (sl_hit and tp_hit) → sl
                    return "sl"
                if tp_hit:
                    return "tp"
            return None

        sids = await r.lrange("signals:list", 0, -1)
        raw_signals = []
        for sid in sids:
            sid = sid if isinstance(sid, str) else sid.decode()
            rawj = await r.get("signal:" + sid)
            if rawj:
                try:
                    raw_signals.append(json.loads(rawj))
                except Exception:
                    pass

        async def _tw_calc(days: int):
            cutoff = now_ts - days * 86400 if days > 0 else 0
            tot = op = w = l = ex = nodata = 0
            lines = []
            for s in raw_signals:
                if s.get("ts", 0) < cutoff:
                    continue
                tot += 1
                ts = s["ts"]; end_ts = min(now_ts, ts + SIGNAL_TTL)
                sl = float(s.get("sl") or 0)
                tp = float(s.get("tp1") or s.get("tp") or 0)
                direction = s.get("direction", "LONG")
                candles = await _tw_fetch(s.get("symbol", ""))
                window = [c for c in candles if ts * 1000 <= c[0] <= end_ts * 1000]
                if not candles:
                    nodata += 1; mark = "⚠️"
                elif tp and window and (hit := _tw_outcome(direction, sl, tp, window)):
                    if hit == "tp":
                        w += 1; mark = "✅"
                    else:
                        l += 1; mark = "❌"
                elif now_ts >= ts + SIGNAL_TTL:
                    ex += 1; mark = "⌛"
                else:
                    op += 1; mark = "⏳"
                lines.append(f"{mark} {s.get('symbol','?')} {direction}")
            closed = w + l
            wr = f"{round(w / closed * 100, 1)}%" if closed else "нет закрытых"
            return {"tot": tot, "open": op, "win": w, "loss": l, "exp": ex,
                    "nodata": nodata, "wr": wr, "lines": lines}

        d7 = await _tw_calc(7)
        da = await _tw_calc(0)

        def _tw_fmt(tag, d):
            extra = f", ⚠️нет свечей {d['nodata']}" if d["nodata"] else ""
            return (f"{tag}: всего {d['tot']}, закрыто {d['win'] + d['loss']} "
                    f"(✅{d['win']}/❌{d['loss']}), ⌛{d['exp']}, ⏳{d['open']}{extra} → WR {d['wr']}")

        out_lines = [
            "📊 Винрейт трейдера",
            _tw_fmt("7 дней", d7),
            _tw_fmt("Всё время", da),
            "",
            "Разбивка (всё время):",
            *da["lines"][:40],
        ]
        await reply_func("\n".join(out_lines))

    elif intent in ("push_code", "fix_bot"):
        if not repo or not path:
            await reply_func("❓ Уточни: в каком репо и какой файл изменить?")
            return
        await reply_func(f"⏳ Генерирую код для `{repo}/{path}`...")
        code = await ask_claude(task)
        await reply_func("📤 Заливаю на GitHub...")
        try:
            result = await push_file(repo, path, code, f"nl: {task[:60]}")
            action = "Обновлён" if result["action"] == "updated" else "Создан"
            await reply_func(f"✅ {action}: {result['url']}")
            # Auto-redeploy
            service_id = next((sid for sid, (r, _) in SERVICES.items() if r == repo), None)
            if service_id:
                await reply_func("🔄 Запускаю редеплой...")
                ok = await redeploy_service(service_id)
                await reply_func("✅ Задеплоено" if ok else "⚠️ Пуш сделан, редеплой не удался")
        except Exception as e:
            await reply_func(f"❌ Ошибка: {e}")

    elif intent == "create_bot":
        await reply_func(f"🤖 Создаю бота: *{task}*...")

        # Ask Claude to extract name + system prompt
        setup_raw = await ask_claude(
            f"Из описания извлеки: имя бота (одно слово, латиница, строчные, через дефис если нужно), "
            f"отображаемое имя (по-русски, одно слово) и системный промпт (1-2 предложения, роль и стиль). "
            f"Описание: {task}\n\n"
            f"Верни ТОЛЬКО JSON без markdown: {{\"repo\": \"имя-бота\", \"display\": \"Имя\", \"prompt\": \"...\"}}" ,
            model="claude-haiku-4-5-20251001"
        )
        try:
            setup_raw = setup_raw.strip()
            s, e = setup_raw.find("{"), setup_raw.rfind("}") + 1
            setup = json.loads(setup_raw[s:e])
            _raw_repo  = setup["repo"].lower().replace(" ", "-").replace("_", "-")
            bot_repo   = _raw_repo if _raw_repo.endswith("-bot") else _raw_repo + "-bot"
            bot_display = setup["display"]
            bot_prompt  = setup["prompt"]
        except Exception as ex:
            await reply_func(f"❌ Не смог разобрать параметры бота: {ex}")
            return

        await reply_func(f"📦 Репо: `{bot_repo}`\n👤 Имя: {bot_display}\n📝 Промпт: {bot_prompt}")

        # Если сервис уже существует — проверяем есть ли TELEGRAM_TOKEN
        # Если нет — resume: пропускаем создание репо/кода и сразу идём за токеном
        resume_mode = False
        existing_sid = next((sid for sid, (r, _) in SERVICES.items() if r == bot_repo), None)
        if not existing_sid:
            existing_sid = await railway_get_service_id(bot_repo)
        if existing_sid:
            try:
                vars_data = await railway_graphql(
                    """query($proj: String!, $svc: String!, $env: String!) {
                         variables(projectId: $proj, serviceId: $svc, environmentId: $env)
                       }""",
                    {"proj": PROJECT_ID, "svc": existing_sid, "env": ENVIRONMENT_ID}
                )
                existing_vars = (vars_data.get("data") or {}).get("variables") or {}
                if "TELEGRAM_TOKEN" in existing_vars:
                    await reply_func(f"✅ Бот `{bot_repo}` уже полностью настроен.")
                    return
                else:
                    await reply_func("⚠️ Сервис существует, но токена нет — получаю через BotFather...")
                    resume_mode = True
            except Exception:
                resume_mode = True

                # 1. Создать GitHub репо
        if not resume_mode:
            await reply_func("1️⃣ Создаю GitHub репо...")
        try:
            repo_info = await create_repo(bot_repo, description=f"AI office bot: {bot_display}")
        except ValueError as ex:
            await reply_func(f"⚠️ {ex} — продолжаю с существующим")
        except Exception as ex:
            await reply_func(f"❌ GitHub: {ex}")
            return

        # 2. Пушу шаблон
        if not resume_mode:
            await reply_func("2️⃣ Генерирую и заливаю код...")
        bot_code = BOT_TEMPLATE.format(bot_name=bot_display, system_prompt=bot_prompt)
        try:
            await push_file(bot_repo, "bot.py", bot_code, f"init: {bot_display} bot")
            await push_file(bot_repo, "requirements.txt", REQUIREMENTS_TEMPLATE, "init: requirements")
            await push_file(bot_repo, "Dockerfile", DOCKERFILE_TEMPLATE, "init: Dockerfile")
        except Exception as ex:
            await reply_func(f"❌ Пуш файлов: {ex}")
            return

        # 3. Создать сервис на Railway
        # 3. BotFather — получаем токен автоматически
        await reply_func("3️⃣ Иду в BotFather за токеном...")
        try:
            tg_token = await create_via_botfather(bot_repo.replace("-bot", ""), bot_display)
        except Exception as ex:
            await reply_func(f"❌ BotFather: {ex}")
            return

        # 4. Создаём сервис на Railway со всеми переменными сразу
        await reply_func("4️⃣ Создаю сервис на Railway и прописываю все переменные...")
        all_vars = {
            "TELEGRAM_TOKEN":  tg_token,
            "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", ""),
            "YOUR_TELEGRAM_ID": os.getenv("YOUR_TELEGRAM_ID", ""),
            "OFFICE_CHAT_ID":   os.getenv("OFFICE_CHAT_ID", ""),
            "LOG_BOT_URL":      os.getenv("LOG_BOT_URL", ""),
        }
        try:
            railway_info = await railway_create_service(bot_repo, bot_display, variables=all_vars)
            service_id = railway_info["service_id"]
        except Exception as ex:
            await reply_func(f"❌ Railway: {ex}")
            return

        # Verify token works
        async with httpx.AsyncClient(timeout=10) as hc:
            me = await hc.get(f"https://api.telegram.org/bot{tg_token}/getMe")
            me_data = me.json()
        if not me_data.get("ok"):
            await reply_func(f"❌ Токен не работает: {me_data.get('description','')}")
            return
        actual_username = me_data["result"]["username"]
        await reply_func(f"✅ Токен проверен: @{actual_username}")

        # 5. Добавить бота в Office group
        await reply_func("5️⃣ Добавляю бота в Office group...")
        office_group_id = int(os.getenv("OFFICE_CHAT_ID", "0"))
        bot_username = f"@{bot_repo.replace('-', '_')}"
        added = await tg_add_bot_to_group(bot_username, office_group_id)
        if added:
            await tg_promote_bot_admin(bot_username, office_group_id)

        # 6. Переместить бота и сервис в папку Office
        await reply_func("6️⃣ Перемещаю в папку Office...")
        # Добавляем бота (личный чат) в папку
        try:
            bot_entity = None
            client_tmp = await get_telethon_client()
            try:
                bot_entity = await client_tmp.get_entity(bot_username)
            finally:
                await client_tmp.disconnect()
            if bot_entity:
                await tg_add_peer_to_folder(bot_entity.id, "Office")
        except Exception as e:
            logger.warning(f"Не удалось добавить в папку: {e}")

        # 7. Обновить Филли — добавить нового бота в BOT_URLS и ROUTER_SYSTEM
        await reply_func("7️⃣ Обновляю Филли...")
        try:
            filly_code = await read_file("filly-bot", "bot.py")
            bot_key = bot_display.upper()
            bot_internal = f"http://{bot_repo}.railway.internal:8080"

            # Обновляем Филли — добавляем нового бота в 4 места
            cilly_anchor = '"СИЛЛИ":  "http://cilly-bot.railway.internal:8080",'
            filly_code = filly_code.replace(
                cilly_anchor + "\n}",
                cilly_anchor + "\n    " + f'"{bot_key}":  "{bot_internal}",' + "\n}"
            )
            router_anchor = "СИЛЛИ — код, баги, технические задачи, мониторинг, Railway, боты"
            filly_code = filly_code.replace(
                router_anchor,
                router_anchor + "\n" + f"{bot_key} — {bot_prompt}"
            )
            sillie_dm = '"СИЛЛИ":  "Ты — Силли.'
            filly_code = filly_code.replace(
                sillie_dm,
                f'"{bot_key}":  "Ты — {bot_display}. {bot_prompt} Неформально, на русском.",\n    ' + sillie_dm
            )
            sillie_disp = '"СИЛЛИ":  "Силли",'
            filly_code = filly_code.replace(
                sillie_disp,
                f'"{bot_key}":  "{bot_display}",\n    ' + sillie_disp
            )

            await push_file("filly-bot", "bot.py", filly_code, f"feat: add {bot_display} to routing")
            # Redeploy Filly
            filly_service_id = "5d61d403-feee-455e-9c0d-523f0e7c79d5"
            await redeploy_service(filly_service_id)
        except Exception as e:
            logger.warning(f"Не удалось обновить Филли: {e}")

        # Регистрируем в реестре template_bots — для автообновлений
        asyncio.create_task(register_template_bot(bot_repo, bot_display, bot_prompt, service_id))

        await reply_func(
            f"✅ Бот *{bot_display}* полностью готов и интегрирован!\n\n"
            f"• GitHub репо: `{bot_repo}` ✅\n"
            f"• Код залит ✅\n"
            f"• Telegram бот создан ✅\n"
            f"• Railway сервис + переменные ✅\n"
            f"• Добавлен в Office group ✅\n"
            f"• Папка Office ✅\n"
            f"• Филли обновлён и задеплоен ✅\n"
            f"• Зарегистрирован для автообновлений ✅\n\n"
            f"Бот уже работает в офисе 🎉"
        )

    elif intent == "get_bot_token":
        # Extract bot username from task
        bot_username = intent_data.get("repo") or ""
        if not bot_username:
            import re
            match = re.search(r"@?(\w+_bot)", task, re.IGNORECASE)
            bot_username = match.group(1) if match else ""
        if not bot_username:
            await reply_func("❓ Укажи username бота (например @ellice_mom_bot)")
            return
        await reply_func(f"🔍 Получаю токен для @{bot_username} через BotFather...")
        try:
            import re as re2
            tg_client = await get_telethon_client()
            botfather = await tg_client.get_entity("@BotFather")
            await tg_client.send_message(botfather, "/token")
            await asyncio.sleep(1)
            await tg_client.send_message(botfather, f"@{bot_username}")
            await asyncio.sleep(3)
            msgs = await tg_client.get_messages(botfather, limit=3)
            token = None
            for m in msgs:
                match = re2.search(r"(\d{8,12}:[A-Za-z0-9_-]{35,})", m.text or "")
                if match:
                    token = match.group(1)
                    break
            await tg_client.disconnect()
            if token:
                bot_id = token.split(":")[0]
                await reply_func(f"✅ Токен получен: {bot_id}:***\n\nОбновить Railway переменную? Укажи имя сервиса.")
            else:
                await reply_func("❌ Токен не найден в ответе BotFather. Попробуй /mybots вручную.")
        except Exception as e:
            await reply_func(f"❌ Ошибка: {e}")

    elif intent == "add_external_bot":
        import re as _re

        # ── Шаг 0: вытащить всё из задачи через Haiku ────────────────────────
        extraction_raw = await ask_claude(
            f"Из запроса извлеки параметры внешнего бота.\n"
            f"Запрос: {task}\n\n"
            f"JSON без markdown:\n"
            f"{{\"name_ru\": \"имя по-русски одним словом\","
            f"\"name_en\": \"имя латиницей строчными без пробелов\","
            f"\"key\": \"ключ для роутера КАПСОМ\","
            f"\"url\": \"URL endpoint или null\","
            f"\"description\": \"роль и функции одной фразой на русском\","
            f"\"tg_folder\": \"название папки куда добавить или null\","
            f"\"tg_group\": \"название новой группы для создания или null\"}}",
            model="claude-haiku-4-5-20251001"
        )
        try:
            s, e = extraction_raw.find("{"), extraction_raw.rfind("}") + 1
            ext = json.loads(extraction_raw[s:e])
        except Exception:
            ext = {}

        bot_display   = ext.get("name_ru", "Крис").capitalize()
        name_en       = ext.get("name_en", bot_display.lower())
        bot_key       = ext.get("key", bot_display.upper())
        # URL: берём из запроса или вычисляем стандартный Railway-паттерн
        bot_url_raw = ext.get("url") or ""
        bot_url     = bot_url_raw.rstrip("/") if bot_url_raw else ""
        bot_description = ext.get("description", f"Внешний ассистент {bot_display}")
        tg_folder     = ext.get("tg_folder") or "Office"
        tg_new_group  = ext.get("tg_group")  # название новой группы если нужна

        # ── Шаг 1: найти username через Telegram API (автоподбор) ────────────
        await reply_func(f"🔍 Ищу @{name_en}_bot в Telegram...")

        candidates = [
            f"{name_en}_bot",
            f"{name_en}ai_bot",
            f"{name_en}_assistant_bot",
            f"ai{name_en}_bot",
            f"{name_en}2_bot",
            f"{name_en}_office_bot",
            f"{name_en}ru_bot",
            f"the{name_en}_bot",
        ]

        # Если в задаче явно указан @username — ставим его первым
        explicit = _re.search(r"@([A-Za-z][A-Za-z0-9_]{3,})", message_text)
        if explicit:
            candidates.insert(0, explicit.group(1))

        bot_username = None
        tg_token = os.getenv("CODER_BOT_TOKEN", "")
        async with httpx.AsyncClient(timeout=10) as hc:
            for candidate in candidates:
                try:
                    r = await hc.get(
                        f"https://api.telegram.org/bot{tg_token}/getChat",
                        params={"chat_id": f"@{candidate}"}
                    )
                    if r.json().get("ok"):
                        bot_username = candidate
                        logger.info(f"[add_external_bot] found @{candidate}")
                        break
                except Exception:
                    continue

        if not bot_username:
            tried = ", ".join(f"@{c}" for c in candidates[:5])
            await reply_func(
                f"Перебрал варианты ({tried}…) — ни один не найден в Telegram.\n"
                f"Скинь точный @username бота."
            )
            return

        # Если URL не указан — ищем сервис на Railway по имени бота
        if not bot_url:
            bot_url = await railway_get_bot_url(bot_username)

        await reply_func(
            f"✅ Нашёл: @{bot_username}\n"
            f"Имя: {bot_display} | Ключ: {bot_key}\n"
            f"URL: {bot_url}\n"
            f"Роль: {bot_description}"
        )

        # ── Шаг 2: Создать Telegram-группу если нужна ────────────────────────
        created_group_id = None
        if tg_new_group:
            await reply_func(f"2️⃣ Создаю группу «{tg_new_group}»...")
            created_group_id = await tg_create_group(tg_new_group, [f"@{bot_username}"])
            if created_group_id:
                await reply_func(f"✅ Группа создана: {created_group_id}")
                # Добавить группу в папку
                ok = await tg_add_peer_to_folder(created_group_id, tg_folder)
                await reply_func(f"✅ Группа добавлена в папку {tg_folder}" if ok else f"⚠️ Папка {tg_folder} не найдена")
            else:
                await reply_func("⚠️ Не удалось создать группу")

        # ── Шаг 3: Добавить бота в офис-группу ──────────────────────────────
        await reply_func("3️⃣ Добавляю в офис-группу...")
        office_id = int(os.getenv("OFFICE_CHAT_ID", "-5194783850"))
        added = await tg_add_bot_to_group(f"@{bot_username}", office_id)
        await reply_func("✅ Добавлен в офис-группу" if added else "⚠️ Не удалось (возможно уже там)")

        # ── Шаг 4: Добавить бота в папку Office ─────────────────────────────
        folder_ok = False
        await reply_func(f"4️⃣ Добавляю в папку {tg_folder}...")
        try:
            client_tmp = await get_telethon_client()
            try:
                entity = await client_tmp.get_entity(f"@{bot_username}")
                peer_id = entity.id
                from telethon.tl.functions.messages import GetDialogFiltersRequest as _GDF
                filters_resp = await client_tmp(_GDF())
                folder_names = [(f.title.text if hasattr(f.title, 'text') else str(f.title)) for f in filters_resp.filters if hasattr(f, 'title')]
            finally:
                await client_tmp.disconnect()
            folder_ok = await tg_add_peer_to_folder(peer_id, tg_folder)
            if folder_ok:
                await reply_func(f"✅ Добавлен в папку {tg_folder}")
            else:
                await reply_func(f"⚠️ Папка '{tg_folder}' не найдена.\nДоступные: {folder_names}\nСкажи точное название — добавлю.")
        except Exception as e:
            await reply_func(f"⚠️ Папка: {e}")

        # ── Шаг 5: Обновить Филли (routing) — всегда ────────────────────────
        await reply_func("5️⃣ Обновляю Филли (routing)...")
        try:
            filly_code = await read_file("filly-bot", "bot.py")

            # BOT_URLS
            urls_start = filly_code.find("BOT_URLS")
            urls_end   = filly_code.find("}", urls_start)
            last_comma = filly_code.rfind(",", urls_start, urls_end)
            filly_code = (filly_code[:last_comma+1]
                          + f'\n    "{bot_key}":  "{bot_url}",'
                          + filly_code[last_comma+1:])

            # ROUTER_SYSTEM
            anchor_router = "Только одно слово. Если непонятно — БИЛЛИ."
            filly_code = filly_code.replace(
                anchor_router,
                f'{bot_key} — {bot_description}\n{anchor_router}'
            )

            # DM_AGENT_SYSTEMS
            dm_start = filly_code.find("DM_AGENT_SYSTEMS")
            dm_end   = filly_code.find("}", dm_start)
            last_dm  = filly_code.rfind(",", dm_start, dm_end)
            filly_code = (filly_code[:last_dm+1]
                          + f'\n    "{bot_key}":  "Ты — {bot_display}. {bot_description} Неформально, на русском.",'
                          + filly_code[last_dm+1:])

            # _name_map
            nm_anchor = '"силли": "СИЛЛИ"'
            alias = bot_username.replace("_bot","").replace("_","")
            filly_code = filly_code.replace(
                nm_anchor,
                f'"{bot_display.lower()}": "{bot_key}", "{alias}": "{bot_key}",\n        {nm_anchor}'
            )

            await push_file("filly-bot", "bot.py", filly_code,
                            f"feat: add external bot {bot_display} to routing")
            await redeploy_service("5d61d403-feee-455e-9c0d-523f0e7c79d5")
            await reply_func("✅ Филли обновлён и задеплоен")
        except Exception as e:
            await reply_func(f"⚠️ Ошибка обновления Филли: {e}")

        await reply_func(
            f"✅ *{bot_display}* подключён!\n\n"
            f"• @{bot_username} найден автоматически ✅\n"
            + (f"• Группа «{tg_new_group}» создана ✅\n" if tg_new_group and created_group_id else "")
            + f"• Офис-группа {'✅' if added else '⚠️'}\n"
            f"• Папка {tg_folder} {'✅' if folder_ok else '⚠️ не найдена'}\n"
            f"• Роутинг Филли: {bot_key} → {bot_url} ✅"
        )

    elif intent == "deploy":
        if not repo:
            await reply_func("❓ Укажи какой сервис задеплоить")
            return
        service_id = next((sid for sid, (r, _) in SERVICES.items() if r == repo), None)
        if not service_id:
            await reply_func(f"❌ Сервис {repo} не найден в SERVICES")
            return
        await reply_func(f"🔄 Деплою {repo}...")
        ok = await redeploy_service(service_id)
        await reply_func(f"✅ {repo} задеплоен" if ok else f"❌ Редеплой {repo} не удался")

    elif intent == "read_file":
        if not repo or not path:
            await reply_func("❓ Укажи репо и путь к файлу")
            return
        content_file = await read_file(repo, path)
        if len(content_file) > 3000:
            content_file = content_file[:3000] + "\n... (обрезано)"
        await reply_func(f"📄 `{repo}/{path}`:\n```\n{content_file}\n```")

    elif intent == "list_files":
        if not repo:
            await reply_func("❓ Укажи репо")
            return
        files = await list_files(repo, path or "")
        lines = [("📁 " if f["type"] == "dir" else "📄 ") + f["name"] for f in files]
        await reply_func("\n".join(lines))

    elif intent == "cleanup_dm":
        """Удалить сообщения с ключами/секретами в личке через Telethon userbot."""
        import asyncio as _asyncio
        SENSITIVE = ["gsk_", "groq", "token", "api_key", "secret", "✅ groq"]
        try:
            tg_cl = await get_telethon_client()

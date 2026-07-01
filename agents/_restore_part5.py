    response = await process(msg, update.effective_user.id)
    await log("MSG_OUT", f"{bot_name}: {{response[:80]}}")
    await update.message.reply_text(response)


async def _legacy_main_unused():  # дублировал main() — сломан
    app_http = web.Application()
    app_http.router.add_post("/task", handle_task)
    runner = web.AppRunner(app_http)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", HTTP_PORT).start()
    logger.info(f"HTTP on :{{HTTP_PORT}}")
    ptb = Application.builder().token(TELEGRAM_TOKEN).build()
    ptb.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    async with ptb:
        await ptb.start()
        await ptb.updater.start_polling(drop_pending_updates=True)
        logger.info("{bot_name} запущен")
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
"""

REQUIREMENTS_TEMPLATE = """python-telegram-bot==21.3
anthropic
aiohttp
httpx
"""

DOCKERFILE_TEMPLATE = """FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "bot.py"]
"""

ENVIRONMENT_ID = "2efaaf60-ba39-492c-bf86-007fd505493f"

async def create_via_botfather(bot_name_en: str, bot_display: str) -> str:
    """Создать бота через BotFather и вернуть токен. bot_name_en — username без _bot."""
    api_id   = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    session  = os.getenv("TELETHON_SESSION", "")

    if not all([api_id, api_hash, session]):
        raise EnvironmentError("TELEGRAM_API_ID / TELEGRAM_API_HASH / TELETHON_SESSION не заданы")

    # Кандидаты username — пробуем по очереди пока не создадим
    username_candidates = [
        f"{bot_name_en}_bot",
        f"{bot_name_en}ai_bot",
        f"{bot_name_en}2_bot",
        f"{bot_name_en}3_bot",
        f"{bot_name_en}_office_bot",
        f"ai{bot_name_en}_bot",
        f"{bot_name_en}_ru_bot",
    ]

    import re as _re

    async with TelegramClient(StringSession(session), api_id, api_hash) as client:
        botfather = await client.get_entity("@BotFather")

        async def send_msg(text: str) -> int:
            """Отправить и вернуть ID последнего сообщения BotFather ДО отправки."""
            msgs = await client.get_messages(botfather, limit=1)
            before_id = msgs[0].id if msgs else 0
            await client.send_message(botfather, text)
            return before_id

        async def wait_new_reply(after_id: int, timeout: float = 8.0) -> str:
            """Ждать новое сообщение BotFather с ID > after_id."""
            for _ in range(int(timeout / 0.5)):
                await asyncio.sleep(0.5)
                msgs = await client.get_messages(botfather, limit=1)
                if msgs and msgs[0].id > after_id:
                    return msgs[0].text or ""
            msgs = await client.get_messages(botfather, limit=1)
            return msgs[0].text if msgs else ""

        # Сбрасываем состояние
        before = await send_msg("/start")
        await wait_new_reply(before, timeout=3.0)  # дожидаемся приветствия, игнорируем
        await asyncio.sleep(1)

        for attempt, bot_username in enumerate(username_candidates):
            logger.info(f"[botfather] попытка {attempt+1}: @{bot_username}")

            if attempt > 0:
                before = await send_msg("/start")
                await wait_new_reply(before, timeout=3.0)
                await asyncio.sleep(1)

            # Шаг 1: /newbot → ждём "Give me a name"
            before1 = await send_msg("/newbot")
            reply1 = await wait_new_reply(before1, timeout=7.0)
            logger.info(f"[botfather] /newbot → {reply1[:80]}")

            # Шаг 2: имя бота → ждём "choose username"
            before2 = await send_msg(bot_display)
            reply2 = await wait_new_reply(before2, timeout=7.0)
            logger.info(f"[botfather] display → {reply2[:80]}")

            # Шаг 3: username → ждём токен или ошибку
            before3 = await send_msg(bot_username)
            reply3 = await wait_new_reply(before3, timeout=10.0)
            logger.info(f"[botfather] username reply → {reply3[:120]}")

            # Успех — есть токен
            token_match = _re.search(r"(\d+:[A-Za-z0-9_-]{35,})", reply3)
            if token_match:
                logger.info(f"[botfather] ✅ создан @{bot_username}")
                return token_match.group(1)

            if any(p in reply3.lower() for p in ["already taken", "taken", "sorry", "try something"]):
                logger.warning(f"[botfather] @{bot_username} занят, пробую следующий...")
                continue

            raise ValueError(f"BotFather ошибка (@{bot_username}): {reply3[:200]}")

        raise ValueError(f"Все {len(username_candidates)} вариантов username заняты для {bot_name_en}")



async def get_telethon_client() -> TelegramClient:
    """Создать и вернуть подключённый Telethon клиент."""
    api_id   = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    session  = os.getenv("TELETHON_SESSION", "")
    if not all([api_id, api_hash, session]):
        raise EnvironmentError("TELEGRAM_API_ID / TELEGRAM_API_HASH / TELETHON_SESSION не заданы")
    client = TelegramClient(StringSession(session), api_id, api_hash)
    await client.connect()
    return client


async def tg_add_bot_to_group(bot_username: str, group_id: int) -> bool:
    """Добавить бота в группу по group_id. Поддерживает Chat и Channel/Supergroup."""
    from telethon.tl.types import Chat, Channel
    from telethon.tl.functions.messages import AddChatUserRequest
    client = await get_telethon_client()
    try:
        bot_entity   = await client.get_entity(bot_username)
        group_entity = await client.get_entity(group_id)
        if isinstance(group_entity, Channel):
            await client(InviteToChannelRequest(group_entity, [bot_entity]))
        else:
            # Обычный Chat
            await client(AddChatUserRequest(
                chat_id=group_entity.id,
                user_id=bot_entity,
                fwd_limit=0
            ))
        logger.info(f"tg_add_bot_to_group: {bot_username} → {group_id}")
        return True
    except Exception as e:
        logger.error(f"tg_add_bot_to_group failed: {e}")
        return False
    finally:
        await client.disconnect()


async def tg_get_folder_id(folder_name: str) -> int | None:
    """Найти ID папки по имени."""
    client = await get_telethon_client()
    try:
        filters = await client(GetDialogFiltersRequest())
        for f in filters.filters:
            if hasattr(f, 'title') and (f.title.text if hasattr(f.title, 'text') else str(f.title)).strip().lower() == folder_name.strip().lower():
                return f.id
        # Логируем все найденные папки для диагностики
        names = [(f.title.text if hasattr(f.title, 'text') else str(f.title)) for f in filters.filters if hasattr(f, 'title')]
        logger.info(f"tg_get_folder_id: папки найдены: {names}, искали: '{folder_name}'")
        return None
    finally:
        await client.disconnect()


async def tg_add_peer_to_folder(peer_id: int, folder_name: str = "Office") -> bool:
    """Добавить диалог (бота или группу) в папку по имени."""
    client = await get_telethon_client()
    try:
        filters = await client(GetDialogFiltersRequest())
        target = None
        # Логируем все папки для диагностики
        all_names = [(f.title.text if hasattr(f.title, 'text') else str(f.title)) for f in filters.filters if hasattr(f, 'title')]
        logger.info(f"tg_add_peer_to_folder: все папки: {all_names}, ищем: '{folder_name}'")
        for f in filters.filters:
            if hasattr(f, 'title') and (f.title.text if hasattr(f.title, 'text') else str(f.title)).strip().lower() == folder_name.strip().lower():
                target = f
                break
        if not target:
            logger.warning(f"Папка '{folder_name}' не найдена. Доступны: {all_names}")
            return False

        peer_entity = await client.get_entity(peer_id)
        input_peer = await client.get_input_entity(peer_entity)

        existing_ids = [getattr(p, 'channel_id', None) or getattr(p, 'user_id', None) or getattr(p, 'chat_id', None)
                        for p in target.include_peers]
        new_id = getattr(input_peer, 'channel_id', None) or getattr(input_peer, 'user_id', None) or getattr(input_peer, 'chat_id', None)
        if new_id in existing_ids:
            logger.info(f"Peer {peer_id} уже в папке {folder_name}")
            return True

        target.include_peers.append(input_peer)
        await client(UpdateDialogFilterRequest(id=target.id, filter=target))
        logger.info(f"tg_add_peer_to_folder: {peer_id} → {folder_name}")
        return True
    except Exception as e:
        logger.error(f"tg_add_peer_to_folder failed: {e}")
        return False
    finally:
        await client.disconnect()


async def tg_create_group(title: str, bot_usernames: list[str] = None) -> int | None:
    """Создать новую группу и вернуть её ID."""
    from telethon.tl.functions.channels import CreateChannelRequest
    client = await get_telethon_client()
    try:
        result = await client(CreateChannelRequest(
            title=title, about="", megagroup=True
        ))
        group = result.chats[0]
        group_id = -100_000_000_000 - group.id  # правильный формат для supergroup

        if bot_usernames:
            for username in bot_usernames:
                try:
                    bot_entity = await client.get_entity(username)
                    await client(InviteToChannelRequest(group, [bot_entity]))
                    await asyncio.sleep(1)
                except Exception as e:
                    logger.warning(f"Не удалось добавить {username}: {e}")

        logger.info(f"tg_create_group: '{title}' → {group_id}")
        return group_id
    except Exception as e:
        logger.error(f"tg_create_group failed: {e}")
        return None
    finally:
        await client.disconnect()


async def tg_promote_bot_admin(bot_username: str, group_id: int) -> bool:
    """Выдать боту права администратора — работает с обычными чатами и супергруппами."""
    from telethon.tl.types import Chat, Channel
    client = await get_telethon_client()
    try:
        group_entity = await client.get_entity(group_id)
        bot_entity   = await client.get_entity(bot_username)

        if isinstance(group_entity, Channel):
            # Супергруппа или канал
            rights = ChatAdminRights(post_messages=True)
            await client(EditAdminRequest(
                channel=group_entity, user_id=bot_entity,
                admin_rights=rights, rank="Bot"
            ))
        else:
            # Обычный чат (Chat)
            await client(EditChatAdminRequest(
                chat_id=group_entity.id,
                user_id=bot_entity,
                is_admin=True
            ))

        logger.info(f"tg_promote_bot_admin: {bot_username} → admin in {group_id}")
        return True
    except Exception as e:
        logger.error(f"tg_promote_bot_admin failed for {bot_username}: {e}")
        return False
    finally:
        await client.disconnect()


async def railway_graphql(query: str, variables: dict = None) -> dict:
    """Выполнить GraphQL запрос к Railway API.
    Бросает RuntimeError при GraphQL-уровне ошибок (auth, permission и т.п.).
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
        payload = {"query": query}
        if variables:
            payload["variables"] = variables
        r = await client.post(
            "https://backboard.railway.com/graphql/v2",
            headers={"Authorization": f"Bearer {RAILWAY_TOKEN_VAL}", "Content-Type": "application/json"},
            json=payload
        )
        r.raise_for_status()
        data = r.json()
        if data.get("data") is None and data.get("errors"):
            msgs = "; ".join(e.get("message", "?") for e in data["errors"])
            raise RuntimeError(f"Railway GraphQL error: {msgs}")
        return data

async def railway_set_variables(service_id: str, variables: dict) -> bool:
    """Записать переменные окружения в Railway сервис."""
    data = await railway_graphql(
        """mutation($input: VariableCollectionUpsertInput!) {
             variableCollectionUpsert(input: $input)
           }""",
        {"input": {
            "projectId": PROJECT_ID,
            "environmentId": ENVIRONMENT_ID,
            "serviceId": service_id,
            "variables": variables
        }}
    )
    return data.get("data", {}).get("variableCollectionUpsert") is True

async def railway_get_service_id(repo_name: str) -> str | None:
    """Найти service_id по имени сервиса в проекте."""
    data = await railway_graphql(
        """query($id: String!) {
             project(id: $id) { services { edges { node { id name } } } }
           }""",
        {"id": PROJECT_ID}
    )
    for edge in ((data.get("data") or {}).get("project") or {}).get("services", {}).get("edges") or []:
        if edge["node"]["name"] == repo_name:
            return edge["node"]["id"]
    return None

async def railway_get_variables(service_id: str) -> dict:
    """Прочитать переменные окружения сервиса Railway (для check_var)."""
    data = await railway_graphql(
        """query($proj: String!, $svc: String!, $env: String!) {
             variables(projectId: $proj, serviceId: $svc, environmentId: $env)
           }""",
        {"proj": PROJECT_ID, "svc": service_id, "env": ENVIRONMENT_ID}
    )
    return (data.get("data") or {}).get("variables") or {}


async def railway_get_bot_url(name_hint: str) -> str:
    """Ищет сервис на Railway по имени, возвращает публичный URL."""
    try:
        data = await railway_graphql(
            """query($id: String!) {
                 project(id: $id) { services { edges { node { id name } } } }
               }""",
            {"id": PROJECT_ID}
        )
        services = ((data.get("data") or {}).get("project") or {}).get("services", {}).get("edges") or []
        # Нормализуем hint
        hint_clean = name_hint.replace("_bot", "").replace("_", "-").replace(" ", "-").lower()
        candidates = [
            hint_clean + "-bot",
            hint_clean,
            name_hint.replace("_", "-").lower(),
        ]
        for svc_edge in services:
            svc_name = svc_edge["node"]["name"].lower()
            for c in candidates:
                if svc_name == c or svc_name.startswith(c):
                    return f"https://{svc_edge['node']['name']}-production.up.railway.app"
    except Exception as e:
        logger.debug(f"railway_get_bot_url failed: {e}")
    # fallback: стандартный паттерн по hint
    clean = name_hint.replace("_", "-").lower()
    if not clean.endswith("-bot"):
        clean = clean.rstrip("-bot") + "-bot"
    return f"https://{clean}-production.up.railway.app"


async def railway_create_service(repo_name: str, bot_display_name: str, variables: dict = None) -> dict:
    """Создать сервис на Railway. Если уже существует — использовать его."""
    # Проверяем существует ли уже
    existing_id = await railway_get_service_id(repo_name)
    if existing_id:
        logger.info(f"[railway] сервис '{repo_name}' уже существует: {existing_id}")
        service_id = existing_id
    else:
        data = await railway_graphql(
            """mutation($input: ServiceCreateInput!) {
                 serviceCreate(input: $input) { id name }
               }""",
            {"input": {
                "projectId": PROJECT_ID,
                "name": repo_name,
                "source": {"repo": f"unperson22-alt/{repo_name}"}
            }}
        )
        if "errors" in data:
            raise Exception(f"serviceCreate failed: {data['errors'][0]['message']}")
        service_id = data["data"]["serviceCreate"]["id"]
        logger.info(f"[railway] создан сервис '{repo_name}': {service_id}")

    # Записать переменные если переданы
    if variables:
        ok = await railway_set_variables(service_id, variables)
        if not ok:
            logger.warning(f"railway_set_variables returned False for {repo_name}")

    return {"service_id": service_id}


async def handle_natural_language(message_text: str, chat_id: int, reply_func, history: list = None, silent: bool = False, repo_override: str = "", file_path_override: str = "", proposal_chat_id: int = 0):
    """Process any natural language request — detect intent and execute."""
    # Читаем ops.md — лог последних действий Claude и Силли
    # Это даёт Силли контекст о том что уже было сделано
    ops_context = ""
    try:
        r_ops = await get_redis()
        raw_ops = ""
        if r_ops:
            ops_entries = await r_ops.lrange("office:ops_log", 0, 19)
            raw_ops = "\n".join(reversed(ops_entries)) if ops_entries else ""
        if raw_ops:
            # Берём последние 3000 символов — самые свежие записи
            ops_context = raw_ops[-3000:]
    except Exception:
        pass  # ops.md может не существовать — не страшно

    # ops.md используется ТОЛЬКО для answer-контекста, не для intent detection
    # (иначе "pilly-bot создан" в ops.md сбивает intent с create_bot на get_bot_token)

    # Detect intent via Haiku (cheap) — без ops.md контекста
    intent_input = message_text[:500] if len(message_text) > 500 else message_text
    raw = await ask_claude(intent_input, system=INTENT_PROMPT, model="claude-haiku-4-5-20251001")
    raw = raw.strip()
    start, end = raw.find("{"), raw.rfind("}") + 1
    if start != -1 and end > start:
        raw = raw[start:end]

    try:
        intent_data = json.loads(raw)
    except Exception:
        # Keyword fallback — better than failing silently
        msg_lower = message_text.lower()
        # Только явные императивные команды — не вопросы о процессе
        question_signals = ["как ", "какой", "какие", "что нужно", "с чего", "как создать",
                            "как задеплоить", "как разверн", "подскажи", "расскажи", "объясни"]
        is_question = any(w in msg_lower for w in question_signals)
        if not is_question and any(w in msg_lower for w in ["создай бота", "create bot", "зарегистрируй бота",
                                           "зарегистрировать бота", "newbot", "зарегистрируй нового",
                                           "создать нового бота", "создай нового"]):
            intent_data = {"intent": "create_bot", "repo": None, "path": None, "task": message_text, "confidence": "low"}
        elif any(w in msg_lower for w in ["задеплой", "redeploy", "передеплой"]):
            intent_data = {"intent": "deploy", "repo": None, "path": None, "task": message_text, "confidence": "low"}
        elif any(w in msg_lower for w in ["залей", "push", "запиши код"]):
            intent_data = {"intent": "push_code", "repo": None, "path": None, "task": message_text, "confidence": "low"}
        else:
            # Fallback: just answer conversationally
            answer = await ask_claude(message_text, system=CHAT_PROMPT, model="claude-haiku-4-5-20251001")
            await reply_func(answer)
            return

    intent     = intent_data.get("intent", "answer")
    # Явный repo из payload (HTTP /task) приоритетнее догадки интента-LLM —
    # иначе планировщик путал репо (tilly-trader ↔ tilly-bot).
    repo       = repo_override or intent_data.get("repo")
    path       = intent_data.get("path")
    task       = intent_data.get("task", message_text)
    _conf_raw = intent_data.get("confidence", 1.0)
    confidence = float(_conf_raw) if isinstance(_conf_raw, (int, float)) else {"high": 0.9, "medium": 0.6, "low": 0.3}.get(str(_conf_raw).lower(), 0.5)

    logger.info(f"[nl] intent={intent} confidence={confidence:.2f} repo={repo}")

    # Для деструктивных/долгих операций — требуем высокую уверенность
    DESTRUCTIVE = ("create_bot", "deploy", "push_code", "get_bot_token")
    if intent in DESTRUCTIVE and confidence < 0.75:
        await reply_func(
            f"🤔 Не уверен что правильно понял задачу (confidence={confidence:.0%}).\n"
            f"Уточни: ты хочешь чтобы я **{intent}** выполнил, или это вопрос?"
        )
        return

    if intent == "answer":
        # Для answer — используем ops.md как контекст и историю разговора
        answer_system = CHAT_PROMPT
        if ops_context:
            answer_system = (
                CHAT_PROMPT +
                f"\n\nПоследние действия в офисе (ops.md, последние записи):\n{ops_context}"
            )
        if history and len(history) > 1:
            answer_resp = await get_claude().messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                system=answer_system,
                messages=history[:-1] + [{"role": "user", "content": message_text}]
            )
            answer = answer_resp.content[0].text
        else:
            answer = await ask_claude(message_text, system=answer_system, model="claude-sonnet-4-6")
        await reply_func(answer)


    elif intent == "redis_query":
        """Выполняет реальные Redis операции — scan, get, hgetall, custom audit."""
        r = await get_redis()
        if not r:
            await reply_func("❌ Redis недоступен")
            return

        task_lower = task.lower()
        result: dict = {}

        # ── 0. DEL — обрабатывается первым и возвращает сразу ────────────
        if any(w in task_lower for w in ["del ", "delete ", "удали ключ"]):
            import re as _re_del
            del_match = _re_del.search(r'(office:[a-z:_0-9]+)', task_lower)
            if del_match:
                del_key = del_match.group(1)
                deleted = await r.delete(del_key)
                msg = f"✅ DEL {del_key}: {'удалён' if deleted else 'не найден'}"
                await reply_func(msg)
                return

        # ── 1. quality audit ──────────────────────────────────────────────
        if any(w in task_lower for w in ["quality", "реакци", "голос", "👍", "👎", "up", "down", "аудит"]):
            async for key in r.scan_iter("office:quality:*"):
                data = await r.hgetall(key)
                bot_name = key.split(":")[-1]
                result[f"quality:{bot_name}"] = {
                    "up":   int(data.get("up",   0)),
                    "down": int(data.get("down", 0)),
                }

        # ── 2. health audit ───────────────────────────────────────────────
        if any(w in task_lower for w in ["health", "здоровь", "status", "up/down", "живой", "живые"]):
            async for key in r.scan_iter("office:health:*"):
                agent = key.split(":")[-1]
                result[f"health:{agent}"] = await r.get(key)

        # ── 3. logs ───────────────────────────────────────────────────────
        if any(w in task_lower for w in ["log", "лог", "событи", "ошибк"]):
            bot_hint = None
            for bot_key in ["билли","тилли","милли","доктор","крисс","эллис","вилли","гослинг","силли","фили"]:
                if bot_key in task_lower:
                    bot_hint = bot_key
                    break
            import datetime as _dt
            today = _dt.date.today().isoformat()
            pattern = f"office:logs:{bot_hint}:{today}" if bot_hint else f"office:logs:*:{today}"
            async for key in r.scan_iter(pattern):
                entries = await r.lrange(key, 0, 19)
                result[key] = [json.loads(e) for e in reversed(entries)]

        # ── 4. routing misses ─────────────────────────────────────────────
        if any(w in task_lower for w in ["miss", "промах", "маршрут", "routing"]):
            raw_misses = await r.lrange("office:routing:misses", 0, 19)
            result["routing_misses"] = [json.loads(m) for m in raw_misses]

        # ── 5. произвольный scan pattern ─────────────────────────────────
        import re as _re
        pattern_match = _re.search(r'(office:[a-z:*_]+)', task_lower)
        if pattern_match and not result:
            pattern_str = pattern_match.group(1)
            if not pattern_str.endswith("*"):
                # Точный ключ — сначала проверяем тип чтобы не получить WRONGTYPE
                try:
                    key_type = await r.type(pattern_str)
                    key_type = key_type.decode() if isinstance(key_type, bytes) else str(key_type)
                    if key_type == "string":
                        val = await r.get(pattern_str)
                        result[pattern_str] = val
                    elif key_type == "hash":
                        result[pattern_str] = await r.hgetall(pattern_str)
                    elif key_type == "set":
                        members = await r.smembers(pattern_str)
                        result[pattern_str] = sorted([m.decode() if isinstance(m, bytes) else m for m in members])
                    elif key_type == "list":
                        result[pattern_str] = await r.lrange(pattern_str, 0, 19)
                    elif key_type == "zset":
                        result[pattern_str] = await r.zrange(pattern_str, 0, 19, withscores=True)
                    else:
                        result[pattern_str] = f"key_type={key_type}"
                except Exception as _e:
                    result[pattern_str] = f"error: {_e}"
            else:
                async for key in r.scan_iter(pattern_str):
                    try:
                        key_type = await r.type(key)
                        key_type = key_type.decode() if isinstance(key_type, bytes) else str(key_type)
                        if key_type == "string":
                            result[key] = await r.get(key)
                        elif key_type == "hash":
                            result[key] = await r.hgetall(key)
                        elif key_type == "set":
                            members = await r.smembers(key)
                            result[key] = sorted([m.decode() if isinstance(m, bytes) else m for m in members])
                        elif key_type == "list":
                            result[key] = await r.lrange(key, 0, 9)
                    except Exception:
                        pass

        # ── 6. если ничего не нашли — показываем ВСЁ ─────────────────────
        if not result:
            for ns in ["office:quality:*", "office:health:*", "office:routing:misses"]:
                if "*" in ns:
                    async for key in r.scan_iter(ns):
                        data = await r.hgetall(key)
                        if not data:
                            data = await r.get(key)

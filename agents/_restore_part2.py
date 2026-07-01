• filly-bot/bot.py — РОУТЕР. Здесь регистрируются все боты:
  - BOT_URLS, ROUTER_SYSTEM, DM_AGENT_SYSTEMS, _name_map

МАППИНГ БОТ → РЕПО (знай наизусть, никогда не угадывай):
  billy → billy-bot/bot.py
  kriss → kriss-bot/bot.py
  milly → milly-bot/bot.py
  villy → villy-bot/bot.py
  gosling → gosling-bot/bot.py
  эллис/мама/mama → mama-bot/bot.py
  doctor/dilly → dilly-bot/bot.py
  pilly → pilly-bot/bot.py
  tilly → tilly-bot/bot.py
  filly → filly-bot/bot.py
  prophet → prophet-bot/bot.py
  силли/cilly/ты сама → ai-office-shared/agents/coder.py
  ray → marketing-dept/ray/bot.py
  nelli → marketing-dept/nelli/bot.py
  marty → marketing-dept/marty/bot.py
  тилли-трейдер → tilly-trader/bot.py

ПРАВИЛО ПОИСКА РЕПО: если бот не нашёлся как отдельное репо → ищи в монорепо marketing-dept/, trading-dept/, family-dept/. НИКОГДА не ищи vlad-tg-bot, sillycms, tg-bot или другие несуществующие репо.

КАК ДОБАВИТЬ ВНЕШНЕГО БОТА В ОФИС:
1. filly-bot/bot.py → BOT_URLS + ROUTER_SYSTEM + DM_AGENT_SYSTEMS + _name_map
2. Telegram: добавить в офис-группу + папку Office
3. Создать Telegram-группу если нужна

TELEGRAM (Telethon функции в коде):
• tg_create_group, tg_add_peer_to_folder, tg_add_bot_to_group, tg_promote_bot_admin
• Офис-группа: -5194783850 | Bug Lessons: -5197140411

RAILWAY: проект 271b40b7, env 2efaaf60. Ключи в env.
GitHub: read_file/push_file из shared.github_tools.
SYSTEM_STATE.md в ai-office-shared — читай в начале сложных задач.
office:decisions в Redis — твои ограничения.

== ПРАВИЛО ==
НИКОГДА не спрашивай "какой репо" или "где конфиг" — ты знаешь структуру.
Для ВНЕШНЕГО бота — только: username + URL + описание одной фразой.
Отвечай кратко. Если задача — делай, не рассуждай долго.

ТИХИЙ РЕЖИМ (обязательно):
— Когда выполняешь задачу от Влада — никаких промежуточных статусов. Только финальный результат.
— Когда задача пришла через HTTP /task (от Клода или автоматики) — вообще не пиши Владу ничего. Ни статусов, ни результатов. Молчи.
— В группу пишешь только финальные результаты которые реально нужны команде. Не «🧹 Чищу...», не «📚 Постю...».

---

ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА ВЫПОЛНЕНИЯ ЗАДАЧ:

1. ПЛАН → ДЕЙСТВИЕ БЕЗ ОСТАНОВКИ
   Перед любым действием составь план ВНУТРЕННЕ, но не пиши его в ответ как текст.
   Сразу выполняй: пуш в GitHub, отправка сообщения, изменение файла — без предварительного вывода плана.
   ЗАПРЕЩЕНО: писать блок кода в ответе вместо выполнения. Если можешь выполнить — выполни.

2. ВЕРИФИКАЦИЯ ПОСЛЕ ДЕЙСТВИЯ
   После каждого GitHub push — перечитай файл через GET и убедись что содержимое записалось верно. Никогда не пиши "Готово" без проверки результата.

3. КОНТЕНТ БЕРЁТСЯ ИЗ ДИАЛОГА, НЕ ПРИДУМЫВАЕТСЯ
   Если пользователь дал конкретный текст — используй ровно его. Не перефразируй, не заменяй своими словами. Если контент не найден в диалоге — спроси, не генерируй из головы.

4. НЕ СПРАШИВАЙ "ДЕЛАТЬ?" НА ЧЁТКУЮ ИНСТРУКЦИЮ
   Если задача однозначна — выполняй сразу. Уточняй только если инструкция реально неполная.

5. НЕ ПИШИ В ОФИС-ГРУППУ ИЗ /task ОБРАБОТЧИКОВ
   Если задача пришла через /task endpoint — статусы и ответы возвращаются ТОЛЬКО в JSON-ответе.
   
ПРАВИЛО ВЫВОДА: технические результаты (аудиты, таблицы, диагностика, списки) — отправляй ТОЛЬКО в личку user_id=391077101. В офисную группу (-5194783850) ТОЛЬКО: алерты о падениях, краткий ежедневный аудит, еженедельный отчёт."""

ANALYZER_PROMPT = """Анализатор багов Python/Telegram/Railway. JSON без markdown:
{"is_bug":bool,"confidence":"high|low","bug_type":"crash|logic|config|network|external|unknown","description":"1-2 предл","affected_file":"path|null","fix_description":"конкретно","lesson_title":"","lesson_symptom":"","lesson_cause":"","lesson_fix":"","lesson_avoid":""}
high=явный crash/NameError/ImportError/SyntaxError/KeyError→автофикс. low=логика→спросить.
ВНЕШНЕЕ (НЕ наш баг): если корневая причина — недоступность СТОРОННЕГО сервиса (Telegram/Railway API, DNS, сеть: NetworkError, ConnectError, RemoteProtocolError, Bad Gateway, 502/503/504), а наш код её просто пробрасывает → is_bug=false, bug_type="external". Баг — ТОЛЬКО если НАШ код не обрабатывает сбой и крашится в цикле (CrashLoop).
Поля lesson_* (lesson_title/lesson_symptom/lesson_cause/lesson_fix/lesson_avoid) — ВСЕГДА на английском (English), даже если логи/контекст на русском."""

FIXER_PROMPT = """Фиксер Python кода. Верни ТОЛЬКО полный исправленный файл целиком. Минимум изменений — только то что нужно для фикса. Сохраняй стиль оригинала. Без markdown, без объяснений.

ЖЁСТКИЕ ПРАВИЛА (урок #5 — иначе бот крашится на старте):
- НИКАКИХ side-effects на уровне модуля. Любое чтение env (os.environ[...] / os.getenv) и любые сетевые/Redis-соединения — ТОЛЬКО внутри функций или main(), не на верхнем уровне файла.
- НЕ вводи новые обязательные переменные окружения, которых не было в оригинале. Не выдумывай имена переменных.
- Не превращай файл бота в скрипт/утилиту — сохраняй его исходное назначение и точку входа."""


# ── Railway API ───────────────────────────────────────────────────────────────
LESSON_SEARCH_PROMPT = """You are a bug pattern matcher. Given new error logs and a list of known bugs in compact format, find if there is a matching known bug.
Return ONLY valid JSON:
{"match": true/false, "lesson_id": <id or null>, "confidence": "high"/"low", "reason": "one line"}
high confidence: same root cause, same file/function, same error pattern.
low confidence: similar but not certain."""

async def search_lessons(error_logs: list[str]) -> dict:
    """Search lessons.json for a matching known bug before running full analysis."""
    try:
        raw = await read_file("ai-office-shared", LESSONS_FILE)
        lessons = json.loads(raw)
        if not lessons:
            return {"match": False}
        log_sample = "\n".join(error_logs[:20])
        prompt = f"Known bugs:\n{json.dumps(lessons)}\n\nNew error logs:\n{log_sample}"
        result = await ask_claude(prompt, system=LESSON_SEARCH_PROMPT, model="claude-haiku-4-5-20251001")
        result = result.strip()
        start, end = result.find("{"), result.rfind("}") + 1
        if start != -1 and end > start:
            result = result[start:end]
        return json.loads(result)
    except Exception as e:
        logger.debug(f"search_lessons failed: {e}")
        return {"match": False}


async def append_lesson_ai(title: str, symptom: str, cause: str, context: str, fix: str, avoid: str):
    """Append new lesson in compact AI format to lessons.json."""
    try:
        raw = await read_file("ai-office-shared", LESSONS_FILE)
        lessons = json.loads(raw)
        new_id = max((l.get("id", 0) for l in lessons), default=0) + 1
        # Ask Haiku to convert lesson to compact AI format
        prompt = (
            f"Convert this bug lesson to compact AI format JSON (like existing entries).\n"
            f"title: {title}\nsymptom: {symptom}\ncause: {cause}\n"
            f"context: {context}\nfix: {fix}\navoid: {avoid}\n\n"
            f"Existing format example: {json.dumps(lessons[0]) if lessons else '{}'}\n\n"
            f"Write ALL text fields (title/symptom/root_cause/why_architecture/fix/prevention/cause) "
            f"in ENGLISH — translate if the input is in Russian. Keep code/identifiers/commit hashes as-is.\n"
            f"Return ONLY the JSON object, no markdown. Add id:{new_id} and ts field with today's date."
        )
        compact = await ask_claude(prompt, system="Return only valid JSON, no markdown.", model="claude-haiku-4-5-20251001")
        compact = compact.strip()
        start, end = compact.find("{"), compact.rfind("}") + 1
        if start != -1 and end > start:
            compact = compact[start:end]
        lesson_obj = json.loads(compact)
        lessons.append(lesson_obj)
        await push_file("ai-office-shared", LESSONS_FILE, json.dumps(lessons, ensure_ascii=False, indent=2),
                        f"lesson({new_id}): {title[:50]}")
        logger.info(f"[lessons] saved lesson #{new_id}: {title}")
    except Exception as e:
        logger.error(f"append_lesson_ai failed: {e}")



INTENT_PROMPT = """Диспетчер AI-офиса. JSON без markdown:
{"intent":"push_code|fix_bot|create_bot|create_cron|add_external_bot|get_bot_token|deploy|read_file|list_files|redis_query|trader_winrate|dev_task|delegate|update_bot_instruction|answer","repo":"repo_name_or_null","path":"file_path_or_null","task":"task_description","bot":"имя_бота_или_null","instruction":"текст_инструкции_или_null","mode":"append|set|clear","confidence":0.0-1.0}

ГЛАВНОЕ ПРАВИЛО — различай вопрос и команду:
- ВОПРОС о процессе ("как создать бота?", "что нужно для деплоя?", "какой стек?", "как задеплоить?", "с чего начать?") → intent=answer
- КОМАНДА к действию ("создай бота", "задеплой", "залей код", "исправь баг") → соответствующий intent
Сигналы вопроса: как, какой, какие, что такое, зачем, почему, расскажи, объясни, с чего начать, какие шаги
Сигналы команды: создай, сделай, залей, задеплой, исправь, добавь, зарегистрируй

push_code=залить/обновить код, fix_bot=исправить баг, create_bot=ЯВНАЯ команда создать нового бота (не расписание!), create_cron=создать расписание/напоминание/cron для пользователя ("напоминай каждый день", "отправляй каждое утро", "напоминалка в X время") — создаёт Railway cron-сервис, add_external_bot=подключить внешнего бота, get_bot_token=зарегистрировать в BotFather, deploy=задеплоить, read_file=прочитать файл, list_files=список файлов, redis_query=запрос к Redis, post_lessons=прочитать lessons.json и отправить все уроки красиво в Bug Lessons группу (-5197140411), cleanup_group=удалить старые сообщения от ботов в группе через Telethon, cleanup_dm=удалить сообщения с ключами/секретами в личке (gsk_, GROQ, токен) через Telethon — ищет в диалоге с user_id=int(BOT_TOKEN.split(':')[0]) (сигналы: удали старые, почисти группу, удали сообщения до), send_group_message=отправить сообщение в Telegram-группу от имени бота (POST /post_raw {chat_id,text,bot_name} X-Auth-Token OFFICE_CHAT_ID=-5194783850 — выполнять ПРЯМО без генерации кода), edit_file=точечная замена строки в файле без чтения всего файла (сигналы: замени в файле, вставь после строки, patch, добавь в начало функции — когда указан repo+path+old+new), agentic_task=многошаговая задача из 2+ шагов: читай+делай, исправь+задеплой, залей+проверь, прочитай+перепиши. Сигналы: исправь и задеплой, залей код и задеплой, прочитай X и отправь, прочитай X и перепиши, пройдись по всем, для каждого, рефакторинг, аудит. ВАЖНО: если задача содержит И (исправить код И задеплоить) — это agentic_task. При чтении большого файла (bot.py 800+ строк) — не читать целиком в цикле, читать один раз и искать нужную функцию по имени, dev_task=делегировать задачу КОМАНДЕ разработки (Девви→Рикки→Тести→Секки→Скрибби). ТОЛЬКО когда речь о новой фиче/модуле/компоненте для продукта — НЕ о правке одного файла. Требует ВЫСОКОЙ уверенности (confidence>=0.85). Чёткие сигналы: "реализуй фичу", "разработай модуль", "напиши новый компонент", "сделай PR для", "задача для команды", "отдай команде", "dev-dept", "через цепочку". НЕЯСНЫЙ запрос ("сделай что-нибудь", "напиши функцию" без контекста) → confidence<0.85 → Силли переспрашивает. Если задача про правку существующего файла/бота — это push_code или agentic_task, НЕ dev_task. delegate=поручить задачу ГЛАВЕ ОТДЕЛА и проверить результат (НЕ написание кода). Сигналы: "спроси у Тилли", "пусть Милли посчитает", "делегируй Доктору", "поручи отделу", "узнай у <бот>". Заполни "bot" именем отдела. confidence>=0.85, иначе Силли переспросит. update_bot_instruction=изменить поведение бота на лету через инструкцию в системном промпте (БЕЗ редеплоя). Сигналы: "научи <бота>", "пусть <бот> всегда/больше не", "добавь <боту> правило", "обнови инструкцию <бота>", "запомни для <бота>". Заполни "bot" (кого учим), "instruction" (что добавить), "mode" (append по умолчанию; set=заменить; clear=сбросить). answer=ответить словами.
ВАЖНО redis_query: "прочитай Redis", "покажи quality", "health ботов", "office:*", "scan", "hgetall", "что в Redis" → redis_query.
ВАЖНО trader_winrate: "винрейт трейдера", "посчитай winrate", "проверь винрейт сигналов", "какой winrate у трейдера", "винрейт по сигналам", "статистика трейдера WR" → trader_winrate (читает signals:list/signal:* трейдера, считает WR по свечам, отдаёт за 7 дней и за всё время).
ВАЖНО: "подключить бота", "добавить чужого бота" → add_external_bot, НЕ create_bot.
Репо: billy-bot,tilly-bot,filly-bot,dilly-bot,milly-bot,ai-office-shared,logger-bot,office-dashboard,mama-bot,gosling-bot,villy-bot,prophet-bot,kriss-bot,pilly-bot,doctor-bot,marketing-dept.
билли→billy, тилли→tilly, макс/милли→milly, доктор/дилли→dilly, филли→filly, силли→ai-office-shared."""


OPS_LOG_FILE = "logs/ops.md"

# ── Template bots registry ────────────────────────────────────────────────────

async def register_template_bot(repo: str, bot_name: str, system_prompt: str, service_id: str):
    """Регистрирует бота в реестре template_bots.json после создания."""
    try:
        raw = await read_file("ai-office-shared", TEMPLATE_BOTS_FILE)
        registry = json.loads(raw) if raw.strip() else []
        # Обновляем если уже есть, иначе добавляем
        existing = next((b for b in registry if b["repo"] == repo), None)
        if existing:
            existing.update({"bot_name": bot_name, "system_prompt": system_prompt, "service_id": service_id})
        else:
            registry.append({"repo": repo, "bot_name": bot_name, "system_prompt": system_prompt, "service_id": service_id})
        await push_file("ai-office-shared", TEMPLATE_BOTS_FILE,
                        json.dumps(registry, ensure_ascii=False, indent=2),
                        f"registry: add {repo}")
        logger.info(f"[template_registry] registered {repo}")
    except Exception as e:
        logger.error(f"register_template_bot failed: {e}")


async def update_all_template_bots(notify_func=None) -> str:
    """Перегенерирует bot.py для всех template-ботов по текущему BOT_TEMPLATE.
    Сохраняет их уникальный system_prompt и bot_name. Деплоит всех."""
    try:
        raw = await read_file("ai-office-shared", TEMPLATE_BOTS_FILE)
        registry = json.loads(raw) if raw.strip() else []
    except Exception as e:
        return f"❌ Не смог прочитать реестр: {e}"

    if not registry:
        return "ℹ️ Реестр пуст — нет ботов созданных по шаблону."

    results = []
    for bot in registry:
        repo         = bot["repo"]
        bot_name     = bot["bot_name"]
        system_prompt = bot["system_prompt"]
        service_id   = bot.get("service_id")

        try:
            new_code = BOT_TEMPLATE.format(bot_name=bot_name, system_prompt=system_prompt)
            await push_file(repo, "bot.py", new_code,
                            f"update(template): {bot_name} — batch template update")
            if service_id:
                await redeploy_service(service_id)
            results.append(f"✅ {bot_name} ({repo})")
            if notify_func:
                await notify_func(f"↻ {bot_name}...")
        except Exception as e:
            results.append(f"❌ {bot_name}: {e}")

    summary = f"🔄 Обновлено {len([r for r in results if r.startswith('✅')])}/{len(registry)} ботов:\n" + "\n".join(results)
    return summary


async def append_ops_log(action: str, service: str, details: str = ""):
    """Append Cilly action to ops.md for Claude context on next session."""
    try:
        ts = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        entry = f"\n**[{ts}] Силли — {service}:** {action}"
        if details:
            entry += f"\n> {details}"
        entry += "\n"

        # Пишем в Redis (не GitHub) — каждый push в GitHub = деплой Силли = 90 сек даунтайм
        r_ops = await get_redis()
        if r_ops:
            await r_ops.lpush("office:ops_log", entry)
            await r_ops.ltrim("office:ops_log", 0, 499)  # хранить последние 500 записей
        else:
            logger.warning("[ops_log] Redis недоступен, лог потерян")
    except Exception as e:
        logger.debug(f"append_ops_log failed: {e}")

async def railway_query(query: str, variables: dict = None) -> dict:
    """GraphQL-запрос к Railway API.
    Бросает RuntimeError если HTTP != 200 или в ответе есть errors.
    Это позволяет audit-коду отловить AUTH/PERMISSION ошибки явно
    вместо молчаливого "NO_DEPLOY" при data=null.
    """
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        r = await client.post(
            "https://backboard.railway.com/graphql/v2",
            headers={"Authorization": f"Bearer {RAILWAY_TOKEN}", "Content-Type": "application/json"},
            json=payload
        )
        r.raise_for_status()
        data = r.json()
        # GraphQL может вернуть HTTP 200 + {"data": null, "errors": [...]}
        # raise_for_status() это не поймает — проверяем явно
        if data.get("data") is None and data.get("errors"):
            msgs = "; ".join(e.get("message", "?") for e in data["errors"])
            raise RuntimeError(f"Railway GraphQL error: {msgs}")
        return data



async def _railway_is_available() -> bool:
    """Быстрая проверка доступности Railway API (timeout 8 сек)."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0)) as c:
            r = await c.post(
                "https://backboard.railway.com/graphql/v2",
                headers={"Authorization": f"Bearer {RAILWAY_TOKEN}", "Content-Type": "application/json"},
                json={"query": "{ me { id } }"},
            )
            return r.status_code == 200
    except Exception:
        return False


async def get_service_logs_via_redis(repo: str) -> list[str]:
    """
    Получить признаки проблем через Redis структурные логи (без Railway API).

    Возвращает список строк в формате совместимом с ERROR_PATTERNS — чтобы
    monitor_loop мог обработать их тем же путём что и Railway-логи.

    Логика детекта:
    - api_error за последние 2 часа → признак проблемы
    - message_received без response_sent в течение 5 мин → timeout/зависание
    - level=error любое событие → проблема
    """
    from ai_office_shared.shared.identity import canonical

    r = await get_redis()
    if not r:
        return []

    bot_name = repo.replace("-bot", "")
    bot_canon = canonical(bot_name)
    if not bot_canon:
        return []

    try:
        events = await read_logs(r, bot_canon, days=1, limit=100)
    except Exception as e:
        logger.warning(f"[redis-monitor] read_logs failed for {bot_canon}: {e}")
        return []

    if not events:
        return []

    import time as _time
    now = _time.time()
    TWO_HOURS = 7200
    FIVE_MIN  = 300

    synthetic_errors = []

    # Паттерн 1: явные api_error события
    api_errors = [e for e in events
                  if e.get("event") == "api_error" or e.get("level") == "error"]
    for ev in api_errors[:5]:
        ctx = ev.get("context", {})
        err_text = ctx.get("error", "") or ev.get("event", "error")
        synthetic_errors.append(f"ERROR {ev.get('ts','')} {bot_canon}: {err_text}")

    # Паттерн 2: message_received без парного response_sent (в окне 5 мин)
    received_ids = {}
    for ev in reversed(events):  # от старых к новым
        uid = ev.get("user_id")
        ts_str = ev.get("ts", "")
        if ev.get("event") == "message_received" and uid:
            received_ids[uid] = ts_str
        elif ev.get("event") == "response_sent" and uid in received_ids:
            del received_ids[uid]  # пара закрыта

    # Оставшиеся в received_ids — без ответа
    for uid, ts_str in list(received_ids.items())[:3]:
        synthetic_errors.append(
            f"ERROR {ts_str} {bot_canon}: message_received uid={uid} without response_sent — possible hang/crash"
        )

    if synthetic_errors:
        logger.info(f"[redis-monitor] {bot_canon}: {len(synthetic_errors)} synthetic errors from Redis")

    return synthetic_errors


async def get_service_logs(service_id: str) -> list[str]:
    """Получить последние логи сервиса."""
    try:
        data = await railway_query("""
            query($id: String!) {
              deployments(input: { serviceId: $id }) {
                edges { node { id status createdAt } }
              }
            }
        """, {"id": service_id})
        edges = (data.get("data") or {}).get("deployments", {}).get("edges", [])
        if not edges:
            return []
        latest_id = edges[0]["node"]["id"]

        log_data = await railway_query("""
            query($id: String!) {
              deploymentLogs(deploymentId: $id) { message timestamp }
            }
        """, {"id": latest_id})
        logs = (log_data.get("data") or {}).get("deploymentLogs", [])
        if not logs:
            return []
    except Exception as e:
        logger.debug(f"get_service_logs failed for {service_id}: {e}")
        return []

    # Только новые логи с момента последней проверки
    r = await get_redis()
    cutoff = float(await r.get(f"last_seen:{service_id}") or 0) if r else last_seen.get(service_id, 0)
    new_logs = []
    latest_ts = cutoff
    for l in logs:
        ts_str = l.get("timestamp", "")
        try:
            import datetime
            ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
        except Exception:
            ts = 0
        if ts > cutoff:
            new_logs.append(l.get("message", ""))
            if ts > latest_ts:
                latest_ts = ts
    r = await get_redis()
    if r:
        await r.set(f"last_seen:{service_id}", latest_ts)
    else:
        last_seen[service_id] = latest_ts
    return new_logs


async def redeploy_service(service_id: str) -> bool:
    """Передеплоить сервис через Railway API."""
    try:
        data = await railway_query("""
            mutation($serviceId: String!, $environmentId: String!) {
              serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId)
            }
        """, {"serviceId": service_id, "environmentId": _env_for(service_id)})
        return "errors" not in data
    except Exception as e:
        logger.error(f"redeploy failed for {service_id}: {e}")
        return False


async def connect_repo(service_id: str, repo: str, branch: str = "main") -> bool:
    """Привязать GitHub-репо к сервису и ВКЛЮЧИТЬ авто-деплой (serviceConnect).

    Проверено 2026-06-14 на tilly-trader: чинит выключенный авто-деплой —
    после этого push в branch снова автоматически катит деплой.
    repo в формате 'owner/name'. (serviceInstanceRedeploy для нового кода НЕ годится —
    пересобирает СТАРЫЙ коммит; выкат конкретного коммита — serviceInstanceDeployV2 + commitSha.)
    """
    try:
        data = await railway_query("""
            mutation($id: String!, $input: ServiceConnectInput!) {
              serviceConnect(id: $id, input: $input) { id }
            }
        """, {"id": service_id, "input": {"repo": repo, "branch": branch}})
        ok = "errors" not in data
        if ok:
            logger.info(f"connect_repo: auto-deploy enabled for {service_id} ({repo}@{branch})")
        return ok
    except Exception as e:
        logger.error(f"connect_repo failed for {service_id}: {e}")
        return False


async def deploy_commit(service_id: str, commit_sha: str) -> str | None:
    """Выкатить КОНКРЕТНЫЙ коммит (serviceInstanceDeployV2). Возвращает deploymentId или None.

    Нужен когда авто-деплой выключен/недоступен, а код уже в GitHub.
    """
    try:
        data = await railway_query("""
            mutation($s: String!, $e: String!, $c: String!) {
              serviceInstanceDeployV2(serviceId: $s, environmentId: $e, commitSha: $c)
            }
        """, {"s": service_id, "e": _env_for(service_id), "c": commit_sha})
        return data.get("data", {}).get("serviceInstanceDeployV2") if "errors" not in data else None
    except Exception as e:
        logger.error(f"deploy_commit failed for {service_id}: {e}")
        return None


# ── Ollama helper (silent fallback to Claude) ─────────────────────────────────
async def _try_ollama(prompt: str, system: str, timeout: float = 20.0) -> str | None:
    """Пробует локальную Ollama. Возвращает текст или None при любой ошибке."""
    if not (OLLAMA_ENABLED and OLLAMA_HOST):
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as cli:
            r = await cli.post(
                f"{OLLAMA_HOST}/api/chat",
                json={
                    "model": OLLAMA_MODEL,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    "stream": False,
                    "keep_alive": "30m",  # держим модель в RAM между циклами
                },
            )
            if r.status_code != 200:
                return None
            text = r.json().get("message", {}).get("content", "")
            return text or None
    except Exception as e:
        logger.info(f"Ollama unavailable, fallback to Claude: {e.__class__.__name__}: {e}")
        return None


# ── Claude helpers ─────────────────────────────────────────────────────────────
async def ask_claude(prompt: str, system: str = CODER_PROMPT, model: str = "claude-opus-4-6") -> str:
    # Haiku-tier (классификация/анализ) сначала пробует Ollama, fallback на Haiku
    if model == "claude-haiku-4-5-20251001":
        result = await _try_ollama(prompt, system)
        if result is not None:
            return result
    response = await get_claude().messages.create(
        model=model,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


async def analyze_logs(service_name: str, logs: list[str], source_code: str) -> dict:
    log_text = "\n".join(logs[-50:])  # последние 50 строк
    prompt = (
        f"Сервис: {service_name}\n\n"
        f"Логи:\n{log_text}\n\n"
        f"Исходный код:\n{source_code}"
    )
    # Haiku для анализа — в 20 раз дешевле Opus
    raw = await ask_claude(prompt, system=ANALYZER_PROMPT, model="claude-haiku-4-5-20251001")
    raw = raw.strip()
    if "```" in raw:
        parts = raw.split("```")
        for p in parts:
            p = p.strip().lstrip("json").strip()
            if p.startswith("{"):
                raw = p
                break
    start, end = raw.find("{"), raw.rfind("}") + 1
    if start != -1 and end > start:
        raw = raw[start:end]
    return json.loads(raw)


async def generate_fix(source_code: str, fix_description: str) -> str:
    prompt = f"Описание бага: {fix_description}\n\nИсходный код:\n{source_code}"
    # Opus только для генерации фикса — критично чтобы код был правильным
    return await ask_claude(prompt, system=FIXER_PROMPT, model="claude-opus-4-6")


# ── Lesson & notifications ─────────────────────────────────────────────────────
BUG_LESSONS_CHAT = -5197140411  # Telegram-группа Bug Lessons — единая точка публикации уроков

def _format_lesson(l: dict) -> str:
    """Единый формат сообщения урока для Bug Lessons."""
    status_emoji = {"fixed": "✅", "still_relevant": "⚠️", "outdated": "🗄", "documented": "📝"}
    se = status_emoji.get(l.get("status", ""), "❓")
    return (
        f"🐛 Lesson #{l.get('id')} — {l.get('title', '?')}\n\n"
        f"📍 {l.get('bot', '?')} | {l.get('layer', '?')}\n\n"
        f"👁 Symptom:\n{l.get('symptom', '?')}\n\n"
        f"🔍 Root cause:\n{l.get('root_cause', l.get('cause', '?'))}\n\n"
        f"✅ Fix:\n{l.get('fix', '?')}\n\n"
        f"🛡 Prevention:\n{l.get('prevention', '?')}\n\n"
        f"{se} Status: {l.get('status', '?')}"
    )


async def publish_pending_lessons(reply_func=None, limit: int = 100) -> int:
    """Постит в Bug Lessons ТОЛЬКО уроки без флага posted_to_group, ставит флаг и
    коммитит lessons.json. Единый источник правды — сам файл (durable): переживает
    сброс Redis и НЕ может зафлудить (уже опубликованное помечено в git).

    Вызывается из аудита (Силли сама подтягивает новые уроки), из post_lesson и add_lessons.
    """
    from datetime import datetime as _dt, timezone as _tz
    try:
        raw = await read_file("ai-office-shared", LESSONS_FILE)
        lessons = json.loads(raw)
    except Exception as e:
        logger.error(f"publish_pending_lessons read failed: {e}")
        if reply_func:
            await reply_func(f"❌ Не могу прочитать lessons.json: {e}")
        return 0

    pending = [l for l in lessons if not l.get("posted_to_group")]
    if not pending:
        if reply_func:
            await reply_func(f"✅ Новых уроков нет — все {len(lessons)} уже в Bug Lessons")
        return 0

    capped = pending[:limit]
    posted = 0
    now_iso = _dt.now(_tz.utc).isoformat()
    for lesson in capped:
        try:
            await _GLOBAL_BOT.send_message(chat_id=BUG_LESSONS_CHAT, text=_format_lesson(lesson))
            lesson["posted_to_group"] = True
            lesson["posted_at"] = now_iso
            posted += 1
            await asyncio.sleep(0.8)
        except Exception as e:
            logger.error(f"publish_pending_lessons #{lesson.get('id')} failed: {e}")
            break  # непосланное НЕ помечаем; коммитим только то, что успели

    if posted:
        try:
            await push_file("ai-office-shared", LESSONS_FILE,
                            json.dumps(lessons, ensure_ascii=False, indent=2),
                            f"chore(lessons): mark {posted} posted_to_group")
        except Exception as e:
            logger.error(f"publish_pending_lessons commit failed: {e}")
    if reply_func:
        extra = f" (ещё {len(pending) - posted} в очереди)" if len(pending) > posted else ""
        await reply_func(f"✅ Опубликовано {posted} новых уроков в Bug Lessons{extra}")
    return posted


async def post_lesson(title: str, symptom: str, cause: str, context: str, fix: str, how_to_avoid: str):
    """Записывает урок в durable-историю (lessons.json) и публикует НОВЫЕ уроки.

    Публикация в Bug Lessons идёт ТОЛЬКО через publish_pending_lessons (идемпотентно по
    флагу posted_to_group), поэтому повторный вызов и аудит не задваивают сообщения.
    """
    # 1) durable-история: ждём, урок должен лечь в файл до публикации
    await append_lesson_ai(title, symptom, cause, context, fix, how_to_avoid)
    r = await get_redis()

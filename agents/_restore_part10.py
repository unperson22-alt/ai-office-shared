    new_emojis = {x.emoji for x in (reaction.new_reaction or []) if getattr(x, "emoji", None)}
    added   = new_emojis - old_emojis
    removed = old_emojis - new_emojis

    delta_up   = sum(1 for e in added if e in REACTION_UP)   - sum(1 for e in removed if e in REACTION_UP)
    delta_down = sum(1 for e in added if e in REACTION_DOWN) - sum(1 for e in removed if e in REACTION_DOWN)

    if delta_up == 0 and delta_down == 0:
        return

    try:
        key = f"office:quality:{BOT_NAME_LOWER}"
        if delta_up:
            await r.hincrby(key, "up", delta_up)
        if delta_down:
            await r.hincrby(key, "down", delta_down)
        logger.info(f"REACTION msg={msg_id} added={added} removed={removed} du={delta_up} dd={delta_down}")
    except Exception as e:
        logger.warning(f"quality hincrby failed: {e}")



async def handle_post_raw(request):
    """Send a raw message to any chat. Auth: X-Auth-Token = Railway token.
    Если передан bot_name — проксирует запрос на /send нужного бота.
    """
    auth = request.headers.get("X-Auth-Token", "")
    if not auth or auth != RAILWAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    data = await request.json()
    chat_id = data.get("chat_id")
    text = data.get("text", "")
    parse_mode = data.get("parse_mode", "HTML")
    bot_name = data.get("bot_name", "").upper()
    if not chat_id or not text:
        return web.json_response({"error": "chat_id and text required"}, status=400)
    if bot_name and bot_name in BOT_URLS:
        bot_url = BOT_URLS[bot_name].rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    f"{bot_url}/send",
                    json={"chat_id": int(chat_id), "text": text},
                    headers=office_headers({"X-Secret-Token": HTTP_SECRET_BOTS}),
                )
                return web.json_response(r.json())
        except Exception as e:
            return web.json_response({"error": f"proxy error: {e}"}, status=500)
    try:
        await bot.send_message(chat_id=int(chat_id), text=text, parse_mode=parse_mode)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)




async def handle_envcheck(request):
    """Диагностика: показывает какие env vars заданы (без значений)."""
    import os
    vars_set = []
    vars_missing = []
    for v in ["CODER_BOT_TOKEN","ANTHROPIC_API_KEY","REDIS_URL","OFFICE_CHAT_ID",
              "LESSONS_CHAT_ID","GH_PAT","RAILWAY_TOKEN_VLAD","YOUR_TELEGRAM_ID",
              "TELEGRAM_API_ID","TELEGRAM_API_HASH","TELETHON_SESSION","OLLAMA_ENABLED"]:
        if os.environ.get(v):
            vars_set.append(v)
        else:
            vars_missing.append(v)
    return web.json_response({"set": vars_set, "missing": vars_missing})


async def handle_redis(request):
    """Proxy Redis commands for Claude diagnostics. Auth required."""
    auth = request.headers.get("X-Auth-Token", "")
    if not auth or auth != RAILWAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
        cmd = body.get("cmd", "")
        args = body.get("args", [])
        if not cmd:
            return web.json_response({"error": "cmd required"})
        r = await get_redis()
        if not r:
            return web.json_response({"error": "redis unavailable"})
        result = await r.execute_command(cmd, *args)
        if isinstance(result, list):
            result = [v.decode() if isinstance(v, bytes) else v for v in result]
        elif isinstance(result, bytes):
            result = result.decode()
        return web.json_response({"result": result})
    except Exception as e:
        return web.json_response({"error": str(e)})


async def handle_web_search(request):
    """Shared web search endpoint for all office bots.
    POST /web_search {"query": "...", "n": 5}
    Auth: X-Auth-Token = Railway token.
    Returns: {"results": [{"title": ..., "url": ..., "snippet": ...}]}
    """
    auth = request.headers.get("X-Auth-Token", "")
    if not auth or auth != RAILWAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
        query = body.get("query", "").strip()
        n = int(body.get("n", 5))
        if not query:
            return web.json_response({"error": "query required"}, status=400)
        from ai_office_shared.shared.web_search import web_search
        results = await web_search(query, n)
        return web.json_response({"results": results, "count": len(results)})
    except Exception as e:
        logger.error(f"[web_search] endpoint error: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def vietnam_cron_loop():
    """Триггерит vietnam-bot каждый день в 01:00 UTC."""
    import datetime
    logger.info("[vietnam_cron] loop started (01:00 UTC daily)")
    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        target = now.replace(hour=1, minute=0, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        wait = (target - now).total_seconds()
        logger.info(f"[vietnam_cron] следующий запуск через {wait/3600:.1f}ч ({target.strftime('%d.%m %H:%M UTC')})")
        await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(
                    "https://vietnam-bot-production.up.railway.app/generate",
                    json={"send": True},
                    headers=office_headers(),
                )
            result = r.json()
            count = result.get("count", 0)
            logger.info(f"[vietnam_cron] ✅ отправлено {count} идей")
        except Exception as e:
            logger.error(f"[vietnam_cron] ❌ ошибка: {e}")
            await notify_office(f"⚠️ Vietnam-bot cron упал: {e}")
        await asyncio.sleep(60)




async def handle_add_lessons(request):
    """Добавить готовые уроки в lessons.json (GitHub) + выложить в Bug Lessons + дедуп.

    Используется Клодом в конце сессии: POST готовых уроков → Силли их персистит и постит.
    Auth: X-Auth-Token = RAILWAY_SECRET.
    Body: {"lessons":[{title,symptom,cause|root_cause,fix,prevention,bot?,layer?,status?,
           why_architecture?,context?}], "post":true, "dry_run":false}
    Идемпотентно по title (дубли по названию пропускаются)."""
    auth = request.headers.get("X-Auth-Token", "")
    if not auth or auth != RAILWAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
        incoming = body.get("lessons", [])
        do_post = body.get("post", True)
        dry_run = body.get("dry_run", False)
        if not isinstance(incoming, list) or not incoming:
            return web.json_response({"error": "lessons (non-empty list) required"}, status=400)
        import datetime as _dt
        raw = await read_file("ai-office-shared", LESSONS_FILE)
        lessons = json.loads(raw)
        existing_titles = {str(l.get("title", "")).strip().lower() for l in lessons}
        next_id = max((l.get("id", 0) for l in lessons), default=0) + 1
        added = []
        for item in incoming:
            title = str(item.get("title", "")).strip()
            if not title or title.lower() in existing_titles:
                continue
            entry = {
                "id": next_id,
                "date": item.get("date") or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"),
                "bot": item.get("bot", "cilly"),
                "layer": item.get("layer", "system"),
                "title": title,
                "symptom": item.get("symptom", ""),
                "root_cause": item.get("root_cause") or item.get("cause", ""),
                "fix": item.get("fix", ""),
                "prevention": item.get("prevention", ""),
                "why_architecture": item.get("why_architecture", ""),
                "cause": item.get("cause") or item.get("root_cause", ""),
                "status": item.get("status", "fixed"),
                "tag": item.get("tag", "system"),
            }
            if item.get("context"):
                entry["context"] = item["context"]
            lessons.append(entry)
            existing_titles.add(title.lower())
            added.append(entry)
            next_id += 1
        if not added:
            return web.json_response({"added": [], "note": "все уроки уже есть (дедуп по title)"})
        if dry_run:
            return web.json_response({"dry_run": True, "would_add": [l["id"] for l in added],
                                      "titles": [l["title"] for l in added]})
        # 1) персист в GitHub одним коммитом
        await push_file("ai-office-shared", LESSONS_FILE,
                        json.dumps(lessons, ensure_ascii=False, indent=2),
                        f"lessons: +{len(added)} (#{added[0]['id']}-#{added[-1]['id']})")
        # 2) опубликовать новые уроки через единый durable-механизм.
        #    Добавленные на шаге 1 записи ещё без posted_to_group → publish их подхватит,
        #    пометит и закоммитит. Без ручного цикла и без Redis-дедупа (single source of truth).
        posted = await publish_pending_lessons() if do_post else 0
        return web.json_response({"added": [l["id"] for l in added], "posted": posted})
    except Exception as e:
        logger.error(f"handle_add_lessons: {e}")
        return web.json_response({"error": str(e)}, status=500)


def _age_seconds(iso_ts: str) -> float:
    """Возраст ISO8601-таймстампа (UTC, формат taskboard) в секундах. 0 при ошибке."""
    from datetime import datetime, timezone
    try:
        dt = datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return 0.0


async def _review_quality_and_routing(r):
    """B3: проактивно заводит задачи по падению качества и роутинг-промахам."""
    from ai_office_shared.shared.identity import BOTS, display, redis_key

    open_tasks = await tb.list_tasks(r, status="open", limit=200)
    open_titles = {t.get("title", "") for t in open_tasks}

    # 1. Качество: много 👎 относительно 👍
    for canon in BOTS:
        try:
            h = await r.hgetall(redis_key(canon, "quality"))
        except Exception:
            continue
        if not h:
            continue
        up = int(h.get("up", 0) or 0)
        down = int(h.get("down", 0) or 0)
        if down >= 5 and down > up:
            title = f"Качество {display(canon)}: {up}👍/{down}👎 — разобраться"
            if title not in open_titles:
                await tb.create_task(r, title, created_by="силли", assignee=canon, status="open")
                await notify_office(f"📉 {title}. Завёл задачу на доске.")

    # 2. Роутинг-промахи: частые промахи на агента
    try:
        raw = await r.lrange("office:routing:misses", 0, 99)
    except Exception:
        raw = []
    counts: dict = {}
    for item in raw:
        try:
            d = json.loads(item)
            agent = d.get("agent", "?")
            counts[agent] = counts.get(agent, 0) + 1
        except Exception:
            continue
    for agent, n in counts.items():
        if n >= 5:
            title = f"Роутинг-промахи {agent}: {n} за окно — проверить доступность/маршрут"
            if title not in open_titles:
                await tb.create_task(r, title, created_by="силли",
                                     assignee=str(agent).lower(), status="open")
                await notify_office(f"🧭 {title}. Завёл задачу на доске.")


async def _management_tick():
    """Один проход проактивного управления: доска + метрики."""
    r = await get_redis()
    if not r:
        return
    # Доска: подвисшие и заблокированные верхнеуровневые задачи
    active = await tb.list_tasks(
        r, status={"in_progress", "needs_fix", "blocked"}, parent_id="", limit=100,
    )
    for t in active:
        tid = t.get("id")
        status = t.get("status")
        age = _age_seconds(t.get("updated_at", ""))
        if status == "blocked" and not t.get("escalated"):
            await tb.update_status(r, tid, "blocked", escalated=True)
            await notify_office(
                f"🚨 Задача [{tid}] заблокирована: {t.get('title','')[:80]}\n"
                f"Причина: {t.get('result','')[:200]}\nНужен твой разбор, шеф."
            )
        elif status in ("in_progress", "needs_fix") and age > MGMT_STUCK_AFTER_SEC \
                and not t.get("escalated"):
            # нудж один раз (помечаем escalated, чтобы не спамить)
            await tb.update_status(r, tid, status, escalated=True)
            await notify_office(
                f"⏳ Задача [{tid}] висит ~{int(age // 3600)}ч в статусе {status}: "
                f"{t.get('title','')[:80]}"
            )
    # B3: метрики качества и роутинга → проактивные задачи
    await _review_quality_and_routing(r)


async def management_loop():
    """
    Проактивная петля управления (A4 + B3). В отличие от monitor_loop (реактивно
    чинит баги), эта ревьюит доску задач и метрики и сама инициирует работу.
    Все РИСК-действия всё равно идут через approval-гейт (/approve) — петля только
    нуджит, эскалирует и заводит задачи на доске.
    """
    await asyncio.sleep(60)  # дать боту стартовать
    logger.info("[management] started")
    while True:
        if MONITOR_PAUSED():
            logger.info("[management] paused via CILLY_MONITOR_PAUSED, sleeping...")
            await asyncio.sleep(60)
            continue
        try:
            await _management_tick()
        except Exception as e:
            logger.error(f"[management] tick error: {e}")
        await asyncio.sleep(MANAGEMENT_INTERVAL)


async def main():
    # Загружаем office:decisions из Redis при старте
    await init_office_decisions()
    asyncio.create_task(monitor_loop())
    asyncio.create_task(daily_audit_loop())
    asyncio.create_task(vietnam_cron_loop())
    asyncio.create_task(management_loop())
    asyncio.create_task(publish_pending_on_startup())  # дозалить pending-уроки (без удалений)
    asyncio.create_task(_lessons_en_migration_once())   # одноразовый перепост уроков на английском
    # HTTP server for Filly routing (office RPC auth via middleware — Layer 1/2)
    app = web.Application(middlewares=[office_auth_middleware])
    app.router.add_post("/task", handle_cilly_task)
    app.router.add_get("/secrets", handle_secrets)
    app.router.add_post("/post_raw", handle_post_raw)
    app.router.add_post("/promote_bots", handle_promote_bots)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/envcheck", handle_envcheck)
    app.router.add_post("/redis", handle_redis)
    app.router.add_post("/add_lessons", handle_add_lessons)
    app.router.add_post("/web_search", handle_web_search)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080)))
    await site.start()
    logger.info("[http] Cilly HTTP server started on :8080")
    # Weekly report handlers (/weekly, /approve, /skip)
    _redis_for_weekly = await get_redis()
    if _redis_for_weekly:
        register_weekly_handlers(dp, _redis_for_weekly, claude)
    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types()
    )


if __name__ == "__main__":
    asyncio.run(main())

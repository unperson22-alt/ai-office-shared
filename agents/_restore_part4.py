                    all_errors[repo] = errs
            except Exception:
                pass

        if all_errors:
            # Просим Haiku найти новые паттерны которых нет в known bugs
            errors_summary = "\n---\n".join(
                f"{repo}:\n" + "\n".join(errs[:10])
                for repo, errs in all_errors.items()
            )
            known_summary = json.dumps(
                [{"id": l.get("id"), "title": l.get("title"), "symptom": l.get("symptom","")} for l in existing_lessons],
                ensure_ascii=False
            )
            scan_prompt = (
                f"Known bug lessons:\n{known_summary}\n\n"
                f"Today's errors by service:\n{errors_summary}\n\n"
                f"Find errors that are NOT covered by known lessons. "
                f"For each new unique bug pattern return JSON array (max 3):\n"
                f'[{{"service":"...","title":"...","symptom":"...","cause":"...","fix":"...","avoid":"..."}}]\n'
                f"Return empty array [] if nothing new. JSON only, no markdown."
            )
            raw_new = await ask_claude(scan_prompt, system="Return only valid JSON array, no markdown.", model="claude-haiku-4-5-20251001")
            raw_new = raw_new.strip()
            s, e = raw_new.find("["), raw_new.rfind("]") + 1
            new_bugs = json.loads(raw_new[s:e]) if s != -1 and e > s else []

            for bug in new_bugs[:3]:
                await post_lesson(
                    title=bug.get("title", "Unknown bug"),
                    symptom=bug.get("symptom", ""),
                    cause=bug.get("cause", ""),
                    context=bug.get("service", ""),
                    fix=bug.get("fix", ""),
                    how_to_avoid=bug.get("avoid", "")
                )
                new_lesson_count += 1

    except Exception as e:
        logger.error(f"[daily_audit] bug scan failed: {e}")

    if new_lesson_count:
        lines.append(f"📚 Новых уроков записано: {new_lesson_count}")
    else:
        lines.append("📚 Новых паттернов багов не найдено")

    # 4b. Публикация новых уроков в Bug Lessons — Силли подтягивает их сама на аудите
    #     (durable: постятся только уроки без posted_to_group, повтор/флуд исключён)
    try:
        published = await publish_pending_lessons()
        if published:
            lines.append(f"📤 Опубликовано новых уроков в Bug Lessons: {published}")
    except Exception as e:
        logger.error(f"[daily_audit] publish lessons failed: {e}")

    # 5. Итог
    lines.append("")
    status_icon = "🟢" if not deploy_fail and not health_fail and not error_services else "🟡"
    lines.append(f"{status_icon} Статус офиса: {'НОРМА' if status_icon == '🟢' else 'ТРЕБУЕТ ВНИМАНИЯ'}")

    return "\n".join(lines)


async def daily_audit_loop():
    """Запускать полный аудит дважды в сутки: 09:00 и 18:00 UTC."""
    import datetime
    logger.info("[daily_audit] loop started (09:00 + 18:00 UTC)")

    AUDIT_HOURS = [9, 18]  # утренний и вечерний аудит

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        # Ищем ближайший слот из AUDIT_HOURS
        target = None
        for hour in AUDIT_HOURS:
            candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if candidate > now:
                target = candidate
                break
        if target is None:
            # Все слоты сегодня прошли — берём первый завтра
            target = now.replace(hour=AUDIT_HOURS[0], minute=0, second=0, microsecond=0)
            target += datetime.timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        slot_label = "утренний" if target.hour == 9 else "вечерний"
        logger.info(f"[daily_audit] следующий аудит ({slot_label}) через {wait_seconds/3600:.1f}ч ({target.strftime('%d.%m %H:%M UTC')})")

        await asyncio.sleep(wait_seconds)

        try:
            report = await run_daily_audit()
            await notify_office(report)
            logger.info(f"[daily_audit] ✅ {slot_label} отчёт отправлен")
            await append_ops_log("daily_audit", "all_services", report[:300])
        except Exception as e:
            logger.error(f"[daily_audit] failed: {e}")
            await notify_office(f"⚠️ Аудит ({slot_label}) упал: {e}")

        await asyncio.sleep(60)  # небольшой отступ чтобы не запустить дважды



async def _deep_diagnose_and_escalate(
    repo: str,
    service_id: str,
    error_signature: str,
    error_logs: list[str],
    fix_count: int,
    redis_client,
):
    """
    Умная диагностика повторяющегося бага.
    Вместо немедленной эскалации — анализирует сам:
    1. Проверяет сколько других ботов имеют эту же сигнатуру
    2. Проверяет это деплой-шум или реальный баг
    3. Читает исходник + Redis логи + историю фиксов
    4. Просит Claude поставить диагноз
    5. Только если Claude не смог — эскалирует Владу с диагнозом
    """
    logger.info(f"[deep_diagnose] starting for {repo} sig={error_signature[:8]}")

    # ── 1. Проверяем сколько ботов имеют эту же сигнатуру ────────────────────
    affected_services = []
    if redis_client:
        async for key in redis_client.scan_iter(f"fix_count:*:{error_signature}"):
            svc = key.split(":")[1]
            count = int(await redis_client.get(key) or 0)
            affected_services.append((svc, count))

    systemic = len(affected_services) >= 3
    if systemic:
        # Та же ошибка в 3+ ботах = системный шум (деплой/сеть), не баг конкретного бота
        logger.info(f"[deep_diagnose] systemic noise: same sig in {len(affected_services)} services, skipping escalation")
        # Сбрасываем счётчики чтобы не эскалировать снова
        if redis_client:
            for svc, _ in affected_services:
                await redis_client.delete(f"fix_count:{svc}:{error_signature}")
        return  # Тихо, без эскалации

    # ── 2. Собираем контекст для глубокого анализа ───────────────────────────
    # 2a. Исходник бота
    source_code = "# не удалось прочитать"
    try:
        main_file = SERVICES.get(service_id, (None, "bot.py"))[1]
        source_code = await read_file(repo, main_file)
    except Exception:
        pass

    # 2b. Redis структурные логи
    redis_ctx = ""
    try:
        _r = await get_redis()
        if _r:
            from ai_office_shared.shared.identity import canonical
            bot_canon = canonical(repo.replace("-bot", ""))
            if bot_canon:
                events = await read_logs(_r, bot_canon, days=1, limit=30, level_filter=None)
                if events:
                    lines_out = []
                    for ev in events[:20]:
                        ts = ev.get("ts", "")[-8:]
                        lines_out.append(
                            f"[{ts}] {ev.get('level','?').upper()} "
                            f"{ev.get('event','?')} uid={ev.get('user_id','?')}"
                        )
                    redis_ctx = "\n--- Redis события (последние 20) ---\n" + "\n".join(lines_out)
    except Exception as _e:
        logger.warning(f"[deep_diagnose] redis ctx failed: {_e}")

    # 2c. Предыдущие попытки починки (ops.md)
    ops_ctx = ""
    try:
        r_ops = await get_redis()
        raw_ops = ""
        if r_ops:
            ops_entries = await r_ops.lrange("office:ops_log", 0, 19)
            raw_ops = "\n".join(reversed(ops_entries)) if ops_entries else ""
        if raw_ops:
            # Ищем записи про этот репо
            relevant = [l for l in raw_ops.split("\n") if repo in l or error_signature[:8] in l]
            if relevant:
                ops_ctx = "\n--- История правок (ops.md) ---\n" + "\n".join(relevant[-10:])
    except Exception:
        pass

    full_context = (
        f"Ошибки из логов (последние {len(error_logs)}):\n"
        + "\n".join(error_logs[:10])
        + f"\n\nИсходник (первые 3000 символов):\n{source_code[:3000]}"
        + redis_ctx
        + ops_ctx
    )

    # ── 3. Глубокий анализ Claude (Sonnet — дороже, но для реальной диагностики) ──
    DEEP_ANALYSIS_PROMPT = """Ты — senior инженер AI-офиса. Этот баг уже встречался 3+ раза и стандартный фикс не помог.

Твоя задача — поставить ТОЧНЫЙ диагноз:
1. Что конкретно ломается (строка кода, функция, контракт)
2. Почему стандартный фикс не помог (симптом лечили, а не причину?)
3. Что нужно исправить РЕАЛЬНО (на уровне логики, не патч)
4. Можешь ли ты это исправить сам прямо сейчас?

Отвечай JSON без markdown:
{
  "root_cause": "точная причина в 1-2 предложениях",
  "why_fix_failed": "почему предыдущие попытки не помогли",
  "real_fix": "что нужно сделать на самом деле",
  "can_self_fix": true/false,
  "self_fix_action": "push_code|redeploy|config_change|null",
  "self_fix_details": "конкретные изменения если can_self_fix=true",
  "confidence": "high|medium|low",
  "escalate_reason": "null или причина почему нужен человек"
}"""

    try:
        raw = await ask_claude(
            f"Повторяющийся баг в {repo} (сигнатура {error_signature[:8]}, fix_count={fix_count}):\n\n{full_context}",
            system=DEEP_ANALYSIS_PROMPT,
            model="claude-sonnet-4-6",
        )
        raw = raw.strip()
        s, e = raw.find("{"), raw.rfind("}") + 1
        diagnosis = json.loads(raw[s:e]) if s != -1 else {}
    except Exception as ex:
        logger.error(f"[deep_diagnose] claude analysis failed: {ex}")
        diagnosis = {"can_self_fix": False, "confidence": "low", "escalate_reason": f"анализ упал: {ex}"}

    can_fix = diagnosis.get("can_self_fix", False)
    confidence = diagnosis.get("confidence", "low")
    root_cause = diagnosis.get("root_cause", "неизвестно")
    real_fix = diagnosis.get("real_fix", "")

    logger.info(f"[deep_diagnose] diagnosis: can_fix={can_fix} confidence={confidence} cause={root_cause[:60]}")

    # ── 4. Пробуем починить сам ───────────────────────────────────────────────
    if can_fix and confidence in ("high", "medium"):
        action = diagnosis.get("self_fix_action")
        details = diagnosis.get("self_fix_details", "")

        await notify_office(
            f"🔍 *{repo}* — нашла причину повторяющегося бага:\n"
            f"_{root_cause}_\n\n"
            f"Применяю фикс: {real_fix[:200]}..."
        )

        if action == "push_code" and details:
            # Пытаемся применить фикс через analyze_logs → handle_bug pipeline
            fix_analysis = {
                "is_bug": True,
                "root_cause": root_cause,
                "fix_description": real_fix,
                "fix_code_snippet": details,
                "confidence": confidence,
            }
            await handle_bug(service_id, repo, repo,
                             SERVICES.get(service_id, (None, "bot.py"))[1],
                             fix_analysis)
        elif action == "redeploy":
            ok = await redeploy_service(service_id)
            if ok:
                logger.info(f"[diagnose] auto-redeploy ok for {repo}")
            else:
                await notify_office(f"⚠️ *{repo}* — редеплой не удался")
        # Сбрасываем счётчик после применения фикса
        if redis_client:
            await redis_client.delete(f"fix_count:{service_id}:{error_signature}")
        return

    # ── 5. Не смогла — эскалируем с ДИАГНОЗОМ, не просто криком ─────────────
    escalate_reason = diagnosis.get("escalate_reason") or "не смогла подобрать фикс с высокой уверенностью"

    await notify_office(
        f"⚠️ *{repo}* — повторяющийся баг, нужна помощь\n\n"
        f"*Причина:* {root_cause}\n"
        f"*Почему предыдущий фикс не помог:* {diagnosis.get('why_fix_failed', 'неизвестно')}\n"
        f"*Что нужно сделать:* {real_fix}\n\n"
        f"*Почему сама не исправила:* {escalate_reason}\n"
        f"Сигнатура: `{error_signature[:16]}` | fix_count={fix_count}"
    )
    logger.warning(f"[deep_diagnose] escalated {repo}: {escalate_reason}")

async def monitor_loop():
    """Фоновая задача: каждые 5 минут проверяет логи всех сервисов.

    Автономный режим: если Railway API недоступен (outage) — переключается
    на детект через Redis структурные логи. Фикс (GitHub push) не требует
    Railway API — Railway автодеплоит из ветки сам.
    """
    await asyncio.sleep(30)  # подождать пока бот стартует
    logger.info("[monitor] started")
    _railway_down_notified = False  # чтобы не спамить уведомлениями об outage

    while True:
        if MONITOR_PAUSED():
            logger.info("[monitor] paused via CILLY_MONITOR_PAUSED env var, sleeping...")
            await asyncio.sleep(60)
            continue

        # Проверяем Railway API один раз в начале цикла
        railway_ok = await _railway_is_available()

        if not railway_ok:
            if not _railway_down_notified:
                await notify_office(
                    "⚠️ *Railway API недоступен* — переключаюсь на Redis-мониторинг.\n"
                    "Фиксы через GitHub работают, Railway автодеплоит сам."
                )
                _railway_down_notified = True
            logger.warning("[monitor] Railway API down — using Redis fallback for all services")
        else:
            if _railway_down_notified:
                await notify_office("✅ Railway API снова доступен — возвращаюсь к полному мониторингу.")
                _railway_down_notified = False

        for service_id, (repo, main_file) in SERVICES.items():
            try:
                # Основной путь: Railway logs. Fallback: Redis structural logs
                if railway_ok:
                    logs = await get_service_logs(service_id)
                else:
                    logs = await get_service_logs_via_redis(repo)

                if not logs:
                    continue

                # === Filter Layer 1: если в логе вообще присутствует deployment noise — пропускаем весь цикл
                # (Conflict/getUpdates ошибки порождают stack trace из строк, не содержащих ignore-паттернов;
                #  они проходили per-line filter и шли на анализ к Claude. Это была реальная дыра.)
                if any(any(p in l for p in IGNORE_PATTERNS) for l in logs):
                    logger.info(f"[monitor] {repo}: deployment-related noise in logs (Conflict/restart), skipping whole cycle")
                    continue

                # === Filter Layer 2: есть ли реальные ошибки помимо deployment-шума
                error_logs = [l for l in logs if any(p in l for p in ERROR_PATTERNS)]
                if not error_logs:
                    continue

                # Доп. per-line ignore (на случай других известных шумовых паттернов)
                filtered_errors = [
                    l for l in error_logs
                    if not any(p in l for p in IGNORE_PATTERNS)
                ]
                if not filtered_errors:
                    logger.info(f"[monitor] {repo}: only ignorable errors after per-line filter, skipping")
                    continue
                error_logs = filtered_errors

                # === Filter Layer 3: внешний/транзиентный сбой — НЕ наш баг → МОЛЧИМ.
                # Telegram/Railway API, DNS, сеть (NetworkError/ConnectError/
                # RemoteProtocolError/Bad Gateway/5xx) при живом боте: предлагать фикс
                # нельзя — это не наша вина. Тихо логируем раз в EXTERNAL_FAULT_COOLDOWN,
                # в офис ничего не шлём. Watchdog поднимет бота, если он реально лёг.
                if classify_fault(error_logs) == "external":
                    import hashlib as _h3
                    _ext_sig = _h3.md5(
                        "\n".join(error_logs)[:500].encode()
                    ).hexdigest()[:12]
                    _ext_key = f"external_fault_seen:{service_id}:{_ext_sig}"
                    _r_ext = await get_redis()
                    _seen = bool(await _r_ext.get(_ext_key)) if _r_ext else False
                    if not _seen:
                        if _r_ext:
                            await _r_ext.setex(_ext_key, EXTERNAL_FAULT_COOLDOWN, "1")
                        try:
                            await log_event(
                                _r_ext, BOT_NAME_LOWER, "external_fault_ignored",
                                service=repo, sample="\n".join(error_logs[:3])[:300],
                            )
                        except Exception:
                            pass
                        logger.info(
                            f"[monitor] {repo}: внешний/сетевой сбой (не наш баг) — молчу, "
                            f"cooldown {EXTERNAL_FAULT_COOLDOWN//3600}ч"
                        )
                    continue

                # Дедупликация: УСТОЙЧИВАЯ сигнатура (тип ошибки + файл + сообщение
                # без чисел), чтобы один и тот же баг не пере-детектился из-за разных
                # номеров строк/динамики и не обнулял fix_count по кругу.
                import hashlib, re as _re
                _err_text = "\n".join(error_logs)
                _exc = _re.findall(r"\b([A-Za-z_]+(?:Error|Exception))\b", _err_text)
                _files = _re.findall(r'File "[^"]*?([^"/\\]+\.py)"', _err_text)
                _msg = ""
                for _line in reversed(error_logs):
                    if _exc and _exc[-1] in _line:
                        _msg = _line
                        break
                _msg_norm = _re.sub(r"0x[0-9a-fA-F]+|\d+", "", _msg).strip()
                _sig_basis = "|".join([
                    _exc[-1] if _exc else "",
                    _files[-1] if _files else "",
                    _msg_norm,
                ]).strip("|")
                if not _sig_basis:  # фолбэк: нормализованный текст без чисел
                    _sig_basis = _re.sub(r"0x[0-9a-fA-F]+|\d+", "", _err_text)[:500]
                error_signature = hashlib.md5(_sig_basis.encode()).hexdigest()
                now = time.time()
                redis_key = f"seen_error:{service_id}:{error_signature}"
                r = await get_redis()
                if r:
                    last_analysis = float(await r.get(redis_key) or 0)
                else:
                    last_analysis = seen_errors.get(f"{service_id}:{error_signature}", 0)
                if now - last_analysis < ERROR_COOLDOWN:
                    logger.info(f"[monitor] skipping duplicate error in {repo} (cooldown)")
                    continue
                if r:
                    await r.setex(redis_key, ERROR_COOLDOWN, now)  # auto-expires
                else:
                    seen_errors[f"{service_id}:{error_signature}"] = now
                    cutoff = now - ERROR_COOLDOWN
                    expired = [k for k, v in seen_errors.items() if v < cutoff]
                    for k in expired:
                        del seen_errors[k]

                # Счётчик повторений (правило D005)
                fix_count_key = f"fix_count:{service_id}:{error_signature}"
                r_count = await get_redis()
                fix_count = 0
                if r_count:
                    fix_count = int(await r_count.get(fix_count_key) or 0)
                    await r_count.incr(fix_count_key)
                    await r_count.expire(fix_count_key, 86400 * 7)

                if fix_count >= 3:
                    # Не просто кричать — сначала разобраться самой
                    logger.warning(f"[monitor] recurring error in {repo} fix_count={fix_count}, running deep analysis")
                    await _deep_diagnose_and_escalate(
                        repo, service_id, error_signature, error_logs, fix_count, r_count
                    )
                    continue

                logger.info(f"[monitor] found {len(error_logs)} error lines in {repo}, analyzing...")

                # Auto-pull структурных логов из Redis для обогащения контекста анализа
                redis_log_context = ""
                try:
                    _r_logs = await get_redis()
                    if _r_logs:
                        from ai_office_shared.shared.identity import canonical
                        bot_canon = canonical(repo.replace("-bot", ""))
                        if bot_canon:
                            recent_events = await read_logs(
                                _r_logs, bot_canon,
                                days=1, limit=30,
                                level_filter=None,
                            )
                            if recent_events:
                                lines = []
                                for ev in recent_events[:20]:
                                    ts = ev.get("ts", "")[-8:]  # HH:MM:SSZ
                                    lines.append(f"[{ts}] {ev.get('level','?').upper()} {ev.get('event','?')} {ev.get('context',{})}")
                                redis_log_context = "\n--- Redis структурные логи (последние 20 событий) ---\n" + "\n".join(lines)
                                logger.info(f"[monitor] pulled {len(recent_events)} Redis events for {bot_canon}")
                except Exception as _e:
                    logger.warning(f"[monitor] auto-pull Redis logs failed for {repo}: {_e}")

                # Читаем исходник
                try:
                    source_code = await read_file(repo, main_file)
                except Exception:
                    source_code = "# файл не удалось прочитать"

                # Если есть Redis-контекст — добавляем к source_code для анализа
                if redis_log_context:
                    source_code = source_code + "\n\n" + redis_log_context

                # Check known bugs first — урок РЕАЛЬНО гейтит, а не украшает текст.
                # Раньше high-confidence матч лишь менял уведомление, но дальше всё
                # равно шёл analyze_logs → handle_bug → новое предложение. Это и был
                # «бред»: «применяю известный фикс» и тут же «нашёл подозрительное».
                known = await search_lessons(error_logs)
                if known.get("match") and known.get("confidence") == "high":
                    lesson_id = known.get("lesson_id")
                    logger.info(f"[monitor] known bug match in {repo}: lesson #{lesson_id}")
                    # Дедуп: один разбор известного урока на сервис за cooldown —
                    # не открываем то же предложение каждый цикл.
                    _r_les = await get_redis()
                    _les_key = f"lesson_applied:{service_id}:{lesson_id}"
                    _already = bool(await _r_les.get(_les_key)) if _r_les else False
                    if _already:
                        logger.info(f"[monitor] {repo}: урок #{lesson_id} уже разобран недавно — молчу")
                        continue
                    if _r_les:
                        await _r_les.setex(_les_key, EXTERNAL_FAULT_COOLDOWN, "1")
                    # Известный урок = разбор уже есть. Не пере-генерируем фикс Opus-ом
                    # и НЕ открываем повторное предложение. Тихая заметка один раз.
                    await notify_office(
                        f"📚 Cilly: повтор известной проблемы в *{repo}* — урок #{lesson_id}.\n"
                        f"_{known.get('reason', '')}_\n"
                        f"Новых действий не требуется (фикс уже задокументирован)."
                    )
                    continue

                analysis = await analyze_logs(repo, error_logs, source_code)

                # Второй слой: анализатор сам мог распознать внешний сбой → молчим.
                if analysis.get("bug_type") == "external" or not analysis.get("is_bug"):
                    logger.info(f"[monitor] {repo}: анализатор — не наш баг ({analysis.get('bug_type')}), молчу")
                    continue

                await handle_bug(service_id, repo, repo, main_file, analysis)

            except Exception as e:
                logger.error(f"[monitor] error checking {repo}: {e}")

        await asyncio.sleep(MONITOR_INTERVAL)




# ── Bot creation pipeline ─────────────────────────────────────────────────────
PROJECT_ID = "271b40b7-199a-429a-88ef-ca417f26a638"
RAILWAY_TOKEN_VAL = os.getenv("RAILWAY_TOKEN_VLAD", "") or os.getenv("RAILWAY_TOKEN", "")

BOT_TEMPLATE = """import os, logging, asyncio, httpx
from aiohttp import web
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import anthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
YOUR_TELEGRAM_ID = int(os.environ["YOUR_TELEGRAM_ID"])
OFFICE_CHAT_ID   = os.environ.get("OFFICE_CHAT_ID", "")
LOG_BOT_URL      = os.environ.get("LOG_BOT_URL", "")
HTTP_PORT        = 8080

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
conversation_history = {{}}

SYSTEM = \"\"\"{system_prompt}\"\"\"

async def log(event: str, msg: str):
    if not LOG_BOT_URL:
        return
    try:
        async with httpx.AsyncClient() as c:
            await c.post(f"{{LOG_BOT_URL}}/log", json={{"agent": "{bot_name}", "type": event, "message": msg}}, timeout=5)
    except Exception:
        pass

async def send_to_group(text: str):
    if not OFFICE_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient() as c:
            await c.post(f"https://api.telegram.org/bot{{TELEGRAM_TOKEN}}/sendMessage",
                json={{"chat_id": OFFICE_CHAT_ID, "text": text}}, timeout=10)
    except Exception as e:
        logger.error(f"send_to_group failed: {{e}}")

async def process(message: str, user_id: int) -> str:
    if user_id not in conversation_history:
        conversation_history[user_id] = []
    conversation_history[user_id].append({{"role": "user", "content": message}})
    if len(conversation_history[user_id]) > 20:
        conversation_history[user_id] = conversation_history[user_id][-10:]
    r = client.messages.create(model="claude-sonnet-4-6", max_tokens=4096,
        system=SYSTEM, messages=conversation_history[user_id])
    text = next((b.text for b in r.content if hasattr(b, "text")), "[нет текста]")
    conversation_history[user_id].append({{"role": "assistant", "content": text}})
    return text

async def handle_task(request):
    data = await request.json()
    message = data.get("message", "")
    user_id = data.get("user_id", YOUR_TELEGRAM_ID)
    await log("MSG_IN", f"[HTTP] {{message[:80]}}")
    try:
        response = await process(message, user_id)
    except Exception as e:
        logger.error(f"process() error: {e}")
        return web.json_response({"status": "error", "responses": [str(e)]}, status=500)
    # В группу ТОЛЬКО если явно передан notify=True
    # По умолчанию ответ идёт только в HTTP response (личка или вызывающий)
    if data.get("notify", False):
        await send_to_group(f"{bot_name}:\n{response}")
    await log("MSG_OUT", f"{bot_name}: {{response[:80]}}")
    return web.json_response({{"status": "ok", "response": response}})

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != YOUR_TELEGRAM_ID:
        return
    if update.effective_chat.type in ["group", "supergroup"]:
        return
    msg = update.message.text
    # Перехват GROQ API ключа
    if msg and msg.strip().startswith("gsk_") and len(msg.strip()) > 20:
        groq_key = msg.strip()
        if redis_client:
            await redis_client.set("office:secrets:groq_api_key", groq_key, ex=86400*365)
        await update.message.reply_text("✅ GROQ_API_KEY сохранён — удали это сообщение вручную 🗑")
        return
    await log("MSG_IN", msg[:80])

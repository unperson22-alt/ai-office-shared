    if r:
        await log_event(r, BOT_NAME_LOWER, "lesson_saved", title=title[:100])
    # 2) опубликовать новые (включая только что записанный) — идемпотентно по флагу
    try:
        await publish_pending_lessons()
    except Exception as e:
        logger.error(f"post_lesson publish failed: {e}")


async def publish_pending_on_startup():
    """Старт-задача: опубликовать pending-уроки (НИЧЕГО не удаляет).

    Идемпотентно — постит только уроки без posted_to_group. Нужна, чтобы при редеплое
    Силли сама дозалила в Bug Lessons новые/восстановленные уроки без ручных команд.
    Никакой авто-чистки/wipe здесь нет — чистка только дедупом по явной команде.
    """
    try:
        await asyncio.sleep(25)  # дать боту и сети подняться
        await publish_pending_lessons()
    except Exception as e:
        logger.error(f"[publish_startup] failed: {e}")


async def outbound_paused() -> bool:
    """Глобальный mute исходящих в офис-группу.
    True если env CILLY_PAUSED ∈ {1,true,yes} ИЛИ выставлен Redis-флаг cilly:paused."""
    if os.getenv("CILLY_PAUSED", "").lower() in ("1", "true", "yes"):
        return True
    try:
        r = await get_redis()
        if r and await r.get("cilly:paused"):
            return True
    except Exception:
        pass
    return False


async def notify_office(text: str):
    if not OFFICE_CHAT_ID:
        return
    if await outbound_paused():
        logger.info("notify_office: подавлено (CILLY_PAUSED/cilly:paused)")
        return
    try:
        sent = await bot.send_message(chat_id=OFFICE_CHAT_ID, text=text)
        await remember_my_message(sent)
    except Exception as e:
        logger.error(f"notify_office failed: {e}")


# ── Auto-fix pipeline ──────────────────────────────────────────────────────────
async def handle_bug(service_id: str, service_name: str, repo: str, main_file: str, analysis: dict):
    """Основная логика: автофикс или запрос подтверждения."""
    confidence  = analysis.get("confidence", "low")
    description = analysis.get("description", "")
    fix_desc    = analysis.get("fix_description", "")
    affected    = main_file  # Всегда используем файл из SERVICES, не доверяем LLM

    # Проверяем office:decisions — нет ли запрета на этот фикс
    if _office_decisions:
        combined = f"{description} {fix_desc} {service_name}"
        blocked = _check_decisions(combined)
        if blocked:
            await notify_office(
                f"⛔ Фикс заблокирован правилом {blocked['id']}:\n"
                f"Нельзя: {blocked['do_not']}\n"
                f"Причина: {blocked['because']}"
            )
            logger.info(f"[decisions] fix blocked by {blocked['id']} for {service_name}")
            return

    try:
        source_code = await read_file(repo, affected)
    except Exception as e:
        logger.error(f"Can't read {repo}/{affected}: {e}")
        return

    # ── Фикс генерит НЕ соло-Opus, а параллельная команда dev-dept с ревью ──
    # Девви → [Рикки ‖ Тести ‖ Секки] → Скрибби. Предложение в офис уходит
    # ТОЛЬКО после прохождения гейта (вердикт Рикки ≠ NEEDS_FIX + compile()).
    # Так аудит становится командным, а в чат не попадает неотревьюенный код.
    from ai_office_shared.shared.dev_pipeline import run_dev_pipeline
    from ai_office_shared.shared.dev_activity import publish_activity
    import uuid as _uuid

    uid      = int(os.getenv("YOUR_TELEGRAM_ID", "391077101"))
    r_tb     = await get_redis()
    _task_id = _uuid.uuid4().hex[:12]
    board_id = await tb.create_task(
        r_tb, f"Фикс {service_name}: {fix_desc[:80]}",
        created_by="силли", assignee="dev-dept",
        status="in_progress", task_id=_task_id,
    ) or _task_id

    devvy_task = (
        f"Исправь баг в боте {service_name} ({repo}/{affected}).\n"
        f"Симптом: {description}\n"
        f"Что нужно сделать: {fix_desc}\n"
        f"Верни ПОЛНЫЙ исправленный файл целиком, минимум изменений."
    )

    MAX_DEV_ATTEMPTS = 3
    final_code = ""
    review_ok = compile_ok = False
    commit_msg = ""
    retry_feedback = ""
    attempt = 0
    ricky_result = ""

    while attempt < MAX_DEV_ATTEMPTS:
        attempt += 1
        cur_task = devvy_task if not retry_feedback else (
            f"{devvy_task}\n\n[ПОВТОР #{attempt}] Предыдущая попытка отклонена:\n{retry_feedback}"
        )
        await publish_activity(r_tb, _task_id, "силли", "plan",
                               f"аудит-фикс попытка {attempt}/{MAX_DEV_ATTEMPTS}: {repo}/{affected}")
        pipe = await run_dev_pipeline(
            cur_task, repo=repo, file_path=affected,
            context=source_code, user_id=uid,
            redis_client=r_tb, task_id=_task_id,
        )
        ricky_result = pipe.get("final_code_artifact", "") or pipe.get("ricky", "")
        commit_msg = pipe.get("commit_msg", "")
        final_code = ""
        if "```python" in ricky_result:
            cs = ricky_result.find("```python") + 9
            ce = ricky_result.find("```", cs)
            if ce > cs:
                final_code = ricky_result[cs:ce].strip()

        review_ok = "NEEDS_FIX" not in (ricky_result or "").upper()
        compile_ok = True
        _se_info = ""
        if final_code and affected.endswith(".py"):
            try:
                compile(final_code, affected, "exec")
            except SyntaxError as _se:
                compile_ok = False
                _se_info = f"{_se.msg}, строка {_se.lineno}"

        if final_code and review_ok and compile_ok:
            break
        await tb.incr_attempts(r_tb, board_id)
        if not final_code:
            retry_feedback = "Рикки не вернул финальный код (блок ```python отсутствует/пуст)."
        elif not review_ok:
            retry_feedback = "Рикки вернул NEEDS_FIX — код требует доработки. " + ricky_result[:600]
        else:
            retry_feedback = f"Финальный код не компилируется: {_se_info}. Похоже воркер обрезал код."

    if final_code and review_ok and compile_ok:
        # Гейт пройден — стейджим деплой на /approve (контракт deploy_fix без изменений).
        fix_id = await stage_pending("deploy_fix", {
            "service_id": service_id,
            "service_name": service_name,
            "repo": repo,
            "affected": affected,
            "fixed_code": final_code,
            "analysis": analysis,
        }, task_id=board_id, title=f"Фикс {service_name}")
        await tb.update_status(r_tb, board_id, "awaiting_approval")
        _testi = (pipe.get("testi") or "")[:120].replace("\n", " ")
        _sekky = (pipe.get("sekky") or "")[:120].replace("\n", " ")
        await send_proposal(
            f"🤔 Cilly нашёл баг в {service_name}:\n\n"
            f"{description}\n\n"
            f"Предлагаемый фикс: {fix_desc}\n\n"
            f"✅ Прошёл ревью команды (Рикки OK + compile).\n"
            f"🧪 Тести: {_testi or '—'}\n"
            f"🔐 Секки: {_sekky or '—'}\n\n"
            f"Применить? (или текстом: /approve {fix_id})",
            "pg", fix_id, chat_id=0,  # автономно → офис-группа
        )
    else:
        # Гейт НЕ пройден — НЕ предлагаем неотревьюенный код, эскалируем владельцу.
        await tb.update_status(r_tb, board_id, "blocked",
                               result=retry_feedback or "рабочий код не получен", escalated=True)
        await notify_office(
            f"⛔ Cilly: баг в *{service_name}* — команда за {MAX_DEV_ATTEMPTS} попыток "
            f"не дала код, прошедший ревью. Нужен твой разбор.\n"
            f"Симптом: {description[:160]}\nПричина провала: {retry_feedback[:200]}"
        )


# ── Monitor loop ───────────────────────────────────────────────────────────────
ERROR_PATTERNS = ["Traceback", "Error:", "Exception:", "CRITICAL", "crashed", "exit code"]

# Kill-switch для аварийной остановки мониторинга (например, во время массовых деплоев)
# Поставь в Railway: CILLY_MONITOR_PAUSED=true → Cilly перестанет анализировать логи ботов
MONITOR_PAUSED = lambda: os.getenv("CILLY_MONITOR_PAUSED", "").lower() in ("1", "true", "yes")

# Паттерны которые НЕ являются багами — игнорируем
IGNORE_PATTERNS = [
    "Conflict: terminated by other getUpdates",  # нормально при редеплое
    "terminated by other getUpdates request",
    "make sure that only one bot instance",
    "NetworkError while getting Updates",        # временная сетевая ошибка
    "TimedOut",                                  # telegram timeout — не баг
    "DeprecationWarning",                        # предупреждение, не ошибка
    "httpx.ReadError",                           # сетевой сбой при polling — не баг
    "httpcore.ReadError",                        # то же
    "TelegramConflictError",                     # конфликт polling при рестарте
    "Failed to fetch updates",                   # временный сбой polling
]

# Внешние/транзиентные сбои — НЕ наш баг. Если корневая причина в недоступности
# стороннего сервиса (Telegram/Railway API, DNS, сеть), а бот жив — Силли МОЛЧИТ
# (по требованию владельца), а не предлагает фикс. Список шире IGNORE_PATTERNS:
# ловит сбои не только на polling/getUpdates, но и при отправке/любых POST.
EXTERNAL_FAULT_PATTERNS = [
    "telegram.error.NetworkError",
    "NetworkError",
    "httpx.ConnectError",
    "httpx.ConnectTimeout",
    "httpx.ReadTimeout",
    "httpx.RemoteProtocolError",
    "httpcore.RemoteProtocolError",
    "ConnectTimeout",
    "ReadTimeout",
    "RemoteProtocolError",
    "Server disconnected",
    "Bad Gateway",
    " 502",
    " 503",
    " 504",
    "getaddrinfo failed",
    "Temporary failure in name resolution",
    "Connection reset by peer",
    "Connection aborted",
]


def classify_fault(error_logs: list[str]) -> str:
    """Внешний транзиентный сбой vs наш баг.

    Возвращает "external" если корневая причина — недоступность стороннего
    сервиса (Telegram/Railway API, DNS, сеть) И в логах НЕТ признаков нашего
    структурного бага (NameError/ImportError/SyntaxError/KeyError/AttributeError).
    Иначе "internal".
    """
    text = "\n".join(error_logs)
    OUR_BUG_MARKERS = (
        "NameError", "ImportError", "ModuleNotFoundError", "SyntaxError",
        "IndentationError", "KeyError", "AttributeError", "TypeError",
        "ValueError", "IndexError", "UnboundLocalError",
    )
    if any(m in text for m in OUR_BUG_MARKERS):
        return "internal"
    if any(p in text for p in EXTERNAL_FAULT_PATTERNS):
        return "external"
    return "internal"


# Cooldown для внешних/известных-урочных сбоев — не дёргаемся по кругу (секунды).
EXTERNAL_FAULT_COOLDOWN = 6 * 3600  # 6 часов тишины по сигнатуре

# Игнорировать ошибки старше этого времени (секунды) — стартовый шум редеплоя
ERROR_MAX_AGE = 120  # 2 минуты

# Фразы которые означают что боту не хватает инструмента
RESPONSE_ANALYZER_PROMPT = """Анализатор ответов AI-агентов. Есть ли проблема с возможностями?
ПРОБЛЕМА: агент не может получить актуальные данные и говорит об этом / отказывается / просит юзера найти самому.
НЕ ПРОБЛЕМА: просит уточнить / отвечает по делу / нет данных от юзера.
JSON без markdown: {"has_problem":bool,"problem_type":"no_web_search|none","fix_needed":"web_search|none","confidence":"high|low","reason":"1 предложение"}"""


async def analyze_bot_response(user_question: str, bot_response: str) -> dict:
    """Анализирует ответ бота — есть ли проблема с возможностями."""
    prompt = f"Вопрос пользователя: {user_question}\n\nОтвет агента: {bot_response}"
    raw = await ask_claude(prompt, system=RESPONSE_ANALYZER_PROMPT, model="claude-haiku-4-5-20251001")
    raw = raw.strip()
    start, end = raw.find("{"), raw.rfind("}") + 1
    if start != -1 and end > start:
        raw = raw[start:end]
    return json.loads(raw)

# Имя бота в группе → репо + файл
BOT_REPOS = {
    "тилли":  ("tilly-bot",  "bot.py"),
    "билли":  ("billy-bot",  "bot.py"),
    "милли":  ("milly-bot",  "bot.py"),
    "доктор": ("dilly-bot",  "bot.py"),  # репо dilly-bot, не doctor-bot
    "эллис":  ("mama-bot",   "bot.py"),  # репо mama-bot (ellice-bot в Railway)
    "мама":   ("mama-bot",   "bot.py"),
}

WEB_SEARCH_FIX_PROMPT = """Добавь web search tool в этот Python код Telegram бота.

Нужно сделать три изменения:
1. В системный промпт добавить в самое начало (первая строка):
   "Используй web_search для получения актуальных данных: цены, курсы, новости, события."
2. В вызов client.messages.create() добавить параметр tools:
   tools=[{{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}}]
3. Парсинг ответа уже перебирает блоки через hasattr(block, "text") — не трогай его.

Верни ТОЛЬКО исправленный код целиком, без объяснений и markdown.

Исходный код:
{source}"""



# ── Daily audit ───────────────────────────────────────────────────────────────

HEALTH_URLS = {
    "pilly-bot":        "https://pilly-bot-production.up.railway.app/health",
    "logger-bot":       "https://logger-bot-production.up.railway.app/health",
    "office-dashboard": "https://office-dashboard-production-b571.up.railway.app/health",
    "mama-bot":         "https://ellice-bot-production.up.railway.app/health",    # Эллис
    "dilly-bot":        "https://dilly-bot-production-4a9b.up.railway.app/health", # Доктор
    "kriss-bot":        "https://kriss-bot-production.up.railway.app/health",
    "filly-bot":        "https://filly-bot-production.up.railway.app/health",
    "gosling-bot":      "https://gosling-bot-production.up.railway.app/health",
    "villy-bot":        "https://villy-bot-production.up.railway.app/health",
    "milly-bot":        "https://milly-bot-production.up.railway.app/health",
    "tilly-bot":        "https://tilly-bot-production.up.railway.app/health",
    "tilly-trader":     "https://tilly-trader-production.up.railway.app/health",
}

async def _all_office_services() -> list:
    """[(service_id, name)] по ВСЕМ проектам-отделам (для аудита/скана логов на баги).
    Фолбэк на статический SERVICES, если Railway API недоступен."""
    try:
        d = await railway_query("{ projects { edges { node { services { edges { node { id name } } } } } } }")
        out = []
        for pe in ((d.get("data") or {}).get("projects") or {}).get("edges") or []:
            for se in (pe["node"].get("services") or {}).get("edges") or []:
                out.append((se["node"]["id"], se["node"]["name"]))
        return out or [(sid, repo) for sid, (repo, _) in SERVICES.items()]
    except Exception as e:
        logger.error(f"_all_office_services: {e}")
        return [(sid, repo) for sid, (repo, _) in SERVICES.items()]


async def run_daily_audit() -> str:
    """Полный аудит офиса: деплои, логи, health. Возвращает текст отчёта."""
    import datetime
    lines = []
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    lines.append(f"📋 Ежедневный аудит офиса — {ts}\n")

    # 0. Early Railway API auth check — prevents 14 fake AUTH_ERRORs on expired token
    _railway_auth_ok = True
    try:
        await railway_query("{ me { id } }")
    except RuntimeError as e:
        if "Not Authorized" in str(e) or "Unauthorized" in str(e):
            _railway_auth_ok = False
            alert = (
                "🔴 RAILWAY_TOKEN истёк или отозван — Railway API недоступен.\n"
                "❌ Деплои: проверить невозможно (AUTH_ERROR на все запросы).\n"
                "🛠 Нужно: сгенерировать новый токен на railway.app → Settings → Tokens\n"
                "   и обновить RAILWAY_TOKEN_VLAD в переменных Силли на Railway."
            )
            lines.append(alert)
            logger.error(f"[audit] Railway API auth failed: {e}")
            await notify_office(alert)
    except Exception:
        pass  # network/timeout — не прерываем аудит

    # 1. Deployment status
    deploy_ok, deploy_fail = [], []
    for service_id, (repo, _) in (SERVICES.items() if _railway_auth_ok else []):
        try:
            data = await railway_query(
                """query($sid: String!) {
                     deployments(first:1, input:{serviceId:$sid}) {
                       edges { node { status } }
                     }
                   }""",
                {"sid": service_id}
            )
            deps = (data.get("data") or {}).get("deployments", {}).get("edges") or []
            status = deps[0]["node"]["status"] if deps else "NO_DEPLOY"
            name = repo
            if status == "SUCCESS":
                deploy_ok.append(name)
            else:
                deploy_fail.append(f"{name}:{status}")
        except RuntimeError as e:
            # GraphQL auth/permission error — критично, Railway API недоступен
            err_msg = str(e)
            if "Not Authorized" in err_msg or "Unauthorized" in err_msg:
                deploy_fail.append(f"{repo}:AUTH_ERROR")
            else:
                deploy_fail.append(f"{repo}:GQL_ERROR")
            logger.error(f"[audit] Railway API error for {repo}: {e}")
        except Exception as e:
            deploy_fail.append(f"{repo}:ERROR({type(e).__name__})")
            logger.error(f"[audit] deploy check failed for {repo}: {e}")

    if deploy_fail:
        lines.append(f"❌ Деплои упали: {', '.join(deploy_fail)}")
        # Auto-fix: читаем логи → анализируем причину → редеплоим или чиним
        for entry in deploy_fail:
            svc_name = entry.split(":")[0]
            svc_status = entry.split(":", 1)[1] if ":" in entry else "UNKNOWN"
            # AUTH_ERROR = Railway API недоступен, авто-фикс невозможен — пропускаем
            if svc_status == "AUTH_ERROR":
                continue
            svc_id = next(
                (sid for sid, (repo_n, _) in SERVICES.items() if repo_n == svc_name),
                None
            )
            if not svc_id:
                continue

            # 1. Читаем логи упавшего деплоя
            crash_logs = []
            crash_reason = "неизвестна"
            fix_action = "redeploy"
            fix_description = "редеплой"
            crash_reason = "неизвестна"
            can_autofix = False
            prevention = ""
            fix_action = "escalate"
            try:
                crash_logs = await get_service_logs(svc_id, limit=30)
                crash_text = "\n".join(crash_logs[:20])

                # 2. Анализируем причину через Claude Haiku
                analysis_raw = await ask_claude(
                    f"Бот {svc_name} упал со статусом {svc_status}. Логи:\n{crash_text}\n\n"
                    f"Определи: 1) точную причину падения, 2) можно ли починить автоматически (да/нет), "
                    f"3) что именно исправить. Ответь JSON без markdown:\n"
                    f'{{"reason": "...", "can_autofix": true/false, "fix": "...", "prevention": "..."}}',
                    system="Ты senior DevOps. Анализируй логи и давай конкретный диагноз. JSON только.",
                    model="claude-haiku-4-5-20251001"
                )
                try:
                    s, e = analysis_raw.find("{"), analysis_raw.rfind("}") + 1
                    analysis = json.loads(analysis_raw[s:e]) if s != -1 else {}
                    crash_reason = analysis.get("reason", "неизвестна")
                    can_autofix = analysis.get("can_autofix", False)
                    fix_description = analysis.get("fix", "редеплой")
                    prevention = analysis.get("prevention", "")

                    if can_autofix and "import" in crash_reason.lower():
                        fix_action = "fix_import"
                    elif can_autofix:
                        fix_action = "redeploy"
                    else:
                        fix_action = "escalate"
                except Exception:
                    pass
            except Exception as ex:
                logger.warning(f"[audit] log analysis failed for {svc_name}: {ex}")

            # 3. Применяем фикс
            if fix_action == "fix_import" or fix_action == "redeploy":
                logger.info(f"[audit] {svc_name} — {fix_action}, reason: {crash_reason[:60]}")
                ok = await redeploy_service(svc_id)
                action_taken = f"редеплой запущен ({fix_description[:80]})" if ok else "редеплой не удался"
                lines.append(
                    f"🔄 *{svc_name}* — редеплой запущен автоматически\n"
                    f"   📍 Причина: {crash_reason[:120]}\n"
                    f"   🛠 Действие: {action_taken}\n"
                    f"   🛡 Предотвращение: {prevention[:120]}" if prevention else
                    f"🔄 *{svc_name}* — редеплой запущен\n"
                    f"   📍 Причина: {crash_reason[:120]}"
                )
                if not ok:
                    await notify_office(f"⚠️ *{svc_name}* — редеплой не удался, нужен ручной разбор")
            elif classify_fault(crash_logs or [crash_reason]) == "external":
                # Внешний/сетевой сбой (Telegram/Railway API, DNS) — НЕ наш баг,
                # команду не дёргаем. Тихая строка в отчёт, без делегирования.
                lines.append(
                    f"🌐 *{svc_name}* — внешний/сетевой сбой (не наш баг), молчу\n"
                    f"   📍 Причина: {crash_reason[:120]}"
                )
            else:
                # Пробуем делегировать команде если это код-проблема
                code_keywords = ["import", "syntax", "error", "exception", "attribute", "module"]
                is_code_issue = any(kw in crash_reason.lower() for kw in code_keywords)
                if is_code_issue:
                    try:
                        await handle_natural_language(
                            f"[audit_autofix] fix_bot {svc_name}: {crash_reason}. "
                            f"Logs: {crash_text[:500]}. Fix needed: {fix_description}",
                            0, lambda x: None
                        )
                        lines.append(
                            f"🤖 *{svc_name}* — делегировала фикс команде\n"
                            f"   📍 Причина: {crash_reason[:120]}\n"
                            f"   🛠 Задача: {fix_description[:120]}"
                        )
                    except Exception as ex:
                        lines.append(f"⚠️ *{svc_name}* — не смогла делегировать: {ex}")
                else:
                    lines.append(
                        f"⚠️ *{svc_name}* — требует ручного вмешательства\n"
                        f"   📍 Причина: {crash_reason[:120]}\n"
                        f"   🛠 Рекомендация: {fix_description[:120]}"
                    )
    else:
        lines.append(f"✅ Деплои ({len(deploy_ok)}): все SUCCESS")

    # 1b. Сквозной аудит ВСЕХ проектов-отделов (а не только awake-happiness).
    #     Только видимость/алерт; авто-фикс остаётся лишь для известных репо из
    #     SERVICES — чужие отделы не чиним вслепую.
    try:
        known_sids = set(SERVICES.keys())
        other_fail, other_total = [], 0
        all_data = await railway_query(
            "{ projects { edges { node { name services { edges { node { id name } } } } } } }"
        )
        for pe in ((all_data.get("data") or {}).get("projects") or {}).get("edges") or []:
            pname = pe["node"]["name"]
            for se in (pe["node"].get("services") or {}).get("edges") or []:
                sid = se["node"]["id"]
                if sid in known_sids:
                    continue
                other_total += 1
                try:
                    dd = await railway_query(
                        "query($sid:String!){deployments(first:1,input:{serviceId:$sid}){edges{node{status}}}}",
                        {"sid": sid})
                    deps2 = (dd.get("data") or {}).get("deployments", {}).get("edges") or []
                    st = deps2[0]["node"]["status"] if deps2 else "NO_DEPLOY"
                    # NO_DEPLOY — это кроны/не настроенные сервисы, не инцидент → не алертим
                    if st not in ("SUCCESS", "NO_DEPLOY"):
                        other_fail.append(f"{pname}/{se['node']['name']}:{st}")
                except Exception:
                    pass
        if other_fail:
            lines.append(f"❌ Другие отделы упали: {', '.join(other_fail)}")
        elif other_total:
            lines.append(f"✅ Другие отделы ({other_total}): все деплои SUCCESS")
    except Exception as e:
        logger.error(f"[audit] cross-project sweep failed: {e}")

    # 2. Health checks for HTTP services
    health_fail = []
    async with httpx.AsyncClient(timeout=10) as c:
        for name, url in HEALTH_URLS.items():
            try:
                r = await c.get(url)
                if r.status_code != 200:
                    # Богатый /health (как у tilly-trader) кладёт в тело причину
                    # деградации — вытаскиваем, чтобы отчёт говорил ПОЧЕМУ, а не
                    # просто ":503". Стоп сканера трейдера теперь виден офису.
                    detail = str(r.status_code)
                    try:
                        body = r.json()
                        reason = body.get("reason") or body.get("status")
                        if reason:
                            detail = f"{r.status_code}/{reason}"
                        age = body.get("last_scan_age_s")
                        if age is not None:
                            detail += f"(scan {age // 60}m)"
                    except Exception:
                        pass
                    health_fail.append(f"{name}:{detail}")
            except Exception as e:
                health_fail.append(f"{name}:TIMEOUT")

    if health_fail:
        lines.append(f"❌ Health failed: {', '.join(health_fail)}")
    else:
        lines.append(f"✅ HTTP health ({len(HEALTH_URLS)}): все OK")

    # 3. Scan logs for new errors (last 2 hours)
    import time, hashlib
    cutoff_ts = time.time() - 7200  # 2 hours
    error_services = []
    IGNORE_LOG = [
        "Conflict: terminated by other getUpdates",
        "DeprecationWarning", "TimedOut", "NetworkError",
    ]
    office_services = await _all_office_services()
    for service_id, repo in office_services:
        try:
            logs = await get_service_logs(service_id)
            errs = [l for l in logs
                    if any(p in l for p in ["Error:", "Traceback", "CRITICAL", "KeyError"])
                    and not any(i in l for i in IGNORE_LOG)]
            if errs:
                error_services.append(f"{repo}({len(errs)})")
        except Exception:
            pass

    if error_services:
        lines.append(f"⚠️  Новые ошибки: {', '.join(error_services)}")
    else:
        lines.append("✅ Логи: ошибок за последние 2 часа нет")

    # 4. Bug lesson scan — ищем новые паттерны ошибок которых нет в lessons.json
    new_lesson_count = 0
    try:
        raw_lessons = await read_file("ai-office-shared", LESSONS_FILE)
        existing_lessons = json.loads(raw_lessons) if raw_lessons.strip() else []

        # Собираем все ошибки за сутки по всем сервисам
        all_errors: dict[str, list[str]] = {}
        for service_id, repo in office_services:
            try:
                logs = await get_service_logs(service_id)
                errs = [l for l in logs if any(p in l for p in ERROR_PATTERNS)
                        and not any(i in l for i in IGNORE_LOG)]
                if errs:

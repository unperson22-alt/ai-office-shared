"""ai_office_shared.shared — общая библиотека для всех ботов офиса.

Доступные модули:
    logging      — log_event()
    log_query    — query()/summarize(): «сколько, когда, с каким тактом» вместо дампа
    redis_helpers — redis_get_history, redis_save_history, redis_get_notes, redis_add_note
    tasks        — auto_extract_interests, schedule_loop, ...
    routing      — forward_to_filly, make_reply_handler, is_routed
    quality      — реакции 👍/👎
    ollama       — try_ollama(), OllamaResult (local LLM fallback)
    web_search   — web_search(), web_search_text()
    impact_client — get_campaigns(), get_ads(), get_tracking_link()
    voice        — transcribe_voice(file_url) → (text, error)
    office       — OFFICE_AGENTS, call_office(), parse_office_tag()
    identity     — canonical(), display(), route_key(), url(), PEOPLE, person(),
                   roster_prompt() — единый реестр ботов и людей офиса,
                   speaker_prompt(user_id, first_name) — «тебе пишет вот этот
                   человек» в системный промт: без него модель подставляет
                   самого вероятного (Крисс назвал Яну Владом, 2026-08-21)
    group_history — push(), get_context() — общая лента офис-группы (пишут ВСЕ боты)
    dedup        — claim_answer() — SET NX с TTL: один ответ на одно сообщение
                   у ботов с двумя входами в группу (свой телеграм-хендлер +
                   HTTP /task от Филли)
    banter       — fanout(), is_banter(), depth_of(), sender_of() — болталка ботов;
                   build_message()/clip() — транскрипт чата в промт званого,
                   BANTER_COOLDOWN — потолок частоты всплесков
    taskboard    — + acceptance/evidence: критерии приёмки замораживаются до
                   работы, done не выдаётся без улик (set_acceptance,
                   add_evidence, acceptance_verdict); успех подписывает НЕ
                   исполнитель; потолок раундов (should_escalate/escalate);
                   отчёт таблицей улик (format_evidence_report)
    gates        — run_gate() — детерминированная проверка критерия:
                   компиляция, HTTP-код, ключ Redis, подстрока. Улика —
                   наблюдённое значение, а не «проверил, работает»
    phantom      — looks_like_phantom() — офф-персонажные заглушки Ollama
    railway_vars — set_var_allowed() — что Силли можно писать в env
    models       — MODEL_SONNET, MODEL_HAIKU, MODEL_OPUS (id моделей + env-override)
    prompt       — enhance_prompt() — уточнение запроса лёгкой моделью
    auth         — office_auth_middleware, office_headers(), check_office_token() — auth RPC-меша
    crypto       — get_price(), get_prices(), get_prices_text()
    currency     — get_rate(), get_rates(), get_rates_text()
    wiki         — wiki_summary(), wiki_search(), wiki_text()
    elevenlabs   — text_to_voice(text) → bytes (mp3)
    imagegen     — generate_image(«нарисуй кота») → ImageResult; промпт
                   переводится на английский, провайдеры Cloudflare FLUX →
                   Pollinations → Replicate, отдаёт байты, а не ссылку
    photo_looks  — адаптивные рецепты обработки: параметры считаются по
                   гистограмме конкретного кадра, а не фиксированы процентами
    photo_ai     — арт-директор: модель смотрит на кадр и правит параметры
                   (PHOTO_AI_DIRECTOR=1); картинку не рисует, рендер локальный
    photo_retouch — ретушь лица: точечно убирает дефекты кожи (маска кожи +
                   «маленькое пятно» через морфологическое открытие), глаза,
                   губы и фон не трогает. Локально, без ключей и лимитов
    photo        — process_photo(raw, «сделай ярче») → PhotoResult; фильтры,
                   кроп, апскейл, сжатие локально на Pillow (без лимитов и
                   ключей); вырез/размытие фона через rembg (PHOTO_REMBG=1);
                   AI-перерисовка через Cloudflare Workers AI (опц.)
    dev_pipeline — run_dev_pipeline(...) → dict (ПАРАЛЛЕЛЬНО: Девви → Рикки‖Тести‖Секки → Скрибби)
    dev_activity — publish_activity()/read_activity() — общий эфир действий dev-dept
    traceback_scan — error_lines()/signature(): отбор строк отказа БЛОКОМ и
                   устойчивая сигнатура по ним. Построчный отбор оставлял от
                   трейсбека один заголовок, и разные баги разных сервисов
                   сходились в один хеш (25.08.2026)
    file_window  — find_regions()/around_lines(): показать модели нужный кусок
                   файла — по подстроке или по номеру строки из трейсбека
    fault_evidence — dispatch_refusal(): находку без улики отделу не отдают
"""
# Импорты по необходимости — from ai_office_shared.shared.X import Y

# AI Office — Operations Log

> Формат: каждая сессия Claude + каждое действие Силли.
> Читать при старте новой сессии чтобы понять контекст.
> Силли дописывает снизу автоматически.

---

## 2026-05-11 — Сессия Claude

### Найденные и исправленные баги
- **SERVICES UUID** — все 6 UUID в coder.py были неправильными → мониторинг не работал совсем
- **billy-bot** — отсутствовал в SERVICES → не мониторился
- **redeploy_service()** — передавал `"production"` вместо UUID окружения → редеплой всегда падал
- **post_lesson() Markdown** — спецсимволы в тексте ломали отправку уроков

### Что сделано
- Исправлены UUID в SERVICES (coder.py)
- Добавлен billy-bot в мониторинг
- Исправлен environmentId: `2efaaf60-ba39-492c-bf86-007fd505493f`
- Создан `lessons/lessons.json` — AI-формат база знаний багов (4 урока)
- Добавлен поиск по урокам перед анализом (`search_lessons()`) — экономия Opus токенов
- Оптимизированы system prompts всех ботов: −44% токенов
- История сообщений: 20 → 10 у всех ботов
- Добавлен NL-обработчик в Силли — теперь понимает обычные запросы без команд
- HTTP endpoint `/task` у Силли для роутинга от семейных ботов
- Создан **prophet-bot** (Пророк) — агрегатор мнений всех советников
- Добавлен маршрут ПРОРОК в Филли
- Исправлен баг Филли: ПРОРОК был в промпте роутера но не в BOT_URLS

### Текущее состояние офиса
| Сервис | Статус | Railway ID |
|--------|--------|------------|
| logger-bot | ✅ | 3319eabd-5bcb-4e59-839e-4813f1e7ef33 |
| tilly-bot | ✅ | 367e25d7-8410-419d-896d-2cc86cd44efd |
| filly-bot | ✅ | 5d61d403-feee-455e-9c0d-523f0e7c79d5 |
| billy-bot | ✅ | b441ce93-9736-49b3-9b5d-d0c82e715b28 |
| doctor-bot | ✅ | d949c4d2-59fa-4cbe-8bb8-a0589a476607 |
| milly-bot | ✅ | db277aff-6638-4b4a-970e-b016bd753608 |
| office-dashboard | ✅ | 3dfc7336-2e91-4ade-950a-4f3d566baced |
| cilly (ai-office-shared) | ✅ | efa6bd21-91d8-467f-8250-60f8a3853791 |
| prophet-bot | ✅ | 9db4108e-19f1-4c1f-a21c-3909442e137c |

### Pending / следующие шаги
- [ ] Этап 2: Telethon сессия для BotFather автоматизации (нужен ноутбук)
- [ ] Этап 3: полный pipeline создания нового бота одной командой
- [ ] Мама-бот и семейные боты
- [ ] PROPHET_URL добавить в env Филли через Railway (сейчас работает по .railway.internal)

---
<!-- Силли дописывает ниже -->

**[2026-05-11 14:10 UTC] Силли — office-dashboard:** автофикс: Добавить try-except вокруг get_required_env() вызовы в main(
> confidence=high | файл=main.py | статус=редеплой запущен ✅

**[2026-05-11 18:35 UTC] Силли — office-dashboard:** автофикс: Передать WEBAPP_URL через context.bot_data или изменить архи
> confidence=high | файл=main.py | статус=редеплой запущен ✅

**[2026-05-11 21:42 UTC] Силли — office-dashboard:** автофикс: 1) Добавить try-except вокруг инициализации Application и ap
> confidence=high | файл=main.py | статус=редеплой запущен ✅

**[2026-05-12 17:06 UTC] Силли — office-dashboard:** автофикс: Добавить параметры read_timeout и write_timeout в Applicatio
> confidence=high | файл=main.py | статус=редеплой запущен ✅

**[2026-05-13 05:13 UTC] Claude — pilly-bot:** Создан сервис для генерации картинок
> GitHub repo: unperson22-alt/pilly-bot (создан, код залит)
> Railway service_id: 5533bc5f-24aa-4079-903b-50bcde4cdd01
> Domain: pilly-bot-production.up.railway.app
> Env vars установлены: REPLICATE_API_TOKEN, OFFICE_CHAT_ID, LOG_BOT_URL, PILLY_BOT_URL (у всех ботов)
> TELEGRAM_TOKEN — ещё не добавлен, ждём токен от BotFather
> Все боты переключены с MAMA_BOT_URL на PILLY_BOT_URL
> Силли добавлен в SERVICES для мониторинга
> Mama-bot освобождён от генерации картинок

**[2026-05-13 05:13 UTC] Claude — ai-office-shared/agents/coder.py:** Фиксы Силли
> FIX: create_via_botfather — добавлен /cancel перед каждым retry (иначе BotFather ждёт username от предыдущего /newbot)
> FIX: handle_natural_language теперь читает ops.md перед обработкой запроса (контекст о сделанном)

**[2026-05-13 09:00 UTC] Силли — all_services:** daily_audit
> 📋 Ежедневный аудит офиса — 13.05.2026 09:00 UTC

✅ Деплои (13): все SUCCESS
❌ Health failed: office-dashboard:404
✅ Логи: ошибок за последние 2 часа нет
📚 Новых паттернов багов не найдено

🟡 Статус офиса: ТРЕБУЕТ ВНИМАНИЯ

**[2026-05-14 09:00 UTC] Силли — all_services:** daily_audit
> 📋 Ежедневный аудит офиса — 14.05.2026 09:00 UTC

✅ Деплои (13): все SUCCESS
❌ Health failed: office-dashboard:404
✅ Логи: ошибок за последние 2 часа нет
📚 Новых паттернов багов не найдено

🟡 Статус офиса: ТРЕБУЕТ ВНИМАНИЯ

**[2026-05-14 18:01 UTC] Силли — all_services:** daily_audit
> 📋 Ежедневный аудит офиса — 14.05.2026 18:00 UTC

✅ Деплои (14): все SUCCESS
❌ Health failed: office-dashboard:404
✅ Логи: ошибок за последние 2 часа нет
📚 Новых паттернов багов не найдено

🟡 Статус офиса: ТРЕБУЕТ ВНИМАНИЯ

**[2026-05-17 09:00 UTC] Силли — all_services:** daily_audit
> 📋 Ежедневный аудит офиса — 17.05.2026 09:00 UTC

❌ Деплои упали: logger-bot:ERROR('NoneType' object has no attribute 'get'), tilly-bot:ERROR('NoneType' object has no attribute 'get'), filly-bot:ERROR('NoneType' object has no attribute 'get'), doctor-bot:ERROR('NoneType' object has no attribute 'get'

**[2026-05-17 18:00 UTC] Силли — all_services:** daily_audit
> 📋 Ежедневный аудит офиса — 17.05.2026 18:00 UTC

❌ Деплои упали: logger-bot:NO_DEPLOY, tilly-bot:NO_DEPLOY, filly-bot:NO_DEPLOY, doctor-bot:NO_DEPLOY, milly-bot:NO_DEPLOY, office-dashboard:NO_DEPLOY, billy-bot:NO_DEPLOY, prophet-bot:NO_DEPLOY, tilly-trader:NO_DEPLOY, mama-bot:NO_DEPLOY, pilly-bot:NO

**[2026-05-18 09:00 UTC] Силли — all_services:** daily_audit
> 📋 Ежедневный аудит офиса — 18.05.2026 09:00 UTC

❌ Деплои упали: logger-bot:NO_DEPLOY, tilly-bot:NO_DEPLOY, filly-bot:NO_DEPLOY, doctor-bot:NO_DEPLOY, milly-bot:NO_DEPLOY, office-dashboard:NO_DEPLOY, billy-bot:NO_DEPLOY, prophet-bot:NO_DEPLOY, tilly-trader:NO_DEPLOY, mama-bot:NO_DEPLOY, pilly-bot:NO

**[2026-05-18 18:00 UTC] Силли — all_services:** daily_audit
> 📋 Ежедневный аудит офиса — 18.05.2026 18:00 UTC

❌ Деплои упали: logger-bot:NO_DEPLOY, tilly-bot:NO_DEPLOY, filly-bot:NO_DEPLOY, doctor-bot:NO_DEPLOY, milly-bot:NO_DEPLOY, office-dashboard:NO_DEPLOY, billy-bot:NO_DEPLOY, prophet-bot:NO_DEPLOY, tilly-trader:NO_DEPLOY, mama-bot:NO_DEPLOY, pilly-bot:NO

**[2026-05-19 09:00 UTC] Силли — all_services:** daily_audit
> 📋 Ежедневный аудит офиса — 19.05.2026 09:00 UTC

❌ Деплои упали: logger-bot:NO_DEPLOY, tilly-bot:NO_DEPLOY, filly-bot:NO_DEPLOY, doctor-bot:NO_DEPLOY, milly-bot:NO_DEPLOY, office-dashboard:NO_DEPLOY, billy-bot:NO_DEPLOY, prophet-bot:NO_DEPLOY, tilly-trader:NO_DEPLOY, mama-bot:NO_DEPLOY, pilly-bot:NO

**[2026-05-19 18:00 UTC] Силли — all_services:** daily_audit
> 📋 Ежедневный аудит офиса — 19.05.2026 18:00 UTC

❌ Деплои упали: logger-bot:NO_DEPLOY, tilly-bot:NO_DEPLOY, filly-bot:NO_DEPLOY, doctor-bot:NO_DEPLOY, milly-bot:NO_DEPLOY, office-dashboard:NO_DEPLOY, billy-bot:NO_DEPLOY, prophet-bot:NO_DEPLOY, tilly-trader:NO_DEPLOY, mama-bot:NO_DEPLOY, pilly-bot:NO

**[2026-05-20 09:04 UTC] Силли — all_services:** daily_audit
> 📋 Ежедневный аудит офиса — 20.05.2026 09:00 UTC

❌ Деплои упали: logger-bot:NO_DEPLOY, tilly-bot:NO_DEPLOY, filly-bot:NO_DEPLOY, doctor-bot:NO_DEPLOY, milly-bot:NO_DEPLOY, office-dashboard:NO_DEPLOY, billy-bot:NO_DEPLOY, prophet-bot:NO_DEPLOY, tilly-trader:NO_DEPLOY, mama-bot:NO_DEPLOY, pilly-bot:NO

**[2026-05-20 21:23 UTC] Силли — all_services:** daily_audit
> 📋 Ежедневный аудит офиса — 20.05.2026 18:00 UTC

✅ Деплои (14): все SUCCESS
❌ Health failed: office-dashboard:404
✅ Логи: ошибок за последние 2 часа нет
📚 Новых паттернов багов не найдено

🟡 Статус офиса: ТРЕБУЕТ ВНИМАНИЯ

**[2026-05-21 18:00 UTC] Силли — all_services:** daily_audit
> 📋 Ежедневный аудит офиса — 21.05.2026 18:00 UTC

❌ Деплои упали: logger-bot:NO_DEPLOY, tilly-bot:NO_DEPLOY, filly-bot:NO_DEPLOY, doctor-bot:NO_DEPLOY, milly-bot:NO_DEPLOY, office-dashboard:NO_DEPLOY, billy-bot:NO_DEPLOY, prophet-bot:NO_DEPLOY, tilly-trader:NO_DEPLOY, mama-bot:NO_DEPLOY, pilly-bot:NO

**[2026-05-25 18:01 UTC] Силли — all_services:** daily_audit
> 📋 Ежедневный аудит офиса — 25.05.2026 18:00 UTC

✅ Деплои (14): все SUCCESS
✅ HTTP health (3): все OK
✅ Логи: ошибок за последние 2 часа нет
📚 Новых паттернов багов не найдено

🟢 Статус офиса: НОРМА

# AI Office — SYSTEM_STATE.md

> **Живой документ.** Силли обновляет после каждого значимого изменения:
> деплой нового бота, смена Redis-контракта, обновление shared lib, закрытие уязвимости.
> Формат обновления — в конце файла.

**Последнее обновление:** 2026-06-19 (автономное управление Силли: доска задач, рантайм-обучение, делегирование, проактивная петля)  
**Версия shared lib:** v0.1.18  
**Активных ботов:** 10 (+1 роутер)  
**GitHub org:** unperson22-alt  
**Платформа:** Railway + Cloudflare Workers

-----

## Архитектура — поток сообщения

```
Сообщение в группу / DM
        │
        ▼
[CF Worker proxy]  ←  если Railway недоступен → "технические работы"
        │
        ▼
   ФИЛЛИ (router)
        │
        ├── health-cache (office:health:{AGENT}) → бот down? → log_routing_miss → другой бот
        │
        ├── Prompt Enhancer (Haiku) → улучшает запрос
        │
        ├── Name-map → прямое обращение по имени? → минует LLM-классификатор
        │
        ├── Router (Haiku) → классифицирует → AGENT
        │
        └── route_via_http(AGENT, message) → POST /task
                │
                ├── success → health_set(up) → log route_ok
                │
                └── fail   → health_set(down) → log route_miss → DM fallback (Haiku)

Ответ бота в чат
        │
        ▼
👍/👎 реакция → quality.py → office:quality:{bot_lowercase} HASH up/down
        │
        ▼
Силли (еженедельно): анализ → предложение правок → /approve → GitHub push
```

-----

## Ростер ботов

|Бот        |Uppercase (BOT_URLS)|Lowercase (quality-ключ)|Репо       |Railway ID|URL                                       |Framework                    |
|-----------|--------------------|------------------------|-----------|----------|------------------------------------------|--------------------------|
|**Филли**  |— (роутер)          |`фили` ¹                |filly-bot  |`5d61d403`|порт 8080, aiohttp                        |python-telegram-bot + aiohttp|
|**Билли**  |`БИЛЛИ`             |`билли`                 |billy-bot  |`b441ce93`|billy-bot-production.up.railway.app       |python-telegram-bot          |
|**Крисс**  |`КРИС`              |`крисс` ²               |kriss-bot  |`92f70bbb`|kriss-bot-production.up.railway.app       |python-telegram-bot          |
|**Эллис**  |`ЭЛЛИС`             |`эллис`                 |ellice-bot |`2f647984`|ellice-bot-production.up.railway.app      |python-telegram-bot          |
|**Нэлли**  |`НЭЛЛИ`             |`нэлли`                 |nelli-bot  |`6bfe9bc1`|nelli-bot-production.up.railway.app       |python-telegram-bot          |
|**Рэй**    |`РЭЙ`               |`рэй`                   |ray-bot    |`8beda34a`|ray-bot-production.up.railway.app         |python-telegram-bot          |
|**Тилли**  |`ТИЛЛИ`             |`тилли`                 |tilly-bot  |`367e25d7`|tilly-bot-production.up.railway.app       |python-telegram-bot          |
|**Милли**  |`МИЛЛИ`             |`милли`                 |milly-bot  |`db277aff`|milly-bot-production.up.railway.app       |python-telegram-bot          |
|**Доктор** |`ДИЛЛИ`             |`доктор`                |dilly-bot  |`d949c4d2`|dilly-bot-production.up.railway.app       |python-telegram-bot          |
|**Вилли**  |`ВИЛЛИ`             |`вилли`                 |villy-bot  |`a5e37cc4`|villy-bot-production.up.railway.app       |python-telegram-bot          |
|**Гослинг**|`ГОСЛИНГ`           |`гослинг`               |gosling-bot|`ed03c9d3`|gosling-bot-production.up.railway.app ³       |python-telegram-bot          |
|**Силли**  |`СИЛЛИ`             |`силли`                 |ai-office-shared|`efa6bd21`|ai-office-shared-production.up.railway.app|aiogram 3.7 + Telethon       |
|**Пророк** |`ПРОРОК`            |— ⁴                     |prophet-bot|—         |prophet-bot-production-df65.up.railway.app|python-telegram-bot          |

**Примечания:**  
¹ Филли-роутер не пишет quality-реакции — `office:quality:фили` всегда 0, это норма.  
² Uppercase агент — `КРИС` (одна С), lowercase quality-ключ — `крисс` (две С). Не путать.  
³ Гослинг на internal URL — доступен только внутри Railway-сети. Внешний curl не сработает.  
⁵ Доктор — рассинхрон BOT_URLS/quality-ключей проверен 2026-06-11: ключи `office:quality:доктор/дилли` отсутствуют, рассинхрона нет.  
⁴ Пророк не в METRICS_BOTS, quality не трекается.

**Специальные пользователи:**

|Переменная        |ID         |Роль                                          |
|------------------|-----------|----------------------------------------------|
|`YOUR_TELEGRAM_ID`|(из env)   |Влад — владелец, полный доступ                |
|`LUK_USER_ID`     |`675773302`|Лук — особый гость, альтернативное приветствие|

-----

## Redis — контракты ключей

> **Правило чтения:** owner (writer) — единственный кто задаёт формат.
> Consumer читает то что написал owner. Если формат разошёлся — фикс у owner.

|Ключ                           |Тип   |Owner (writer)                    |Consumer (reader)                   |TTL                  |Формат значения                                                           |
|-------------------------------|------|----------------------------------|------------------------------------|---------------------|--------------------------------------------------------------------------|
|`office:group:history`         |LIST  |Филли (`group_history_push`)      |Все боты (через `group_ctx`)        |7д                   |JSON `{"from": str, "text": str}`, LPUSH + LTRIM 20                       |
|`office:health:{AGENT}`        |STRING|Филли (`health_set`)              |Филли (`health_get` перед роутингом)|60с (up) / 30с (down)|`"up"` или `"down"`. **AGENT — UPPERCASE** (БИЛЛИ, ТИЛЛИ…)                |
|`office:routing:misses`        |LIST  |Филли (`log_routing_miss`)        |Филли `/metrics`, Силли             |—                    |JSON `{"agent": UPPERCASE, "message": str, "ts": int}`, LPUSH + LTRIM 100 |
|`office:quality:{bot}`         |HASH  |Каждый бот (`record_reaction`)    |Филли `/metrics`                    |—                    |Поля: `up` (int), `down` (int). **bot — lowercase** (билли, тилли…)       |
|`office:msg:{chat_id}:{msg_id}`|STRING|Каждый бот (`remember_my_message`)|Каждый бот (`reaction_owner`)       |из quality.py        |Имя бота lowercase — нужно для атрибуции реакции                          |
|`office:logs:{bot}:{date}`     |LIST  |Каждый бот (`log_event`)          |Силли (`read_logs()`)               |7д                   |JSON события. `bot` — lowercase, `date` — `YYYY-MM-DD`. LPUSH + LTRIM 1000|
|`office:members`               |STRING|Филли (`group_members_update`)    |Филли (`group_members_get`)         |30д                  |Текстовый профиль команды, генерируется Haiku раз в неделю               |
|`office:mom_queue`             |LIST  |Эллис (`ellice-bot`)                |Эллис (`/mention` endpoint)         |7д                   |JSON очередь сообщений мамы, сбрасывается при пинге из Филли             |
|`office:task:{id}`             |HASH  |Силли (`taskboard.create_task`)   |Силли (`management_loop`), дашборд  |∞ / 30д у done       |Доска задач. Поля: `id,title,created_by,assignee,status,parent_id,result,attempts,escalated,created_at,updated_at`. Статусы: open/in_progress/needs_fix/blocked/awaiting_approval/done/rejected |
|`office:tasks:index`           |ZSET  |Силли (`taskboard`)               |Силли (`taskboard.list_tasks`)      |—                    |member=task_id, score=updated_at(epoch). ZREVRANGE → свежие первыми       |
|`office:pending:{id}`          |STRING|Силли (`stage_pending`)           |Силли (`/approve`, `pop_pending`)   |24ч                  |JSON pending-действия approval-гейта. `type`: deploy_fix/deploy_devtask/update_instruction/delegate |
|`office:instructions:{bot}`    |STRING|Силли (`set_bot_instruction`)     |Каждый бот (`build_system` через `office.instructions_suffix`)|∞|Рантайм-инструкция тимлида, аппендится к системному промпту БЕЗ редеплоя. `bot` — lowercase canonical |

**Типичные события в `office:logs`:**
`route_ok`, `route_miss`, `route_decision`, `message_received`, `response_sent`,
`task_received`, `api_error`, `lesson_saved`, `capability_gap_detected`, `dm_fallback`

-----

## Shared Library — ai_office_shared

**Репо:** unperson22-alt/ai-office-shared (публичный)  
**Установка:** `ai_office_shared @ git+https://github.com/unperson22-alt/ai-office-shared@v0.1.18`

|Модуль                |С версии|Что экспортирует                                                                                               |
|----------------------|--------|---------------------------------------------------------------------------------------------------------------|
|`shared.logging`      |v0.1.0  |`log_event(redis, bot, event, **kwargs)`                                                                       |
|`shared.identity`     |v0.1.1  |`BOTS`, `canonical()`, `display()`, `redis_key()`                                                              |
|`shared.redis_helpers`|v0.1.2  |`redis_get_history`, `redis_save_history`, `redis_get_notes`, `redis_add_note`                                 |
|`shared.tasks`        |v0.1.2  |`auto_extract_interests`, `weekly_review`                                                                      |
|`shared.quality`      |v0.1.3  |`remember_my_message`, `reaction_owner`, `classify_reaction`, `record_reaction`, `REACTION_UP`, `REACTION_DOWN`|
|`shared.url_check`    |v0.1.7  |`check_url`, `filter_live_urls`, `extract_urls`, `verify_text_urls`                                            |
|`shared.office`       |v0.1.18 |`call_office`, `OFFICE_AGENTS`, `parse_office_tag`, `instructions_suffix(redis, bot)` ← рантайм-обучение      |
|`shared.taskboard`    |v0.1.18 |`create_task`, `update_status`, `set_result`, `incr_attempts`, `add_subtask`, `get_task`, `list_tasks`        |

### ⚡ MIGRATION RULE

> При любом касании бота по любой причине — **обязательно**:
> 
> 1. Поднять в `requirements.txt`: `ai_office_shared @ ...@v0.1.18`
> 1. Заменить локальные копии на импорты из `ai_office_shared.shared`:
>    `redis_get_history`, `redis_save_history`, `redis_get_notes`, `redis_add_note`,
>    `auto_extract_interests`, `weekly_review`

**Текущий статус миграции ботов:** 8 ботов (билли, тилли, доктор, гослинг, вилли, эллис/mama, крисс, милли)
переведены на `v0.1.18` на ветке `claude/cilly-autonomous-management-8oze50` —
их `build_system` дочитывает `office:instructions:{bot}` (рантайм-обучение).

> ⚠️ **ПЕРЕД деплоем ботов с v0.1.18:** выпустить релиз/таг `v0.1.18` в `ai-office-shared`
> (git tag + GitHub release), иначе pip-сборка ботов упадёт на несуществующем теге.
> Силли (сам репо ai-office-shared) использует код локально — тег ей не нужен.

-----

## Skills Library

**Путь:** `ai-office-shared/skills/{name}/SKILL.md`  
Силли читает скилл при знакомом сценарии → не изобретает заново.

|Скилл              |Когда читать                            |
|-------------------|----------------------------------------|
|`railway-deploy`   |Деплой, перезапуск, переменные окружения|
|`redis-migration`  |Изменение Redis-ключей, миграция формата|
|`ptb-reactions`    |Реакции 👍/👎 через python-telegram-bot   |
|`telethon-handlers`|Работа с Telethon (только у Силли)      |
|`github-push`      |Пуш файла/патча в GitHub через API      |

-----

## Инфраструктура

|Компонент|Адрес                                    |Назначение                                                                                            |
|---------|-----------------------------------------|------------------------------------------------------------------------------------------------------|
|CF Worker|ai-office-watchdog.unperson22.workers.dev|Прокси перед Филли. Railway лежит → "технические работы". Проверяет status.railway.app перед алертами.|
|Railway  |railway.app                              |Хостинг всех ботов                                                                                    |
|Redis    |`$REDIS_URL` (Railway addon)             |Всё состояние системы                                                                                 |

-----

## Известные проблемы

|ID        |Компонент            |Описание                                                                                                                              |Статус            |
|----------|---------------------|--------------------------------------------------------------------------------------------------------------------------------------|------------------|
|`BUG-001` |filly-bot `/metrics` |Handler зависает (http=000) — таймауты asyncio.wait_for добавлены.|✅ Закрыт 2026-05-26|
|`DATA-001`|Доктор / METRICS_BOTS|Аудит запущен 2026-05-27: ключ `office:quality:дилли` в Redis отсутствует — бот пишет под `office:quality:доктор`. Рассинхрона нет.|✅ Закрыт 2026-05-27|

-----

## Дебаг-протокол (для Силли)

Перед правкой любого бота:

```
1. read_logs(bot, n=50)          # что происходило последние 50 событий
2. Найти pattern ошибки          # route_miss? api_error? exception?
3. Диагностировать по слоям:
   данные/контракты → бизнес-логика → async/timing → интеграция → архитектура
4. Найти owner-слой (не чинить симптом у consumer)
5. Прочитать релевантный SKILL.md
6. Хирургический фикс → новая ветка → PR → /approve → merge
7. Обновить SYSTEM_STATE.md (этот файл)
```

**Правило одного файла:** если фикс только в одном файле — обоснуй почему другие слои не затронуты.

-----

## Как обновлять этот документ

После каждого из событий:

|Событие                    |Что обновить                             |
|---------------------------|-----------------------------------------|
|Новый бот задеплоен        |Добавить в таблицу ростера               |
|Изменился Redis-ключ       |Обновить таблицу контрактов              |
|Новая версия shared lib    |Обновить таблицу модулей + migration rule|
|Баг закрыт                 |Убрать из «Известных проблем»            |
|Новый скилл добавлен       |Добавить в таблицу Skills                |
|Обнаружен рассинхрон данных|Добавить в «Известные проблемы»          |

**Формат коммита:** `docs: update SYSTEM_STATE — {краткое что изменилось}`
-----

## Рефакторинг 2026-05-26

| Что сделано | Статус |
|---|---|
| BUG-002: HTTP_SECRET в filly-bot (NameError) | ✅ Исправлен |
| BUG-003: /health endpoint у 6 ботов (billy,milly,tilly,dilly,villy,prophet) | ✅ Исправлен (prophet — нужен UI) |
| BUG-004: miraculous-contentment CRASHED сервис | ✅ Удалён из Railway |
| BUG-005: shared lib версии (4 разных) | ✅ Все на v0.1.5 |
| BUG-006: decode_responses=False у 6 ботов | ✅ Унифицировано на True |
| BUG-007: Дублирование Ollama кода | ✅ Вынесено в shared/ollama.py |
| BUG-008: KEYS вместо SCAN в redis_helpers | ✅ Исправлен в v0.1.4 |
| BUG-011: нет Dockerfile у mama-bot | ✅ Добавлен |
| TD-002: sync Anthropic в trading-dept | ✅ Уже использует asyncio.to_thread |

**prophet-bot требует ручного действия:** Railway Dashboard → prophet-bot → Settings → переподключить GitHub repo (webhook не активен).
-----

## Рефакторинг Фаза 3 — 2026-05-26

| Что сделано | Статус |
|---|---|
| Ф3.3а: shared lib + log_event в nelli, ray, copy, lex | ✅ Готово |
| Ф3.3б: copy/lex SyntaxError (literal newline в строке) исправлен | ✅ Готово |
| Ф3.3в: nelli/ray локальный log_event заменён на импорт из shared lib | ✅ Готово |
| Ф3.3б: quality_keys_audit.py создан в scripts/ | ✅ Готово |
| DATA-001: Доктор/Дилли рассинхрон — Redis аудит проведён, ключи office:quality:доктор/дилли отсутствуют, рассинхрона нет | ✅ Закрыто |
| Villy/Gosling/Prophet — статус: активные официальные члены офиса | ✅ Подтверждено |

**Shared lib v0.1.15** — предыдущая версия.  
**Marketing-dept** теперь пишет в Redis через log_event (office:logs:копи, office:logs:лекс). Нэлли и Рэй переехали в family-dept.

---

## Архитектура роутинга (2026-05-26)

### Схема

```
Пользователь → ЛЮБОЙ бот (DM или группа)
                    ↓ POST /task
              Филли (filly-bot)
              ├─ enhance_prompt (Haiku)
              ├─ _resolve_agent (имя в тексте → LLM классификатор)
              └─ POST target_bot/task  { source: "ФИЛЛИ" }
                    ↓
         Глава отдела (опрашивает своих сам)
         ├─ МАРТИ  → Копи / Лекс (внутри marketing-dept)
         ├─ ТИЛЛИ  → Чарт / Вайс / Леджер / Фир (внутри trading-dept)
         ├─ ДИЛЛИ  → (в будущем) своя команда
         └─ СИЛЛИ  → dev-dept цепочка (dev_task intent)
                    ↓ JSON { "response": "..." }
              Филли получает ответ
                    ↓ POST source_bot/reply  { chat_id, text, from_agent }
              Бот-источник → bot.send_message(chat_id, text)
                    ↓
              Пользователь получает ответ
```

### BOT_URLS Филли (только главы + личные)

| Ключ | Бот | Роль |
|---|---|---|
| БИЛЛИ | billy-bot | Личный / дефолт |
| КРИС | kriss-bot | Личный ассистент |
| ГОСЛИНГ | gosling-bot | Чат-персонаж (Billie-чат) |
| ЭЛЛИС | ellice-bot | Семейный мост |
| ТИЛЛИ | tilly-bot | Глава трейдинг-отдела |
| ДИЛЛИ | dilly-bot | Глава медотдела |
| МАРТИ | marty-bot | Глава маркетинг-отдела |
| СИЛЛИ | ai-office-shared | Технический отдел |
| МИЛЛИ | milly-bot | Бизнес/автоматизация |
| ВИЛЛИ | villy-bot | Дизайн |
| ПРОРОК | prophet-bot | Сценарии/решения |

### Ключевые правила

- **source=ФИЛЛИ** в `/task` → бот возвращает JSON, НЕ пишет в Telegram сам
- **notify=True** → пишет в офис-группу (явный запрос)
- **Прямой вызов из группы** → пишет в офис-группу
- Тилли — эталонная реализация `notify=True` паттерна

### Новые endpoints (все боты)

| Endpoint | Метод | Назначение |
|---|---|---|
| `/task` | POST | Входящий запрос (от юзера или Филли) |
| `/reply` | POST | Принимает ответ от Филли, шлёт юзеру |
| `/health` | GET | Health check |

### shared lib v0.1.6

`ai_office_shared/shared/routing.py`:
- `forward_to_filly(message, user_id, reply_bot, reply_chat_id)` — форвард на Филли
- `make_reply_handler(bot, bot_name)` — фабрика /reply handler
- `is_routed(data)` — True если source=ФИЛЛИ

---

## Изменения 2026-05-26

| Что | Статус |
|---|---|
| Центральный роутинг через Филли | ✅ |
| /reply endpoint во всех ботах | ✅ |
| marketing-dept: ray/nelli форвард на Филли | ✅ |
| send_to_group fix: dilly/cilly молчат при source=ФИЛЛИ | ✅ |
| prophet-bot: inline routing (без shared lib) | ✅ |
| mama-bot: railway.json + git в Dockerfile | ✅ |
| Силли: auto-redeploy при FAILED деплоях | ✅ |
| shared lib v0.1.6: routing.py | ✅ |
| Силли: redis_query intent — реальный Redis query | ✅ |
| Силли: умная эскалация (_deep_diagnose_and_escalate) | ✅ |
| CLAUDE_WORK_PROTOCOL.md создан | ✅ |

**Ожидает ручного действия:**
- E2E тест с реальным DM (reply_chat_id != 0) — требует живого Telegram DM для проверки

✅ FILLY_URL в marketing-dept (marty, copy, lex) — исправлено 2026-06-11, добавлен https://

---

## Структура Railway проектов (2026-05-26)

```
dev-dept          — Отдел разработки (Силли — руководитель)
awake-happiness   — Офис (Силли переехала в dev-dept)
trading-dept      — Торговый отдел
marketing-dept    — Маркетинг
medical-dept      — Медотдел       [создан 2026-05-26]
family-dept       — Семья          [создан 2026-05-26]
```

### Распределение ботов по проектам

| Проект | Бот | URL | Роль |
|---|---|---|---|
| awake-happiness | filly-bot | filly-bot-production.up.railway.app | Диспетчер |
| awake-happiness | billy-bot | billy-bot-production.up.railway.app | Дефолт |
| dev-dept | ai-office-shared | ai-office-shared-production.up.railway.app | Глава dev-dept |
| awake-happiness | milly-bot | milly-bot-production.up.railway.app | Бизнес |
| awake-happiness | villy-bot | villy-bot-production.up.railway.app | Дизайн |
| awake-happiness | kriss-bot | kriss-bot-production.up.railway.app | Личный |
| awake-happiness | prophet-bot | prophet-bot-production-df65.up.railway.app | Решения |
| trading-dept | tilly-bot | tilly-bot-production.up.railway.app | Глава трейдинга |
| marketing-dept | marty-bot | marty-bot-production.up.railway.app | Глава маркетинга |
| family-dept | ray-bot | ray-bot-production.up.railway.app | Контент (семья) |
| marketing-dept | copy-bot | copy-bot-production.up.railway.app | Копирайтер |
| marketing-dept | lex-bot | lex-bot-production.up.railway.app | Юрист |
| medical-dept | dilly-bot | dilly-bot-production-4a9b.up.railway.app | Глава медотдела |
| family-dept | ellice-bot | ellice-bot-production.up.railway.app | Глава семьи |
| family-dept | nelli-bot | nelli-bot-production.up.railway.app | Семейная группа |
| dev-dept | devvy-bot | devvy-bot-production-9a4f.up.railway.app | Junior dev |
| dev-dept | ricky-bot | ricky-bot-production-ab47.up.railway.app | Code review |
| dev-dept | testi-bot | testi-bot-production-9cab.up.railway.app | QA/тестирование |
| dev-dept | sekky-bot | sekky-bot-production-9718.up.railway.app | Security audit |
| dev-dept | scribbi-bot | scribbi-bot-production-9aa7.up.railway.app | Документация |

### Filly BOT_URLS (только главы отделов + личные)

```python
"БИЛЛИ":   "billy-bot-production.up.railway.app"
"КРИС":    "kriss-bot-production.up.railway.app"
"ГОСЛИНГ": "gosling-bot-production.up.railway.app"
"СИЛЛИ":   "ai-office-shared-production.up.railway.app"
"МИЛЛИ":   "milly-bot-production.up.railway.app"
"ВИЛЛИ":   "villy-bot-production.up.railway.app"
"ПРОРОК":  "prophet-bot-production-df65.up.railway.app"
"ТИЛЛИ":   "tilly-bot-production.up.railway.app"          # trading-dept
"ДИЛЛИ":   "dilly-bot-production-4a9b.up.railway.app"     # medical-dept
"МАРТИ":   "marty-bot-production.up.railway.app"          # marketing-dept
"ЭЛЛИС":   "ellice-bot-production.up.railway.app"         # family-dept (глава)
```

### Redis
Все проекты используют один Redis: `yamabiko.proxy.rlwy.net:11592` (публичный URL).
Internal URL `redis.railway.internal:6379` доступен только из awake-happiness.

### Изменения 2026-05-26 (реструктуризация)
- Dilly переехал из awake-happiness → medical-dept (новый URL)
- Ellice/mama-bot переехала из awake-happiness → family-dept (новый URL)
- Nelli переехалась из marketing-dept → family-dept
- Ellice = глава family-dept, роутит на Filly
- Старые сервисы удалены из awake-happiness и marketing-dept

---

## Аудит и рефакторинг 2026-05-27

| Что сделано | Статус |
|---|---|
| Тег v0.1.7 создан (включает shared/url_check.py) | ✅ |
| ray-bot: requirements → v0.1.7 | ✅ |
| marty-bot: requirements SHA → тег v0.1.6 | ✅ |
| prophet-bot: /reply зарегистрирован в router (был определён но не смонтирован) | ✅ |
| prophet-bot: добавлен ai-office-shared v0.1.6 в requirements | ✅ |
| gosling-bot: добавлены /health и /reply endpoints | ✅ |
| mama-bot: добавлен /reply endpoint | ✅ |
| kriss-bot: добавлен /reply endpoint | ✅ |
| shared/url_check.py: HEAD/GET liveness check для всех ботов | ✅ |
| DATA-001: Доктор/Дилли quality рассинхрон | ✅ Закрыт 2026-05-27 — рассинхрона нет, :дилли пустой |


## Инциденты 2026-05-29

| Что | Причина | Фикс |
|---|---|---|
| Ellice-bot IndentationError строка 871 | Строка `/reply` выпала из блока `async with ptb:` — 4 пробела вместо 8 | Восстановлен правильный отступ, запушено ef910bb78e |
| Силли галлюцинирует выполнение Railway API | При agentic_task без явного http_request инструмента — генерирует псевдокод вместо реального вызова | Добавить source=CLAUDE в запросах; использовать явный http_request интент |
| Силли флудит в офис-группу при делегировании | Запросы без source=CLAUDE → _cilly_source не в _SILENT → send_to_group | Всегда передавать source=CLAUDE в /task запросах от Claude |


---

## dev-dept команда (2026-05-30)

| Бот | Railway ID | URL | Роль |
|---|---|---|---|
| Тести | 6a7af02c | testi-bot-production-9cab.up.railway.app | QA/тестирование |
| Девви | 0fd57999 | devvy-bot-production-9a4f.up.railway.app | Junior dev |
| Рикки | 7fd4059c | ricky-bot-production-ab47.up.railway.app | Code review |
| Секки | 664563e2 | sekky-bot-production-9718.up.railway.app | Security audit |
| Скрибби | 618ca12f | scribbi-bot-production-9aa7.up.railway.app | Документация |

Цепочка: Силли -> Девви -> Рикки -> Тести -> Секки -> Скрибби -> Силли  
Framework: ptb 21.3 + aiohttp + claude-haiku-4-5. Задеплоены 2026-05-30.

## Рефакторинг 2026-06-05

| Что сделано | Статус |
|---|---|
| OFFICE-routing накатан на tilly-bot, villy-bot (milly, doctor уже имели) | ✅ |
| github_tools.py: fallback GH_PAT когда GITHUB_TOKEN не задан | ✅ |
| Силли URL исправлен в BOT_URLS: cilly-bot → ai-office-shared | ✅ |
| SYSTEM_STATE.md обновлён до актуального состояния | ✅ |
## Рефакторинг 2026-06-11 (наведение порядка)

| Что сделано | Статус |
|---|---|
| Shared lib версия: v0.1.7 → v0.1.15 (шапка + migration rule + статус) | ✅ |
| DATA-001 Доктор/Дилли: Redis аудит проведён, рассинхрона нет, закрыто | ✅ |
| Схема роутинга: убраны Рэй/Нэлли из команды Марти (они в family-dept) | ✅ |
| Marketing-dept лог: убраны office:logs:нэлли/рэй | ✅ |
| Ростер ботов: добавлены Нэлли и Рэй, исправлен Эллис URL/репо | ✅ |
| Примечание Доктор ⚠: заменено на подтверждение отсутствия рассинхрона | ✅ |
| LUK_USER_ID: исправлен 331989769 → 675773302 | ✅ |
| BOT_URLS: doctor-bot → dilly-bot, mama-bot → ellice-bot | ✅ |
| FILLY_URL marketing-dept (marty/copy/lex): добавлен https:// | ✅ |
| office:mom_queue owner: mama-bot → ellice-bot | ✅ |
| Структура Railway: ray-bot перенесён marketing → family | ✅ |
| E2E тест DM | ⏳ Требует ручного теста |

## Рефакторинг 2026-06-13 (dev-dept воркеры + фиксы)

| Что сделано | Статус |
|---|---|
| Shared lib версия: v0.1.15 → v0.1.16 (pyproject.toml) | ✅ |
| dev-dept: Dockerfiles для всех 5 воркеров (devvy/ricky/testi/sekky/scribbi) | ✅ |
| Воркеры обновлены на v0.1.16 в requirements.txt | ✅ |
| SYSTEM_PROMPT воркеров улучшен: архитектура офиса, строгий формат ответа, haiku model | ✅ |
| Силли coder.py BUG: commit_msg из Скрибби приходил с префиксом "COMMIT_MSG: " в git push | ✅ Исправлен (извлечение ДО деплоя) |
| Пайплайн-тест (live): Девви→Рикки→Тести→Секки подтверждён рабочим | ✅ |
| ВСЕ изменения на ветке `claude/stroy-dev-dept-j6q4o9` — ждут merge | ⏳ |

---

## Инцидент 2026-06-13 — Силли упала из-за stub-пуша

### Хронология

| Время UTC | Событие |
|---|---|
| ~09:56 | squash-merge ветки `claude/stroy-dev-dept-j6q4o9` в main. Sub-agent вместо полного coder.py (4399 строк) записал в feature-ветку 1-строчный stub-комментарий. Merge пустил stub в main. |
| ~10:01 | Railway задетектил новый коммит в main, запустил редеплой Силли. Cilly клонирует main → `agents/coder.py` = stub → Python выполняет (no-op) → порт не открывается → Railway убивает контейнер. |
| ~10:06 | WatchDog: "🔴 Силли не восстановилась после редеплоя. Вероятно сломан код." |
| ~10:41 | Первая попытка emergency-fix: 56 строк вместо 4399 (неполный чанк). |
| ~15:25 | Полное восстановление: git-пуш 4399-строчного coder.py напрямую из /home/user/ai-office-shared через git proxy. Commit `7bcff99`. |

### Корневая причина (why×5)

1. Sub-agent записал stub → потому что разбивал файл на чанки и сохранил только первый
2. Первый чанк → потому что MCP push_files имеет лимит, агент не дочитал задание до конца
3. Агент не перепроверил → потому что не было обязательной верификации перед merge
4. Merge без проверки → потому что не было правила "читай первую+последнюю строку из GitHub ДО merge"
5. Нет правила → потому что это первый случай stub-пуша; паттерн не был известен

### Уязвимости (карта)

| Компонент | Риск |
|---|---|
| Любой sub-agent пуш большого файла | Может записать chunk вместо полного файла |
| squash-merge без content-review | Stub попадает в main мгновенно |
| Cilly Dockerfile: `git clone ... main` | Любой broken commit в main = downtime |
| Emergency-fix через sub-agent | Та же уязвимость что и при основном пуше |

### Новые правила (обязательные с 2026-06-13)

> **ПРАВИЛО: ПЕРЕД MERGE в main — читай `wc -l` или `git diff --stat` файла в ветке**  
> Если файл должен быть N строк, а в GitHub N/10 — это stub. Merge запрещён.

> **ПРАВИЛО: Большие файлы (>500 строк) — ТОЛЬКО через git в /home/user/{repo}, НЕ через sub-agent**  
> Sub-agent + push_files MCP → риск stub. Прямой git push через proxy → гарантия.

> **ПРАВИЛО: После emergency-fix — проверяй `additions` в commit stats (GitHub)**  
> Если additions << ожидаемого числа строк → fix неполный.

### Статус после восстановления

| Что | Результат |
|---|---|
| agents/coder.py в main | ✅ 4399 строк, commit `7bcff99` |
| Все 5 воркеров (devvy/ricky/testi/sekky/scribbi) | ✅ полный код, не stub |
| Railway редеплой Силли | ⏳ запущен автоматически (~2 мин) |

---

## Параллельный dev-dept + эфир активности (2026-06-13)

Ветка: `claude/dev-parallel-workflow-rz4hkt` (во всех репо отдела).

### Что изменилось

| Что | Статус |
|---|---|
| Пайплайн dev-dept: последовательный → **параллельный fan-out** | ✅ |
| Девви → **[Рикки ‖ Тести ‖ Секки]** (`asyncio.gather`) → Скрибби | ✅ |
| Новый модуль `ai_office_shared/shared/dev_activity.py` — общий эфир действий | ✅ |
| `dev_pipeline.py` переписан: gather + Semaphore + timeout + ретраи с backoff | ✅ |
| coder.py (Силли): inline-цепочка заменена вызовом `run_dev_pipeline()`, Силли публикует план/деплой в эфир | ✅ 4393 строки, compile OK |
| Воркеры (×5): читают эфир команды → блок `[ДЕЙСТВИЯ КОМАНДЫ DEV-DEPT]` в промпт; публикуют start/done; try/except вокруг LLM (не роняют оркестратор) | ✅ |
| Shared lib: v0.1.16 → **v0.1.17** (pyproject) | ✅ |
| **БАГ найден и исправлен**: `scribbi-bot/bot.py` не компилировался — вложенные `"""` в SYSTEM_PROMPT (строка 54) закрывали строку. Скрибби не мог стартовать. | ✅ Исправлен |

### Контракт эфира (НОВЫЙ Redis-ключ)

| Ключ/канал | Тип | Назначение |
|---|---|---|
| `dev-dept:activity:{task_id}` | LIST (LTRIM 200, TTL 24ч) | лента действий команды по задаче |
| `dev-dept:activity` | pub/sub channel | live-вещание событий |

Запись: `{ts, task_id, bot, phase, summary, level}`, phase ∈ plan/start/done/error/deploy.
Контракт реализован дважды идентично: `shared/dev_activity.py` (Силли) и inline в `bot.py` воркеров
(воркеры пинят shared lib по тегу v0.1.15 → inline-копия, чтобы фича не зависела от нового релиза).

### ENV-тюнинг нагрузки (по умолчанию безопасны)

| Переменная | Default | Смысл |
|---|---|---|
| `DEV_MAX_CONCURRENCY` | 6 | одновременных вызовов воркеров на процесс Силли |
| `DEV_WORKER_TIMEOUT` | 120 | таймаут одного `/task` (сек) |
| `DEV_WORKER_RETRIES` | 2 | доп. попыток сверх первой (backoff 1с/2с) |

### Верификация (локально, без живых ботов)

- `test_pipeline`: доказан порядок Девви→[parallel]→Скрибби, реальное пересечение Рикки‖Секки во времени, ретрай упавшего Тести, заполненный эфир — 6/6 ✅
- `test_interop`: worker↔orchestrator пишут/читают эфир по одному контракту, exclude-self, fail-silent при Redis=None — 5/5 ✅
- `py_compile` всех тронутых файлов ✅

### Урок

> Прошлая проверка воркеров искала **stub** (по `wc -l`), но не запускала `py_compile`.
> Поэтому syntax-баг в scribbi (полный по строкам, но не компилируется) прошёл незамеченным.
> **ПРАВИЛО: верификация = `py_compile`, а не только подсчёт строк.**

---

## Апгрейд торгового отдела (2026-06-13)

Ветка: `claude/trading-dept-upgrade-rz4hkt` (tilly-bot, prophet-bot, trading-dept, ai-office-shared).
Полная дорожная карта: `trading-dept/UPGRADE_ROADMAP.md`.

### Исправленные краши (найдены через pyflakes — `py_compile` их не ловит)

| Бот | Баг | Эндпоинт |
|---|---|---|
| tilly-bot | `check_secret`, `HTTP_SECRET` не определены | `/send_scheduled`, `/send` |
| tilly-bot | `/reply` использовал необъявленный `ptb` | `/reply` (доставка ответа юзеру) |
| prophet-bot | `redis_client`, `BOT_NAME_LOWER`, `REACTION_UP/DOWN`, `HTTP_SECRET` не определены; dup `import httpx`; `ptb` в `/reply` | реакции-качество, `/send`, `/reply` |

### Качество (промт + модели)

| Что | Было | Стало |
|---|---|---|
| tilly-bot вердикт совета (промт) | пересказ отчётов | синтез + разрешение противоречий + R/R≥1.5 + инвалидация + сайзинг |
| tilly-bot вердикт (модель) | sonnet-4-6 | **opus-4-8** (финальное решение) |
| tilly-bot: учёт недоступных советников | молча терялись | явно сообщается модели (честная уверенность) |
| tilly-bot: запрос к советнику | 1 попытка | + ретрай (backoff 2с) |
| prophet-bot полный прогноз (модель) | sonnet-4-6 | **opus-4-8** (короткий режим — sonnet) |
| trading-dept Vision (графики) | sonnet-4-0 (`...20250514`) | **sonnet-4-6** |
| trading-dept `_fetch` данных | без ретрая | + ретрай |

### Модели — заметка
`claude-sonnet-4-6` **актуальна** (не путать с устаревшими cutoff-данными). Финальные решения отдела (вердикт Тилли, прогноз Пророка) — `claude-opus-4-8`; рабочие вызовы (советники, vision, web_search, короткие реплики) — `claude-sonnet-4-6` / `claude-haiku-4-5-20251001`.

### Урок
> Субагент-разведчик дважды выдал ложные «баги» (актуальную модель назвал несуществующей; приписал bingx_client чужой API-ключ). **ПРАВИЛО: claim субагента про код/модели — проверять руками (`grep`/чтение) до правки.** `tilly-trader` НЕ трогали — money-adjacent, только в роадмапе.

---

## Силли: защита от галлюцинаций и битых деплоев (2026-06-13)

Ветка: `claude/cilly-safety-rz4hkt` (ai-office-shared, devvy-bot, ricky-bot).
Поводом стал живой тест делегирования: Силли на простой задаче **сказала «закиданул в trading-dept», но НЕ запушила** (галлюцинация деплоя); на dev_task команда отработала, но **Девви (Haiku) обрезал код** по `max_tokens` — Рикки/Тести это поймали (NEEDS_FIX), однако Силли игнорировала вердикт и могла бы запушить битое на существующий файл.

| Что | Где |
|---|---|
| **Гейт деплоя dev_task**: не пушим, если Рикки вернул `NEEDS_FIX` ИЛИ код не проходит `compile()` | `coder.py` (deploy-блок dev_task) |
| **Анти-галлюцинация**: правило в `CHAT_PROMPT` — никогда не утверждать про push/деплой/создание, если фактически не сделано | `coder.py` (CHAT_PROMPT) |
| **max_tokens 4096 → 8192** у Девви и Рикки (полный файл целиком) | `devvy-bot/bot.py`, `ricky-bot/bot.py` |

> Тонкость: `compile()` ловит структурный обрыв (незакрытая скобка/строка), но НЕ «валидный, но недописанный» код (`raise ValueErro` — синтаксически валиден). Поэтому гейт двойной: **вердикт Рикки + compile**.

### Поведение Силли при делегировании (для справки)
- `/task` контракт: `{message, agent, source:"CLAUDE"}`. `source=CLAUDE` → тихий режим, ответ только в JSON.
- Команду (`dev_task`) зовёт ТОЛЬКО при явных сигналах («отдай команде», «через цепочку», «реализуй модуль») + confidence≥0.85. Иначе отвечает сама.
- Планировщик может **переопределить** явный `repo` из payload (выбирает свой).

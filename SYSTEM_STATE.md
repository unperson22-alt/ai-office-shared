# AI Office — SYSTEM_STATE.md

> **Живой документ.** Силли обновляет после каждого значимого изменения:
> деплой нового бота, смена Redis-контракта, обновление shared lib, закрытие уязвимости.
> Формат обновления — в конце файла.

**Последнее обновление:** 2026-06-05  
**Версия shared lib:** v0.1.7  
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
|-----------|--------------------|------------------------|-----------|----------|------------------------------------------|-----------------------------|
|**Филли**  |— (роутер)          |`фили` ¹                |filly-bot  |`5d61d403`|порт 8080, aiohttp                        |python-telegram-bot + aiohttp|
|**Билли**  |`БИЛЛИ`             |`билли`                 |billy-bot  |`b441ce93`|billy-bot-production.up.railway.app       |python-telegram-bot          |
|**Крисс**  |`КРИС`              |`крисс` ²               |kriss-bot  |`92f70bbb`|kriss-bot-production.up.railway.app       |python-telegram-bot          |
|**Эллис**  |`ЭЛЛИС`             |`эллис`                 |mama-bot   |`fa7c87cf`|mama-bot-production.up.railway.app        |python-telegram-bot          |
|**Тилли**  |`ТИЛЛИ`             |`тилли`                 |tilly-bot  |`367e25d7`|tilly-bot-production.up.railway.app       |python-telegram-bot          |
|**Милли**  |`МИЛЛИ`             |`милли`                 |milly-bot  |`db277aff`|milly-bot-production.up.railway.app       |python-telegram-bot          |
|**Доктор** |`ДИЛЛИ`             |`доктор` ⚠              |dilly-bot  |`d949c4d2`|dilly-bot-production.up.railway.app       |python-telegram-bot          |
|**Вилли**  |`ВИЛЛИ`             |`вилли`                 |villy-bot  |`a5e37cc4`|villy-bot-production.up.railway.app       |python-telegram-bot          |
|**Гослинг**|`ГОСЛИНГ`           |`гослинг`               |gosling-bot|`ed03c9d3`|gosling-bot-production.up.railway.app ³       |python-telegram-bot          |
|**Силли**  |`СИЛЛИ`             |`силли`                 |ai-office-shared|`efa6bd21`|ai-office-shared-production.up.railway.app|aiogram 3.7 + Telethon       |
|**Пророк** |`ПРОРОК`            |— ⁴                     |prophet-bot|—         |prophet-bot-production-df65.up.railway.app|python-telegram-bot          |

**Примечания:**  
¹ Филли-роутер не пишет quality-реакции — `office:quality:фили` всегда 0, это норма.  
² Uppercase агент — `КРИС` (одна С), lowercase quality-ключ — `крисс` (две С). Не путать.  
³ Гослинг на internal URL — доступен только внутри Railway-сети. Внешний curl не сработает.  
⚠ Доктор — **неверифицированный рассинхрон**: health/routing пишется через `ДИЛЛИ`, а quality Филли читает через `доктор`. Бот (dilly-bot) может писать `office:quality:дилли`. Требует проверки через `quality_keys_audit.py`.  
⁴ Пророк не в METRICS_BOTS, quality не трекается.

**Специальные пользователи:**

|Переменная        |ID         |Роль                                          |
|------------------|-----------|----------------------------------------------|
|`YOUR_TELEGRAM_ID`|(из env)   |Влад — владелец, полный доступ                |
|`LUK_USER_ID`     |`331989769`|Лук — особый гость, альтернативное приветствие|

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
|`office:mom_queue`             |LIST  |Эллис (`mama-bot`)                |Эллис (`/mention` endpoint)         |7д                   |JSON очередь сообщений мамы, сбрасывается при пинге из Филли             |

**Типичные события в `office:logs`:**
`route_ok`, `route_miss`, `route_decision`, `message_received`, `response_sent`,
`task_received`, `api_error`, `lesson_saved`, `capability_gap_detected`, `dm_fallback`

-----

## Shared Library — ai_office_shared

**Репо:** unperson22-alt/ai-office-shared (публичный)  
**Установка:** `ai_office_shared @ git+https://github.com/unperson22-alt/ai-office-shared@v0.1.3`

|Модуль                |С версии|Что экспортирует                                                                                               |
|----------------------|--------|---------------------------------------------------------------------------------------------------------------|
|`shared.logging`      |v0.1.0  |`log_event(redis, bot, event, **kwargs)`                                                                       |
|`shared.identity`     |v0.1.1  |`BOTS`, `canonical()`, `display()`, `redis_key()`                                                              |
|`shared.redis_helpers`|v0.1.2  |`redis_get_history`, `redis_save_history`, `redis_get_notes`, `redis_add_note`                                 |
|`shared.tasks`        |v0.1.2  |`auto_extract_interests`, `weekly_review`                                                                      |
|`shared.quality`      |v0.1.3  |`remember_my_message`, `reaction_owner`, `classify_reaction`, `record_reaction`, `REACTION_UP`, `REACTION_DOWN`|
|`shared.url_check`    |v0.1.7  |`check_url`, `filter_live_urls`, `extract_urls`, `verify_text_urls`                                            |

### ⚡ MIGRATION RULE

> При любом касании бота по любой причине — **обязательно**:
> 
> 1. Поднять в `requirements.txt`: `ai_office_shared @ ...@v0.1.7`
> 1. Заменить локальные копии на импорты из `ai_office_shared.shared`:
>    `redis_get_history`, `redis_save_history`, `redis_get_notes`, `redis_add_note`,
>    `auto_extract_interests`, `weekly_review`

**Текущий статус миграции ботов:** все боты мигрированы на `v0.1.7` (2026-05-27).
Включает: quality, redis_helpers, tasks, ollama. Локальные копии удалены.

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
| DATA-001: Доктор/Дилли рассинхрон задокументирован, аудит-скрипт готов | ⚠️ Требует запуска |
| Villy/Gosling/Prophet — статус: активные официальные члены офиса | ✅ Подтверждено |

**Shared lib v0.1.5** — текущая актуальная версия.  
**Marketing-dept** теперь пишет в Redis через log_event (office:logs:нэлли, office:logs:рэй, office:logs:копи, office:logs:лекс).

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
         ├─ МАРТИ  → Рэй / Копи / Лекс / Нелли (внутри marketing-dept)
         ├─ ТИЛЛИ  → Чарт / Вайс / Леджер / Фир (внутри trading-dept)
         ├─ ДИЛЛИ  → (в будущем) своя команда
         └─ СИЛЛИ  → Claude Code субагент
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
| ГОСЛИНГ | gosling-bot | Группа Лука |
| ЭЛЛИС | mama-bot | Семейный мост |
| ТИЛЛИ | tilly-bot | Глава трейдинг-отдела |
| ДИЛЛИ | doctor-bot | Глава медотдела |
| МАРТИ | marty-bot | Глава маркетинг-отдела |
| СИЛЛИ | cilly-bot | Технический отдел |
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
- E2E тест с реальным DM (reply_chat_id != 0)
- FILLY_URL в Railway Dashboard → marketing-dept (5 сервисов)

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
| marketing-dept | ray-bot | ray-bot-production.up.railway.app | Контент |
| marketing-dept | copy-bot | copy-bot-production.up.railway.app | Копирайтер |
| marketing-dept | lex-bot | lex-bot-production.up.railway.app | Юрист |
| medical-dept | dilly-bot | dilly-bot-production-4a9b.up.railway.app | Глава медотдела |
| family-dept | ellice-bot | ellice-bot-production.up.railway.app | Глава семьи |
| family-dept | nelli-bot | nelli-bot-production.up.railway.app | Семейная группа |

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
- Nelli переехала из marketing-dept → family-dept
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


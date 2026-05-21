# AI Office — SYSTEM_STATE.md

> **Живой документ.** Силли обновляет после каждого значимого изменения:
> деплой нового бота, смена Redis-контракта, обновление shared lib, закрытие уязвимости.
> Формат обновления — в конце файла.

**Последнее обновление:** 2026-05-20  
**Версия shared lib:** v0.1.3  
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
|**Гослинг**|`ГОСЛИНГ`           |`гослинг`               |gosling-bot|`ed03c9d3`|gosling-bot.railway.internal:8080 ³       |python-telegram-bot          |
|**Силли**  |`СИЛЛИ`             |`силли`                 |cilly-bot  |`efa6bd21`|cilly-bot-production.up.railway.app       |aiogram 3.7 + Telethon       |
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
|`office:members`               |—     |—                                 |—                                   |—                    |**Не реализован.** Запланирован (Блок 02)                                 |
|`office:mom_queue`             |—     |—                                 |—                                   |—                    |**Не реализован.** Запланирован (Блок 05)                                 |

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

### ⚡ MIGRATION RULE

> При любом касании бота по любой причине — **обязательно**:
> 
> 1. Поднять в `requirements.txt`: `ai_office_shared @ ...@v0.1.3`
> 1. Заменить локальные копии на импорты из `ai_office_shared.shared`:
>    `redis_get_history`, `redis_save_history`, `redis_get_notes`, `redis_add_note`,
>    `auto_extract_interests`, `weekly_review`

**Текущий статус миграции ботов:** все на `@v0.1.0` (только `log_event`).
Полная миграция — попутно при следующей правке каждого бота.

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
|`BUG-001` |filly-bot `/metrics` |Handler зависает (http=000) — нет таймаутов на Redis-awaits. Патч готов (`handle_metrics_fixed.py`), ждёт деплоя после Railway outage.|⏳ Ожидает деплоя  |
|`DATA-001`|Доктор / METRICS_BOTS|`office:quality:доктор` vs возможный `office:quality:дилли` у dilly-bot. Проверить `quality_keys_audit.py`.                           |⚠ Требует проверки|

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

# SYSTEM REFACTOR — PLAN (AI Office)

> Персистентный артефакт для per-module сессий. Источник правды — файлы на диске.
> Эталон стандарта = **Крисс (`kriss-bot`)**. Общие модули приводятся К Криссу.
> Ветка разработки: `claude/system-refactor-audit-plan-srdg1z`.
> Статус: **AUDIT+PLAN одобрен Владом. EXECUTION по компонентам — с подтверждением каждого.**

## Красные линии (НЕ трогать без согласования)
- Схемы Redis-ключей и формат истории/notes (данные — источник правды, не код).
- Контракт `source=CLAUDE`→silent; `target_chat` vs `chat_id`.
- Версии shared-lib / PTB живых ботов на v0.1.18 — **заморожены** (решение Влада).
- Боевые токены/ID — только через env, никогда в коде.
- Каждый коммит в `ai-office-shared` = **90 сек даунтайма Силли** → батчить shared-правки
  в один тег, потом патчить ботов (сначала тег символа в shared, затем боты).
- HTTP-старт только в `main()`. Перед чтением незнакомого Redis-ключа — `r.type()`.

## Находки по приоритетам (file:line)

### 🔴 Критично
- `kriss-bot/bot.py`: `async def _call_office` определена дважды (стр. 28 делегирует в shared;
  стр. 46 инлайн, затирает первую) — мёртвый/конфликтующий код, растиражирован в форки
  (`mama-bot`, `family-dept/elliss`).
- Дрейф версий shared-lib: эталон v0.1.18; `devvy/ricky/scribbi/sekky/testi`=v0.1.15;
  `filly/prophet/ray`=v0.1.14; часть без пина. → **только документируем** (задача A1).

### 🟠 Высокий
- Инлайн-дубли `_enhance_prompt`/`_call_office` в 9 ботах
  (`billy, doctor, gosling, kriss, mama, milly, tilly, villy, family-dept/elliss`).
  `office.py` уже имеет `call_office`; `enhance` в shared отсутствует → кандидат на вынос.
- Дубль-бот `mama-bot/bot.py` (972) ↔ `family-dept/elliss/bot.py` (889) — идентичные заголовки.
  Канон неясен → открытый вопрос #1.
- Фрагментация PTB: 20.7 / 20.8–21.0 / 21.3 (эталон) / 21.5 / 21.10 / >=21.0 / >=21.3,<22.
  → документируем (задача A1).
- Хардкод TG Влада `chat_id=391077101`: `mama-bot:802`, `kriss-bot:814`,
  `family-dept/elliss:764` (скопированный блок feature_text). → env/конфиг.

### 🟡 Средний
- Хардкод id моделей: 25 ботов хардкодят `model="claude-..."`, 11 берут из env. Устаревшие:
  `claude-sonnet-4-20250514` (1×), `claude-haiku-4-5` без даты (1×); канон —
  `claude-sonnet-4-6` (43×), `claude-haiku-4-5-20251001` (38×). → константы моделей в shared
  + env-override.
- Резилентность неравномерна: `retry` 14 файлов, `backoff` 2, `try_ollama` 13. Нет единого
  паттерна graceful-degradation.

### 🟢 Улучшения
- Департаменты (`family-dept`, `marketing-dept`: `main.py+handlers.py`; `trading-dept`:
  `bot.py+connectors.py`) — единый каркас не выделен.
- `SYSTEM_STATE.md` только у `ai-office-shared`.

## Порядок EXECUTION (снизу вверх; каждый ярус целиком)
1. **`ai-office-shared`** (фундамент): убрать дубль/мёртвый код; вынести `_enhance_prompt`
   в `office.py` (или новый модуль); добавить константы моделей. Батч → один тег.
2. **`kriss-bot`**: перейти на новый shared API (импорт вместо инлайна).
3. **Форки по одному**: `mama/elliss` → `billy, tilly, milly, doctor, gosling, villy`.
4. **Хардкоды** (`chat_id`, модели) → env/конфиг.
5. **Департаменты + резилентность** (🟡/🟢).

## Поведение при отказе сервисов (реализовать на EXECUTION)
- Claude API: retry+backoff → Ollama (`try_ollama`) → понятное сообщение + лог.
- Redis: ретраи → локальный кэш/in-memory очередь → не падать в хендлере.
- Notion/GitHub: ретраи → отложить в очередь задач, не блокировать ответ.
- web_search: таймаут → ответ без веб-контекста + пометка в логе.

## DoD — «проверено» = ЗАПУЩЕНО
- shared: `python -c "import ai_office_shared.shared.<mod>"` + прогон затронутой функции.
- бот: `python -c "import bot"` с замоканным env; тесты если есть; dry-run триггера.
- Перед бампом версии бота — smoke-импорт новых символов shared.

## Открытые вопросы
1. **mama vs elliss — РЕШЕНО (2026-06-28).** Канон = **`mama-bot`**. Доказательства:
   `mama-bot` активен (коммиты 19–20 июня, shared **v0.1.18**, PTB 21.5, office:instructions —
   фича v0.1.18), полный деплой-конфиг. `family-dept/elliss` застыл 28 мая, shared **v0.1.9**;
   `family-dept/README.md` «Статус миграции» — все чекбоксы пустые, вкл. «[ ] Создать Railway
   project family-dept» → `family-dept` на Railway **никогда не разворачивался**. Реестр Notion:
   Эллис = Мама-бот = один бот `@ellice_mom_bot` (Service ID «—»); Рэй/Нелли — в `marketing-dept`.
   **Итог:** живой `@ellice_mom_bot` деплоится из `mama-bot`; весь `family-dept` — заброшенная
   незавершённая миграция. **Действие:** рефакторим только `mama-bot`; `family-dept/elliss`
   исключён из волны, к удалению/deprecated (решение об удалении — за Владом). Хардкод
   `chat_id=391077101` правим в `mama-bot:802` и `kriss-bot:814` (строку в elliss игнорируем).
2. Делегирование Силли: массовые бампы/redeploy — один компактный self-contained промпт Силли
   через `POST /task` (`source: "CLAUDE"`).

## Не входит в текущую волну
Выравнивание версий PTB/shared (задача A1) — только документируется.

## HANDOFF LOG (дополнять в конце каждой сессии: done / next / state)
- 2026-06-28: Проведён AUDIT, план одобрен. Создан REFACTOR_PLAN.md + Notion. Кода ещё нет.
  NEXT: компонент №1 — `ai-office-shared` (после явного OK Влада).
- 2026-06-28: Закрыт открытый вопрос #1 — канон Эллис/Мама-бот = `mama-bot` (`family-dept` никогда
  не деплоился на Railway). `family-dept` исключён из волны. STATE: ждём OK на компонент №1.
- 2026-06-28: **Компонент №1 DONE** (на ветке, без тега/деплоя). Добавлены `shared/models.py`
  (MODEL_SONNET/HAIKU/OPUS + env-override) и `shared/prompt.py` (`enhance_prompt`, каноник из
  Крисса, fail-silent); `__init__` реестр дополнен; версия 0.1.18→**0.1.19**. Верификация:
  импорт-чек + env-override + функциональный smoke (длинный без изменений, fail-silent, enhance) —
  всё зелёное. NEXT: Компонент №2 — `kriss-bot`: удалить инлайн-дубль `_call_office` (46-57,
  постит без `source`!) и `_enhance_prompt`, импортировать из shared, пин на v0.1.19.
  ⚠️ Тег v0.1.19 + деплой shared (90с даунтайма) — батчим после готовности Крисса.
- 2026-06-28: **Компонент №2 (kriss-bot) DONE** — ветка `claude/ai-office-kriss-bot-refactor-u5f9o3`,
  коммит kriss-bot `c123d13`. Удалён мёртвый инлайн `_call_office` v2 (постил `/task` без `source`);
  остался делегирующий v1 → shared `call_office` (шлёт `source`). Удалён локальный `_enhance_prompt`
  → импорт `enhance_prompt` из shared. Хардкод моделей → `MODEL_SONNET` (3×) / `MODEL_HAIKU` (1×).
  `requirements.txt` пин `@v0.1.19`. Верификация: `py_compile` зелёный, нет остаточных хардкодов/дублей.
  Решение Влада: компонент №1 слит в `u5f9o3` (тегаем оттуда); тег v0.1.19 + деплой shared + redeploy
  Крисса — выполняются в этой сессии. NEXT: после деплоя — Компонент №3 (форки: mama → billy/tilly/…).
- 2026-06-28: **РАЗБОР ТЕГОВ + переход на SHA-пины (по решению Влада).** Находка: удалённые теги
  shared есть только до `v0.1.15`. Теги `v0.1.16/17/18` НИКОГДА не пушились на GitHub — агентские
  сессии получают `HTTP 403` на push `refs/tags/*` (подтверждено: я тоже не смог запушить `v0.1.19`;
  GitHub MCP не умеет создавать теги/релизы). Код main при этом на `0.1.18`. Следствие: 7 ботов
  на висячем `@v0.1.18` (billy, doctor, gosling, mama, milly, tilly, villy) — их пин ссылается на
  несуществующий ref; чистый ребилд упал бы на `pip install`. Рантайм-проверка: mama `/health`→404
  (хотя маршрут в коде есть) ⇒ живые образы старее, реально крутятся на shared **≤ v0.1.15**;
  «фичи v0.1.18» (office:instructions) в проде у них НЕ выкачены. **Решение Влада (1a+2a):**
  релиз-механизм shared переведён с тегов на **commit-SHA пины** (агент может пушить ветки/SHA,
  GitHub отдаёт любой reachable-SHA). Все живые боты перепинены на shared SHA
  `b621e947c5ec1f7d6aae119547a31b5df4a1a234` (kriss `1c0534a`; 7 ботов — по 1 коммиту на ветке).
  Открыто **9 PR** `claude/ai-office-kriss-bot-refactor-u5f9o3 → main`: ai-office-shared#17,
  kriss-bot#2, billy#3, doctor#2, gosling#2, mama#2, milly#2, tilly#3, villy#2.
  ⚠️ **Порядок merge: СНАЧАЛА shared#17**, потом 8 остальных (SHA `b621e94` живёт на ветке shared;
  достижим пока ветка не удалена / после merge — из истории main). Деплой/Railway/merge — за Владом
  (агент в `main` пушить не может, Railway не триггерит).
  NEXT (новая сессия): (1) после merge — смоук: mama `/health` должен стать 200; kriss и остальные
  без регрессий. (2) **Компонент №3 — собственно рефактор форков** (mama → billy/tilly/milly/doctor/
  gosling/villy): у них всё ещё инлайн-дубли `_call_office`/`_enhance_prompt` + хардкод моделей —
  в этой волне их ТОЛЬКО перепинили на shared, код НЕ трогали. Канон = `mama-bot` (см. вопрос #1).
- 2026-06-29: **SECURITY-ВОЛНА (Влад: «Ебош 1 и 2»).** Повод: в окружении другой сессии найдены
  staged-payload'ы `silli_*.json` (`source:CLAUDE`, сбор GitHub PAT+Railway-токенов, `cleanup_dm`),
  Влад их НЕ создавал → враждебные. Корень: **RPC-меш офиса не аутентифицирован** — `/task`,`/reply`,
  `/send*` ботов открыты из интернета (публичные Railway URL), у Силли `/task`/`/promote_bots`/`/envcheck`
  без auth, `source:CLAUDE` подделываем. Также: теги shared >v0.1.15 не существуют (агент не пушит
  теги, 403) → перешли на SHA-пины.
  **Фикс (shared v0.1.20, SHA `bf3fb46`):** новый модуль `shared/auth.py` — `office_headers()` (исходящий
  `X-Office-Token`), `office_auth_middleware` (закрывает все маршруты кроме `/health`), `check_office_token`.
  Двухфазный выкат БЕЗ даунтайма: warn-режим пока не выставлены `OFFICE_RPC_TOKEN`+`OFFICE_RPC_STRICT`.
  Покрыт ВЕСЬ живой меш: Силли + kriss,billy,doctor,gosling,mama,milly,tilly,villy + devvy,ricky,scribbi,
  sekky,testi + prophet,filly(роутер,+7 исходящих),ray,marketing-dept(marty/nelli/lex) + vietnam,pilly,
  railway-deployer,pilly-bot-bot,trading-dept (+добавлена shared-зависимость и git в Dockerfile) +
  new_bot_template (будущие боты). Все на ветке `claude/ai-office-kriss-bot-refactor-u5f9o3`, SHA-пин `bf3fb46`.
  Исключены: `family-dept/*` (заброшен), `marty/bot.py` (мёртв, entrypoint main.py).
  **РОЛЛАУТ (env-first, без даунтайма):** (0) контейнмент: закрыть публичный `/task` Силли + ротация его
  секретов. (1) выставить `OFFICE_RPC_TOKEN=<секрет>` на ВСЕ сервисы. (2) задеплоить новый код везде.
  (3) проверить логи `[office-auth] WARN` — кто ещё не шлёт токен. (4) когда WARN чисто → `OFFICE_RPC_STRICT=1`
  на всех → enforcement (401).
  **Flip-time гэпы (закрыть до STRICT):** (a) Силли провижинит Railway-cron, который POST'ит `/send_scheduled`
  без `X-Office-Token` (embedded-команда в cron) → расписания упадут при STRICT; (b) `/secrets`,`/redis` Силли
  теперь под middleware → их вызыватели (Claude-тулинг) должны слать `X-Office-Token`. Dead double-brace
  `{{filly}}/task` в ray/lex/nelli main.py — пре-существующий баг, НЕ трогали.

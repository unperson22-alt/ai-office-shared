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
1. **mama vs elliss** — какой реально задеплоен на Railway? Определить канон, второй — к выводу.
2. Делегирование Силли: массовые бампы/redeploy — один компактный self-contained промпт Силли
   через `POST /task` (`source: "CLAUDE"`).

## Не входит в текущую волну
Выравнивание версий PTB/shared (задача A1) — только документируется.

## HANDOFF LOG (дополнять в конце каждой сессии: done / next / state)
- 2026-06-28: Проведён AUDIT, план одобрен. Создан REFACTOR_PLAN.md + Notion. Кода ещё нет.
  NEXT: компонент №1 — `ai-office-shared` (после явного OK Влада).

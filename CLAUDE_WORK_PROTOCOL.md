# Claude Work Protocol — AI Office

> Читать ПЕРВЫМ перед любым действием в этом репо.

---

## 1. Context Scan (обязательно)

Перед началом работы:
- Проверить memories и прошлые разговоры
- Прочитать `SYSTEM_STATE.md` — текущая архитектура, открытые баги, Redis контракты
- Прочитать `shared/ops.md` если есть — последние действия

---

## 2. Пауза Силли при массовых изменениях

**Правило:** если меняются ≥2 бота одновременно — **сначала** поставить Силли на паузу.

**Почему:** Силли мониторит все боты. Во время деплоев она видит краши, пытается чинить,
создаёт конфликты с текущей работой и мешает диагностике.

**Как поставить на паузу** — через Railway API:
```
CILLY_MONITOR_PAUSED = true  (env var в Railway → cilly-bot)
```
Или через Railway GraphQL:
```graphql
mutation {
  variableUpsert(input: {
    projectId: "271b40b7-199a-429a-88ef-ca417f26a638"
    environmentId: "2efaaf60-ba39-492c-bf86-007fd505493f"
    serviceId: "efa6bd21-91d8-467f-8250-60f8a3853791"
    name: "CILLY_MONITOR_PAUSED"
    value: "true"
  })
}
```

**Как снять паузу** — то же самое, `value: "false"`.

**Проверить статус паузы** перед работой:
```graphql
{ variables(projectId: "271b40b7...", environmentId: "2efaaf60...", serviceId: "efa6bd21...") }
# → смотреть CILLY_MONITOR_PAUSED
```

**Порядок работы при массовом деплое:**
1. Поставить Силли на паузу
2. Деплоить ботов по одному
3. Проверить /health каждого
4. Убедиться что все SUCCESS в Railway
5. Снять паузу с Силли
6. Проверить логи Силли — нет ли ложных алертов

---

## 3. Добавление нового action в Силли

**Правило:** прежде чем просить Силли выполнить РЕАЛЬНОЕ ДЕЙСТВИЕ —
убедиться что оно есть в `INTENT_PROMPT` как явный intent.

**Файл:** `agents/coder.py` → `INTENT_PROMPT` (~L539)

**Текущие intents:** push_code, fix_bot, create_bot, add_external_bot,
get_bot_token, deploy, read_file, list_files, redis_query, answer

**Если нужного action нет:**
1. Добавить intent в INTENT_PROMPT (одно слово, snake_case)
2. Добавить триггерные фразы ("что именно пишет пользователь → этот intent")
3. Добавить `elif intent == "новый_action":` handler в `handle_natural_language`
4. Только потом просить Силли

**Почему:** Haiku классифицирует intent по INTENT_PROMPT. Если action не описан —
всегда падает в `answer` и LLM генерирует текст вместо выполнения.

---

## 4. Проверить активность Силли в чатах

Перед массовыми деплоями — проверить что Силли не в середине активной задачи.
Симптом: сообщения "⏳ делаю...", "🔄 деплою..." в офис-группе (-5194783850).
Если есть — дождаться завершения или поставить на паузу.

---

## 5. После каждого исправления

- Записать Bug Lesson в группу -5197140411
- Обновить `SYSTEM_STATE.md` (закрыть баг, обновить версию)
- Bump версии shared lib если менялся `ai_office_shared/`

---

## Ключевые ID (Railway)

| Сервис | ID |
|---|---|
| Project awake-happiness | `271b40b7-199a-429a-88ef-ca417f26a638` |
| Environment production | `2efaaf60-ba39-492c-bf86-007fd505493f` |
| Силли (cilly-bot) | `efa6bd21-91d8-467f-8250-60f8a3853791` |
| Филли (filly-bot) | `5d61d403-feee-455e-9c0d-523f0e7c79d5` |
| Redis | `b62bdd8d-237a-4f2b-b4dc-9fed787c168d` |
| Watchdog | `e23833d2-8a05-4749-adce-c856ec026927` |

## Ключевые чаты (Telegram)

| Чат | ID |
|---|---|
| Офис-группа | `-5194783850` |
| Bug Lessons | `-5197140411` |

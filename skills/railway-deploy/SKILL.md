# SKILL: railway-deploy

## Когда использовать
Когда нужно задеплоить бота на Railway после пуша в GitHub.
Триггер: после любого `github-push` в бот-репо — жди деплоя и проверь статус.

## Переменные окружения
```
RAILWAY_TOKEN = из env (9cf51308-... в этом доке — УСТАРЕЛ, даёт "Not Authorized")
PROJECT_ID    = 271b40b7-199a-429a-88ef-ca417f26a638   (проект "awake-happiness")
ENV_ID        = 2efaaf60-ba39-492c-bf86-007fd505493f   (единственное окружение: production)
```

> ⚠️ **Проверено 2026-06-14:** прямой Railway-токен из этого дока отвечает `Not Authorized`.
> Рабочий путь — через **прокси Силли**: `POST /task {"message":"/railway <graphql>", "source":"CLAUDE"}`
> на `ai-office-shared-production.up.railway.app` (у Силли в env лежит рабочий аккаунт-токен,
> email `unperson22@gmail.com`, доступ ко всем проектам).

## Service ID по боту
**ПРОВЕРЕНО 2026-06-14** через `project(id:271b40b7){services}` — проект awake-happiness, 14 сервисов:
```
# personal/основные боты (ID подтверждены):
филли   = 5d61d403-feee-455e-9c0d-523f0e7c79d5   (filly-bot)
билли   = b441ce93-9736-49b3-9b5d-d0c82e715b28   (billy-bot)
крисс   = 92f70bbb-70ea-474c-be0d-5cc1c9bd8f4e   (kriss-bot)
тилли   = 367e25d7-8410-419d-896d-2cc86cd44efd   (tilly-bot)
милли   = db277aff-6638-4b4a-970e-b016bd753608   (milly-bot)
вилли   = a5e37cc4-0a9f-4700-b6d3-d39b958ce0cb   (villy-bot)
гослинг = ed03c9d3-e83f-4675-9f0a-a4d4fc622365   (gosling-bot)
# добавлены 2026-06-14 (были не в скилле):
logger-bot      = 3319eabd-5bcb-4e59-839e-4813f1e7ef33
pilly-bot       = 5533bc5f-24aa-4079-903b-50bcde4cdd01
prophet-bot     = 9db4108e-19f1-4c1f-a21c-3909442e137c
watchdog-bot    = e23833d2-8a05-4749-adce-c856ec026927
office-dashboard= 3dfc7336-2e91-4ade-950a-4f3d566baced
# Redis        = b62bdd8d-237a-4f2b-b4dc-9fed787c168d
```
**⚠️ В ДРУГОМ проекте (мигрировали из awake-happiness):**
```
# проект trading-dept = 3e58f2c8-a07c-482e-9886-8d356ba8e672
# env production       = 7ff2ff7a-b6d7-4c06-95c9-9958f0d3af7b
tilly-trader = 1c08bbcc-32bb-4e91-9bc9-d196c937c1c4   ✅ auto-deploy с main ВКЛ (репо подключён)
# старый id 9f868f0c-… из awake-happiness УДАЛЁН — не использовать (давал ложный NO_DEPLOY)
```
При редеплое tilly-trader использовать env `7ff2ff7a-…`, НЕ awake-happiness `2efaaf60-…`.

**⚠️ ПРОТУХЛИ (не в активном списке проекта 271b40b7):**
```
силли   = efa6bd21-...  → резолвится в УДАЛЁННЫЙ сервис "cilly-bot-<UUID>". Реальный неизвестен.
эллис   = fa7c87cf-...  → не в проекте awake-happiness.
доктор  = d949c4d2-...  → не в проекте awake-happiness.
```
> **TODO:** Силли/Девви/dev-dept (devvy/ricky/testi/sekky/scribbi) — в ДРУГОМ Railway-проекте.
> Через прокси перечислить проекты не удалось (project/workspace-запросы 400). Нужно достать
> ID офисного проекта (напр. из env Силли `RAILWAY_PROJECT_ID`), затем `project(id){services}`.

## Деплой конкретного коммита (когда auto-deploy ВЫКЛ)
**Проверено 2026-06-14 на tilly-trader.** `serviceInstanceRedeploy` НЕ годится — пересобирает
СТАРЫЙ (последний задеплоенный) коммит. Чтобы выкатить новый код:
```graphql
mutation{ serviceInstanceDeployV2(
  serviceId:"<SVC>", environmentId:"2efaaf60-...", commitSha:"<полный SHA из git rev-parse>"
)}   # возвращает deploymentId
```
Проверка: `deployments(first:1, input:{serviceId,environmentId}){edges{node{status meta}}}` →
`meta.commitHash` должен совпасть с целевым SHA, `status` → SUCCESS.



## Алгоритм

### 1. Дождаться нового деплоя (после пуша)
Railway триггерит деплой автоматически при push в подключённую ветку.
Ждать нужно 90–150 секунд (apt-get git + pip install занимает ~2 мин).

```python
import asyncio, httpx

RAILWAY_TOKEN = "..."
SERVICE_ID = "..."
ENV_ID = "..."

async def wait_for_deploy(service_id: str, timeout_sec: int = 180) -> dict:
    """Ждёт новый деплой и возвращает его статус."""
    url = "https://backboard.railway.app/graphql/v2"
    headers = {"Authorization": f"Bearer {RAILWAY_TOKEN}", "Content-Type": "application/json"}
    query = """query($svc: String!, $env: String!) {
      deployments(first: 1, input: {serviceId: $svc, environmentId: $env}) {
        edges { node { id status createdAt } }
      }
    }"""
    await asyncio.sleep(120)  # ждём начало деплоя
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(url, headers=headers,
                         json={"query": query, "variables": {"svc": service_id, "env": ENV_ID}})
        node = r.json()["data"]["deployments"]["edges"][0]["node"]
    return node
```

### 2. Проверить статус
```python
node = await wait_for_deploy(SERVICE_ID)
status = node["status"]   # "SUCCESS" | "FAILED" | "DEPLOYING" | "CRASHED"
deploy_id = node["id"]
```

### 3. Если FAILED — читать логи билда
```python
async def get_build_logs(deploy_id: str) -> list[str]:
    query = """query($id: String!) {
      buildLogs(deploymentId: $id, limit: 50) { message timestamp }
    }"""
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(url, headers=headers,
                         json={"query": query, "variables": {"id": deploy_id}})
    logs = r.json()["data"]["buildLogs"]
    return [l["message"] for l in logs]
```

Частые причины FAILED:
- `git: command not found` → Dockerfile не содержит `apt-get install git`
- `No module named X` → зависимость не в requirements.txt
- `SHA does not match` → устаревший SHA при пуше файла (см. skill github-push)

### 4. Если SUCCESS — проверить runtime логи
```python
async def get_runtime_logs(deploy_id: str, limit: int = 10) -> list[str]:
    query = """query($id: String!) {
      deploymentLogs(deploymentId: $id, limit: 20) { message timestamp }
    }"""
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(url, headers=headers,
                         json={"query": query, "variables": {"id": deploy_id}})
    logs = r.json()["data"]["deploymentLogs"]
    noise = {"getUpdates", "Heartbeat", "/health"}
    return [l["message"] for l in logs if not any(n in l["message"] for n in noise)][-limit:]
```

Бот живой если в логах есть: `Запущен` / `HTTP on :8080` / `Application started`

## Важные нюансы
- Railway API иногда возвращает 504 — ретраить с паузой 30 сек, не паниковать
- `deploymentLogs` возвращает пустой список если деплой ещё не запустил контейнер
- Auto-deploy работает только если в Railway включён "Auto deploy from branch"
- Все боты используют `Dockerfile` с `apt-get install git` — это обязательно для pip git+ deps

## Dockerfile шаблон (все боты)
```dockerfile
FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["python", "bot.py"]
```

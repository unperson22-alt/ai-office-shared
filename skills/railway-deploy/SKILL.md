# SKILL: railway-deploy

## Когда использовать
Когда нужно задеплоить бота на Railway после пуша в GitHub.
Триггер: после любого `github-push` в бот-репо — жди деплоя и проверь статус.

## Переменные окружения
```
RAILWAY_TOKEN = из env (9cf51308-07ba-4161-b955-4a00d650c8da)
PROJECT_ID    = 271b40b7-199a-429a-88ef-ca417f26a638
ENV_ID        = 2efaaf60-ba39-492c-bf86-007fd505493f
```

## Service ID по боту
```
филли   = 5d61d403-feee-455e-9c0d-523f0e7c79d5
билли   = b441ce93-9736-49b3-9b5d-d0c82e715b28
крисс   = 92f70bbb-70ea-474c-be0d-5cc1c9bd8f4e
эллис   = fa7c87cf-454c-4946-ab25-6a5091f0ac47
тилли   = 367e25d7-8410-419d-896d-2cc86cd44efd
милли   = db277aff-6638-4b4a-970e-b016bd753608
доктор  = d949c4d2-59fa-4cbe-8bb8-a0589a476607
вилли   = a5e37cc4-0a9f-4700-b6d3-d39b958ce0cb
гослинг = ed03c9d3-e83f-4675-9f0a-a4d4fc622365
силли   = efa6bd21-91d8-467f-8250-60f8a3853791
```

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

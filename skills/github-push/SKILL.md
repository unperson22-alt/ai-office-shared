# SKILL: github-push

## Когда использовать
Когда нужно создать или обновить файл в GitHub репозитории через API.
Используется для деплоя исправлений в бот-репо без локального git.

## Переменные
```
GH_PAT  = из env (хранится в Railway как GH_PAT)
ORG     = unperson22-alt
```

## Репозитории по боту
```
филли   → filly-bot
билли   → billy-bot
крисс   → kriss-bot
эллис   → mama-bot       (НЕ ellis-bot!)
тилли   → tilly-bot
милли   → milly-bot
доктор  → doctor-bot     (НЕ dilly-bot!)
вилли   → villy-bot
гослинг → gosling-bot
силли   → ai-office-shared  (файл: agents/coder.py)
```

## Алгоритм

### 1. Получить текущий SHA файла (обязательно перед PUT)
```python
import httpx, base64

GH_PAT = "..."
ORG = "unperson22-alt"

async def get_file_sha(repo: str, path: str) -> tuple[str, str]:
    """Возвращает (sha, current_content_decoded)."""
    url = f"https://api.github.com/repos/{ORG}/{repo}/contents/{path}"
    async with httpx.AsyncClient() as c:
        r = await c.get(url, headers={"Authorization": f"token {GH_PAT}"})
    d = r.json()
    sha = d["sha"]
    content = base64.b64decode(d["content"]).decode("utf-8")
    return sha, content
```

**КРИТИЧНО:** SHA устаревает мгновенно после любого коммита.
Всегда получай SHA непосредственно перед PUT — не кешируй между операциями.

### 2. Запушить файл
```python
async def push_file(repo: str, path: str, content: str, message: str, sha: str = None) -> str:
    """
    Создаёт или обновляет файл. Возвращает SHA нового коммита.
    sha=None → создаёт новый файл (PUT без sha).
    sha=<str> → обновляет существующий.
    """
    url = f"https://api.github.com/repos/{ORG}/{repo}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
    }
    if sha:
        payload["sha"] = sha

    async with httpx.AsyncClient() as c:
        r = await c.put(url,
                        headers={"Authorization": f"token {GH_PAT}",
                                 "Content-Type": "application/json"},
                        json=payload)
    d = r.json()
    if "commit" not in d:
        raise RuntimeError(f"push_file failed: {d.get('message', d)}")
    return d["commit"]["sha"]
```

### 3. Типичный паттерн (read → modify → push)
```python
sha, current = await get_file_sha("billy-bot", "bot.py")
new_content = current.replace("OLD_TEXT", "NEW_TEXT")
commit_sha = await push_file("billy-bot", "bot.py", new_content,
                              "fix(billy): описание исправления", sha=sha)
print(f"Pushed: {commit_sha[:10]}")
# Дальше → railway-deploy skill
```

## Частые ошибки

| Ошибка | Причина | Решение |
|--------|---------|---------|
| `422 SHA does not match` | SHA устарел (кто-то пушнул между get и put) | Заново получить SHA и повторить |
| `404 Not Found` | Неправильное имя репо или путь | Проверь таблицу репо выше |
| `409 Conflict` | Одновременный пуш | Ждать и повторить |
| Пустой ответ / timeout | Railway API 504 (не GitHub) | Это другая проблема, см. railway-deploy |

## Бинарные файлы
```python
with open("file.bin", "rb") as f:
    content_b64 = base64.b64encode(f.read()).decode()

payload = {"message": "...", "content": content_b64, "sha": sha}
# PUT напрямую без .encode()
```

## Commit message convention
```
feat(бот): краткое описание новой функции
fix(бот): что починили
chore(бот): служебные изменения (deps, docker)
```

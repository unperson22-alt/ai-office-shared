# SKILL: redis-migration

## Когда использовать
Когда заходишь в любого бота по любой причине.
**Правило офиса:** каждое касание бота = попутная миграция на @v0.1.2.

## Что мигрировать

### 1. Bump requirements.txt
Найти строку с `ai-office-shared` (или добавить если нет):
```
# было:
ai-office-shared @ git+https://github.com/unperson22-alt/ai-office-shared.git@v0.1.0

# стало:
ai-office-shared @ git+https://github.com/unperson22-alt/ai-office-shared.git@v0.1.2
```

### 2. Заменить локальные функции на импорты

В начале `bot.py` добавить:
```python
from ai_office_shared.shared.redis_helpers import (
    redis_get_history,
    redis_save_history,
    redis_get_notes,
    redis_add_note,
    redis_set_notes,
)
from ai_office_shared.shared.tasks import (
    auto_extract_interests,
    weekly_review,
    weekly_review_loop,
)
```

### 3. Удалить локальные копии этих функций
Из `bot.py` удалить (они теперь в shared):
- `async def redis_get_history(...)`
- `async def redis_save_history(...)`
- `async def redis_get_notes(...)`
- `async def redis_add_note(...)`
- `async def auto_extract_interests(...)`
- `async def weekly_review(...)`
- `async def weekly_review_loop(...)`

### 4. Обновить сигнатуры вызовов
Shared-версии принимают `redis_client` и `bot_name` первыми аргументами.

```python
# было (локальная версия):
history = await redis_get_history(user_id)
await redis_save_history(user_id, history)
notes = await redis_get_notes(user_id)
await redis_add_note(user_id, fact)
asyncio.create_task(auto_extract_interests(message, user_id))
asyncio.create_task(weekly_review_loop())

# стало (shared):
history = await redis_get_history(redis_client, BOT_NAME_LOWER, user_id)
await redis_save_history(redis_client, BOT_NAME_LOWER, user_id, history)
notes = await redis_get_notes(redis_client, BOT_NAME_LOWER, user_id)
await redis_add_note(redis_client, BOT_NAME_LOWER, user_id, fact)
asyncio.create_task(auto_extract_interests(redis_client, BOT_NAME_LOWER, user_id, message, client))
asyncio.create_task(weekly_review_loop(redis_client, BOT_NAME_LOWER, client))
```

### 5. Убедиться что BOT_NAME_LOWER есть
```python
BOT_NAME       = "Билли"   # для UI
BOT_NAME_LOWER = "билли"   # для Redis ключей — canonical
```

Если нет — добавить рядом с `BOT_NAME`.

## Чеклист перед деплоем
- [ ] requirements.txt → `@v0.1.2`
- [ ] Импорты добавлены
- [ ] Локальные копии удалены
- [ ] Все call-sites обновлены (grep: `redis_get_history(user_id`)
- [ ] `BOT_NAME_LOWER` объявлен
- [ ] `ast.parse(src)` без ошибок

## Проверка после деплоя
```python
# Быстрая проверка что логи пишутся
from ai_office_shared.shared.logging import read_logs
entries = await read_logs(redis_client, BOT_NAME_LOWER, days=1, limit=5)
print(entries)  # должны быть message_received / response_sent
```

## Примечание по redis_set_notes
`redis_set_notes` — новая функция (перезапись целиком), используется в `weekly_review`.
В старом коде было: `await redis_client.set(f"notes:{BOT_NAME}:{user_id}", new_profile)`.
Заменить на: `await redis_set_notes(redis_client, BOT_NAME_LOWER, user_id, new_profile)`.

# SKILL: ptb-reactions

## Когда использовать
Когда нужно добавить или отлаживать обработку реакций (👍/👎) в боте
на python-telegram-bot (PTB) версии 20.x / 21.x.

Все боты кроме Силли используют PTB. Силли — см. skill telethon-handlers.

## Как работают реакции в Telegram Bot API

Telegram присылает `message_reaction` update когда пользователь ставит/убирает реакцию.
Структура:
```json
{
  "message_reaction": {
    "chat": {"id": -100...},
    "message_id": 12345,
    "user": {"id": 67890},
    "old_reaction": [],
    "new_reaction": [{"type": "emoji", "emoji": "👍"}]
  }
}
```

**Важно:** бот получает реакции только на свои собственные сообщения.
Нужно хранить `message_id` своих сообщений чтобы знать к какому боту относится реакция.

## Настройка allowed_updates

В PTB нужно явно разрешить `message_reaction` в `allowed_updates`:
```python
await application.bot.delete_webhook(drop_pending_updates=True)
await application.updater.start_polling(
    allowed_updates=[
        "message",
        "message_reaction",
        "callback_query",
    ]
)
```

Или через `run_polling`:
```python
application.run_polling(allowed_updates=Update.ALL_TYPES)
```

## Handler в PTB

```python
from telegram import Update
from telegram.ext import MessageReactionHandler, ContextTypes

async def handle_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reaction = update.message_reaction
    if not reaction:
        return

    chat_id   = reaction.chat.id
    msg_id    = reaction.message_id
    user_id   = reaction.user.id if reaction.user else None
    new_emojis = [r.emoji for r in (reaction.new_reaction or [])
                  if hasattr(r, 'emoji')]
    old_emojis = [r.emoji for r in (reaction.old_reaction or [])
                  if hasattr(r, 'emoji')]

    # Фильтруем — только наши сообщения
    if not await is_my_message(chat_id, msg_id):
        return

    # Считаем качество
    for emoji in new_emojis:
        if emoji == "👍":
            await save_quality_vote(user_id, "up")
        elif emoji == "👎":
            await save_quality_vote(user_id, "down")

# Регистрация в application:
application.add_handler(MessageReactionHandler(handle_reaction))
```

## Хранение своих сообщений (remember_my_message)

```python
MY_MESSAGES_KEY = f"office:my_messages:{BOT_NAME_LOWER}"
MAX_STORED = 500

async def remember_my_message(chat_id: int, msg_id: int):
    """Запоминает message_id нашего ответа для последующей фильтрации реакций."""
    key = f"{MY_MESSAGES_KEY}:{chat_id}"
    await redis_client.lpush(key, str(msg_id))
    await redis_client.ltrim(key, 0, MAX_STORED - 1)
    await redis_client.expire(key, 86400 * 7)  # 7 дней

async def is_my_message(chat_id: int, msg_id: int) -> bool:
    key = f"{MY_MESSAGES_KEY}:{chat_id}"
    members = await redis_client.lrange(key, 0, -1)
    return str(msg_id).encode() in members or str(msg_id) in members
```

Вызывать `remember_my_message` после каждого `reply_text` / `send_message`:
```python
sent = await update.message.reply_text(response)
await remember_my_message(update.effective_chat.id, sent.message_id)
```

## Запись качества в Redis

```python
QUALITY_KEY = f"office:quality:{BOT_NAME_LOWER}"  # Hash

async def save_quality_vote(user_id: int, direction: str):
    """direction: 'up' или 'down'."""
    field = "quality_up" if direction == "up" else "quality_down"
    await redis_client.hincrby(QUALITY_KEY, field, 1)
    await log_event(redis_client, BOT_NAME_LOWER,
                    "reaction_received", user_id=user_id, direction=direction)
```

## Чтение метрик

```python
async def get_quality_stats() -> dict:
    raw = await redis_client.hgetall(QUALITY_KEY)
    return {
        k.decode() if isinstance(k, bytes) else k:
        int(v.decode() if isinstance(v, bytes) else v)
        for k, v in raw.items()
    }
# → {"quality_up": 12, "quality_down": 2}
```

## Частые проблемы

| Проблема | Причина | Решение |
|----------|---------|---------|
| Реакции не приходят | `message_reaction` не в allowed_updates | Добавить в start_polling |
| `AttributeError: 'NoneType' has no emoji` | PTB версия < 20.8 | Обновить PTB или проверять hasattr |
| Реакции от других ботов | Боты не реагируют эмодзи | Только юзеры ставят реакции |
| is_my_message всегда False | remember_my_message не вызывается | Добавить после каждого send |

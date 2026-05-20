# SKILL: telethon-handlers

## Когда использовать
При работе с Силли (`agents/coder.py`) — он использует aiogram 3.7 + Telethon,
а не python-telegram-bot. Всё остальное в офисе — PTB.

## Архитектура Силли

Силли использует два клиента одновременно:
- **aiogram Bot** — для отправки сообщений (стандартный Bot API)
- **Telethon UserClient** — для чтения группового чата как пользователь (User API)

Telethon нужен потому что Bot API не даёт читать реакции от других ботов
и не позволяет видеть все сообщения в группе без упоминания.

```python
from aiogram import Bot, Dispatcher
from telethon import TelegramClient, events
from telethon.sessions import StringSession

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Telethon userbot
tg_client = TelegramClient(
    StringSession(SESSION_STRING),
    api_id=API_ID,
    api_hash=API_HASH,
)
```

## Обработка событий в Telethon

### Новое сообщение в группе
```python
@tg_client.on(events.NewMessage(chats=OFFICE_CHAT_ID))
async def on_group_message(event):
    msg = event.message
    sender = await event.get_sender()
    
    # Игнорируем сообщения самого Силли
    me = await tg_client.get_me()
    if sender.id == me.id:
        return
    
    text = msg.text or msg.caption or ""
    sender_name = getattr(sender, 'first_name', '') or getattr(sender, 'username', 'unknown')
    
    # Дальнейшая обработка...
```

### Реакции через Telethon
```python
@tg_client.on(events.MessageEdited(chats=OFFICE_CHAT_ID))
async def on_reaction(event):
    # Telethon не даёт прямого события для реакций
    # Используем raw updates:
    pass

# Для реакций лучше использовать raw handler:
from telethon import events
from telethon.tl.types import UpdateMessageReactions

@tg_client.on(events.Raw(UpdateMessageReactions))
async def on_raw_reaction(event):
    chat_id = event.peer.channel_id  # или chat_id
    msg_id  = event.msg_id
    # event.reactions.results → список ReactionCount
    for r in event.reactions.results:
        emoji = getattr(r.reaction, 'emoticon', None)
        count = r.count
```

## Отправка сообщений

Силли отправляет через aiogram Bot (не Telethon) — так проще управлять форматированием:
```python
await bot.send_message(chat_id=OFFICE_CHAT_ID, text="...")
await bot.send_message(chat_id=OFFICE_CHAT_ID, text="...", reply_to_message_id=msg_id)
```

Через Telethon — только если нужна функциональность User API (пересылка, редактирование чужих):
```python
await tg_client.send_message(OFFICE_CHAT_ID, "...")
```

## Запуск двух клиентов

```python
async def main():
    await tg_client.start()
    
    # Запускаем aiogram polling в фоне
    asyncio.create_task(dp.start_polling(bot))
    
    # Telethon run_until_disconnected — основной loop
    await tg_client.run_until_disconnected()

asyncio.run(main())
```

## SESSION_STRING

Силли использует StringSession — хранится в Railway env как `SESSION_STRING`.
Генерируется один раз:
```python
from telethon.sessions import StringSession
from telethon import TelegramClient

async def gen_session():
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.start()
    print(client.session.save())  # → сохранить в env

asyncio.run(gen_session())
```

**Никогда не регенерировать без нужды** — Telegram может потребовать повторную авторизацию.

## Частые проблемы

| Проблема | Причина | Решение |
|----------|---------|---------|
| `FloodWaitError` | Слишком частые запросы | Добавить `asyncio.sleep` между вызовами |
| `SessionPasswordNeeded` | 2FA включён | Передать password в `client.start(password=...)` |
| Telethon и aiogram конфликтуют по event loop | Разные версии | Использовать один `asyncio.run()` для обоих |
| Сообщения дублируются | И aiogram и Telethon обрабатывают одно событие | Фильтровать по source в каждом handler |
| `PeerIdInvalidError` | chat_id без минуса или неправильный формат | Для каналов: `-100{channel_id}` |

## Отличие от PTB
- PTB: `application.add_handler(MessageHandler(...))` — event-driven
- aiogram: `@dp.message()` декораторы — то же самое
- Telethon: `@client.on(events.NewMessage())` — низкоуровневый доступ к MTProto

Силли использует Telethon именно для мониторинга группы без упоминания бота.

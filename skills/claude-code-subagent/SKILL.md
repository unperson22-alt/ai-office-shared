# SKILL: claude-code-subagent

## Когда использовать
Когда нужно изменить НЕСКОЛЬКО файлов согласованно — например:
- Обновить `ai-office-shared` до новой версии в 5 ботах
- Добавить новый импорт во все bot.py
- Синхронно изменить контракт между ботами

Одиночные правки → обычный `push_file`. Многофайловые → `/cc`.

## Команды

### Запуск рефактора
```
/cc <задача> @бот1 @бот2 ...
```

Примеры:
```
/cc обнови ai-office-shared до v0.1.2 в requirements.txt @билли @тилли @доктор
/cc добавь импорт from ai_office_shared.shared.logging import log_event @крисс @вилли
/cc замени BOT_NAME_LOWER = "xxx" на правильное значение @гослинг
```

### Мерж PR-ов
```
/approve_pr <id>    # один PR
/approve_pr all     # все pending PR-ы
```

## Как работает изнутри

```
Силли получает /cc задача @бот1 @бот2
    │
    ▼
Скачивает bot.py каждого бота через GitHub API
    │
    ▼
Один вызов Sonnet: задача + все файлы в контексте
    │
    ▼
Sonnet возвращает JSON {files: [{repo, path, content, reason}]}
    │
    ▼
Для каждого затронутого репо:
  create_branch("cc/{timestamp}")
  push_file_to_branch(...)
  create_pull_request(...)
    │
    ▼
Уведомление в чат со списком PR-ов
    │
    ▼
/approve_pr → merge_pull_request() → Railway автодеплоит
```

## Ограничения

- Работает только с `bot.py` по умолчанию (один файл на репо)
- Для нескольких файлов в одном репо — уточни задачу явно
- Sonnet max_tokens=8000 — не передавай слишком большие файлы (>500 строк) одновременно
- Если PR уже существует в ветке — push добавит коммит поверх

## Формат ответа Sonnet

Силли ожидает строго этот JSON (без markdown-обёртки):
```json
{
  "files": [
    {
      "repo": "billy-bot",
      "path": "bot.py",
      "content": "полный новый контент файла...",
      "reason": "добавлен импорт log_event, добавлен вызов в handle_message"
    }
  ],
  "summary": "добавлен log_event в 3 бота"
}
```

Если изменений нет — `files: []`.

## Паттерн: обновление shared lib версии

Самый частый кейс — bump `ai-office-shared` в requirements.txt:

```
/cc замени @v0.1.0 на @v0.1.2 в строке ai-office-shared в requirements.txt @билли @тилли @милли @крисс @доктор
```

Силли скачает bot.py — но requirements.txt там не будет. Нужно явно:
```
/cc замени версию ai-office-shared на v0.1.2 в requirements.txt @билли @тилли
```

В этом случае задача должна указывать путь. TODO: добавить параметр `--file requirements.txt`.

## Связанные скиллы
- `github-push` — одиночный файл
- `railway-deploy` — проверка статуса после мержа
- `redis-migration` — что именно менять при обновлении shared lib

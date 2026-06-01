# SKILL: patch-bot-routing

## Назначение
Добавить OFFICE-routing в бот по эталону kriss-bot.

## Когда использовать
Когда нужно добавить умный routing через агентов офиса в бота у которого его нет.

## Эталон
Паттерн из kriss-bot. LLM сам решает нужен ли специалист — добавляет тег [OFFICE:ИМЯ:запрос] в конец ответа. process() парсит тег, вызывает агента по HTTP /task, синтезирует финальный ответ пользователю.

## Шаги

### 1. Прочитай файл через GitHub API
```
GET https://api.github.com/repos/unperson22-alt/{BOT_REPO}/contents/bot.py
Authorization: token {GITHUB_TOKEN}
Decode content: base64 -> text
Сохрани sha для шага 4
```

### 2. Найди SYSTEM_PROMPT / SYSTEM_BASE
Найди переменную SYSTEM_PROMPT = """...""" или SYSTEM_BASE = """..."""
Добавь ПЕРЕД закрывающими тройными кавычками:

```
\n\n== ОФИСНЫЕ АГЕНТЫ ==\nУ тебя есть доступ к специалистам AI-офиса. Когда нужны их возможности — добавь в конец ответа тег [OFFICE:ИМЯ:запрос].\nТИЛЛИ — поиск/новости, МИЛЛИ — бизнес, СИЛЛИ — код/автоматизация, ДОКТОР — здоровье, БИЛЛИ — мотивация/жизнь, КРИСС — личный ассистент.\nИспользуй только когда реально нужно. Один тег за раз.
```

### 3. Найди async def process() — последний return text
ПЕРЕД последним `return text` внутри process() вставь:

```python
    # ── OFFICE routing ────────────────────────────────────────
    import re as _re
    def _parse_office_tag(t):
        m = _re.search(r'\[OFFICE:(\w+):(.+?)\]', t)
        return (m.group(1).upper(), m.group(2).strip()) if m else (None, None)
    async def _call_office(name, query, uid):
        _urls = {
            'ТИЛЛИ':  'https://tilly-bot-production.up.railway.app',
            'МИЛЛИ':  'https://milly-bot-production.up.railway.app',
            'СИЛЛИ':  'https://ai-office-shared-production.up.railway.app',
            'ДОКТОР': 'https://dilly-bot-production.up.railway.app',
            'БИЛЛИ':  'https://billy-bot-production.up.railway.app',
            'КРИСС':  'https://kriss-bot-production.up.railway.app',
        }
        _url = _urls.get(name)
        if not _url: return ''
        try:
            async with httpx.AsyncClient(timeout=25) as _c:
                _r = await _c.post(f'{_url}/task', json={'message': query, 'user_id': uid})
            return _r.json().get('response', '') if _r.status_code == 200 else ''
        except: return ''
    _agent, _query = _parse_office_tag(text)
    text = _re.sub(r'\[OFFICE:[^\]]+\]', '', text).strip()
    if _agent and _query:
        _result = await _call_office(_agent, _query, user_id)
        if _result:
            text = text + f'\n\n📡 {_agent}: {_result[:500]}'
    # ─────────────────────────────────────────────────────────
```

### 4. Проверь синтаксис и запушь
```python
import ast
ast.parse(new_code)  # должен пройти без ошибок
```
PUT в GitHub с { "message": "feat: add OFFICE routing", "content": base64(new_code), "sha": {sha} }

### 5. Подтверди деплой
Через 90 сек: GET https://{бот}-production.up.railway.app/health

## Очерёдность
1. gosling-bot
2. milly-bot
3. tilly-bot
4. villy-bot
5. doctor-bot
6. billy-bot (убрать безусловный forward_to_filly, заменить на OFFICE-тег паттерн)

## Константы
GITHUB_TOKEN = {GITHUB_TOKEN}
ORG = unperson22-alt

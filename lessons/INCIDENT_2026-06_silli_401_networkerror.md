# Инцидент 2026-06 — Силли в петле «NetworkError → 401 → blocked»

## Симптом (что видели в чате)
Силли по кругу находит в ботах `telegram.error.NetworkError: Bad Gateway`
(tilly-trader, office-dashboard, dilly-bot, gosling-bot …), опознаёт как «урок
#8/#43», генерит фикс и пытается применить — и каждый раз падает:
`Client error '401 Unauthorized' for url '.../contents/bot.py'`. Задача уходит в
`blocked` и эскалируется шефу. Петля повторяется на каждом боте.

## Корневые причины (две разные ошибки!)

### Корень A — почему вообще возникает NetworkError
Транзиентный 502/timeout от Telegram/Railway — это норма на нестабильной сети.
Боты падали, потому что отправка в Telegram шла «голым» вызовом
(`reply_text`/`send_message`) без ретраев. Базовый `shared/new_bot_template.py`
тиражировал эту хрупкость во все ~30 ботов. Корень: **в shared-слое не было
единого безопасного хелпера отправки с ретраями**.

### Корень B — почему Силли НЕ МОЖЕТ починить это сама
`shared/github_tools.py` слал `Authorization: token <GH_PAT>` и для чтения, и
для записи. `GH_PAT` выдан **только на чтение** (так и было в доке: «PAT для
чтения кода»). Чтение работало → Силли анализировала баг; запись (`PUT
contents/`) отвергалась с 401 → фикс не применялся. В `coder.py` 401 ловился как
обычная ошибка задачи (`blocked` + эскалация по каждой задаче), без распознавания,
что это **системный отказ креденшела**, ломающий ВСЕ записи. Нет префлайта токена
на старте и нет различения «auth-сбой» vs «сеть» → бесконечная петля. Корень:
**у Силли единственный GitHub-креденшел был read-only; не было write-токена,
префлайта и классификации auth-ошибок**.

## Что исправлено в shared-слое (ветка claude/silli-errors-root-cause-pr36t2)

1. **`ai_office_shared/shared/telegram_safe.py`** (новый) — `safe_send`/
   `safe_reply`/`safe_edit`/`with_tg_retry`: ретраи с экспоненциальным backoff на
   `NetworkError`/`TimedOut`, уважение `RetryAfter`, graceful degradation вместо
   краша. Подключён в `new_bot_template.py`. → лечит Корень A.
2. **`shared/github_tools.py`** — поддержка **GitHub App** (installation-token
   через JWT) как write-креденшела с fallback на личный токен; `verify_write_access()`
   (префлайт прав на запись); `deploy_via_pr()` (ветка→PR→squash вместо прямого
   push в main); 401/403 → явный `PermissionError` «нужен write-креденшел». →
   лечит Корень B (как только заданы GITHUB_APP_*).
3. **`requirements.txt`** (shared) и **`dev-dept/cilly/requirements.txt`** —
   `PyJWT[crypto]` для минта JWT.
4. **`dev-dept/README.md`** — исправлено описание `GH_PAT` (read-only) + добавлены
   переменные GitHub App.

## Действие шефа (вне кода — это нельзя сделать из репозитория)
1. Создать **GitHub App** в org `unperson22-alt` с правами **Contents: write** +
   **Pull requests: write**, установить на нужные репо.
2. Задать на Railway-сервисе Силли (`ai-office-shared`): `GITHUB_APP_ID`,
   `GITHUB_APP_PRIVATE_KEY` (PEM), `GITHUB_APP_INSTALLATION_ID`.
3. После мёржа shared-изменений выпустить новый тег `ai-office-shared` и бумпнуть
   пин в `dev-dept/cilly/requirements.txt` (и в requirements ботов при раскатке).

После этого `push_file()`/`deploy_via_pr()` Силли аутентифицируются как App и
401 уходит.

## Оставшаяся доводка `coder.py` (делегируется Силли/Девви — точный спек)
Файл большой (центральный оркестратор), правим аккуратно через пайплайн dev-dept:

1. **Префлайт на старте.** В `main()` после инициализации:
   ```python
   from github_tools import verify_write_access
   ok, detail = await verify_write_access("ai-office-shared")
   if not ok:
       DEPLOY_PAUSED = True
       await notify_office(f"🔑 Силли не может писать в GitHub ({detail}). "
                           f"Авто-деплой фиксов на ПАУЗЕ — нужен GitHub App token.")
   ```
   Завести глобальный флаг `DEPLOY_PAUSED` (по умолч. False).

2. **Не уходить в петлю при auth-сбое.** В обработке `apply_pending` для
   `deploy_fix`: если `DEPLOY_PAUSED` — не генерить/не применять фикс, а копить
   находки; ловить `PermissionError` отдельно от прочих:
   ```python
   except PermissionError as e:
       DEPLOY_PAUSED = True            # это НЕ ошибка задачи — креденшел мёртв
       await notify_office_once(f"🔑 GitHub auth: {e}")  # один алерт, не на каждую задачу
       return f"⏸ Деплой на паузе (нужен write-токен): {e}"
   ```
   Сетевые/транзиентные ошибки (502/timeout) — ретраить с backoff, как и раньше.

3. **Безопасный деплой.** Заменить прямой `push_file(repo, affected, fixed_code, …)`
   на `deploy_via_pr(repo, affected, fixed_code, commit_msg, branch=f"cilly/fix-{service}-{lesson}")`.

4. **Снятие паузы.** При восстановлении доступа (успешный `verify_write_access`)
   сбросить `DEPLOY_PAUSED` и сообщить в офис.

## Раскатка фикса NetworkError по ботам (делегируется Силли)
Когда у Силли появится write-доступ: для каждого бота открыть PR, который
(а) бумпает пин `ai_office_shared` на новый тег, (б) переключает отправки в
Telegram на `safe_send`/`safe_reply`/`safe_edit`, (в) мёржит. Это раскатка
руками Силли через пайплайн (Девви→Рикки‖Тести‖Секки→Скрибби), а не точечные
ручные правки.

## Критерии «починено на корню»
- Транзиентный 502 от Telegram НЕ роняет бота (ретраи + degrade).
- `verify_write_access()` зелёный; `push_file`/`deploy_via_pr` не дают 401.
- Auth-сбой эскалируется ОДИН раз как «креденшел мёртв», без петли blocked-задач.
- Деплой идёт через PR, а не прямой push в main.

## Урок (для матчинга в lessons.json — добавить запись)
«Read-only GitHub токен делает самодеплой невозможным (401). Auth-сбой (401/403)
≠ ошибка задачи: эскалировать как ‘креденшел мёртв’ и ставить деплой на паузу, а
НЕ ретраить/блокировать по каждой задаче. На старте делать префлайт write-доступа.
Сетевые ошибки Telegram (Bad Gateway) лечатся ретраями в shared-хелпере, а не
точечно в каждом боте.»

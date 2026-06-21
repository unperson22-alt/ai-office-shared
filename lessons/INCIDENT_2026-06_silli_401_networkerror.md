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

### Корень B — почему Силли НЕ МОЖЕТ починить это сама (форензика истории)
**Это НЕ «токен read-only by design». Силли умела писать в репо — пока её
GitHub-токен был валиден.** Что показала git-история:

- **`401`, а не `403`.** 401 = креденшел **невалиден** (протух/отозван/битый), а
  не «не хватает прав» (это был бы 403).
- **5 июня** (`3a27da7`) — github_tools пропатчили на
  `GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") or os.getenv("GH_PAT")`: фолбэк
  прикрутили, потому что `GITHUB_TOKEN` уже **перестал работать**.
- **15 июня** (`37785fb`) — коммит дословно: *«Силли зафлудила офис-группу,
  выпрашивая токен»*. Проблема с GitHub-токеном тянется недели.
- Реальные правки репо в истории — это **Claude Code-сессии**
  (`claude.ai/code/session_…`) и merge PR, **не** автономный `push_file` Силли;
  ряд коммитов: *«команда dev-dept не вытянула… доделано вручную»*.
- **Разные ключи (источник путаницы!):** `RAILWAY_TOKEN_VLAD` («токен Влад») — это
  **Railway** API-токен (редеплой, `check_var`), его и обновляли «с полными
  правами». GitHub-**запись** идёт через ДРУГОЙ креденшел — `GITHUB_TOKEN`/
  `GH_PAT` — и именно он невалиден. Силли может **редеплоить** (Railway ✓), но не
  **коммитить** (GitHub ✗).
- **Асимметрия read/write:** чтение в `coder.py` идёт напрямую через
  `os.getenv("GH_PAT")` (рабочий) → анализ бага работает; запись через
  `github_tools` (`GITHUB_TOKEN or GH_PAT`) → `401`. Если `GITHUB_TOKEN` задан, но
  битый (truthy) — `or` коротко замыкается на него и не доходит до `GH_PAT`.

**Корень B (итог):** GitHub-write-креденшел Силли стал невалиден (≈начало июня);
обновлённый «полноправный» токен — это Railway-токен, не GitHub. Плюс нет
префлайта токена на старте и нет классификации auth-ошибок → бесконечная петля
«нашёл → 401 → заблокировал».

## Что исправлено в shared-слое (ветка claude/silli-errors-root-cause-pr36t2)

1. **`ai_office_shared/shared/telegram_safe.py`** (новый) — `safe_send`/
   `safe_reply`/`safe_edit`/`with_tg_retry`: ретраи с экспоненциальным backoff на
   `NetworkError`/`TimedOut`, уважение `RetryAfter`, graceful degradation вместо
   краша. Подключён в `new_bot_template.py`. → лечит Корень A.
2. **`shared/github_tools.py`** — **GitHub App** (installation-token через JWT) как
   постоянный write-креденшел (не протухает), с fallback на личный токен;
   `verify_write_access()` различает `401` (ротация) и `push=false` (read-only);
   `deploy_via_pr()` (ветка→PR→squash вместо прямого push в main); 401/403 → явный
   `PermissionError`. → лечит Корень B и предотвращает рецидив «протухшего токена».
3. **`requirements.txt`** (shared) и **`dev-dept/cilly/requirements.txt`** —
   `PyJWT[crypto]` для минта JWT.
4. **`dev-dept/README.md`** — разнесены три креденшела: `RAILWAY_TOKEN_VLAD`
   (Railway), `GITHUB_TOKEN`/`GH_PAT` (GitHub, текущий — невалиден), GitHub App
   (постоянный write).

## Действие шефа (вне кода)
1. **Durable:** создать **GitHub App** в org `unperson22-alt` (Contents: write +
   Pull requests: write), установить на репо, задать на Railway-сервисе Силли
   (`ai-office-shared`): `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` (PEM),
   `GITHUB_APP_INSTALLATION_ID`.
2. **Немедленный обход (если нужно прямо сейчас, до App):** положить ВАЛИДНЫЙ PAT
   с Contents: write в `GITHUB_TOKEN`, **или** убрать битый `GITHUB_TOKEN`, чтобы
   `or`-фолбэк взял валидный `GH_PAT`. (`RAILWAY_TOKEN_VLAD` тут не поможет — это
   Railway-токен.)
3. После мёржа shared-изменений выпустить новый тег `ai-office-shared` и бумпнуть
   пин в `dev-dept/cilly/requirements.txt` (и в requirements ботов при раскатке).

## Оставшаяся доводка `coder.py` (делегируется Силли/Девви — точный спек)
Файл большой (центральный оркестратор), правим аккуратно через пайплайн dev-dept:

1. **Префлайт на старте.** В `main()` после инициализации:
   ```python
   from github_tools import verify_write_access
   ok, detail = await verify_write_access("ai-office-shared")
   if not ok:
       DEPLOY_PAUSED = True
       await notify_office(f"🔑 Силли не может писать в GitHub ({detail}). "
                           f"Авто-деплой фиксов на ПАУЗЕ — нужен валидный write-токен / GitHub App.")
   ```
   Завести глобальный флаг `DEPLOY_PAUSED` (по умолч. False).

2. **Не уходить в петлю при auth-сбое.** В обработке `apply_pending` для
   `deploy_fix`: если `DEPLOY_PAUSED` — копить находки, не долбить деплой; ловить
   `PermissionError` отдельно:
   ```python
   except PermissionError as e:
       DEPLOY_PAUSED = True            # это НЕ ошибка задачи — креденшел невалиден
       await notify_office_once(f"🔑 GitHub auth: {e}")  # один алерт, не на каждую задачу
       return f"⏸ Деплой на паузе (нужен валидный write-токен): {e}"
   ```
   Сетевые/транзиентные ошибки (502/timeout) — ретраить с backoff, как и раньше.

3. **Безопасный деплой.** Заменить прямой `push_file(repo, affected, fixed_code, …)`
   на `deploy_via_pr(repo, affected, fixed_code, commit_msg, branch=f"cilly/fix-{service}-{lesson}")`.

4. **Снятие паузы.** При восстановлении доступа (успешный `verify_write_access`)
   сбросить `DEPLOY_PAUSED` и сообщить в офис.

## Раскатка фикса NetworkError по ботам (делегируется Силли)
Когда у Силли появится валидный write-доступ: для каждого бота открыть PR, который
(а) бумпает пин `ai_office_shared` на новый тег, (б) переключает отправки в
Telegram на `safe_send`/`safe_reply`/`safe_edit`, (в) мёржит. Раскатка руками
Силли через пайплайн (Девви→Рикки‖Тести‖Секки→Скрибби), а не точечные правки.

## Критерии «починено на корню»
- `verify_write_access()` зелёный; `push_file`/`deploy_via_pr` не дают 401.
- Транзиентный 502 от Telegram НЕ роняет бота (ретраи + degrade).
- Auth-сбой эскалируется ОДИН раз как «креденшел невалиден», без петли blocked.
- Деплой идёт через PR, а не прямой push в main.

## Урок (для матчинга в lessons.json — добавить запись)
«GitHub-write-креденшел может ПРОТУХНУТЬ (→ 401, не 403) — и тогда самодеплой
встаёт, хотя раньше работал. Railway-токен (RAILWAY_TOKEN_VLAD) ≠ GitHub-токен:
полные права в Railway не дают права писать в репо. Auth-сбой (401/403) ≠ ошибка
задачи: эскалировать как ‘креденшел невалиден’ и ставить деплой на паузу, а НЕ
ретраить/блокировать по каждой задаче. На старте делать префлайт write-доступа
(verify_write_access). Durable-решение — GitHub App (installation-token не
протухает). Сетевые ошибки Telegram (Bad Gateway) лечатся ретраями в shared-
хелпере (telegram_safe), а не точечно в каждом боте.»

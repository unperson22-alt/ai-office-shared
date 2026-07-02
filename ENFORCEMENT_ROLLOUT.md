# OFFICE RPC AUTH — ENFORCEMENT ROLLOUT (руками Влада)

> Контекст: auth-меш (shared v0.1.20, `shared/auth.py`) выкачен на все живые сервисы
> в **warn-режиме**. Этот документ — пошаговый флип в **strict** (401) без даунтайма.
> Значение токена в репо НЕ хранится — только env (красная линия).

## Шаг 0 — Контейнмент Силли (СРОЧНО, независимо от остального)
Был code-exec через открытый `/task` (staged-payload'ы `silli_*.json`, сбор PAT/токенов).
1. Закрыть публичный вход Силли: Railway → сервис `ai-office-shared` → private networking
   для `/task` (или ограничить публичный домен), внешние вызовы — только с токеном.
2. Ротировать секреты Силли: `GH_PAT`/`GITHUB_TOKEN`, `RAILWAY_TOKEN` и `RAILWAY_TOKEN_VLAD`,
   `ANTHROPIC_API_KEY`, `REDIS_*`, токены ботов, до кучи `OFFICE_SECRET`.

## Шаг 1 — Выставить OFFICE_RPC_TOKEN на ВСЕ сервисы
- Токен один на весь меш (длинный случайный hex, сгенерён в сессии — см. чат, в репо его нет).
- **Только через Railway UI / Railway CLI под своим аккаунтом.**
  ⚠️ НЕ передавать значение через чат Силли / Telegram / `dev_task` — секрет пройдёт через
  LLM-контекст и логи; ровно так выглядела атака silli_*.json.
- Список сервисов = все живые из security-волны: cilly(ai-office-shared), kriss, billy, doctor,
  gosling, mama(ellice), milly, tilly, villy, devvy, ricky, scribbi, sekky, testi, prophet,
  filly, ray, marty, nelli, lex, vietnam, pilly, railway-deployer, pilly-bot-bot, trading-dept.

## Шаг 2 — Убедиться, что новый код задеплоен
- Все PR волн (security + enforcement-prep) смержены, Railway пересобрал сервисы.
- Быстрый чек: `/health` каждого бота = 200 (мониторит watchdog).

## Шаг 3 — Мониторинг WARN
- Смотреть логи всех сервисов на `[office-auth] WARN ... missing/invalid token`.
- Каждая WARN-строка = вызыватель без токена. Известные и уже закрытые кодом (в этой волне):
  cron `/send_scheduled`, mama inline-вызов, billy→gosling, tilly→marty, nelli исходящие.
- Отдельно: вызыватели `/secrets` и `/redis` Силли (Claude-тулинг из сессий) обязаны слать
  `X-Office-Token: <токен>` — после STRICT без него будет 401.

## Шаг 4 — Пропатчить СТАРЫЕ cron-сервисы
Новые cron'ы (после мержа фикса `agents/coder.py`) получают env var `T` и header автоматически.
Старые cron-сервисы созданы без токена — при STRICT их POST упадёт. Для каждого старого крона:
1. Railway UI → cron-сервис → Variables → добавить `T = <OFFICE_RPC_TOKEN>`.
2. Settings → Deploy → Custom Start Command → заменить на:
   `curl -sf -X POST <bot_url>/send_scheduled -H Content-Type:application/json -H X-Office-Token:$T -d $P`
   (то же, что было, плюс `-H X-Office-Token:$T`).
Либо проще: удалить старые cron-сервисы и пересоздать через Силли после деплоя фикса.

## Шаг 5 — Флип
- Когда WARN-логи чистые ≥ 1 суток: выставить `OFFICE_RPC_STRICT=1` на ВСЕ сервисы.
- После флипа: смоук — межбот-вызов (например, kriss → [OFFICE:ТИЛЛИ:...]), расписание
  (`send_scheduled` по крону), `/health` везде 200.
- Откат: убрать `OFFICE_RPC_STRICT` (вернётся warn-режим), даунтайма нет.

## Ротация токена (на будущее)
1. Выставить новое значение `OFFICE_RPC_TOKEN` на всех сервисах (+ var `T` у cron-сервисов).
2. Redeploy. Порядок неважен: приёмник сравнивает с собственным env, отправитель читает свой.
   На время рассинхрона в strict возможны 401 между уже/ещё не обновлёнными сервисами —
   делать батчем, быстро.

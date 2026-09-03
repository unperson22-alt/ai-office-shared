"""
ai_office_shared.shared.auth
Аутентификация внутреннего RPC-меша офиса (бот↔бот, Claude↔Силли).

Модель: общий секрет ``OFFICE_RPC_TOKEN`` в env ВСЕХ сервисов. Исходящие office-
вызовы добавляют заголовок ``X-Office-Token``; входящие проверяют его через
``office_auth_middleware``.

Двухфазный выкат БЕЗ даунтайма:
  Фаза A (по умолчанию): код шлёт и проверяет токен, но НЕ отклоняет запросы
    с отсутствующим/неверным токеном — только пишет WARN в лог. Порядок:
    1) выставить OFFICE_RPC_TOKEN на ВСЕ сервисы, 2) задеплоить новый код везде.
    Трафик пойдёт с заголовком; по WARN-логам видно, кто ещё не шлёт токен.
  Фаза B (включение enforcement): выставить OFFICE_RPC_STRICT=1 на всех сервисах —
    теперь запросы без валидного токена получают 401.

``/health`` и ``/version`` всегда открыты: первый нужен Railway healthcheck,
второй — чтобы версию запущенного кода можно было спросить без секрета офиса.
"""
import logging
import os

from aiohttp import web

logger = logging.getLogger("ai_office_shared.auth")

OFFICE_RPC_TOKEN = os.getenv("OFFICE_RPC_TOKEN", "")
OFFICE_RPC_STRICT = os.getenv("OFFICE_RPC_STRICT", "").lower() in ("1", "true", "yes")
OFFICE_AUTH_HEADER = "X-Office-Token"

# Пути без auth (healthcheck/инфраструктура).
#
# `/version` открыт по той же причине, что и `/health`, и ещё по одной. 02.09.2026
# выяснилось, что «смёржено» и «работает» неразличимы: единственным
# свидетельством деплоя были слова его исполнителя (инвариант 5). `/version`
# существует затем, чтобы это можно было проверить НЕЗАВИСИМО — а проверка,
# которой нужен секрет офиса, независимой не является: её не сделает ни сессия
# без токена, ни внешний watchdog. Сегодня она проходит и так, но только потому,
# что OFFICE_RPC_TOKEN ещё не выставлен (Фаза A); после включения enforcement
# ответом стал бы 401, и эндпоинт молча перестал бы отвечать на вопрос, ради
# которого написан.
#
# Отдаются два SHA и имя бота — по ним нельзя ни прочитать код, ни попасть в
# репозиторий; `/logs`, который уже открыт, раскрывает несопоставимо больше.
_OPEN_PATHS = {"/health", "/version"}


def office_headers(extra: dict | None = None) -> dict:
    """Заголовки для ИСХОДЯЩЕГО office-вызова: добавляет X-Office-Token если задан."""
    h = dict(extra or {})
    if OFFICE_RPC_TOKEN:
        h[OFFICE_AUTH_HEADER] = OFFICE_RPC_TOKEN
    return h


def check_office_token(request) -> bool:
    """
    True если запрос несёт валидный office-токен.
    Если OFFICE_RPC_TOKEN не сконфигурирован — возвращает True (Фаза A: не блокируем
    до того как секрет выставлен на всех сервисах).
    """
    if not OFFICE_RPC_TOKEN:
        return True
    return request.headers.get(OFFICE_AUTH_HEADER, "") == OFFICE_RPC_TOKEN


@web.middleware
async def office_auth_middleware(request, handler):
    """
    aiohttp middleware: защищает все маршруты кроме /health.

    Использование в main():
        from ai_office_shared.shared.auth import office_auth_middleware
        app = web.Application(middlewares=[office_auth_middleware])
    """
    path = request.path.rstrip("/") or "/"
    if path in _OPEN_PATHS or request.method == "OPTIONS":
        return await handler(request)

    if check_office_token(request):
        return await handler(request)

    # OFFICE_RPC_TOKEN задан, но запрос его не несёт / он неверный.
    if OFFICE_RPC_STRICT:
        logger.warning("[office-auth] 401 %s %s — missing/invalid X-Office-Token",
                       request.method, path)
        return web.json_response({"error": "unauthorized"}, status=401)

    # Фаза A: не блокируем, но логируем пробел, чтобы видеть кто ещё не шлёт токен.
    logger.warning("[office-auth] WARN %s %s — missing/invalid token (allowed: non-strict)",
                   request.method, path)
    return await handler(request)


# ── Вайтлист пользователей: отказ должен быть ВИДЕН ───────────────────────────
# ПРОБЛЕМА, которую решаем (инцидент 2026-08-12):
#     У Яны сменился аккаунт. Она нажала /start у Крисса — и не получила НИЧЕГО:
#     ни ответа, ни строки в логах, ни события в офис-группе. Причина: каждый
#     гейт вайтлиста выглядел так
#
#         if update.effective_user.id not in ALLOWED_USERS:
#             return
#
#     — голый return без единой записи. Снаружи это неотличимо от «бот умер»:
#     полчаса ушло на проверку деплоя и здоровья сервиса, хотя бот работал
#     штатно и просто не знал этого человека. Тот же класс, что и урок #73
#     (подмена сообщения была невидима в логах) — отказ без следа не отлаживается.
#
# РЕШЕНИЕ:
#     Одна функция на все боты. Отказ всегда пишется в лог и в office:logs.
#     Пользователю по умолчанию по-прежнему не отвечаем (не светим бота
#     посторонним), но у /start есть исключение — см. deny_message().

async def note_denied_access(
    redis_client,
    bot_name: str,
    user_id: int,
    *,
    username: str = "",
    first_name: str = "",
    kind: str = "message",
) -> None:
    """
    Записать отказ по вайтлисту. Fail-silent: логирование не должно ронять хендлер.

    Зовётся ВМЕСТО голого `return` в гейтах ALLOWED_USERS.

    Args:
        kind: что именно отклонили — "start" / "message" / "photo" / "callback".
    """
    who = f"@{username}" if username else (first_name or "без имени")
    logger.warning(
        "[access] %s: отказано %s (id=%s, %s) — нет в ALLOWED_USERS",
        bot_name, who, user_id, kind,
    )
    try:
        from .logging import log_event
        await log_event(
            redis_client, bot_name, "access_denied", level="warn",
            user_id=user_id, username=username or first_name or "", kind=kind,
        )
    except Exception:
        pass  # лог отказа сам по себе не критичен


DENY_MESSAGE = (
    "Привет! Я тебя пока не знаю — этот аккаунт не в списке доступа. "
    "Напиши Владу, он добавит, и я сразу отвечу."
)


def deny_message() -> str:
    """
    Что ответить незнакомцу на /start.

    Отвечаем ТОЛЬКО на /start, а не на каждое сообщение: ссылка на бота и так
    публична, так что факт его существования мы не выдаём, зато человек сразу
    понимает, что делать, вместо того чтобы гадать, сломан бот или нет. На
    обычные сообщения по-прежнему молчим, чтобы не давать посторонним канал.
    """
    return DENY_MESSAGE

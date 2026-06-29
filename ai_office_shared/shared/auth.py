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

``/health`` всегда открыт (нужен Railway healthcheck).
"""
import logging
import os

from aiohttp import web

logger = logging.getLogger("ai_office_shared.auth")

OFFICE_RPC_TOKEN = os.getenv("OFFICE_RPC_TOKEN", "")
OFFICE_RPC_STRICT = os.getenv("OFFICE_RPC_STRICT", "").lower() in ("1", "true", "yes")
OFFICE_AUTH_HEADER = "X-Office-Token"

# Пути без auth (healthcheck/инфраструктура).
_OPEN_PATHS = {"/health"}


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

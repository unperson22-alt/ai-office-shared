"""Секреты не попадают в логи — ни свои, ни прочитанные обратно.

Зачем: 23.08.2026 в логах villy-bot нашлись двенадцать подряд строк вида
`HTTP Request: POST https://api.telegram.org/bot<ТОКЕН>/getUpdates "200 OK"`.
Их пишет сам httpx на уровне INFO, а `logging.basicConfig(level=INFO)` стоит
в каждом боте офиса. То есть токен бота печатался в логи Railway при КАЖДОМ
опросе — раз в несколько секунд, годами, в сервисе, чьи логи читает кто угодно
с доступом к дашборду.

Утечка не заканчивается на Railway. Силли тянет `deploymentLogs` обратно и
кладёт хвост в промпт анализатора, в отчёт Владу и в контекст `/office`. Один
логгер третьей стороны — и секрет уезжает и в облако, и к модели, и в чат.

Здесь ОДИН механизм на обе стороны:

  * `install_secret_redaction()` — ставит фабрику LogRecord, которая чистит
    сообщение, аргументы и трейсбек ДО того, как их увидит любой обработчик.
    Фабрика, а не фильтр на обработчике: обработчик, добавленный позже (uvicorn,
    aiohttp, aiogram), фильтра бы не унаследовал, а фабрика одна на процесс.
  * `redact(text)` — то же самое для текста, прочитанного ИЗВНЕ: хвост логов
    Railway, тело чужого ответа, всё, что уходит в модель или в Telegram.

Сопоставление здесь ПОДСТРОЧНОЕ, вопреки правилу офиса «по слову, а не по
подстроке». Правило про поиск смысла, а тут задача обратная: секрет надо
накрыть, даже если он приклеен к соседнему символу. Ошибка в сторону лишней
замазки стоит нечитаемой строки лога, ошибка в другую сторону — стоит токена.

ИСПОЛЬЗОВАНИЕ (bot.py любого бота, сразу после basicConfig):

    import logging
    from ai_office_shared.shared.log_redaction import (
        install_secret_redaction, quiet_http_client_logs,
    )

    logging.basicConfig(level=logging.INFO)
    quiet_http_client_logs()        # убирает саму строку с токеном
    install_secret_redaction()      # и вырезает секреты из всего остального

Порядок важен: `basicConfig` создаёт обработчики, `install_secret_redaction`
меняет фабрику записей — она действует и на обработчики, созданные позже.

КОНТРАКТ: ни одна функция не бросает исключение наружу. Логирование, которое
роняет бота, хуже логирования, которое пропустило секрет.
"""

from __future__ import annotations

import logging
import os
import re
import traceback
from typing import Iterable

logger = logging.getLogger("ai_office_shared.log_redaction")

MASK = "<секрет вырезан>"

# Токен Telegram: <числовой id бота>:<хвост base64url>. Нижняя граница хвоста —
# 30: у токена из документации Telegram (110201543:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw)
# хвост длиной 34, у новых — 35, и порог 35 такой токен бы пропустил.
_TG_TAIL = r"\d{6,12}:[A-Za-z0-9_-]{30,}"
# Внутри URL токен приклеен к «bot» — границы слова перед цифрами там нет.
_TG_IN_URL = re.compile(r"/bot" + _TG_TAIL)
# Отдельно стоящий токен. Слева не должно быть буквы, цифры, дефиса или точки:
# иначе выкусим хвост из чужого длинного идентификатора.
_TG_BARE = re.compile(r"(?<![\w.:-])" + _TG_TAIL)

_KEY_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),          # Anthropic
    re.compile(r"(?<![\w-])sk-[A-Za-z0-9]{20,}"),      # OpenAI и совместимые
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),         # GitHub PAT
    re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}"),      # Slack
)

# Значение короче этого не регистрируется как секрет: замена коротких строк
# по всему тексту превращает логи в кашу, а секретов такой длины не бывает.
MIN_SECRET_LEN = 12

_registered: set[str] = set()
_installed = False


def register_secret(value: str) -> bool:
    """Запомнить конкретное значение, чтобы вырезать его дословно.

    Шаблоны выше знают формы известных провайдеров. Регистрация нужна для всего
    остального: TELETHON_SESSION, пароль Redis, токен Railway — у них нет формы,
    по которой их узнать в тексте.

    Возвращает False, если значение слишком короткое или пустое — тогда оно НЕ
    вырезается, и это намеренно (см. MIN_SECRET_LEN).
    """
    try:
        v = (value or "").strip()
    except Exception:
        return False
    if len(v) < MIN_SECRET_LEN:
        return False
    _registered.add(v)
    return True


SECRET_ENV_NAMES = (
    "TELEGRAM_TOKEN", "CODER_BOT_TOKEN", "BOT_TOKEN",
    "ANTHROPIC_API_KEY", "ANTHROPIC_KEY",
    "GITHUB_TOKEN", "GH_TOKEN",
    "RAILWAY_TOKEN", "RAILWAY_TOKEN_VLAD",
    "OFFICE_RPC_TOKEN",
    "TELETHON_SESSION", "TELEGRAM_API_HASH",
    "REDIS_URL", "REDIS_PASSWORD",
    "ELEVENLABS_API_KEY", "OPENAI_API_KEY",
)


def register_env_secrets(names: Iterable[str] = SECRET_ENV_NAMES) -> int:
    """Зарегистрировать значения переменных окружения по именам.

    Списком имён, а не значений: боту не приходится перечислять свои секреты в
    коде, а тот, кто заведёт новую переменную, добавляет её в SECRET_ENV_NAMES
    один раз для всего офиса. Отсутствующие и короткие значения пропускаются.

    Возвращает, сколько значений добавилось.
    """
    added = 0
    for name in names or ():
        try:
            if register_secret(os.getenv(name, "")):
                added += 1
        except Exception as e:
            logger.debug("register_env_secrets(%s) failed: %s", name, e)
    return added


def registered_count() -> int:
    """Сколько значений зарегистрировано. Для проверок и для диагностики."""
    return len(_registered)


def redact(text: str) -> str:
    """Текст без секретов. Идемпотентна: маска ни под один шаблон не подходит.

    Применяется и к своим логам (через фабрику), и к чужим — к хвосту
    deploymentLogs, который Силли тянет обратно из Railway и кладёт в промпт,
    в отчёт Владу и в контекст /office.
    """
    if not text:
        return text
    try:
        out = text
        # Дословные значения — первыми: они точнее шаблонов.
        for secret in sorted(_registered, key=len, reverse=True):
            if secret in out:
                out = out.replace(secret, MASK)
        out = _TG_IN_URL.sub("/bot" + MASK, out)
        out = _TG_BARE.sub(MASK, out)
        for pat in _KEY_PATTERNS:
            out = pat.sub(MASK, out)
        return out
    except Exception as e:                      # чистка не должна ронять логи
        logger.debug("redact failed: %s", e)
        return text


def _redact_arg(arg):
    """Аргумент %-форматирования. Не-строки не трогаем: там секрета не бывает."""
    if isinstance(arg, str):
        return redact(arg)
    return arg


def _make_factory(previous):
    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(_redact_arg(a) for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: _redact_arg(v) for k, v in record.args.items()}
            if record.exc_info and not record.exc_text:
                # Трейсбек рисует форматтер — и уже после всех фильтров. Рисуем
                # сами и кладём в exc_text: форматтер использует готовое поле и
                # второй раз к exc_info не пойдёт.
                record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)).rstrip())
            elif record.exc_text:
                record.exc_text = redact(record.exc_text)
            if record.stack_info:
                record.stack_info = redact(record.stack_info)
        except Exception as e:
            logger.debug("record redaction failed: %s", e)
        return record

    return factory


def install_secret_redaction(*, extra_secrets: Iterable[str] = (), from_env: bool = True) -> bool:
    """Поставить чистку на весь logging процесса. Повторный вызов безопасен.

    from_env=True — заодно зарегистрировать значения из SECRET_ENV_NAMES: это
    то, ради чего вызов делается одной строкой и без аргументов.

    Возвращает True, если фабрика поставлена этим вызовом, False — если она уже
    стояла (секреты регистрируются в обоих случаях).
    """
    global _installed
    if from_env:
        register_env_secrets()
    for value in extra_secrets or ():
        register_secret(value)
    if _installed:
        return False
    try:
        logging.setLogRecordFactory(_make_factory(logging.getLogRecordFactory()))
        _installed = True
        return True
    except Exception as e:
        logger.warning("install_secret_redaction failed: %s", e)
        return False


# Логгеры, которые печатают URL запроса на INFO. httpx делает это на каждый
# getUpdates — то есть каждые несколько секунд у каждого бота на polling.
HTTP_CLIENT_LOGGERS = ("httpx", "httpcore", "urllib3", "aiohttp.client", "aiohttp.access")


def quiet_http_client_logs(level: int = logging.WARNING) -> None:
    """Поднять порог у HTTP-клиентов: строка с токеном не пишется вовсе.

    Чистка накрыла бы её и так, но эта строка не несёт ничего, кроме шума, а
    шум стоит дорого: 17.08.2026 анализатор увидел двенадцать строк
    «getUpdates 200 OK», не нашёл в них отказа и придумал баг сам (урок #117).
    Ошибки клиентов остаются — порог поднят до WARNING, а не выключен.
    """
    for name in HTTP_CLIENT_LOGGERS:
        try:
            logging.getLogger(name).setLevel(level)
        except Exception as e:
            logger.debug("quiet_http_client_logs(%s) failed: %s", name, e)

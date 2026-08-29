"""
ai_office_shared.shared.fault_class — чей это отказ: внешний, наш, или неизвестно.

ПРОБЛЕМА (инцидент 27–29.08.2026):
    За трое суток офис получил 11 сообщений вида

        📚 Cilly: повтор известной проблемы в *molly-trader* — урок #8.
        _httpx timeout during HTTP request…_
        Новых действий не требуется (фикс уже задокументирован).

    из четырёх сервисов (molly-trader, villy-bot, tilly-trader,
    office-dashboard), при том что все аудиты тех же суток отчитались
    «🟢 НОРМА, ошибок за последние 2ч нет», а health-проверки — OK.
    Сообщения приходили по 2–4 в сутки на сервис, ровно через 6 часов, —
    это `EXTERNAL_FAULT_COOLDOWN` на ключе `lesson_applied:{svc}:{lesson}`.

    Ничего не ломалось. httpx-таймаут до Telegram и до биржи на Railway —
    штатное дребезжание сети: бот ретраит и работает дальше. Уроки #8 и #64
    ровно про это и написаны, и монитор специально устроен так, чтобы про
    такие сбои МОЛЧАТЬ (Filter Layer 3 в monitor_loop).

ПОЧЕМУ ГЕЙТ ТИШИНЫ НЕ СРАБОТАЛ:
    `classify_fault` искала внешние паттерны во ВСЁМ тексте и, не найдя
    ничего, возвращала "internal" — то есть «наш баг» — по умолчанию.
    А трейсбек доходит до монитора не всегда целиком: логи читаются по
    водяному знаку (`get_service_logs`, режим last_seen), Railway отдаёт
    каждую строку стека отдельной записью со своей меткой времени, и чтение,
    попавшее в середину выгрузки, забирает заголовок с кадрами БЕЗ строки
    исключения:

        Traceback (most recent call last):                        ← есть
          File ".../httpx/_client.py", line 1774, in get          ← есть
            return await self.request("GET", url, **kwargs)       ← есть
        httpx.ReadTimeout: timed out after 25.0 seconds           ← ЕЩЁ НЕ ПРИШЛА

    В этом огрызке нет ни одной строки из EXTERNAL_FAULT_PATTERNS: все они —
    имена исключений, а осталась ровно та часть, где имени исключения нет.
    Гейт открывался, `search_lessons` узнавала по кадрам `httpx/_client.py`
    урок #8 с высокой уверенностью — и офис получал сообщение, чей
    собственный текст гласит «новых действий не требуется».

    Отсюда же и «причины», которых в логе не было: в сообщениях от 28.08
    названы `RemoteProtocolError` и `ConnectError`, хотя ни одна из этих
    строк до монитора не доезжала — модели показали обезглавленный стек, и
    она дописала недостающее. Ровно то, против чего написан урок #123.

ПРАВИЛО:
    Отсутствие улики — не улика. «Внешних признаков не нашлось» и «признаки
    нашего бага нашлись» — разные утверждения, и сводить второе к первому
    нельзя: по такому выводу офис идёт чинить исправный код (инвариант №4 —
    проверка, которой не на чем было запуститься, это провал, а не пропуск).

    Поэтому вердикта три, а не два. UNKNOWN — это не «наверное наш»:
    это «стек оборвался, называть виноватого не на чем» (инвариант №8).

    И совпадение — по слову, а не по подстроке (инвариант №7): маркер
    `TypeError` внутри `TypeErrorHandler` — не исключение.
"""

from __future__ import annotations

import re

from .traceback_scan import TB_HEADER, signature_basis

EXTERNAL = "external"
INTERNAL = "internal"
UNKNOWN = "unknown"

# Внешние/транзиентные сбои — НЕ наш баг. Если корневая причина в недоступности
# стороннего сервиса (Telegram/Railway API, DNS, сеть), а бот жив — Силли МОЛЧИТ
# (по требованию владельца), а не предлагает фикс. Список шире IGNORE_PATTERNS:
# ловит сбои не только на polling/getUpdates, но и при отправке/любых POST.
EXTERNAL_FAULT_PATTERNS = [
    "telegram.error.NetworkError",
    "NetworkError",
    "httpx.ConnectError",
    "httpx.ConnectTimeout",
    "httpx.ReadTimeout",
    "httpx.RemoteProtocolError",
    "httpcore.RemoteProtocolError",
    "ConnectTimeout",
    "ReadTimeout",
    "RemoteProtocolError",
    "Server disconnected",
    "Bad Gateway",
    " 502",
    " 503",
    " 504",
    "getaddrinfo failed",
    "Temporary failure in name resolution",
    "Connection reset by peer",
    "Connection aborted",
]

# Признаки СТРУКТУРНОГО бага в нашем коде. Ищутся на строке исключения, а не
# по всему окну: `KeyError` в тексте обработанного и залогированного ретрая —
# не падение, а `TypeError` в кадре чужой библиотеки — не наш баг.
OUR_BUG_MARKERS = [
    "NameError", "ImportError", "ModuleNotFoundError", "SyntaxError",
    "IndentationError", "KeyError", "AttributeError", "TypeError",
    "ValueError", "IndexError", "UnboundLocalError",
]


def _rx(pattern: str) -> re.Pattern:
    """Регексп паттерна с границами слова там, где границы вообще есть.

    `\\b` приклеивается только к тому концу паттерна, который начинается или
    кончается символом слова: у `" 502"` левый край — пробел, и `\\b` там
    означал бы не то, что нужно, а у `"KeyError"` оба края словесные.
    """
    left = r"\b" if pattern[:1].isalnum() or pattern[:1] == "_" else ""
    right = r"\b" if pattern[-1:].isalnum() or pattern[-1:] == "_" else ""
    return re.compile(left + re.escape(pattern) + right)


_EXTERNAL_RX = [(p, _rx(p)) for p in EXTERNAL_FAULT_PATTERNS]
_BUG_RX = [(m, _rx(m)) for m in OUR_BUG_MARKERS]


def _first_hit(text: str, table) -> str:
    for name, rx in table:
        if rx.search(text):
            return name
    return ""


class Verdict(str):
    """Вердикт + чем он обоснован.

    Наследует str, поэтому старые сравнения `classify(...) == "external"`
    работают как раньше; `.reason` нужен, чтобы в лог уходило НАЗВАНИЕ
    сработавшего паттерна, а не пересказ (инвариант №8).
    """

    reason: str
    evidence: str

    def __new__(cls, value: str, reason: str = "", evidence: str = ""):
        obj = super().__new__(cls, value)
        obj.reason = reason
        obj.evidence = evidence
        return obj


def classify(error_logs) -> Verdict:
    """Внешний транзиентный сбой / наш баг / нечем судить.

    EXTERNAL — корневая причина в недоступности стороннего сервиса
      (Telegram/Railway API, DNS, сеть) и признаков нашего структурного бага
      на строке исключения нет.
    INTERNAL — на строке исключения стоит маркер нашего бага, ЛИБО стек
      полный и ни на что внешнее не похож (падение, которое стоит разобрать).
    UNKNOWN — в логе есть заголовок трейсбека, но строки исключения в нём
      нет: стек оборван (водяной знак разрезал выгрузку, SIGKILL/OOM посреди
      печати). Судить не на чем — и молчать об этом тоже нельзя, поэтому это
      отдельный вердикт, а не тихо приписанный нам баг.
    """
    lines = [str(x) for x in (error_logs or [])]
    text = "\n".join(lines)
    if not text.strip():
        return Verdict(UNKNOWN, "пустой лог", "")

    has_tb = any(TB_HEADER in ln for ln in lines)
    basis = signature_basis(lines)

    if has_tb and not basis.exc:
        # Заголовок есть, исключения нет. Раньше отсюда выходило "internal".
        head = next((ln for ln in lines if TB_HEADER in ln), "")
        return Verdict(
            UNKNOWN,
            "трейсбек без строки исключения — стек оборван",
            (basis.file and f'{basis.file}:{basis.line}') or head.strip()[:120],
        )

    # Строка исключения, если она есть, — единственное место, где имя класса
    # что-то доказывает. Всё остальное в блоке — кадры чужих библиотек.
    subject = basis.msg or text if has_tb else text

    bug = _first_hit(subject, _BUG_RX)
    if bug:
        return Verdict(INTERNAL, f"маркер нашего бага: {bug}", basis.msg[:160] or subject[:160])

    ext = _first_hit(subject, _EXTERNAL_RX) or _first_hit(text, _EXTERNAL_RX)
    if ext:
        return Verdict(EXTERNAL, f"внешний паттерн: {ext}", basis.msg[:160] or subject[:160])

    return Verdict(INTERNAL, "полный отказ, ни на что внешнее не похож",
                   basis.msg[:160] or subject[:160])


def is_ours(error_logs) -> bool:
    """Доказано ли, что чинить это нам. UNKNOWN — не доказано."""
    return classify(error_logs) == INTERNAL

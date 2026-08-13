"""
ai_office_shared.shared.gates — детерминированные гейты приёмки.

ЗАЧЕМ ЭТО ЕСТЬ:
    Фаза 1 дала критерии приёмки и улики: задача не становится done, пока
    каждый критерий не закрыт. Но кто закрывает — оставалось на совести
    исполнителя, а исполнитель, глядя на свою работу, всегда видит успех.
    Замена «LLM проверяет LLM» на «LLM проверяет ЧУЖУЮ работу» половину
    проблемы снимает, но не всю: судья остаётся той же природы, что и
    подсудимый, и на пограничном случае договорится сам с собой.

    Поэтому там, где успех вообще МОЖНО измерить, его меряет не модель, а код:
    компиляция, HTTP-код, наличие ключа в Redis, подстрока в артефакте. Такой
    гейт не устаёт, не хочет понравиться и выдаёт улику, которую можно
    перепроверить, — наблюдённое значение, а не «проверил, работает».

    LLM-проверка остаётся для того, что померить нечем («ответ звучит
    по-человечески»). Правило приоритета: если критерий выразим гейтом —
    он ОБЯЗАН быть гейтом, иначе проверку опять напишут прозой.

ФОРМАТ КРИТЕРИЯ:
    Критерий на доске — либо строка (проверяет человек/бот), либо словарь
    {"text": "...", "gate": {"kind": ..., ...}} — проверяет этот модуль.
    Старый формат (список строк) продолжает работать без изменений.

ПОЧЕМУ ЗДЕСЬ НЕТ pyflakes/pytest:
    Они есть в CI, но не в рантайме ботов — гейт, который на проде всегда
    падает с «команда не найдена», хуже отсутствующего: он превращается в шум,
    который научатся игнорировать. Здесь только то, что исполнимо там, где
    крутится бот.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

_logger = logging.getLogger("ai_office_shared.gates")

# Сколько символов наблюдённого значения кладём в улику. Улика должна быть
# перепроверяемой, но не должна раздувать HASH задачи.
MAX_PROOF_LEN = 500

GATE_KINDS = ("python_compile", "http_status", "redis_key", "text_contains", "text_absent")


def criterion_text(item: Any) -> str:
    """Текст критерия из любого поддерживаемого формата (строка или dict)."""
    if isinstance(item, dict):
        return str(item.get("text") or "")
    return str(item or "")


def criterion_gate(item: Any) -> Optional[dict]:
    """Гейт критерия, если он задан и выглядит валидным. Иначе None."""
    if not isinstance(item, dict):
        return None
    gate = item.get("gate")
    if not isinstance(gate, dict):
        return None
    if gate.get("kind") not in GATE_KINDS:
        return None
    return gate


def _proof(text: str) -> str:
    return (text or "")[:MAX_PROOF_LEN]


async def _gate_python_compile(gate: dict, **_) -> tuple[bool, str]:
    """Код компилируется. Ловит обрезанный воркером файл — самый частый отказ."""
    code = gate.get("code") or ""
    name = gate.get("name") or "<gate>"
    if not code:
        return False, "нечего компилировать: code пустой"
    try:
        compile(code, name, "exec")
    except SyntaxError as e:
        return False, _proof(f"SyntaxError: {e.msg}, строка {e.lineno}")
    return True, _proof(f"compile() ок, {len(code.splitlines())} строк")


async def _gate_http_status(gate: dict, **_) -> tuple[bool, str]:
    """HTTP-код совпадает с ожидаемым. Улика — реально полученный код."""
    url = gate.get("url") or ""
    expect = int(gate.get("expect") or 200)
    if not url:
        return False, "url не задан"
    try:
        import httpx
        from .auth import office_headers
    except Exception as e:  # pragma: no cover — окружение без httpx
        return False, _proof(f"гейт неисполним: {e}")
    try:
        async with httpx.AsyncClient(timeout=float(gate.get("timeout") or 15)) as client:
            resp = await client.get(url, headers=office_headers())
    except Exception as e:
        return False, _proof(f"запрос не прошёл: {type(e).__name__}: {e}")
    ok = resp.status_code == expect
    return ok, _proof(f"GET {url} → {resp.status_code} (ждали {expect})")


async def _gate_redis_key(gate: dict, *, redis_client=None, **_) -> tuple[bool, str]:
    """
    Ключ существует и (опционально) содержит подстроку.

    Это гейт «данные реально записались», а не «бот сказал, что записал».
    """
    key = gate.get("key") or ""
    if not key:
        return False, "key не задан"
    if redis_client is None:
        return False, "нет redis-клиента для проверки"
    contains = gate.get("contains")
    try:
        exists = await redis_client.exists(key)
    except Exception as e:
        return False, _proof(f"redis недоступен: {type(e).__name__}: {e}")
    if not exists:
        return False, _proof(f"ключ {key} отсутствует")
    if contains is None:
        return True, _proof(f"ключ {key} существует")
    # Читаем значение только если просили проверить содержимое: тип ключа
    # заранее неизвестен, поэтому пробуем строку и мягко падаем на repr.
    try:
        val = await redis_client.get(key)
    except Exception as e:
        return False, _proof(f"ключ {key} есть, но не читается как строка: {e}")
    text = val.decode("utf-8", "replace") if isinstance(val, bytes) else str(val or "")
    ok = str(contains) in text
    return ok, _proof(f"{key} = {text[:200]!r}; искали {contains!r}")


async def _gate_text_contains(gate: dict, **_) -> tuple[bool, str]:
    """Артефакт содержит подстроку."""
    text = str(gate.get("text") or "")
    needle = str(gate.get("needle") or "")
    if not needle:
        return False, "needle не задан"
    ok = needle in text
    return ok, _proof(f"искали {needle!r} в тексте {len(text)} симв. → "
                      f"{'найдено' if ok else 'не найдено'}")


async def _gate_text_absent(gate: dict, **_) -> tuple[bool, str]:
    """
    Артефакт НЕ содержит подстроку.

    Нужен чаще, чем кажется: «в ответе нет слова про техработы», «в диффе не
    осталось выпиленного ID». Отрицательные критерии иначе не проверить.
    """
    text = str(gate.get("text") or "")
    needle = str(gate.get("needle") or "")
    if not needle:
        return False, "needle не задан"
    ok = needle not in text
    return ok, _proof(f"искали {needle!r} в тексте {len(text)} симв. → "
                      f"{'отсутствует' if ok else 'ВСТРЕЧАЕТСЯ'}")


_RUNNERS = {
    "python_compile": _gate_python_compile,
    "http_status": _gate_http_status,
    "redis_key": _gate_redis_key,
    "text_contains": _gate_text_contains,
    "text_absent": _gate_text_absent,
}


async def run_gate(gate: dict, *, redis_client=None) -> tuple[bool, str]:
    """
    Исполнить один гейт. Возвращает (прошёл, улика).

    Ошибка исполнения — это ПРОВАЛ, а не «пропустим». Гейт, который не смог
    проверить, ничего не подтвердил; трактовать его как успех означает вернуть
    ровно ту дыру, ради которой он написан.
    """
    kind = (gate or {}).get("kind")
    runner = _RUNNERS.get(kind)
    if runner is None:
        return False, _proof(f"неизвестный тип гейта: {kind!r}")
    try:
        return await runner(gate, redis_client=redis_client)
    except Exception as e:  # pragma: no cover — защита от кривого спека
        _logger.warning("gate %s упал: %s", kind, e)
        return False, _proof(f"гейт упал: {type(e).__name__}: {e}")

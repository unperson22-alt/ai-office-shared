"""
ai_office_shared.shared.json_extract — достать JSON из ответа модели.

ПРОБЛЕМА (инцидент 2026-08-15, агентная петля Силли):
    По всему coder.py тринадцать раз повторён один и тот же приём:

        start, end = raw.find("{"), raw.rfind("}") + 1
        data = json.loads(raw[start:end])

    «От первой скобки до последней». Пока модель отдаёт ровно один объект, это
    работает. Стоит ей вернуть два подряд — а на длинной агентной задаче это
    вопрос времени — срез захватывает ОБА, и json.loads падает с «Extra data».
    Влад отправил через Крисса баг-репорт, Силли дошла до десятого шага и
    умерла на разборе собственного ответа: «❌ Шаг 10: ошибка парсинга: Extra
    data: line 3 column 1». Задача не выполнена, причина непонятна, а человек
    идёт делать руками ровно то, ради чего заводил ассистента.

    Тот же срез ломается и на прозе вокруг JSON, если в ней есть фигурная
    скобка («верну {"action": ...} — надеюсь, это то, что нужно {sic}»).

РЕШЕНИЕ:
    Разбирать ПЕРВЫЙ синтаксически полный объект через json.JSONDecoder.
    raw_decode() читает ровно один объект и говорит, где он кончился, — всё,
    что после, нас не касается. Это стандартный способ читать JSON из потока,
    и он не зависит от того, что модель дописала следом.

    Функция ничего не бросает: на входе может быть что угодно, включая пустую
    строку и болтовню без JSON вовсе. Вызывающий отличает «не разобралось» по
    None, а не по типу исключения.
"""

from __future__ import annotations

import json
from typing import Any, Optional

_DECODER = json.JSONDecoder()


def first_json_object(text: str | None) -> Optional[dict]:
    """
    Первый полный JSON-объект в тексте. None — если его там нет.

    Возвращает только dict: во всех местах, где это используется, ожидается
    объект действия. Массив или число на верхнем уровне — не то, что просили,
    и притворяться, что это результат, хуже, чем честный None.

    >>> first_json_object('{"action": "done"}')
    {'action': 'done'}
    >>> first_json_object('{"a": 1}\\n{"b": 2}')      # два объекта — берём первый
    {'a': 1}
    >>> first_json_object('вот ответ: {"a": 1}. Готово {sic}')
    {'a': 1}
    >>> first_json_object('никакого json тут нет') is None
    True
    """
    if not text:
        return None
    s = str(text)
    idx = s.find("{")
    while idx != -1:
        try:
            obj, _end = _DECODER.raw_decode(s, idx)
        except ValueError:
            # Здесь объект не начинается (например «{sic}» или обрезанный
            # хвост) — ищем следующую скобку, а не сдаёмся.
            idx = s.find("{", idx + 1)
            continue
        if isinstance(obj, dict):
            return obj
        idx = s.find("{", idx + 1)
    return None


def first_json_value(text: str | None) -> Any:
    """
    То же, но принимает и массив на верхнем уровне.

    Нужно там, где модель законно отвечает списком (например перечнем правок).
    None — если разобрать нечего.
    """
    if not text:
        return None
    s = str(text)
    positions = sorted(
        p for p in (s.find("{"), s.find("[")) if p != -1
    )
    for start in positions:
        idx = start
        while idx != -1:
            try:
                obj, _end = _DECODER.raw_decode(s, idx)
                return obj
            except ValueError:
                nxt_o = s.find("{", idx + 1)
                nxt_a = s.find("[", idx + 1)
                cands = [p for p in (nxt_o, nxt_a) if p != -1]
                idx = min(cands) if cands else -1
    return None

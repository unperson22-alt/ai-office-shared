"""
ai_office_shared.shared.target_repo — в каком репозитории чинить.

ПРОБЛЕМА (инцидент 2026-08-15):
    Влад через Крисса попросил убрать быстрые кнопки из /menu. Силли завела
    задачу и ушла в billy-bot: `[nl] intent=agentic_task confidence=0.92
    repo=billy-bot`. Кнопки — у Крисса (make_task_keyboard в kriss-bot),
    в billy-bot их ноль. Ничего не найдя, петля начала повторять одно и то же
    действие и упёрлась в стоп-гард: «⚠️ Остановлено: зацикливание».

    Со стороны это выглядит как «Силли тупит и зацикливается». На деле она
    добросовестно искала кнопки там, куда её послали. Зацикливание — симптом,
    а диагноз в том, что репозиторий ВЫБИРАЛА МОДЕЛЬ, угадывая, хотя ответ был
    известен точно: заявка пришла ЧЕРЕЗ Крисса и была про Крисса.

    Уверенность 0.92 здесь особенно вредна: она выглядит как знание, но модель
    не может знать, у кого какие кнопки, — она угадала правдоподобно.

ПРАВИЛО (сверху вниз, первое сработавшее выигрывает):
    1. В тексте назван бот — чиним его репозиторий. Человек сказал прямо.
    2. Иначе, если заявку принёс бот — его репозиторий. Жалоба, принесённая
       Криссом и не называющая никого другого, — жалоба на Крисса.
    3. Иначе — догадка модели. Худший источник, поэтому последний.

    Первые два шага детерминированы и проверяемы; модель включается, только
    когда фактов нет вовсе.
"""

from __future__ import annotations

import re
from typing import Optional

from .identity import BOTS, canonical


def repo_for_bot(name: str) -> Optional[str]:
    """Репозиторий бота по любому написанию имени. None — если бот неизвестен."""
    canon = canonical(name or "")
    if not canon:
        return None
    return (BOTS.get(canon) or {}).get("repo")


def bots_named_in(text: str) -> list[str]:
    """
    Канонические имена ботов, явно названных в тексте.

    Матчим по границе слова: подстрока ловила бы «били» внутри «побили», а
    неверная цель здесь дороже пропущенной — из-за неё правят чужой файл.
    """
    if not text:
        return []
    low = str(text).lower()
    found: list[str] = []
    for canon, meta in BOTS.items():
        names = {canon, *(a.lower() for a in meta.get("aliases", []) if a)}
        for n in names:
            if not n or len(n) < 4:
                continue                      # слишком короткое — ложные срабатывания
            if re.search(rf"(?<!\w){re.escape(n.lower())}(?!\w)", low):
                if canon not in found:
                    found.append(canon)
                break
    return found


def target_repo(message: str, *, sender: str = "",
                llm_guess: str = "") -> tuple[Optional[str], str]:
    """
    Куда идти чинить. Возвращает (репозиторий, чем_обосновано).

    Обоснование возвращается не для красоты: когда Силли снова уедет не туда,
    по логу должно быть видно, ПОЧЕМУ она туда уехала, — иначе разбор опять
    сведётся к «модель что-то решила».
    """
    named = bots_named_in(message)
    if len(named) == 1:
        repo = repo_for_bot(named[0])
        if repo:
            return repo, f"в тексте назван {named[0]}"
    elif len(named) > 1:
        # Названо несколько — угадывать, кто из них цель, не наше дело;
        # пусть решает модель, но в логе будет видно, что случай спорный.
        pass

    if sender:
        repo = repo_for_bot(sender)
        if repo:
            return repo, f"заявку принёс {canonical(sender)}"

    return (llm_guess or None), "догадка модели"

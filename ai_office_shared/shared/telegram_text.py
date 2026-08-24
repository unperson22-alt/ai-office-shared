"""
ai_office_shared.shared.telegram_text — резка длинного текста под лимит Telegram.

ПРОБЛЕМА (инцидент 12.08–23.08.2026):
    Группа Bug Lessons одиннадцать дней стояла на уроке #90, пока файл ушёл
    вперёд на двадцать три записи. Причина: урок #91 форматируется в 5035
    символов при жёстком лимите Telegram в 4096. `send_message` отвечал
    «message is too long», а публикатор на любой ошибке делал `break` — то есть
    одно слишком длинное сообщение намертво запирало ВСЮ очередь за собой.

    Сам `break` был написан правильно и намеренно: «непосланное НЕ помечаем».
    Беда не в нём, а в том, что отказ одной записи он превращал в отказ всех
    последующих, а единственным сигналом был `logger.error`, которого никто не
    видел. Тихая остановка неотличима от «новых уроков нет».

ПРАВИЛО:
    Текст, который не помещается, режется по границам смысла — сначала абзацы,
    потом строки, и только в последнюю очередь посреди слова. Обрезать молча
    нельзя: потерянный хвост урока выглядит как урок, а не как потеря.
"""

from __future__ import annotations

import re

# Жёсткий предел Telegram на текст одного сообщения.
TELEGRAM_LIMIT = 4096
# Запас под маркер «[2/3]\n» — считаем до нарезки, иначе маркер сам вылезет за лимит.
_MARKER_RESERVE = 12

# Маркер части. Живёт рядом с кодом, который его ПИШЕТ: читателю (перепост
# урока ищет хвосты своего сообщения) нужен тот же формат, а две копии одного
# регекспа в разных файлах расходятся молча.
_PART_MARKER = re.compile(r"^\[(\d+)/(\d+)\]\n")


def is_continuation_part(text: str) -> bool:
    """Текст — часть разрезанного сообщения, кроме первой.

    Первая часть несёт и маркер, и заголовок, поэтому находится по заголовку;
    хвосты заголовка не несут вовсе и опознаются только этим маркером.
    """
    m = _PART_MARKER.match(str(text or ""))
    return bool(m) and m.group(1) != "1"


def _hard_chunks(text: str, size: int) -> list:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def _pack(pieces: list, sep: str, budget: int) -> list:
    """Складывает куски в части, не превышая бюджет. Кусок больше бюджета — как есть."""
    out: list = []
    cur = ""
    for piece in pieces:
        candidate = piece if not cur else cur + sep + piece
        if len(candidate) <= budget:
            cur = candidate
            continue
        if cur:
            out.append(cur)
        cur = piece if len(piece) <= budget else ""
        if not cur:
            out.extend(_hard_chunks(piece, budget))
    if cur:
        out.append(cur)
    return out or [""]


def split_for_telegram(text: str, limit: int = TELEGRAM_LIMIT) -> list:
    """
    Текст, нарезанный на части не длиннее limit. Помещается — список из одного
    элемента БЕЗ маркера: подавляющее большинство сообщений короткие, и метить
    их «[1/1]» значило бы уродовать норму ради исключения.

    Режем по убыванию осмысленности: абзацы → строки → жёстко по символам.
    Ничего не выбрасываем: обрезанный хвост выглядел бы как целый урок.
    """
    text = str(text or "")
    if len(text) <= limit:
        return [text]

    budget = max(1, limit - _MARKER_RESERVE)
    parts = _pack(text.split("\n\n"), "\n\n", budget)

    # Абзац сам мог не влезть — доводим построчно.
    refined: list = []
    for p in parts:
        refined.extend([p] if len(p) <= budget else _pack(p.split("\n"), "\n", budget))

    total = len(refined)
    return [f"[{i}/{total}]\n{p}" for i, p in enumerate(refined, 1)]

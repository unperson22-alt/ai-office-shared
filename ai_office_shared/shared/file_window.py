"""
ai_office_shared.shared.file_window — показать модели нужный кусок файла.

ПРОБЛЕМА (инцидент 2026-08-15, третий слой одной задачи):
    Влад попросил убрать быстрые кнопки из /menu у Крисса. Силли дошла до
    правильного репозитория, прочитала `kriss-bot/bot.py` — и не смогла
    выполнить задачу за 12 шагов.

    Числа: файл 51 340 символов, окно чтения 24 000. Целевая строка

        keyboard = make_task_keyboard() if should_show_quick_reply(response) else None

    стоит на 823-й строке из 1005, то есть примерно на 42 000-м символе —
    ЗА окном. Силли физически не видела того, что надо править, и жгла шаги.

    Это второй раз: в июле окно уже поднимали с 4 000 до 24 000 по той же
    причине, и в коде остался комментарий «окна хватает на весь bot.py».
    Хватало — на bot.py того размера. Порог, привязанный к сегодняшнему
    размеру файлов, истекает молча, вместе с ростом файлов.

РЕШЕНИЕ:
    Не «поднять окно ещё раз», а дать возможность СПРОСИТЬ нужное место:
    вырезать окрестности искомой строки. Тогда размер файла перестаёт быть
    ограничением: 51 КБ или 500 КБ — модель получает те же сорок строк вокруг
    совпадения, и на них хватает и окна, и внимания.
"""

from __future__ import annotations

MAX_MATCHES = 4          # больше — это не поиск, а чтение файла целиком
CTX_LINES = 40           # строк контекста вокруг совпадения


def find_regions(text: str, needle: str, *, ctx_lines: int = CTX_LINES,
                 max_matches: int = MAX_MATCHES) -> str:
    """
    Фрагменты файла вокруг вхождений `needle`, с номерами строк.

    Пустая строка в ответ означает «не найдено» — и это ЧЕСТНЫЙ ответ, по
    которому видно, что искать надо иначе. Отдавать вместо этого начало файла
    (как делало обрезанное чтение) хуже: модель видит валидный код, не находит
    в нём цели и делает вывод, что цели не существует.
    """
    if not text or not needle:
        return ""
    lines = text.splitlines()
    low_needle = needle.lower()
    hits = [i for i, ln in enumerate(lines) if low_needle in ln.lower()]
    if not hits:
        return ""

    tail = ""
    if len(hits) > max_matches:
        tail = (f"[ещё {len(hits) - max_matches} совпадений не показано — "
                f"уточни поисковую строку]")
    return _render(lines, hits[:max_matches], ctx_lines) + tail


def _render(lines: list[str], hits, ctx_lines: int) -> str:
    """Фрагменты вокруг указанных индексов строк, с номерами и без повторов."""
    # Схлопываем соседние совпадения в один фрагмент, чтобы не показывать
    # одни и те же строки дважды.
    regions: list[tuple[int, int]] = []
    for i in sorted(hits):
        start, end = max(0, i - ctx_lines), min(len(lines), i + ctx_lines + 1)
        if regions and start <= regions[-1][1]:
            regions[-1] = (regions[-1][0], max(regions[-1][1], end))
        else:
            regions.append((start, end))

    out: list[str] = []
    for start, end in regions:
        out.append(f"[строки {start + 1}–{end}]")
        out.extend(f"{n + 1:5d}| {lines[n]}" for n in range(start, end))
        out.append("")
    return "\n".join(out)


def around_lines(text: str, linenos, *, ctx_lines: int = CTX_LINES,
                 max_regions: int = MAX_MATCHES) -> str:
    """
    Фрагменты файла вокруг НОМЕРОВ строк (1-based), с номерами строк.

    Зачем отдельно от find_regions: трейсбек даёт не подстроку, а координату —
    `File "bot.py", line 412`. Искать по подстроке тут нечего, а показывать
    вместо этого начало файла — ровно тот дефект, на который 25.08 пожаловались
    обе эскалации: причина лежала за 3000-символьным срезом, и модель, не найдя
    её, придумала свою (обе выдумки затем опроверг компилятор).

    Пустая строка означает «таких строк в файле нет» — честный ответ, по
    которому видно, что кадр из лога не соответствует прочитанному файлу
    (деплой разъехался с логом). Это лучше, чем показать не то место молча.
    """
    if not text:
        return ""
    lines = text.splitlines()
    hits = []
    for n in (linenos or []):
        try:
            i = int(n) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(lines) and i not in hits:
            hits.append(i)
    if not hits:
        return ""
    return _render(lines, hits[:max_regions], ctx_lines)


def summary(text: str) -> str:
    """Одна строка про файл: сколько в нём всего. Нужна, когда показан кусок."""
    if not text:
        return "файл пуст"
    return f"всего {len(text.splitlines())} строк, {len(text)} символов"

"""
ai_office_shared.shared.traceback_scan — отказ целиком, а не его заголовок.

ПРОБЛЕМА (инцидент 25.08.2026):
    За одну ночь в офис пришли две эскалации — villy-bot в 03:28 и
    office-dashboard в 10:47 — с ОДНОЙ И ТОЙ ЖЕ сигнатурой `1b21026f90825877`
    и fix_count=3 каждая. Обе жаловались на одно: «traceback заканчивается на
    "Traceback (most recent call last):" без тела исключения».

    Сигнатура выдала себя сама:

        md5("Traceback (most recent call last):")[:16] == "1b21026f90825877"

    Это хеш ровно одной строки — заголовка. Не логов villy, не логов дашборда:
    заголовка, одинакового у любого питон-процесса на свете.

ПОЧЕМУ ТАК ВЫШЛО:
    `strip_ignored_tracebacks` в coder.py специально собирает трейсбек БЛОКОМ:
    шум выбрасывается вместе с телом, настоящая ошибка доходит вместе с телом.
    Строкой ниже результат прогонялся через ПОСТРОЧНЫЙ фильтр ERROR_PATTERNS —
    и тело, только что бережно сохранённое, выбрасывалось:

        Traceback (most recent call last):              ← "Traceback" ✓ остаётся
          File "/app/bot.py", line 412, in transcribe   ← ни одного паттерна ✗
            return r                                    ← ни одного паттерна ✗
        aiogram.…TelegramBadRequest: file is too big    ← "Error:"? нет ✗

    Класс исключения проходил фильтр, только если оканчивался на Error/Exception
    ПЕРЕД двоеточием. TelegramBadRequest, BadRequest, Conflict, Forbidden,
    KeyboardInterrupt — не проходили, и от стека оставался один заголовок.

    Дальше по цепочке всё было честно и всё было бесполезно: регексп для
    `File "…"` не мог совпасть никогда (эти строки уже выброшены), сигнатура
    сходилась к общему хешу у РАЗНЫХ сервисов, а диагносту уходил обезглавленный
    лог — и он делал ровно то, ради чего написан fault_evidence.py: выдумывал
    причину. Обе выдуманные причины 25.08 проверены компилятором и оказались
    ложными; оба бота компилируются и чисты по pyflakes.

ПРАВИЛО:
    Отбор строк отказа обязан быть блочным на всём пути, а не только в одном
    его звене. Улику, собранную блоком и отфильтрованную построчно, теряют
    целиком — и хеш от остатка выглядит как исправная дедупликация.

    И второе: неполный стек бывает по-настоящему (SIGKILL, OOM). Тогда это не
    повод молчать, а факт, который обязан дойти до человека словами, а не
    раствориться в хеше. Отсюда `Basis.complete`.
"""

from __future__ import annotations

import hashlib
import re
from typing import NamedTuple

# Единственный источник паттернов отказа. Переехал сюда из coder.py: там его
# нельзя было покрыть тестом (файл не импортируется), а именно его применение
# и оказалось дефектом.
ERROR_PATTERNS = ["Traceback", "Error:", "Exception:", "CRITICAL", "crashed", "exit code"]

TB_HEADER = "Traceback (most recent call last)"

MAX_SIG_TEXT = 500          # потолок основы сигнатуры, когда трейсбека нет вовсе

_FRAME = re.compile(r'File "([^"]+)", line (\d+)(?:, in (\S+))?')
# Класс исключения берётся СТРУКТУРНО — как первая строка блока без отступа, —
# а имя из неё вытаскивается этим регекспом. Именно попытка узнать исключение
# по написанию имени («…Error», «…Exception») и потеряла TelegramBadRequest.
_EXC_NAME = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*(?::|$)")
_VOLATILE = re.compile(r"0x[0-9a-fA-F]+|\d+")


def _s(x) -> str:
    return str(x if x is not None else "")


def _block_end(logs, i: int) -> int:
    """Индекс ЗА концом блока трейсбека, начатого строкой i.

    Тело — строки с отступом (и пустые); исключение — первая строка без него.
    Та же ходьба, что в coder.py:strip_ignored_tracebacks: расхождение здесь
    означало бы, что шум режется по одной границе, а отбор идёт по другой.
    """
    n = len(logs)
    j = i + 1
    while j < n and (not _s(logs[j]).strip() or _s(logs[j])[:1] in (" ", "\t")):
        j += 1
    return j + 1 if j < n else n


def _has_exc_tail(block: list[str]) -> bool:
    """Дошёл ли блок до строки исключения (последняя строка без отступа)."""
    if len(block) < 2:
        return False
    tail = block[-1]
    return bool(tail.strip()) and tail[:1] not in (" ", "\t")


def unterminated_tail(logs) -> int | None:
    """Индекс заголовка ХВОСТОВОГО трейсбека, который ещё не дописан.

    None — все блоки в списке дошли до строки исключения (или трейсбеков нет).

    Зачем это отдельно от `Basis.complete`: тот отвечает на вопрос «можно ли по
    этому поставить диагноз», и отвечает по всему списку разом. Здесь вопрос
    другой и более ранний — «дочитали ли мы лог до конца отказа», — и ответ
    нужен ТОЛЬКО про хвост: блок в середине списка, за которым идут другие
    строки, уже не дописать, а хвостовой дописывается следующим чтением.

    Инцидент 27–29.08.2026: монитор читает логи по водяному знаку, Railway
    отдаёт каждую строку стека отдельной записью со своей меткой времени, и
    чтение, попавшее в середину выгрузки, забирало заголовок с кадрами без
    строки исключения. Обе половины пропадали: огрызок уходил на разбор без
    исключения, а хвост в следующем цикле приезжал сиротой — без заголовка, и
    ERROR_PATTERNS его не брали («Error:»/«Exception:» есть, а
    «httpx.ReadTimeout:» — нет).
    """
    lines = [_s(x) for x in (logs or [])]
    i, n = 0, len(lines)
    while i < n:
        if TB_HEADER in lines[i]:
            end = _block_end(lines, i)
            if end >= n and not _has_exc_tail(lines[i:end]):
                return i          # блок упёрся в конец списка, исключения нет
            i = end
            continue
        i += 1
    return None


def error_lines(logs, *, ignore=()) -> list[str]:
    """Строки логов, образующие отказ. Трейсбек — целиком, всё прочее — построчно.

    `ignore` сверяется с ОДНОЙ строкой исключения блока, а не со всем телом:
    так же, как в strip_ignored_tracebacks. Паттерн, случайно попавший в кадр
    стека, не имеет права выбросить настоящую ошибку.

    Порядок и повторы сохраняются: это лог, а не множество.
    """
    logs = list(logs or [])
    out: list[str] = []
    i, n = 0, len(logs)
    while i < n:
        line = _s(logs[i])
        if TB_HEADER in line:
            end = _block_end(logs, i)
            block = [_s(x) for x in logs[i:end]]
            # Строка исключения — последняя в блоке, но только если блок вообще
            # до неё дошёл: оборванный стек кончается телом, а не исключением.
            exc_line = block[-1] if _has_exc_tail(block) else ""
            if not (ignore and exc_line and any(p in exc_line for p in ignore)):
                out.extend(block)
            i = end
            continue
        if any(p in line for p in ERROR_PATTERNS) and not any(p in line for p in (ignore or ())):
            out.append(line)
        i += 1
    return out


def frames(lines) -> list[tuple[str, int, str]]:
    """Кадры стека: (файл, номер строки, функция).

    Нужны, чтобы показать модели НУЖНОЕ место файла. До 25.08 в промпт уезжали
    первые 3000 символов исходника — то есть начало файла независимо от того,
    где упало; обе эскалации той ночи именно на это и жаловались.
    """
    text = "\n".join(_s(x) for x in (lines or []))
    return [(m.group(1), int(m.group(2)), m.group(3) or "") for m in _FRAME.finditer(text)]


class Basis(NamedTuple):
    """Из чего сложена сигнатура — и хватило ли на неё улик.

    `complete=False` означает ровно одно: в логе БЫЛ заголовок трейсбека, но ни
    класса исключения, ни кадров из него добыть не удалось. Это не ошибка
    разбора — процесс мог правда умереть от SIGKILL посреди печати стека.
    Это признак, который обязан дойти до эскалации словами.
    """
    exc: str
    file: str
    line: int
    msg: str
    text: str
    complete: bool


# `INFO:aiogram:Update is handled` разбирается регекспом исключения ТОЧНО так
# же, как `KeyError: 'text'`: имя, двоеточие, текст. Если стек оборвался, а
# следом в логе стоит обычная строка логгера, без этого списка она стала бы
# «причиной падения». Ни одно исключение так не называется.
_LOG_LEVELS = frozenset(
    "TRACE DEBUG INFO WARN WARNING ERROR CRITICAL FATAL NOTSET".split()
)


def _exception_line(lines: list[str]) -> tuple[str, str]:
    """(строка исключения, имя класса) последнего трейсбека. ('','') — нет такого.

    Строка исключения принимается только ПОСЛЕ хотя бы одного кадра: CPython
    всегда печатает `File "…"` перед ней, поэтому не-кадр сразу за заголовком
    означает оборванный стек, а не исключение.
    """
    idx = [i for i, ln in enumerate(lines) if TB_HEADER in ln]
    if not idx:
        return "", ""
    block = lines[idx[-1]:]
    seen_frame = False
    for ln in block[1:]:
        if not ln.strip() or ln[:1] in (" ", "\t"):
            seen_frame = seen_frame or bool(_FRAME.search(ln))
            continue
        if not seen_frame:
            return "", ""
        m = _EXC_NAME.match(ln)
        # Не похоже на исключение — значит стек оборвался, а следом в логе
        # просто стоит чужая строка. Выдать её за причину падения нельзя
        # (инвариант офиса №8: провал называет того, кто упал).
        if not m or m.group(1).split(".")[-1].upper() in _LOG_LEVELS:
            return "", ""
        return ln, m.group(1).split(".")[-1]
    return "", ""


def signature_basis(lines) -> Basis:
    """Устойчивая основа сигнатуры: класс исключения + файл + сообщение без чисел.

    Номер строки в основу НЕ входит намеренно: он ездит при каждой правке файла,
    а баг остаётся тем же. В Basis он возвращается отдельно — для окна по файлу.
    """
    lines = [_s(x) for x in (lines or [])]
    has_tb = any(TB_HEADER in ln for ln in lines)
    exc_line, exc = _exception_line(lines)
    fr = frames(lines)
    file_name, line_no = "", 0
    if fr:
        path, line_no, _ = fr[-1]
        file_name = re.split(r"[/\\]", path)[-1]

    msg = _VOLATILE.sub("", exc_line).strip()
    text = "|".join([exc, file_name, msg]).strip("|")
    if not text:
        # Трейсбека нет вовсе (например, синтетические строки из Redis) —
        # хешируем нормализованный текст. Это законный путь, а не отказ.
        text = _VOLATILE.sub("", "\n".join(lines))[:MAX_SIG_TEXT]
    complete = not (has_tb and not exc and not fr)
    return Basis(exc=exc, file=file_name, line=line_no, msg=msg,
                 text=text, complete=complete)


def signature(lines, *, scope: str = "") -> str:
    """md5 основы. При неполной улике подмешивается `scope` (service_id).

    Зачем scope: голый заголовок одинаков у всех питон-процессов офиса, и без
    примеси разные баги разных сервисов сходятся в один хеш. Хуже того, дальше
    по цепочке «та же сигнатура в 3+ сервисах» считается системным шумом и
    ГАСИТ эскалацию — то есть коллизия не просто путает счётчики, а выключает
    монитор. Полная улика в scope не нуждается: она и так различает сервисы,
    и общий хеш у неё означает настоящий общий баг.
    """
    b = signature_basis(lines)
    basis = b.text if b.complete else f"{scope}|{b.text}"
    return hashlib.md5(basis.encode()).hexdigest()

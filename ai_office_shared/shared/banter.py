"""
ai_office_shared.shared.banter — болталка офис-чата (реплики ботов друг другу).

ПОЧЕМУ ЭТО ЖИВЁТ ЗДЕСЬ, А НЕ В ХЕНДЛЕРАХ БОТОВ:
    Telegram НЕ доставляет боту сообщения других ботов. Поэтому весь код вида
    «если отправитель бот — ответить с шансом N%», который был написан в
    billy/kriss/gosling/villy/doctor/milly/tilly/prophet, никогда не исполнялся:
    апдейт до бота просто не доходит. Отсюда жалоба «Гослинг вообще не отвечает
    Билли» — ветка `is_billy → отвечаем всегда` мёртвая.

    Единственный работающий канал — HTTP: кто-то должен ПОЗВАТЬ бота по /task.
    Оркестрацию делает Филли после того, как основной агент ответил в группе.
    Раньше эта логика лежала внутри filly-bot одной функцией `_banter_fanout` и
    текла в трёх местах: Милли и Тилли реплику генерировали и молча выбрасывали
    (их /task постил в группу только при notify=True), Пророка в пуле не было, а
    счётчика глубины не существовало вовсе — «перекинуться 1-2 фразами и
    затихнуть» реализовать было нечем.

КОНТРАКТ /task ДЛЯ БОЛТАЛКИ:
    {"message": ..., "user_id": ..., "group_ctx": ..., "source": "BANTER",
     "sender": "<кто спровоцировал>", "depth": <1..BANTER_MAX_DEPTH>}
    Принимающий бот обязан: ответить КОРОТКО и запостить в группу сам
    (source=BANTER игнорирует флаг notify).

ИЗ ЧЕГО СКЛАДЫВАЕТСЯ КАЧЕСТВО РЕПЛИКИ (а не сам факт её появления):
    Механика — «дошёл ли пинг» — работает с 16.08: обе волны отрабатывают,
    отказов ноль. Разговором это делают четыре вещи, и все четыре про то, ЧТО
    именно уезжает в `message`:

    1. Званый видит чат, а не одну строку. Раньше в промт шёл только
       trigger_text — сообщение ВЛАДА. Ответ основного агента, ради которого
       болталку и запускают, до званых не доходил, и первая волна отвечала
       человеку хором. Теперь транскрипт собирается из `office:group:history`
       (туда пишут и Филли за человека, и каждый бот за себя) — см. _chat_tail.
    2. Внутри одной волны боты идут последовательно и второй видит первого.
       Просьба «не повторяй уже сказанное» стояла в промте с самого начала, но
       была невыполнима: текст собирался один раз до цикла.
    3. Реплики режутся по границе слова (clip), а не срезом по счётчику.
    4. Правила выписаны поштучно (BANTER_RULES), включая «реплика адресована не
       тебе — не отвечай за адресата»: 16.08 Милли получила чужое
       «Билли, ты ...» и ответила так, будто Билли — это она.

    Плюс потолок частоты (BANTER_COOLDOWN): шанс отвечает на вопрос «шуметь ли
    сейчас», но ни на что не отвечает «не шумели ли мы секунду назад».

Использование:
    from ai_office_shared.shared.banter import fanout
    spawn(fanout(redis, primary_agent="БИЛЛИ", trigger_text=msg,
                 group_ctx=ctx, sender="Влад"))
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import uuid
import re

import httpx

from .identity import canonical, display, route_key, url as bot_url, who_is
from .models import MODEL_HAIKU

logger = logging.getLogger("ai_office_shared.banter")

# Шанс, что после ответа основного агента кто-то ещё вставит реплику.
#
# Было 0.35 и это оказалось «никогда». Замер 15.08 по логам Филли: за ТРИ дня
# она сроутила в группе ровно два сообщения — офис-чат тихий, Влад в основном
# пишет ботам в личку, а болталка запускается только после группового роутинга.
# Два броска по 0.35 дают 0.42 вероятности, что не сработает ни разу; так и
# вышло — ноль пингов за весь срок жизни деплоя. Частота события, которое и так
# случается раз в сутки, не должна дополнительно резаться втрое.
BANTER_CHANCE = float(os.environ.get("BANTER_CHANCE", "0.7"))

# ПОТОЛОК, а не порог. BANTER_CHANCE отвечает на вопрос «шуметь ли сейчас», но
# ни на что не отвечает вопрос «не шумели ли мы только что». На 0.9 (значение в
# проде) это значит: три сообщения Влада подряд — три всплеска подряд, каждый на
# 2-4 реплики. Ровно тот класс ошибки, что и алерт без дедупа: условие есть,
# ограничения сверху нет. Лечится так же — SET NX с TTL.
#
# 60 с выбраны по живым данным 16.08: два всплеска были в 08:42 и 08:44, обоих
# окно не тронуло бы. Режется только «пулемётный» случай — всплеск на каждое
# сообщение в очереди подряд. 0 отключает потолок совсем.
BANTER_COOLDOWN = int(os.environ.get("BANTER_COOLDOWN", "60"))

# Кого зовём. Решение Влада 2026-08-12: офисное ядро + Пророк.
# Эллис не участвует (семейный контекст в общей группе) и Силли тоже
# (её реплики путались бы с алертами о падениях).
BANTER_POOL: list[str] = ["БИЛЛИ", "КРИС", "ГОСЛИНГ", "МИЛЛИ",
                          "ВИЛЛИ", "ТИЛЛИ", "ДИЛЛИ", "ПРОРОК"]

# Чем занят каждый — единственное, что отличает «уместную реплику» от дежурной.
# Строка идёт в промт распорядителя (RANK_SYSTEM); держим её здесь, а не в
# identity, потому что это роль в БОЛТАЛКЕ, а не должность в офисе.
_SPECIALITY: dict[str, str] = {
    "БИЛЛИ":   "прямой практик без церемоний, режет правду",
    "КРИС":    "личный ассистент Влада: заметки, расписание, быт",
    "ГОСЛИНГ": "чатовый персонаж, разряжает обстановку, не ассистент",
    "МИЛЛИ":   "бизнес и деньги: результат, монетизация, цифры",
    "ВИЛЛИ":   "дизайн и визуал: вкус, интерфейсы, критика глазами",
    "ТИЛЛИ":   "трейдинг и рынки: сделки, риск, статистика",
    "ДИЛЛИ":   "здоровье: сон, тренировки, восстановление",
    "ПРОРОК":  "агрегатор мнений, говорит обобщениями и прогнозами",
}

BANTER_PROMPT = (
    "[Болталка офис-чата] Это не задача и не вопрос к тебе — просто живой чат "
    "коллег. Кинь ОДНУ короткую реплику (одна строка) в своём характере: можно "
    "поддеть, съязвить, согласиться. Без приветствий, без развёрнутых ответов, "
    "не повторяй уже сказанное. Если добавить нечего — ответь одним словом."
)

# Правила, которые пришлось выписать поштучно — общая просьба «одна короткая
# реплика» их не покрывала, и качество плавало от волны к волне:
#   • «реагируй на последнюю» — без этого бот отвечал на весь блок сразу и
#     выходила сводка беседы, а не реплика;
#   • «реплика адресована не тебе» — 16.08 Милли получила чужое «Билли, ты
#     спиздел» и ответила так, будто Билли это она. Адресат в болталке почти
#     никогда не совпадает с получателем пинга: зовём мы третьего;
#   • «не повторяй» стояло и раньше, но было невыполнимо — бот не видел, что
#     сказали до него. Теперь видит (см. _chat_tail), и правило имеет смысл;
#   • потолок в символах — «коротко» модель понимает как три предложения.
BANTER_RULES = (
    "Твоя очередь. Правила:\n"
    "— ОДНА строка, до ~120 символов, без переносов;\n"
    "— реагируй на ПОСЛЕДНЮЮ реплику, а не на весь разговор;\n"
    "— если она адресована не тебе — вставь своё со стороны, "
    "не отвечай за того, кому она адресована, и не выдавай себя за него;\n"
    "— не повторяй то, что уже сказано выше: ни мысль, ни формулировку;\n"
    "— без приветствий, без «чем помочь», без разбора задачи и без выводов;\n"
    "— не подписывайся своим именем — его подставят за тебя;\n"
    "— добавить нечего — ответь одним словом."
)

# Правила прямого ответа. Отличаются от BANTER_RULES ровно двумя строками:
# снята «вставь со стороны» (она и запрещала диалог) и добавлено разрешение
# задать встречный вопрос — именно вопрос продлевает нить, см. invites_reply.
BANTER_RULES_REPLY = (
    "Твоя очередь. Правила:\n"
    "— ОДНА строка, до ~120 символов, без переносов;\n"
    "— отвечай ИМЕННО ему и ИМЕННО на последнюю реплику;\n"
    "— можно задать встречный вопрос, если разговор того стоит;\n"
    "— не повторяй то, что уже сказано выше: ни мысль, ни формулировку;\n"
    "— без приветствий, без «чем помочь», без разбора задачи и без выводов;\n"
    "— не подписывайся своим именем — его подставят за тебя;\n"
    "— добавить нечего — ответь одним словом."
)

# Сколько последних реплик чата показываем. Четыре — это «Влад + ответ агента +
# одна-две реплики болталки», то есть ровно текущий всплеск. Больше — бот
# начинает отвечать на позавчерашнее, меньше — теряется, кто кому что сказал.
BANTER_CTX_LINES = 6

# Потолок глубины стал именно ПОТОЛКОМ, а не длиной. Раньше всплеск всегда был
# ровно две волны: «перекинуться парой фраз» реализовано числом. Настоящий
# разговор так не устроен — он кончается там, где кончился, а не на втором
# такте. Теперь длину решает `invites_reply()` (вопрос или обращение по имени →
# продолжаем), а это число только не даёт нити уйти в бесконечность.
BANTER_MAX_DEPTH = int(os.environ.get("BANTER_MAX_DEPTH", "4"))

# Потолок длины одной строки контекста.
_LINE_LIMIT = 200

# Короче этого реплика не годится в затравку второй волны: на «ага» вторая волна
# отвечает «ага» — это шум, а не разговор. Молчание тут лучше.
_SEED_MIN_CHARS = 12

# Почему последний pick никого не вернул — читает fanout, чтобы положить причину
# в office:logs. Модульная переменная, а не возврат из pick: сигнатура pick уже
# зафиксирована тестами и внешними вызывающими, а причина нужна только для лога.
last_pick_reason: dict[str, str] = {"value": ""}

# Дедуп нити: кто уже вставил реплику в текущий всплеск разговора.
_THREAD_KEY = "office:banter:thread"
_THREAD_TTL = 300  # 5 минут — всплеск закончился, можно чирикать заново

# Потолок частоты всплесков (см. BANTER_COOLDOWN).
_COOLDOWN_KEY = "office:banter:cooldown"


# ── Идентификатор всплеска ───────────────────────────────────────────────────
# Всплеск — это разговор, у разговора есть начало и участники. До 13.09.2026
# такого объекта не было: `thread_id` во всех вызовах равнялся строке "office",
# то есть один на всю историю офиса. Следствий было два, и оба видны в логах:
# транскрипт склеивал реплики из РАЗНЫХ всплесков (см. group_history.push), а
# дедуп «кто уже говорил» жил 5 минут поверх всех всплесков сразу — второй
# разговор начинался с половиной пула, выбывшей в первом.
def new_thread_id() -> str:
    """Идентификатор нового всплеска. Короткий — он ездит в payload и в лог."""
    return uuid.uuid4().hex[:12]


def thread_of(task_data: dict) -> str:
    """Нить из payload `/task`. Пусто — вызов не из болталки."""
    return str((task_data or {}).get("thread_id") or "").strip()


# ── Приглашает ли реплика к ответу ───────────────────────────────────────────
# Условие продолжения нити. Детерминированное намеренно: спрашивать модель
# «продолжать ли» значит платить вызовом за решение, которое читается из текста,
# и получать в ответ вежливое «да» (модель склонна соглашаться с продолжением).
# Обращение — это звательная позиция, а не всякое упоминание имени. Ловим имя
# в начале строки либо после запятой, и обязательно с запятой/двоеточием следом:
# «Гослинг, ты где», «…комплимент, Тилли, скажи честно?», «Тилли: рынок стоит».
#
# Почему не просто «имя где угодно»: «я спросил Гослинга вчера» — это рассказ о
# коллеге, а не вопрос к нему, и позвать по нему Гослинга значит сделать ровно
# то, от чего уходим — реплику мимо разговора. Запятые тут и есть разделитель
# между обращением и упоминанием (инвариант №7: совпадение по слову, не по
# подстроке).
_ADDRESS_RE = re.compile(r"(?:^|,)\s*([А-ЯЁA-Z][\wа-яё]+)\s*[,:]", re.UNICODE)


def invites_reply(text: str, pool: list[str] | None = None) -> tuple[bool, str]:
    """
    Зовёт ли эта реплика кого-то ответить, и кого именно.

    Returns:
        (продолжать, кому). Кому — route_key адресата, если он назван по имени
        и он из пула; пусто — продолжаем без конкретного адресата.

    Два признака, оба про форму, а не про смысл:
      • вопрос — знак вопроса в конце;
      • обращение — реплика начинается с имени коллеги через запятую или
        двоеточие («Гослинг, ты где?»).

    Утверждение, ни к кому не обращённое, нить закрывает. Это и есть
    естественный конец разговора: человек, которому нечего добавить, молчит,
    а не произносит ещё одну реплику.
    """
    t = " ".join(str(text or "").split())
    if not t:
        return False, ""

    target = ""
    m = _ADDRESS_RE.search(t)
    if m:
        key = _norm(m.group(1))
        if key and key in [_norm(x) for x in (pool if pool is not None else BANTER_POOL)]:
            target = key

    return (bool(target) or t.endswith("?")), target


# ── Гейт повторов ────────────────────────────────────────────────────────────
def repeats_existing(text: str, lines: list[tuple[str, str]]) -> bool:
    """
    Повторяет ли реплика то, что уже сказано в этом всплеске.

    Правило «не повторяй то, что уже сказано выше» стоит в BANTER_RULES и
    выполняется не всегда: 13.09.2026 на «кто на месте» три бота подряд начали
    с «На месте». Просьба — не гейт; здесь она становится проверкой.

    Сравниваем по началу строки (`_same_line` уже так устроен): совпадение
    первых слов — это и есть тот самый хор. Смысловое сходство не ловим
    намеренно — для него нужна модель, а ошибаться в сторону съеденной живой
    реплики дороже, чем пропустить один повтор (развилка phantom.py).
    """
    return any(_same_line(t, text) for _, t in lines)


def clip(text: str, limit: int = _LINE_LIMIT) -> str:
    """
    Обрезка реплики по границе слова.

    Было `text[:200]` в двух местах, и обе обрезки уезжали в промт следующего
    бота. Реплику рубило на полуслове («...я же говорил что деплой упа»), и
    коллега реагировал на огрызок — со стороны это выглядит как бот, который не
    дочитал. Многострочный ответ схлопываем в одну строку: транскрипт строится
    как «Кто: что», перенос внутри реплики ломает эту разметку.
    """
    t = " ".join(str(text or "").split())
    if len(t) <= limit:
        return t
    cut = t[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:     # слово-монстр на весь лимит не режем по букве
        cut = cut[:space]
    return cut.rstrip(" ,.;:—-") + "…"


def _same_line(a: str, b: str) -> bool:
    """
    Одна и та же реплика или нет. Сравниваем начало без регистра и пробелов:
    в ленте текст обрезан на 300 символах, в trigger_text приходит целиком, и
    строгое равенство их не сматчит — строка задвоится в транскрипте.
    """
    na = " ".join(str(a or "").lower().split())[:60]
    nb = " ".join(str(b or "").lower().split())[:60]
    return bool(na) and na == nb


def _add_line(lines: list[tuple[str, str]], who: str, text: str) -> None:
    """Дописать реплику в транскрипт, если такой там ещё нет."""
    who = str(who or "").strip()
    text = str(text or "").strip()
    if not who or not text:
        return
    if any(_same_line(t, text) for _, t in lines):
        return
    lines.append((who, clip(text)))


def strip_self_prefix(text: str, speaker: str) -> str:
    """
    «Милли: ага, и не говори» → «ага, и не говори».

    Имя автора в транскрипт подставляем мы. Если модель подписалась сама (а
    просьба «не подписывайся» выполняется не всегда), во второй волне выходит
    «Милли: Милли: ...» — и следующий бот честно принимает это за цитату
    цитаты.
    """
    disp = display(speaker) or str(speaker or "")
    if not disp:
        return str(text or "").strip()
    # Хвост после двоеточия — только markdown-подчёркивание. «**Милли:** ага»
    # раньше давало «** ага»: двоеточие стоит ВНУТРИ жирного, и закрывающие
    # звёздочки оставались в тексте (13.09.2026). Брать здесь \W нельзя —
    # съест осмысленную пунктуацию: «Милли: — да» превратилось бы в «да».
    return re.sub(rf"^\W{{0,3}}{re.escape(disp)}\W{{0,3}}:[*_~`]{{0,2}}\s*", "",
                  str(text or "").strip(), count=1, flags=re.IGNORECASE)


async def _chat_tail(redis_client, n: int = BANTER_CTX_LINES,
                     thread_id: str = "") -> list[tuple[str, str]]:
    """
    Последние реплики офис-группы как [(кто, что)] — хронологически.

    Источник — `office:group:history`, куда пишут И Филли за человека, И каждый
    бот за себя. Раньше болталка не читала ленту вообще: в промт уезжало только
    `trigger_text` — сообщение ВЛАДА. То есть все званые видели реплику
    человека и не видели, что на неё уже ответил основной агент, и отвечали
    Владу хором. Вторая волна (#60) чинила это только на втором шаге; первая
    так и оставалась хором.
    """
    if redis_client is None or n <= 0:
        return []
    try:
        from .group_history import read
        rows = await read(redis_client, n, thread_id=thread_id)
    except Exception as e:
        logger.info(f"[banter] лента недоступна, контекст только из trigger_text: {e}")
        return []
    out: list[tuple[str, str]] = []
    for row in (rows or []):
        who = str((row or {}).get("from") or "").strip()
        what = str((row or {}).get("text") or "").strip()
        if who and what:
            _add_line(out, who_is(who) or who, what)
    return out[-n:]


def visible_lines(lines: list[tuple[str, str]],
                  target: str = "") -> list[tuple[str, str]]:
    """
    Что именно покажем этому боту.

    Args:
        target: кого зовём (route_key). Хвост его собственных реплик снимаем:
                pick() ленту не читает и вполне может позвать того, кто минуту
                назад сам написал в группу, — просить его отреагировать на
                самого себя бессмысленно.
    """
    shown = list(lines)
    if target:
        me = display(target) or target
        while len(shown) > 1 and shown[-1][0] == me:
            shown.pop()
    return shown[-max(1, BANTER_CTX_LINES):]


def last_speaker(lines: list[tuple[str, str]], target: str = "",
                 default: str = "") -> str:
    """
    С кем этот бот сейчас разговаривает — автор последней ПОКАЗАННОЙ ему строки.

    Это же значение уезжает в payload как `sender`, и оно обязано совпадать с
    транскриптом. Раньше не совпадало: Филли ставила sender="Влад" на всю
    первую волну, принимающий бот подставлял «[от Влад]» в свой промт — и
    получал инструкцию говорить с человеком поверх реплики, которую последним
    написал коллега. Ровно то расхождение, из-за которого волна выходила хором
    в сторону Влада.
    """
    shown = visible_lines(lines, target)
    return shown[-1][0] if shown else default


def build_message(lines: list[tuple[str, str]], target: str = "",
                  addressee: str = "") -> str:
    """
    Промт для одного званого бота: транскрипт + правила.

    Args:
        lines:  [(кто, что)] хронологически, последняя реплика — внизу.
        target: кого зовём (route_key), см. visible_lines().
        addressee: кому этот бот отвечает. Пусто — реплика «со стороны», как
                и было; заполнено — прямой ответ конкретному коллеге.

                Смешанный режим: первая волна комментирует, дальше идёт
                диалог. Без него все волны были комментарием сбоку — правило
                BANTER_RULES прямо велит «вставь своё со стороны, не отвечай
                за того, кому адресовано», — и всплеск читался как N человек,
                говорящих об одной строке, а не друг с другом.
    """
    shown = visible_lines(lines, target)
    if not shown:
        return f"{BANTER_PROMPT}\n\n{BANTER_RULES}"

    transcript = "\n".join(f"{who}: {what}" for who, what in shown)
    last = shown[-1][0]
    if addressee:
        who = display(addressee) or addressee
        return (
            f"{BANTER_PROMPT}\n\n"
            f"Что сейчас в чате (внизу — последняя реплика, её автор: {last}):\n"
            f"{transcript}\n\n"
            f"Тебе отвечает {who} — обратись к нему НАПРЯМУЮ, как в живом "
            f"разговоре: подхвати сказанное, согласись или возрази. "
            f"Не комментируй со стороны.\n\n"
            f"{BANTER_RULES_REPLY}"
        )
    return (
        f"{BANTER_PROMPT}\n\n"
        # «автор: Гослинг», а не «она от Гослинг»: имена в реестре лежат в
        # именительном падеже, а предлог требует родительного. Двоеточие
        # снимает вопрос склонения — и заодно вопрос рода, который у «его
        # реплика» встал бы на Милли и Тилли.
        f"Что сейчас в чате (внизу — последняя реплика, её автор: {last}):\n"
        f"{transcript}\n\n"
        f"{BANTER_RULES}"
    )


async def _cooldown_claim(redis_client, thread_id: str) -> bool:
    """
    Занять окно всплеска. True — шуметь можно, False — только что уже шумели.

    SET NX EX, одной операцией: проверка «а не занято ли» отдельным вызовом
    оставила бы щель между чтением и записью. Занимаем ПОСЛЕ броска шанса —
    иначе несработавший бросок молча съедал бы чужое окно; и ДО pick — если
    всех кандидатов отфильтровали, всплеск всё равно состоялся как попытка.

    Fail-open: болталка не тот путь, ради которого стоит падать из-за Redis.
    """
    if redis_client is None or BANTER_COOLDOWN <= 0:
        return True
    try:
        got = await redis_client.set(f"{_COOLDOWN_KEY}:{thread_id}", "1",
                                     nx=True, ex=BANTER_COOLDOWN)
        return bool(got)
    except Exception:
        return True


async def _thread_members(redis_client, thread_id: str) -> set[str]:
    if redis_client is None:
        return set()
    try:
        raw = await redis_client.smembers(f"{_THREAD_KEY}:{thread_id}")
        return {r.decode() if isinstance(r, bytes) else r for r in (raw or set())}
    except Exception:
        return set()


async def _thread_mark(redis_client, thread_id: str, agents: list[str]) -> None:
    if redis_client is None or not agents:
        return
    try:
        key = f"{_THREAD_KEY}:{thread_id}"
        await redis_client.sadd(key, *agents)
        await redis_client.expire(key, _THREAD_TTL)
    except Exception:
        pass


def _norm(agent: str) -> str | None:
    """Любое написание → route_key, которым бот адресуется по HTTP."""
    canon = canonical(agent)
    return route_key(canon) if canon else None


RANK_SYSTEM = (
    "Ты распорядитель офисного чата. Тебе дают кусок разговора и список коллег, "
    "каждый со своей специальностью. Выбери ОДНОГО-ДВУХ, кому по этой теме "
    "действительно есть что сказать — чью реплику читатель воспримет как "
    "уместную, а не как дежурную. Верни ТОЛЬКО имена через запятую, без "
    "пояснений. Если тема никого конкретно не касается — верни одно любое имя."
)


async def rank_speakers(client, lines: list[tuple[str, str]],
                        candidates: list[str], model: str = "") -> list[str]:
    """
    Кому из кандидатов есть что сказать по теме. Пусто — решай без нас.

    ЗАЧЕМ. Раньше выбор был `random.shuffle` по восьми: сообщение про дизайн с
    равной вероятностью вытягивало Тилли и Вилли. Характеры у ботов есть, а к
    теме они не применялись никак — отсюда ощущение, что отвечают не те.

    Fail-open во всём: нет клиента, модель молчит, вернула мусор или незнакомые
    имена — возвращаем пустой список, и `pick` бросает жребий как раньше.
    Болталка не тот путь, ради которого стоит падать; худший исход здесь —
    прежнее поведение, а не тишина.
    """
    if client is None or not candidates:
        return []
    roster = []
    for key in candidates:
        who = display(key) or key
        roster.append(f"{who} — {_SPECIALITY.get(_norm(key) or '', 'офис')}")
    transcript = "\n".join(f"{who}: {what}" for who, what in lines[-BANTER_CTX_LINES:])
    try:
        r = await client.messages.create(
            model=model or MODEL_HAIKU,
            max_tokens=60,
            system=RANK_SYSTEM,
            messages=[{"role": "user",
                       "content": f"Разговор:\n{transcript}\n\nКоллеги:\n"
                                  + "\n".join(roster)}],
        )
        raw = (r.content[0].text or "").strip()
    except Exception as e:
        logger.info(f"[banter] ранжирование недоступно, бросаем жребий: {e}")
        return []

    out: list[str] = []
    for part in re.split(r"[,;\n]+", raw):
        key = _norm(part.strip(" .*-—"))
        if key and key in candidates and key not in out:
            out.append(key)
    if not out:
        logger.info("[banter] ранжирование не назвало никого из пула: %r", raw[:120])
    return out[:2]


async def pick(
    redis_client,
    primary_agent: str,
    thread_id: str = "office",
    pool: list[str] | None = None,
    limit: int | None = None,
    health_check=None,
    client=None,
    lines: list[tuple[str, str]] | None = None,
    allow_repeat: bool = False,
) -> list[str]:
    """
    Кого позвать в этот раз. Вынесено отдельно от отправки, чтобы это можно было
    протестировать без сети.

    Исключаются: сам основной агент, боты без URL, те кто уже говорил в этой
    нити, и (если передан health_check) отмеченные как down.
    """
    primary = _norm(primary_agent)
    # Отсев ведём со СЧЁТЧИКОМ причин. Раньше кандидаты выбрасывались молча по
    # трём разным поводам, и «в чате тишина» было неотличимо от «всех отфильтровали»
    # и от «не сработал бросок». Отладить это было нечем.
    dropped = {"сам": 0, "нет url": 0, "уже говорил": 0, "health=down": 0}
    candidates = []
    for name in (pool if pool is not None else BANTER_POOL):
        key = _norm(name)
        if not key or key == primary:
            dropped["сам"] += 1
            continue
        if not bot_url(key):
            dropped["нет url"] += 1
            continue
        candidates.append(key)

    # Дедуп нити держит одного бота от того, чтобы говорить весь всплеск.
    # Но в ДИАЛОГЕ те же двое говорят по очереди — там это правило запрещало бы
    # ровно то, ради чего диалог и заводится. Поэтому обращение по имени его
    # снимает: назвали — отвечает, сколько бы раз он уже ни говорил.
    if not allow_repeat:
        already = await _thread_members(redis_client, thread_id)
        before = len(candidates)
        candidates = [c for c in candidates if c not in already]
        dropped["уже говорил"] = before - len(candidates)

    if health_check is not None:
        alive = []
        for c in candidates:
            try:
                if (await health_check(c)) != "down":
                    alive.append(c)
                else:
                    dropped["health=down"] += 1
            except Exception:
                alive.append(c)  # нет данных — считаем живым, пусть HTTP решит
        candidates = alive

    if not candidates:
        logger.info("[banter] некого звать: отсев %s",
                    {k: v for k, v in dropped.items() if v})
        last_pick_reason["value"] = f"некого звать ({dropped})"
        return []
    # Кого именно — решает распорядитель, если он доступен. Жребий остаётся
    # запасным путём, а не основным: он не знает темы и ровно поэтому звал
    # Тилли на разговор про дизайн.
    if client is not None and lines:
        ranked = await rank_speakers(client, lines, candidates)
        if ranked:
            last_pick_reason["value"] = f"ранжирование: {','.join(ranked)}"
            return ranked[:(limit if limit is not None else len(ranked))]

    # Порядок вызовов random здесь значим: shuffle ДО randint. Не ради
    # красоты — тесты болталки не сеют генератор, и перестановка этих двух
    # строк меняет всю последующую последовательность. Латентно флаки тест
    # (`randint` и так может вернуть 1) от этого становится видимо флаки, и
    # разбираться приходится не с тем.
    random.shuffle(candidates)
    return candidates[:(limit if limit is not None else random.randint(1, 2))]


async def fanout(
    redis_client,
    primary_agent: str,
    trigger_text: str,
    group_ctx: str = "",
    sender: str = "",
    depth: int = 0,
    user_id: int = 0,
    thread_id: str = "office",
    chance: float | None = None,
    health_check=None,
    pool: list[str] | None = None,
    context_lines: list[tuple[str, str]] | None = None,
    client=None,
    addressee: str = "",
    allow_repeat: bool = False,
) -> list[str]:
    """
    Fire-and-forget: с шансом BANTER_CHANCE зовём 1–2 живых бота кинуть реплику.

    Args:
        primary_agent: кто только что ответил — его самого не зовём.
        depth:         глубина текущей нити. На depth >= BANTER_MAX_DEPTH
                       фанаут не запускается — это и есть «затихнуть».
        sender:        кто спровоцировал реплику (Влад / имя бота) — уезжает в
                       payload, чтобы отвечающий знал, с кем говорит.
        health_check:  async (route_key) -> "up"/"down"/None. Опционально.
        context_lines: [(кто, что)] — реплики, которые вызывающий уже знает, но
                       которых может ещё не быть в ленте группы. Так вторая
                       волна получает первую целиком, не полагаясь на то, что
                       позванный бот успел записать себя в `office:group:history`
                       до того, как отдал HTTP-ответ. Дубли лента и этот список
                       переживают: строки склеиваются по тексту.

    Returns:
        Список реально пинганутых ботов (пустой — если не сработал шанс,
        некого звать или достигнут потолок глубины).
    """
    async def _note(event: str, **fields) -> None:
        """
        Решение болталки — в office:logs, а не только в stdout Филли.

        Влад смотрит Log-бот, а не Railway. Пока решение видел только stdout,
        «в чате тишина» было неотличимо от «фича сломана»: 15.08 болталка
        отработала два раза и оба смолчала, и понять это можно было лишь
        сравнив два фильтра по логам деплоя. Тот же класс, что молчаливый
        отказ вайтлиста — сбой без следа не отлаживается.
        """
        try:
            from .logging import log_event
            await log_event(redis_client, "филли", event, **fields)
        except Exception:
            pass

    try:
        if depth >= BANTER_MAX_DEPTH:
            logger.info(f"[banter] depth {depth} >= {BANTER_MAX_DEPTH} — затихаем")
            await _note("banter_skip", reason="depth", depth=depth)
            return []

        roll = BANTER_CHANCE if chance is None else chance
        if random.random() >= roll:
            logger.info(f"[banter] бросок не прошёл (шанс {roll})")
            await _note("banter_skip", reason="chance", chance=roll)
            return []

        # Потолок частоты — только на первой волне: вторая волна идёт внутри
        # уже занятого окна и не должна упираться в замок, который поставила
        # сама же первая.
        if depth == 0 and not await _cooldown_claim(redis_client, thread_id):
            logger.info(f"[banter] всплеск был меньше {BANTER_COOLDOWN} с назад — молчим")
            await _note("banter_skip", reason="cooldown", cooldown=BANTER_COOLDOWN)
            return []

        # Транскрипт всплеска: лента группы + то, ради чего нас позвали.
        # trigger_text добавляем последним и только если его в ленте ещё нет —
        # Филли пишет сообщение человека в ленту сама, и без дедупа последняя
        # строка задваивалась.
        #
        # Лента читается ПО НИТИ: транскрипт должен быть одним разговором, а не
        # окном по всей истории офиса. Если нить пуста (первая волна — реплики
        # в неё ещё не записаны), падаем на общий хвост.
        #
        # Читаем ДО pick: распорядителю нужен текст разговора, чтобы понять,
        # кому по этой теме есть что сказать.
        lines = await _chat_tail(redis_client, BANTER_CTX_LINES,
                                 thread_id=thread_id)
        if not lines:
            lines = await _chat_tail(redis_client, BANTER_CTX_LINES)
        for _who, _what in (context_lines or []):
            _add_line(lines, _who, _what)
        _add_line(lines, who_is(sender) or sender or "Влад", trigger_text)

        last_pick_reason["value"] = ""
        chosen = await pick(redis_client, primary_agent, thread_id=thread_id,
                            pool=pool, health_check=health_check,
                            client=client, lines=lines,
                            allow_repeat=allow_repeat)
        if not chosen:
            await _note("banter_skip", reason="no_candidates",
                        detail=last_pick_reason["value"][:200])
            return []

        await _thread_mark(redis_client, thread_id, chosen)

        from .auth import office_headers

        pinged: list[str] = []
        failed: list[str] = []
        replies: list[tuple[str, str]] = []
        for agent in chosen:
            target = bot_url(agent)
            if not target:
                continue
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as c:
                    resp = await c.post(f"{target}/task", headers=office_headers(), json={
                        # Промт собираем на КАЖДОГО заново, а не один раз до
                        # цикла. Внутри одной волны боты идут последовательно, и
                        # раньше второй получал ровно тот же текст, что первый:
                        # правило «не повторяй уже сказанное» он выполнить не
                        # мог физически — сказанного он не видел. Отсюда пары
                        # реплик об одном и том же разными словами.
                        "message":   build_message(lines, target=agent,
                                                   addressee=addressee),
                        "user_id":   user_id,
                        "group_ctx": group_ctx,
                        "source":    "BANTER",
                        # sender = автор последней показанной строки, а не тот,
                        # с кого начался всплеск: принимающий бот кладёт его в
                        # промт как «[от X]», и разойтись с транскриптом это
                        # поле не имеет права.
                        "sender":    last_speaker(lines, target=agent,
                                                  default=sender),
                        "depth":     depth + 1,
                        # Нить едет к боту: он запишет свою реплику в ленту с
                        # этим id, и следующая волна прочитает ОДИН разговор,
                        # а не окно по всей истории офиса.
                        "thread_id": thread_id,
                    })
                # Код ответа проверяем ЯВНО. Раньше pinged.append выполнялся
                # независимо от него: при 401 (а он появится, как только
                # включат OFFICE_RPC_STRICT) лог написал бы «позвал МИЛЛИ», в
                # чате было бы пусто, и следующий разбор ушёл бы искать баг у
                # Милли вместо auth. Ложный успех хуже молчания — молчание не
                # уводит по ложному следу.
                if resp.status_code not in (200, 202):
                    logger.warning("[banter] %s ответил %s — реплики не будет",
                                   agent, resp.status_code)
                    failed.append(f"{agent}:{resp.status_code}")
                    continue
                pinged.append(agent)
                # Ответ бота — повод для ВТОРОЙ волны: пусть коллега отреагирует
                # на коллегу, а не снова на Влада. Без этого depth никогда не
                # доходил до 2, хотя BANTER_MAX_DEPTH=2 заведён с самого начала:
                # каждый бот получал пинг с «Последним говорил: Влад» и отвечал
                # человеку. Со стороны это выглядит как коллеги, говорящие
                # хором в одну сторону, а не как разговор между собой.
                try:
                    _reply = str((resp.json() or {}).get("response", "") or "").strip()
                except Exception:
                    _reply = ""
                _reply = strip_self_prefix(_reply, agent)
                # Гейт повторов. Правило «не повторяй сказанное» стоит в
                # BANTER_RULES и выполняется не всегда: 13.09.2026 на «кто на
                # месте» три бота подряд начали с «На месте». Реплику-повтор в
                # разговор не пускаем — в ленту она уже не попадёт (бот постит
                # сам, но вторая волна на неё не сошлётся) и затравкой не
                # станет. Перегенерацию не заказываем: лишний вызов ради
                # строки, которой лучше не быть, — это шум за деньги.
                if _reply and repeats_existing(_reply, lines):
                    logger.info("[banter] %s повторил уже сказанное — не считаем",
                                agent)
                    await _note("banter_repeat", agent=str(agent),
                                detail=clip(_reply, 80))
                    _reply = ""
                if _reply:
                    replies.append((agent, _reply))
                    # Следующий в этой же волне увидит сказанное как реплику
                    # чата — иначе он говорит вслепую поверх коллеги.
                    _add_line(lines, display(agent) or agent, _reply)
                logger.info(f"[banter] pinged {agent} (depth={depth + 1})")
            except Exception as e:
                logger.info(f"[banter] {agent} ping failed: {e}")
                failed.append(f"{agent}:{type(e).__name__}")
            await asyncio.sleep(random.uniform(1.5, 4.0))  # разносим во времени
        await _note("banter_ping", agents=",".join(pinged) or "нет",
                    failed=",".join(failed) or "нет",
                    primary=str(_norm(primary_agent) or primary_agent),
                    depth=depth + 1)

        # ── Следующая волна: разговор продолжается, пока его продолжают ──
        # Раньше волн было ровно две — «перекинуться парой фраз» было записано
        # числом. Теперь длину решает сама реплика: `invites_reply` смотрит,
        # есть ли в ней вопрос или обращение по имени. Утверждение, ни к кому
        # не обращённое, нить закрывает — это и есть естественный конец
        # разговора. BANTER_MAX_DEPTH остаётся потолком, а не длиной.
        #
        # В затравку годится не всякий ответ. Промт сам разрешает «нечего
        # добавить — ответь одним словом», и на «ага» следующая волна отвечает
        # «ага»: две строки шума вместо разговора.
        seeds = [(a, t) for a, t in replies if len(t) >= _SEED_MIN_CHARS]
        if replies and not seeds:
            await _note("banter_skip", reason="no_seed", depth=depth + 1,
                        detail=clip(replies[0][1], 80))

        if seeds and (depth + 1) < BANTER_MAX_DEPTH:
            # Продолжаем от реплики, которая ЗОВЁТ ответить. Если таких нет —
            # всплеск закончился, и это нормальный исход, а не сбой.
            inviting = [(a, t, invites_reply(t, pool)) for a, t in seeds]
            live = [(a, t, who) for a, t, (yes, who) in inviting if yes]
            if not live:
                await _note("banter_skip", reason="no_invite", depth=depth + 1,
                            detail=clip(seeds[-1][1], 80))
            else:
                speaker, said, named = random.choice(live)
                speaker_name = display(speaker) or speaker
                # Назвали коллегу по имени — отвечает ИМЕННО он, а не случайный
                # из пула. Без этого «Гослинг, ты где?» уходило кому угодно, и
                # обращение повисало без ответа: со стороны это ровно то, из-за
                # чего всплеск читался как реплики мимо друг друга.
                next_pool = [named] if named else pool
                if named:
                    logger.info("[banter] %s обратился к %s — зовём именно его",
                                speaker, named)
                await fanout(
                    redis_client,
                    primary_agent=speaker,
                    trigger_text=said,
                    group_ctx=(group_ctx + f"\n{speaker_name}: {clip(said)}").strip(),
                    sender=speaker_name,
                    depth=depth + 1,
                    user_id=user_id,
                    thread_id=thread_id,
                    chance=chance,
                    health_check=health_check,
                    pool=next_pool,
                    context_lines=lines,
                    client=client,
                    # Назвали по имени — пусть отвечает, даже если уже говорил.
                    allow_repeat=bool(named),
                    # Смешанный режим: первая волна комментирует со стороны,
                    # дальше идёт адресный разговор. Отвечают ТОМУ, кто позвал.
                    addressee=speaker,
                )
        return pinged
    except Exception as e:
        logger.warning(f"[banter] fanout error: {e}")
        return []


def is_banter(task_data: dict) -> bool:
    """True если /task пришёл из болталки — ответ должен быть коротким."""
    return str(task_data.get("source", "")).upper() == "BANTER"


def depth_of(task_data: dict) -> int:
    """Глубина нити из payload /task. 0 — обычный запрос, не болталка."""
    try:
        return int(task_data.get("depth", 0) or 0)
    except (TypeError, ValueError):
        return 0


def sender_of(task_data: dict) -> str:
    """
    Кто написал — для префикса в промт. Пусто, если вызывающий не передал.

    Это ровно то поле, отсутствие которого сломало идентификацию 2026-08-07:
    Гослинг получал по HTTP голый текст без автора и называл Билли «Йодкой».
    """
    raw = str(task_data.get("sender", "") or "").strip()
    if not raw or raw.upper() == "HTTP":
        return ""
    # who_is() покрывает и людей, и ботов: «Yodka» → «Влад», «БИЛЛИ» → «Билли».
    return who_is(raw) or raw

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
import re

import httpx

from .identity import canonical, display, route_key, url as bot_url, who_is

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

# «Перекинутся 1-2 репликой и затихнуть» — буквально это число.
# depth=1 — первая волна реплик; depth=2 — ответ на реплику; дальше тишина.
BANTER_MAX_DEPTH = int(os.environ.get("BANTER_MAX_DEPTH", "2"))

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

# Сколько последних реплик чата показываем. Четыре — это «Влад + ответ агента +
# одна-две реплики болталки», то есть ровно текущий всплеск. Больше — бот
# начинает отвечать на позавчерашнее, меньше — теряется, кто кому что сказал.
BANTER_CTX_LINES = 4

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
    return re.sub(rf"^\W{{0,3}}{re.escape(disp)}\W{{0,3}}:\s*", "",
                  str(text or "").strip(), count=1, flags=re.IGNORECASE)


async def _chat_tail(redis_client, n: int = BANTER_CTX_LINES) -> list[tuple[str, str]]:
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
        rows = await read(redis_client, n)
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


def build_message(lines: list[tuple[str, str]], target: str = "") -> str:
    """
    Промт для одного званого бота: транскрипт + правила.

    Args:
        lines:  [(кто, что)] хронологически, последняя реплика — внизу.
        target: кого зовём (route_key), см. visible_lines().
    """
    shown = visible_lines(lines, target)
    if not shown:
        return f"{BANTER_PROMPT}\n\n{BANTER_RULES}"

    transcript = "\n".join(f"{who}: {what}" for who, what in shown)
    last = shown[-1][0]
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


async def pick(
    redis_client,
    primary_agent: str,
    thread_id: str = "office",
    pool: list[str] | None = None,
    limit: int | None = None,
    health_check=None,
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

        last_pick_reason["value"] = ""
        chosen = await pick(redis_client, primary_agent, thread_id=thread_id,
                            pool=pool, health_check=health_check)
        if not chosen:
            await _note("banter_skip", reason="no_candidates",
                        detail=last_pick_reason["value"][:200])
            return []

        await _thread_mark(redis_client, thread_id, chosen)

        from .auth import office_headers

        # Транскрипт всплеска: лента группы + то, ради чего нас позвали.
        # trigger_text добавляем последним и только если его в ленте ещё нет —
        # Филли пишет сообщение человека в ленту сама, и без дедупа последняя
        # строка задваивалась.
        lines = await _chat_tail(redis_client, BANTER_CTX_LINES)
        for _who, _what in (context_lines or []):
            _add_line(lines, _who, _what)
        _add_line(lines, who_is(sender) or sender or "Влад", trigger_text)

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
                        "message":   build_message(lines, target=agent),
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

        # ── Вторая волна: бот отвечает боту ──────────────────────────────────
        # Берём ОДИН ответ, а не все: цель — «перекинуться парой фраз», а не
        # устроить лавину. Глубина, шанс и дедуп нити ограничивают её сверху,
        # причём дедуп гарантирует, что второй волне достанется тот, кто в этом
        # всплеске ещё не говорил.
        #
        # В затравку годится не всякий ответ. Промт сам разрешает «нечего
        # добавить — ответь одним словом», и на «ага» вторая волна отвечает
        # «ага»: две строки шума вместо разговора. Если содержательного ответа
        # в волне не нашлось — всплеск закончился, и это нормальный исход.
        seeds = [(a, t) for a, t in replies if len(t) >= _SEED_MIN_CHARS]
        if replies and not seeds:
            await _note("banter_skip", reason="no_seed", depth=depth + 1,
                        detail=clip(replies[0][1], 80))
        if seeds and (depth + 1) < BANTER_MAX_DEPTH:
            speaker, said = random.choice(seeds)
            speaker_name = display(speaker) or speaker
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
                pool=pool,
                context_lines=lines,
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

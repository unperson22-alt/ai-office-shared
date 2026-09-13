"""
ai_office_shared.shared.group_post — отправка в офис-группу, которая не врёт.

ПРОБЛЕМА (инцидент 2026-09-10 22:38 UTC):
    Влад смотрит Log-бота и видит подряд три записи:

        📤 MSG_OUT  Агент: Гослинг  Кому: group  «Слушай, живём! ...»
        📤 MSG_OUT  Агент: Милли    Кому: Влад   «Работаем, деньги пока ...»
        📤 MSG_OUT  Агент: Доктор   Кому: Милли  «Влад, четверг — ноги ...»

    В группе при этом ОДНА реплика — Гослинга. Милли и Доктор не написали
    ничего, но лог за них отчитался. Полчаса поиска бага в болталке: механика
    пинга исправна, Филли позвала обоих, оба ответили 200, оба сгенерировали
    (и оплатили) реплику. Потерялась она на последнем шаге — в `send_to_group`.

ПОЧЕМУ ЭТО БЫЛО НЕВИДИМО:
    `send_to_group` у всех шести ботов был скопирован один в один и имел три
    молчаливых выхода, из которых ДВА не оставляли следа вообще:

        if not OFFICE_CHAT_ID:      return None    # ← ни строчки в stdout
        ...
        if data.get("ok"):          return msg_id
        return None                                 # ← ok:false, ни строчки

    То есть отказ Telegram («chat not found», «bot was blocked», 429 flood
    control) и незаданный chat_id выглядели снаружи ровно как успех. А вызов
    в `handle_task` возвращаемое значение не смотрел:

        await send_to_group(response)               # ← результат выброшен
        await log("MSG_OUT", ...)                   # ← отчёт безусловный

    Это инвариант №4 CLAUDE.md дословно: «Гейт не отчитывается „пройдено“, не
    исполнившись». Отчёт подписывал сам исполнитель (инвариант №5), а причину
    отказа Telegram называл вслух — и её выбрасывали, не прочитав (инвариант
    №8: «провал называет того, кто упал»).

    Цена не в косметике. Ложный MSG_OUT дороже молчания: молчание отправляет
    разбираться в болталку, а ложный успех отправляет искать баг у Милли —
    там, где всё работает. Тот же класс, что lesson #129 («смёржено ≠
    запущено») и что молчаливый отказ вайтлиста.

РЕШЕНИЕ — одна точка правды:
    `post_to_group()` возвращает `PostResult`, а не `int | None`. У результата
    есть `ok`, машинно-читаемый `kind` и `reason` — дословные слова дальней
    стороны, а не наша догадка о них. Отказ логируется ВСЕГДА, на всех трёх
    выходах, и `log_event` пишет его в `office:logs` — Влад смотрит логи, а не
    Railway stdout.

    Дальше `PostResult` — улика для лога: MSG_OUT пишется, только если
    отправка состоялась. Не состоялась — в лог уходит ERROR с причиной.
    Хелпер `log_delivery()` делает этот выбор за бота, чтобы шесть копий
    решения не разошлись снова.

ЧЕГО ЭТОТ МОДУЛЬ НЕ ДЕЛАЕТ:
    Не решает, НУЖНО ли постить (это notify-гейт и `banter.is_banter`) и не
    трогает `remember_my_message` — ключ там завязан на имя конкретного бота.
    Бот вызывает его сам по `result.ok`, получив `result.message_id`.

Использование:
    from ai_office_shared.shared.group_post import post_to_group, log_delivery

    res = await post_to_group(
        token=TELEGRAM_TOKEN, chat_id=OFFICE_CHAT_ID, text=response,
        sender_name=BOT_NAME, redis_client=redis_client, bot=BOT_NAME_LOWER,
    )
    if res.ok:
        await remember_my_message(res.chat_id_int, res.message_id)
    await log_delivery(log, res, text=f"{BOT_NAME}: {response}", agent=BOT_NAME)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from . import group_history as _ghist

logger = logging.getLogger("ai_office_shared.group_post")

SEND_TIMEOUT = 10.0

# Машинно-читаемые причины. Нужны отдельно от текста, потому что по тексту
# Telegram никто фильтровать не должен: он меняет формулировки, а `kind`
# остаётся тем же.
NO_CHAT_ID  = "no_chat_id"       # OFFICE_CHAT_ID не задан в env сервиса
NO_TOKEN    = "no_token"         # TELEGRAM_TOKEN не задан
REFUSED     = "telegram_refused" # ответ пришёл, ok:false — причина ЕСТЬ
MIGRATED    = "chat_migrated"     # группу повысили до супергруппы, id сменился
UNREACHABLE = "unreachable"      # до api.telegram.org не доехали
BAD_BODY    = "bad_body"         # 200, но тело не разобрать
SENT        = "sent"


@dataclass(frozen=True)
class PostResult:
    """
    Что реально произошло с отправкой.

    Правдивость тут в том, что `ok` нельзя получить, не дочитав ответ Telegram
    до `ok:true`. Прежний контракт (`int | None`) этим свойством не обладал:
    None означал и «не настроено», и «Telegram отказал», и «сеть легла», а
    вызывающему было проще всего не смотреть вовсе.
    """
    ok: bool
    kind: str
    reason: str = ""
    message_id: int | None = None
    chat_id: str = ""
    error_code: int | None = None
    # Новый chat_id, если Telegram его назвал. Отдельным полем, а не внутри
    # reason: это не пояснение, а ДЕЙСТВИЕ — значение, которое надо положить в
    # OFFICE_CHAT_ID. Строкой в тексте его пришлось бы выковыривать регуляркой.
    migrate_to: str = ""

    def __bool__(self) -> bool:
        return self.ok

    @property
    def chat_id_int(self) -> int | None:
        try:
            return int(self.chat_id)
        except (TypeError, ValueError):
            return None

    def describe(self) -> str:
        """Одна строка для лога: кто упал и его словами."""
        if self.ok:
            return f"доставлено в {self.chat_id} (message_id={self.message_id})"
        head = f"НЕ доставлено ({self.kind})"
        if self.error_code is not None:
            head += f" [{self.error_code}]"
        tail = f"{head}: {self.reason}" if self.reason else head
        if self.migrate_to:
            tail += (f". Новый chat_id: {self.migrate_to} — его и надо "
                     f"положить в OFFICE_CHAT_ID сервисов")
        return tail


async def post_to_group(
    *,
    token: str,
    chat_id: str | int,
    text: str,
    sender_name: str = "",
    redis_client=None,
    bot: str = "",
    parse_mode: str | None = None,
    disable_web_page_preview: bool | None = None,
    timeout: float = SEND_TIMEOUT,
) -> PostResult:
    """
    Отправить текст в офис-группу и СКАЗАТЬ ПРАВДУ о результате.

    Args:
        token:        TELEGRAM_TOKEN бота.
        chat_id:      OFFICE_CHAT_ID. Пустой — это отказ `no_chat_id`, а не
                      успех: именно на этом выходе молча терялись реплики.
        sender_name:  display-имя для `office:group:history` («Милли»). Пусто —
                      в ленту не пишем (её ведёт кто-то другой).
        redis_client: для ленты и для `office:logs`. None — просто без них.
        bot:          каноническое lowercase имя для `log_event`.

    Returns:
        PostResult. Никогда не бросает: отправка в группу не должна ронять
        обработчик. Но и не молчит — каждый отказ виден в stdout и в
        `office:logs`.
    """
    chat = str(chat_id or "").strip()

    if not chat:
        return await _fail(
            redis_client, bot, PostResult(
                ok=False, kind=NO_CHAT_ID, chat_id=chat,
                reason="OFFICE_CHAT_ID не задан в env сервиса — постить некуда",
            ),
        )
    if not token:
        return await _fail(
            redis_client, bot, PostResult(
                ok=False, kind=NO_TOKEN, chat_id=chat,
                reason="TELEGRAM_TOKEN не задан в env сервиса",
            ),
        )

    payload: dict = {"chat_id": chat, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if disable_web_page_preview is not None:
        payload["disable_web_page_preview"] = disable_web_page_preview

    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=payload, timeout=timeout,
            )
    except Exception as e:
        # Своими словами о чужом отказе — но называем, ЧТО именно не доехало.
        return await _fail(
            redis_client, bot, PostResult(
                ok=False, kind=UNREACHABLE, chat_id=chat,
                reason=f"api.telegram.org недоступен: {type(e).__name__}: {e}",
            ),
        )

    try:
        data = r.json()
    except Exception:
        return await _fail(
            redis_client, bot, PostResult(
                ok=False, kind=BAD_BODY, chat_id=chat, error_code=r.status_code,
                reason=f"HTTP {r.status_code}, тело не JSON: {r.text[:200]!r}",
            ),
        )

    if not data.get("ok"):
        # Вот та самая ветка, которая раньше возвращала None без единой
        # строчки в логах. Telegram здесь САМ говорит, почему отказал —
        # «chat not found», «bot was kicked from the group chat», «Too Many
        # Requests: retry after 37». Цитируем его, не пересказываем.
        #
        # И забираем ВСЁ, что он сказал, а не только текст. При повышении
        # группы до супергруппы её chat_id меняется, и Telegram кладёт новый
        # в parameters.migrate_to_chat_id. 13.09.2026 офис на этом встал
        # целиком: каждый бот слал на старый -5194783850 и получал
        # «group chat was upgraded to a supergroup chat». Причину мы называли,
        # а лежащее рядом решение выбрасывали — то есть применяли инвариант №8
        # наполовину.
        migrate = str((data.get("parameters") or {}).get("migrate_to_chat_id") or "")
        return await _fail(
            redis_client, bot, PostResult(
                ok=False,
                kind=MIGRATED if migrate else REFUSED,
                chat_id=chat, migrate_to=migrate,
                error_code=data.get("error_code") or r.status_code,
                reason=str(data.get("description") or "Telegram ответил ok:false без описания"),
            ),
        )

    msg_id = ((data.get("result") or {}).get("message_id"))

    # В общую ленту — только после подтверждённой доставки. Иначе коллеги
    # читают в group_ctx реплику, которой в чате нет, и отвечают на призрак.
    if sender_name:
        await _ghist.push(redis_client, sender_name, text)

    return PostResult(ok=True, kind=SENT, message_id=msg_id, chat_id=chat)


async def _fail(redis_client, bot: str, result: PostResult) -> PostResult:
    """Отказ обязан оставить след в ОБОИХ местах, куда смотрят при разборе."""
    logger.error("[group_post] %s", result.describe())
    if bot:
        try:
            from .logging import log_event
            await log_event(
                redis_client, bot, "group_post_failed", level="error",
                kind=result.kind, reason=result.reason[:300],
                error_code=result.error_code, chat_id=result.chat_id,
                migrate_to=result.migrate_to or None,
            )
        except Exception:
            pass
    return result


async def log_delivery(log_fn, result: PostResult, *, text: str,
                       agent: str = "", to: str = "group") -> None:
    """
    Записать в Log-бота то, что произошло, а не то, что задумывалось.

    `log_fn` — метод бота `log(event, msg, from_=, to_=)`. Разделение простое:
    доставлено → MSG_OUT; не доставлено → ERROR с причиной. Третьего варианта
    («MSG_OUT на всякий случай») здесь нет — с него и начался инцидент.

    `to="group"` не случайно жёсткое. Раньше боты писали `to_=sender`, и
    реплика, ушедшая в общую группу, логировалась как «Кому: Влад» или «Кому:
    Милли» — адресат РАЗГОВОРА вместо канала ДОСТАВКИ. Читая лог, отличить
    «ответил Владу в личку» от «сказал в группу» было нельзя, и Влад искал в
    группе то, что по логу туда и не адресовалось.
    """
    if result.ok:
        await log_fn("MSG_OUT", text, from_=agent, to_=to)
    else:
        await log_fn(
            "ERROR",
            f"реплика в группу не доставлена — {result.describe()}. "
            f"Текст, который не увидели: {text[:200]}",
            from_=agent, to_=to,
        )

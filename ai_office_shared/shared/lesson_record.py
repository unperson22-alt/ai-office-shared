"""
ai_office_shared.shared.lesson_record — запись в архив уроков: что стамповать
кодом и как не издать её дважды.

ПРОБЛЕМА (инцидент 25.08.2026, вечерний аудит):
    Аудит впервые за долгое время нашёл «новые паттерны багов» и записал два
    урока сам. Оба негодны, и каждый по своей причине:

      #123 — «Truncated traceback ... buffer limits in Railway logging»,
             дата 2025-04-10. Это была не проблема Railway: аудит прочитал
             СОБСТВЕННЫЙ вывод Силли из её же логов (обезглавленный трейсбек,
             урок #123) и записал сбой офиса как чужую платформенную беду.
      #124 — `telegram.error.NetworkError: httpx.ConnectError:` у molly-trader,
             дата 2025-01-10. Ровно тот класс сбоя, о котором монитор нарочно
             молчит: обе строки дословно лежат в EXTERNAL_FAULT_PATTERNS.

    Оба ушли в группу ПО ДВА РАЗА с разницей в четыре секунды.

ТРИ ВЫВОДА, РАДИ КОТОРЫХ ЭТОТ МОДУЛЬ:

1. ДАТУ НЕЛЬЗЯ СПРАШИВАТЬ У МОДЕЛИ. Промпт `append_lesson_ai` кончался словами
   «Add id:{new_id} and ts field with today's date». У модели нет «сегодня» —
   она ответила правдоподобно и промахнулась больше чем на год, и сверить
   ответ с часами было некому. Дата, id, kind и флаг публикации — факты,
   которыми процесс уже располагает; они стамповываются здесь, а из ответа
   модели вычищаются.

2. ЗАПИСЬ В ДОЛГУЮ ПАМЯТЬ — НЕ ТЕКСТ ОТЧЁТА. `lessons.json` Силли читает
   первым делом на каждом аудите. Ложная запись — не плохой абзац, а стойкая
   инструкция следующим сессиям искать не там. Поэтому объект модели
   нормализуется по схеме и проверяется на пустоту ДО того, как попадёт в файл.

3. ИДЕМПОТЕНТНОСТЬ ЖИВЁТ НЕ ТАМ, ГДЕ ФЛАГ. `publish_pending_lessons` считает
   себя защищённой от флуда, потому что `posted_to_group` лежит в git. Флаг и
   правда там — но гарантия нужна не файлу, а ЧТЕНИЮ, а GitHub Contents API
   read-after-write консистентности не даёт. История коммитов 25.08 читается
   буквально: `lesson(123)` в 20:01:27, `lesson(124)` в 20:01:33 — и #123 в нём
   ВСЁ ЕЩЁ pending (пометки не случилось: публикация не увидела собственной
   записи), затем два отдельных коммита «mark 2 posted_to_group» в 20:01:38 и
   20:01:42 с разными posted_at — то есть два независимых захода, каждый
   отправил оба урока. Замок поэтому берётся в Redis и НАМЕРЕННО fail-closed,
   в отличие от `dedup.claim_answer`: там молчание дороже дубля, здесь дубль в
   вечном архиве дороже отложенной записи.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("ai_office_shared.lesson_record")

# Поля, которые обязан заполнить автор урока. Пустое любое из них — запись
# бесполезна: она займёт номер и будет читаться вечно, ничего не сообщая.
REQUIRED_TEXT = ("title", "symptom", "root_cause", "fix", "prevention")

# Поля, которые модель НЕ ИМЕЕТ ПРАВА задавать: их знает процесс.
STAMPED = ("id", "date", "kind", "posted_to_group", "posted_at", "ts")

# Прозаические поля, которые переносим из ответа модели как есть.
_PROSE = ("title", "symptom", "root_cause", "why_architecture", "fix",
          "prevention", "cause", "status", "bot", "layer", "tag", "context",
          "anchors")

MIN_TEXT_LEN = 12          # «unknown», «n/a», «-» — это не симптом


def today_iso() -> str:
    """Сегодня по часам процесса. Отдельной функцией — чтобы тест мог сверить."""
    return datetime.now(timezone.utc).date().isoformat()


def normalize(obj, *, lesson_id: int, date: str = "", bot: str = "",
              layer: str = "", tag: str = "", context: str = "") -> dict:
    """Ответ модели → запись архива. Служебные поля стамповываются кодом.

    Бросает ValueError, если запись непригодна. Отказ здесь дешевле мусора в
    файле: пропущенный урок можно записать руками, ложный — читается вечно.
    """
    if not isinstance(obj, dict):
        raise ValueError(f"урок не разобрался как объект: {type(obj).__name__}")

    out = {k: obj[k] for k in _PROSE if obj.get(k) not in (None, "")}

    missing = [k for k in REQUIRED_TEXT
               if len(str(out.get(k, "")).strip()) < MIN_TEXT_LEN]
    if missing:
        raise ValueError(f"урок без содержания в полях: {', '.join(missing)}")

    # Служебные поля — только отсюда. Всё, что модель написала в них, отброшено:
    # 25.08 она сочинила 2025-04-10 и 2025-01-10, и сверить было некому.
    out["id"] = int(lesson_id)
    out["date"] = date or today_iso()
    out["kind"] = "lesson"
    out["posted_to_group"] = False
    if context:
        # Чей это сервис — знает вызывающий, а не модель.
        out["context"] = context
        out.setdefault("bot", context)
    if bot:
        out.setdefault("bot", bot)
    if layer:
        out.setdefault("layer", layer)
    if tag:
        out.setdefault("tag", tag)
    out.setdefault("status", "open")
    return out


def invented_date(obj, *, today: str = "") -> str:
    """Дата записи, если она разошлась с сегодняшней. '' — всё в порядке.

    Нужна не для normalize (там дата ставится кодом), а чтобы поймать записи,
    пришедшие мимо него: #123 и #124 попали в файл именно так.
    """
    today = today or today_iso()
    got = str((obj or {}).get("date", "")).strip()
    return got if got and got != today else ""


# ── Замок публикации ──────────────────────────────────────────────────────────

CLAIM_PREFIX = "office:lesson:published"
CLAIM_TTL = 7 * 24 * 3600      # переживает недельную серию аудитов


def publish_claim_key(lesson_id) -> str:
    return f"{CLAIM_PREFIX}:{lesson_id}"


async def claim_publish(redis_client, lesson_id, ttl: int = CLAIM_TTL) -> bool:
    """Занять право отправить урок в группу. True — отправляем.

    FAIL-CLOSED, и это главное отличие от `dedup.claim_answer`. Там замок
    fail-open: пропущенная реплика в чат хуже лишней. Здесь наоборот — дубль
    ложится в вечный архив группы и двигает порядок, а неотправленный урок
    доедет следующим аудитом. Без Redis публикации не будет, и это верно.
    """
    if redis_client is None:
        logger.warning("[lesson] замка нет (Redis недоступен) — публикацию #%s "
                       "откладываю: дубль в архиве дороже задержки", lesson_id)
        return False
    try:
        got = await redis_client.set(publish_claim_key(lesson_id), "1",
                                     nx=True, ex=ttl)
        return bool(got)
    except Exception as e:
        logger.warning("[lesson] замок #%s недоступен (%s) — не публикую",
                       lesson_id, e)
        return False


async def release_publish(redis_client, lesson_id) -> None:
    """Вернуть замок: отправка не удалась, урок обязан уехать следующим заходом."""
    if redis_client is None:
        return
    try:
        await redis_client.delete(publish_claim_key(lesson_id))
    except Exception as e:
        logger.warning("[lesson] замок #%s не снялся: %s", lesson_id, e)

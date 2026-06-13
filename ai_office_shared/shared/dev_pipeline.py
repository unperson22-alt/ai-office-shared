"""
ai_office_shared.shared.dev_pipeline — оркестрация отдела разработки.

МОДЕЛЬ (параллельная, fan-out):

    Силли (план)
       └─ Девви  ── пишет код по ТЗ           (стадия 1, единственный продюсер)
            └─ [ Рикки ‖ Тести ‖ Секки ]      (стадия 2, ПАРАЛЛЕЛЬНО — все по коду Девви)
                  └─ Скрибби                  (стадия 3, документирует по всем findings)

Почему так, а не «все 5 разом»: Рикки/Тести/Секки/Скрибби семантически
зависят от кода Девви — ревьюить/тестировать/аудировать/документировать нечего,
пока код не написан. Зато сами Рикки, Тести и Секки независимы друг от друга,
поэтому идут одновременно (asyncio.gather). Это режет критический путь с 5
последовательных вызовов до 3 стадий.

КАЖДЫЙ ЗНАЕТ О ДЕЙСТВИЯХ КАЖДОГО: на каждой стадии действия публикуются в
общий эфир задачи (dev_activity). Воркеры читают эфир и видят, что делает
остальная команда над той же задачей (см. dev_activity + bot.py воркеров).

УСТОЙЧИВОСТЬ ПОД НАГРУЗКОЙ:
  - bounded concurrency: глобальный Semaphore (DEV_MAX_CONCURRENCY) ограничивает
    число одновременных HTTP-вызовов воркерам через ВСЕ задачи разом;
  - per-worker timeout + ретраи с экспоненциальным backoff;
  - asyncio.gather(return_exceptions=True) — падение одного воркера не рушит стадию;
  - единый httpx.AsyncClient на весь пайплайн (reuse соединений);
  - namespaced ключи Redis по task_id — задачи не пересекаются;
  - эфир fail-silent — вещание никогда не ломает пайплайн.

Контракт возврата (обратная совместимость с coder.py сохранена):
    dict: devvy, ricky, testi, sekky, scribbi,
          final_code_artifact, commit_msg, task_id, activity(list)
"""

import os
import asyncio
import logging
import uuid

import httpx

from .dev_activity import publish_activity, read_activity

logger = logging.getLogger(__name__)

DEVVY_URL   = os.getenv("DEVVY_URL",   "https://devvy-bot-production-9a4f.up.railway.app")
RICKY_URL   = os.getenv("RICKY_URL",   "https://ricky-bot-production-ab47.up.railway.app")
TESTI_URL   = os.getenv("TESTI_URL",   "https://testi-bot-production-9cab.up.railway.app")
SEKKY_URL   = os.getenv("SEKKY_URL",   "https://sekky-bot-production-9718.up.railway.app")
SCRIBBI_URL = os.getenv("SCRIBBI_URL", "https://scribbi-bot-production-9aa7.up.railway.app")

# Каноничные имена воркеров (для эфира) по url
_WORKER_NAME = {
    DEVVY_URL: "девви", RICKY_URL: "рикки", TESTI_URL: "тести",
    SEKKY_URL: "секки", SCRIBBI_URL: "скрибби",
}

_TIMEOUT       = float(os.getenv("DEV_WORKER_TIMEOUT", "120"))   # на один вызов воркера
_MAX_RETRIES   = int(os.getenv("DEV_WORKER_RETRIES", "2"))       # доп. попытки сверх первой
_MAX_CONC      = int(os.getenv("DEV_MAX_CONCURRENCY", "6"))      # одновременных вызовов на процесс

# Глобальный ограничитель конкурентности на весь процесс Силли — защищает
# воркеров и саму Силли при шквале параллельных dev_task'ов (максимальная нагрузка).
_SEMAPHORE = asyncio.Semaphore(_MAX_CONC)


def _short_summary(text: str, limit: int = 160) -> str:
    """Достаёт строку SUMMARY: из ответа воркера, иначе первые непустые символы."""
    if not text:
        return ""
    for line in text.splitlines():
        s = line.strip()
        if s.upper().startswith("SUMMARY:"):
            return s[8:].strip()[:limit]
    return text.strip().replace("\n", " ")[:limit]


async def _call_worker(
    client: httpx.AsyncClient,
    url: str,
    message: str,
    *,
    artifact: str = "",
    context: str = "",
    repo: str = "",
    file_path: str = "",
    user_id: int = 0,
    task_id: str = "",
    redis_client=None,
) -> str:
    """
    Один вызов воркера с эфиром, таймаутом, ретраями и backoff.
    Никогда не бросает — возвращает текст ответа или 'ERROR: ...'.
    Конкурентность ограничена глобальным семафором.
    """
    name = _WORKER_NAME.get(url, url)
    payload = {
        "message": message,
        "user_id": user_id,
        "repo": repo,
        "file_path": file_path,
        "context": context,
        "artifact": artifact,
        "task_id": task_id,       # воркер по нему читает/пишет эфир команды
        "source": "СИЛЛИ",
    }

    await publish_activity(redis_client, task_id, name, "start",
                           f"взял в работу: {message[:80]}")

    last_err = ""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            async with _SEMAPHORE:
                resp = await client.post(f"{url}/task", json=payload, timeout=_TIMEOUT)
            resp.raise_for_status()
            out = resp.json().get("response", "")
            await publish_activity(redis_client, task_id, name, "done",
                                   _short_summary(out))
            return out
        except Exception as e:
            last_err = str(e)
            logger.warning("[dev_pipeline] %s attempt %d/%d failed: %s",
                           name, attempt + 1, _MAX_RETRIES + 1, e)
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(2 ** attempt)   # 1s, 2s, 4s ...

    await publish_activity(redis_client, task_id, name, "error", last_err, level="error")
    return f"ERROR: {last_err}"


async def run_dev_pipeline(
    task: str,
    repo: str = "",
    file_path: str = "",
    context: str = "",
    user_id: int = 0,
    redis_client=None,
    task_id: str = "",
) -> dict:
    """Параллельный пайплайн dev-dept. См. docstring модуля.

    Args:
        task: Текст задачи для разработчиков
        repo: GitHub репозиторий (например "billy-bot")
        file_path: Путь к файлу (например "bot.py")
        context: Готовый контекст кода (Силли читает файл один раз и передаёт всем)
        user_id: Telegram user_id для логирования
        redis_client: async Redis (для эфира команды). None = работаем без эфира.
        task_id: id задачи. Пустой → сгенерируется. По нему живёт лента активности.

    Returns:
        dict: devvy, ricky, testi, sekky, scribbi,
              final_code_artifact, commit_msg, task_id, activity
    """
    task_id = task_id or uuid.uuid4().hex[:12]
    results: dict = {"task_id": task_id}
    logger.info("[dev_pipeline] start task_id=%s task=%.60s repo=%s", task_id, task, repo)

    async with httpx.AsyncClient() as client:
        # ── Стадия 1: Девви пишет код (единственный продюсер) ──────────────
        devvy_out = await _call_worker(
            client, DEVVY_URL, f"Напиши код по ТЗ: {task}",
            context=context, repo=repo, file_path=file_path,
            user_id=user_id, task_id=task_id, redis_client=redis_client,
        )
        results["devvy"] = devvy_out
        if devvy_out.startswith("ERROR:"):
            # Без кода Девви остальным нечего делать — отдаём что есть.
            logger.error("[dev_pipeline] devvy failed, aborting fan-out: %s", devvy_out)
            results["final_code_artifact"] = ""
            results["activity"] = await read_activity(redis_client, task_id)
            return results
        logger.info("[dev_pipeline] devvy done (%d chars)", len(devvy_out))

        # Подсказка остальным: вы работаете параллельно над одним и тем же кодом.
        feed_note = "\n\n[ПАРАЛЛЕЛЬНО] Рикки, Тести и Секки сейчас работают над этим же кодом одновременно."

        # ── Стадия 2: Рикки ‖ Тести ‖ Секки — ПАРАЛЛЕЛЬНО по коду Девви ────
        ricky_co, testi_co, sekky_co = await asyncio.gather(
            _call_worker(
                client, RICKY_URL, f"Сделай code review и выдай FINAL_CODE: {task}{feed_note}",
                artifact=devvy_out, context=context, repo=repo, file_path=file_path,
                user_id=user_id, task_id=task_id, redis_client=redis_client,
            ),
            _call_worker(
                client, TESTI_URL, f"Протестируй и найди баги: {task}{feed_note}",
                artifact=devvy_out, context=context, repo=repo, file_path=file_path,
                user_id=user_id, task_id=task_id, redis_client=redis_client,
            ),
            _call_worker(
                client, SEKKY_URL, f"Проведи security audit: {task}{feed_note}",
                artifact=devvy_out, context=context, repo=repo, file_path=file_path,
                user_id=user_id, task_id=task_id, redis_client=redis_client,
            ),
            return_exceptions=True,
        )
        # _call_worker не бросает, но gather(return_exceptions) — страхуемся.
        results["ricky"] = ricky_co if isinstance(ricky_co, str) else f"ERROR: {ricky_co}"
        results["testi"] = testi_co if isinstance(testi_co, str) else f"ERROR: {testi_co}"
        results["sekky"] = sekky_co if isinstance(sekky_co, str) else f"ERROR: {sekky_co}"

        # Финальный код — из ревью Рикки, если он отработал; иначе код Девви.
        ricky_ok = results["ricky"] and not results["ricky"].startswith("ERROR:")
        final_code_artifact = results["ricky"] if ricky_ok else devvy_out

        # ── Стадия 3: Скрибби документирует по коду + findings QA/Security ──
        combined = (
            final_code_artifact[:2000]
            + "\n\n[QA — Тести]\n" + (results.get("testi", "") or "")[:800]
            + "\n\n[SECURITY — Секки]\n" + (results.get("sekky", "") or "")[:800]
        )
        scribbi_out = await _call_worker(
            client, SCRIBBI_URL, f"Задокументируй изменения и дай COMMIT_MSG: {task}",
            artifact=combined, context=context, repo=repo, file_path=file_path,
            user_id=user_id, task_id=task_id, redis_client=redis_client,
        )
        results["scribbi"] = scribbi_out

    # Извлекаем commit message из ответа Скрибби
    for line in (results.get("scribbi", "") or "").splitlines():
        if line.startswith("COMMIT_MSG:"):
            results["commit_msg"] = line.replace("COMMIT_MSG:", "").strip()
            break

    results["final_code_artifact"] = final_code_artifact
    results["activity"] = await read_activity(redis_client, task_id)
    logger.info("[dev_pipeline] complete task_id=%s", task_id)
    return results

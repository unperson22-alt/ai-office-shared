import os
import httpx
import logging

logger = logging.getLogger(__name__)

DEVVY_URL   = os.getenv("DEVVY_URL",   "https://devvy-bot-production-9a4f.up.railway.app")
RICKY_URL   = os.getenv("RICKY_URL",   "https://ricky-bot-production-ab47.up.railway.app")
TESTI_URL   = os.getenv("TESTI_URL",   "https://testi-bot-production-9cab.up.railway.app")
SEKKY_URL   = os.getenv("SEKKY_URL",   "https://sekky-bot-production-9718.up.railway.app")
SCRIBBI_URL = os.getenv("SCRIBBI_URL", "https://scribbi-bot-production-9aa7.up.railway.app")

_TIMEOUT = 120


async def _call_worker(
    url: str,
    message: str,
    artifact: str = "",
    context: str = "",
    repo: str = "",
    file_path: str = "",
    user_id: int = 0,
) -> str:
    payload = {
        "message": message,
        "user_id": user_id,
        "repo": repo,
        "file_path": file_path,
        "context": context,
        "artifact": artifact,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(f"{url}/task", json=payload)
        resp.raise_for_status()
        return resp.json().get("response", "")


async def run_dev_pipeline(
    task: str,
    repo: str = "",
    file_path: str = "",
    context: str = "",
    user_id: int = 0,
) -> dict:
    """Цепочка дев-отдела: Девви → Рикки → Тести → Секки → Скрибби.

    Args:
        task: Текст задачи для разработчиков
        repo: GitHub репозиторий (например "billy-bot")
        file_path: Путь к файлу в репозитории (например "bot.py")
        context: Готовый контекст кода (если уже зачитан)
        user_id: Telegram user_id для логирования

    Returns:
        dict с ключами: devvy, ricky, testi, sekky, scribbi, final_code_artifact, commit_msg
    """
    results: dict = {}
    logger.info("[dev_pipeline] start task=%.60s repo=%s", task, repo)

    # Шаг 1: Девви пишет код
    try:
        devvy_out = await _call_worker(
            DEVVY_URL, task, context=context, repo=repo, file_path=file_path, user_id=user_id
        )
        results["devvy"] = devvy_out
        logger.info("[dev_pipeline] devvy done (%d chars)", len(devvy_out))
    except Exception as e:
        logger.error("[dev_pipeline] devvy failed: %s", e)
        results["devvy"] = f"ERROR: {e}"
        return results

    # Шаг 2: Рикки ревьювит и выдаёт FINAL_CODE
    try:
        ricky_out = await _call_worker(
            RICKY_URL, task, artifact=devvy_out, context=context, repo=repo, file_path=file_path, user_id=user_id
        )
        results["ricky"] = ricky_out
        logger.info("[dev_pipeline] ricky done")
    except Exception as e:
        logger.error("[dev_pipeline] ricky failed: %s", e)
        results["ricky"] = f"ERROR: {e}"

    final_code_artifact = results.get("ricky", "") or devvy_out

    # Шаг 3: Тести пишет тест-кейсы
    try:
        testi_out = await _call_worker(
            TESTI_URL, task, artifact=final_code_artifact, context=context, repo=repo, file_path=file_path, user_id=user_id
        )
        results["testi"] = testi_out
        logger.info("[dev_pipeline] testi done")
    except Exception as e:
        logger.error("[dev_pipeline] testi failed: %s", e)
        results["testi"] = f"ERROR: {e}"

    # Шаг 4: Секки проверяет безопасность
    try:
        sekky_out = await _call_worker(
            SEKKY_URL, task, artifact=final_code_artifact, context=context, repo=repo, file_path=file_path, user_id=user_id
        )
        results["sekky"] = sekky_out
        logger.info("[dev_pipeline] sekky done")
    except Exception as e:
        logger.error("[dev_pipeline] sekky failed: %s", e)
        results["sekky"] = f"ERROR: {e}"

    # Шаг 5: Скрибби документирует и формирует commit message
    combined = (
        final_code_artifact[:2000]
        + "\n\n[QA]\n" + results.get("testi", "")[:800]
        + "\n\n[SECURITY]\n" + results.get("sekky", "")[:800]
    )
    try:
        scribbi_out = await _call_worker(
            SCRIBBI_URL, task, artifact=combined, context=context, repo=repo, file_path=file_path, user_id=user_id
        )
        results["scribbi"] = scribbi_out
        logger.info("[dev_pipeline] scribbi done")
    except Exception as e:
        logger.error("[dev_pipeline] scribbi failed: %s", e)
        results["scribbi"] = f"ERROR: {e}"

    # Извлекаем commit message из ответа Скрибби
    for line in results.get("scribbi", "").splitlines():
        if line.startswith("COMMIT_MSG:"):
            results["commit_msg"] = line.replace("COMMIT_MSG:", "").strip()
            break

    results["final_code_artifact"] = final_code_artifact
    logger.info("[dev_pipeline] complete")
    return results

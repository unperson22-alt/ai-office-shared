"""
github_tools.py — общий модуль для работы с GitHub API
Репо: unperson22-alt/ai-office-shared
"""

import httpx
import base64
import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") or os.getenv("GH_PAT")
GITHUB_USER = "unperson22-alt"
BASE_URL = "https://api.github.com"

HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json"
}

TIMEOUT = httpx.Timeout(15.0)


async def create_repo(repo: str, description: str = "", private: bool = True) -> dict:
    """
    Создать новый репозиторий на GitHub.
    Возвращает {"url": html_url, "clone_url": clone_url}
    """
    if not GITHUB_TOKEN:
        raise EnvironmentError("GITHUB_TOKEN не задан")
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.post(
            f"{BASE_URL}/user/repos",
            headers=HEADERS,
            json={"name": repo, "description": description, "private": private, "auto_init": False}
        )
        if r.status_code == 422:
            raise ValueError(f"Репо '{repo}' уже существует или имя недопустимо")
        r.raise_for_status()
        data = r.json()
        logger.info(f"create_repo OK: {repo}")
        return {"url": data["html_url"], "clone_url": data["clone_url"]}


async def push_file(repo: str, path: str, content: str, commit_msg: str) -> dict:
    """
    Создать или обновить файл в репо.
    Для .py файлов автоматически валидирует синтаксис перед пушем.
    """
    if not GITHUB_TOKEN:
        raise EnvironmentError("GITHUB_TOKEN не задан в переменных окружения Railway")

    # Валидация Python синтаксиса — предотвращает пуш сломанного кода
    if path.endswith(".py"):
        try:
            import ast as _ast
            _ast.parse(content)
        except SyntaxError as e:
            raise ValueError(
                f"Python SyntaxError в {path}: {e.msg} (строка {e.lineno}). "
                f"Файл НЕ запушен. Исправь код перед повторной попыткой."
            )

    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/contents/{path}"
    encoded = base64.b64encode(content.encode()).decode()
    sha = await _get_file_sha(repo, path)
    payload = {"message": commit_msg, "content": encoded}
    if sha:
        payload["sha"] = sha

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.put(url, headers=HEADERS, json=payload)
        if r.status_code in (401, 403):
            raise PermissionError(
                f"GitHub auth failed ({r.status_code}): проверь GITHUB_TOKEN в Railway Variables"
            )
        r.raise_for_status()
        data = r.json()
        logger.info(f"push_file OK: {repo}/{path}")
        return {
            "url": data.get("content", {}).get("html_url", ""),
            "action": "updated" if sha else "created"
        }


async def read_file(repo: str, path: str, ref: str = "") -> str:
    """Прочитать содержимое файла из репо.

    ref — ветка/коммит. Пусто = дефолтная ветка (прежнее поведение). Нужен
    там, где правка уже лежит в рабочей ветке: без ref гейт размера и следующая
    попытка сравнивались бы с версией из main, то есть с текстом, который никто
    уже не правит.
    """
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/contents/{path}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.get(url, headers=HEADERS,
                             params={"ref": ref} if ref else None)
        r.raise_for_status()
        data = r.json()
        return base64.b64decode(data["content"]).decode()


async def list_files(repo: str, path: str = "") -> list:
    """Список файлов в папке репо."""
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/contents/{path}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.get(url, headers=HEADERS)
        r.raise_for_status()
        items = r.json()
        return [
            {"name": i["name"], "type": i["type"], "path": i["path"]}
            for i in items
        ]


async def delete_file(repo: str, path: str, commit_msg: str) -> bool:
    """Удалить файл из репо."""
    sha = await _get_file_sha(repo, path)
    if not sha:
        return False
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/contents/{path}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.delete(url, headers=HEADERS, json={"message": commit_msg, "sha": sha})
        r.raise_for_status()
        return True


async def _get_file_sha(repo: str, path: str) -> Optional[str]:
    """Получить sha файла (нужен для обновления/удаления)."""
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/contents/{path}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.get(url, headers=HEADERS)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("sha")


# ── Branch & PR operations ────────────────────────────────────────────────────

async def get_default_branch_sha(repo: str, branch: str = "main") -> str:
    """Возвращает SHA последнего коммита в ветке."""
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/git/refs/heads/{branch}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(url, headers=HEADERS)
        r.raise_for_status()
    return r.json()["object"]["sha"]


async def commit_exists(repo: str, ref: str) -> bool:
    """True, если коммит/ссылка ref реально существует в репо unperson22-alt/{repo}.

    Используется для валидации git+…@<sha> пинов в requirements.txt перед пушем:
    несуществующий SHA (например HEAD ЧУЖОГО репо) → build FAILED на pip install.
    GET /repos/{owner}/{repo}/commits/{ref}: 200 = есть, 404/422 = нет.
    При сетевой/иной ошибке НЕ подтверждаем существование (возвращаем False) —
    гейт должен быть fail-closed, чтобы битый пин не проскочил.
    """
    if not GITHUB_TOKEN:
        raise EnvironmentError("GITHUB_TOKEN не задан")
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/commits/{ref}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(url, headers=HEADERS)
        if r.status_code == 200:
            return True
        if r.status_code in (404, 422):
            return False
        logger.warning(f"commit_exists({repo}@{ref}): неожиданный статус {r.status_code}")
        return False
    except Exception as e:
        logger.warning(f"commit_exists({repo}@{ref}) ошибка сети: {e}")
        return False


async def create_branch(repo: str, branch_name: str, from_branch: str = "main") -> str:
    """
    Создаёт новую ветку от from_branch.
    Возвращает имя созданной ветки.
    Если ветка уже существует — возвращает её имя без ошибки.
    """
    try:
        sha = await get_default_branch_sha(repo, from_branch)
    except Exception as e:
        raise RuntimeError(f"Не удалось получить SHA ветки {from_branch} в {repo}: {e}")

    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/git/refs"
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(url, headers=HEADERS,
                         json={"ref": f"refs/heads/{branch_name}", "sha": sha})
        if r.status_code == 422:
            # Ветка уже существует — не ошибка
            logger.info(f"create_branch: {branch_name} already exists in {repo}")
            return branch_name
        r.raise_for_status()
    logger.info(f"create_branch OK: {repo}/{branch_name} from {from_branch}")
    return branch_name


async def push_file_to_branch(
    repo: str, path: str, content: str, commit_msg: str, branch: str
) -> dict:
    """
    Создаёт или обновляет файл в конкретной ветке.
    Для .py файлов валидирует синтаксис перед пушем.
    """
    if not GITHUB_TOKEN:
        raise EnvironmentError("GITHUB_TOKEN не задан")

    if path.endswith(".py"):
        try:
            import ast as _ast
            _ast.parse(content)
        except SyntaxError as e:
            raise ValueError(f"SyntaxError в {path}: {e.msg} (строка {e.lineno}). Файл НЕ запушен.")

    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/contents/{path}"
    encoded = base64.b64encode(content.encode()).decode()

    # Получаем SHA текущей версии в ЭТОЙ ветке
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(url, headers=HEADERS, params={"ref": branch})
        sha = r.json().get("sha") if r.status_code == 200 else None

    payload = {"message": commit_msg, "content": encoded, "branch": branch}
    if sha:
        payload["sha"] = sha

    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.put(url, headers=HEADERS, json=payload)
        r.raise_for_status()

    data = r.json()
    logger.info(f"push_file_to_branch OK: {repo}/{path} → {branch}")
    return {
        "url": data.get("content", {}).get("html_url", ""),
        "action": "updated" if sha else "created",
    }


async def create_pull_request(
    repo: str,
    title: str,
    body: str,
    head_branch: str,
    base_branch: str = "main",
) -> dict:
    """
    Создаёт Pull Request. Возвращает {"number": int, "url": str, "html_url": str}.
    """
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/pulls"
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(url, headers=HEADERS, json={
            "title": title,
            "body": body,
            "head": head_branch,
            "base": base_branch,
        })
        r.raise_for_status()
    data = r.json()
    logger.info(f"create_pull_request OK: {repo} #{data['number']}")
    return {"number": data["number"], "url": data["url"], "html_url": data["html_url"]}


async def merge_pull_request(repo: str, pr_number: int, commit_msg: str = "") -> bool:
    """
    Мержит PR squash-методом. Возвращает True при успехе.
    """
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/pulls/{pr_number}/merge"
    payload = {"merge_method": "squash"}
    if commit_msg:
        payload["commit_title"] = commit_msg
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.put(url, headers=HEADERS, json=payload)
        if r.status_code == 405:
            logger.warning(f"merge_pull_request: PR #{pr_number} not mergeable")
            return False
        r.raise_for_status()
    logger.info(f"merge_pull_request OK: {repo} #{pr_number}")
    return True


async def get_pr_by_url(html_url: str) -> dict:
    """
    Возвращает данные PR по его html_url.
    Парсит: github.com/{user}/{repo}/pull/{number}
    """
    import re
    m = re.search(r"github\.com/[^/]+/([^/]+)/pull/(\d+)", html_url)
    if not m:
        raise ValueError(f"Не удалось распарсить PR URL: {html_url}")
    repo, number = m.group(1), int(m.group(2))
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/pulls/{number}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(url, headers=HEADERS)
        r.raise_for_status()
    return {"repo": repo, "number": number, **r.json()}


# ── Состояние CI по коммиту ───────────────────────────────────────────────────
# Зачем: до этого ни один шаг пайплайна не ЗАПУСКАЛ код. compile() разбирает
# исходник, pyflakes ходит по AST, гейт размера считает строки — все три меряют
# текст. Ровно поэтому 01.07.2026 гейт пропустил валидную 8-строчную заглушку
# вместо файла на 5766 строк. Единственная проверка, которая исполняет код, уже
# есть в каждом репо офиса — его собственный CI (pyflakes + compileall + тесты).
# Спрашиваем его, а не выдумываем песочницу внутри процесса Силли.

# Заключения, которые считаем провалом. "skipped"/"neutral" — не провал: джоба
# сознательно не применима к этому изменению.
_CHECK_BAD = {"failure", "timed_out", "action_required", "cancelled", "stale"}


def parse_check_runs(payload: dict) -> tuple:
    """
    (состояние, имена_упавших, ссылка) из ответа GET .../check-runs.

    Состояние:
      "empty"   — ни одного прогона (в репо нет CI, ЛИБО он ещё не зарегистрирован);
      "pending" — есть незавершённые;
      "failure" — хоть один завершился провалом;
      "success" — все завершились и ни один не провалился.

    ⚠️ "empty" НЕ синоним "success". Проверка, которая не смогла выполниться, —
    провал, а не пропуск (урок #93): иначе репозиторий без CI автоматически
    выдавал бы зелёный свет любому коду. Решение, сколько ждать регистрации
    прогонов, принимает вызывающий — здесь только разбор.

    Чистая функция: разбирается на фикстуре, без сети.
    """
    runs = (payload or {}).get("check_runs") or []
    if not runs:
        return "empty", [], ""
    url = ""
    failed = []
    pending = False
    for run in runs:
        if not isinstance(run, dict):
            continue
        url = url or run.get("html_url") or ""
        if run.get("status") != "completed":
            pending = True
            continue
        if (run.get("conclusion") or "") in _CHECK_BAD:
            name = run.get("name") or "?"
            failed.append(name)
            url = run.get("html_url") or url
    if failed:
        return "failure", failed, url
    if pending:
        return "pending", [], url
    return "success", [], url


async def get_checks_status(repo: str, ref: str) -> tuple:
    """
    Состояние CI для коммита/ветки ref. См. parse_check_runs.

    Сетевая ошибка → ("pending", [], "") — не «зелено» и не «упало»: мы просто
    не знаем. Вызывающий упрётся в свой таймаут и запишет это как непройденную
    проверку, а не как успех.
    """
    if not GITHUB_TOKEN:
        raise EnvironmentError("GITHUB_TOKEN не задан")
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/commits/{ref}/check-runs"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(url, headers=HEADERS,
                            params={"per_page": 100},
                            follow_redirects=True)
            r.raise_for_status()
            return parse_check_runs(r.json())
    except Exception as e:
        logger.warning(f"get_checks_status({repo}@{ref}) ошибка: {e}")
        return "pending", [], ""


async def get_default_branch(repo: str) -> str:
    """Дефолтная ветка репо. При любой ошибке — 'main' (так во всех репо офиса)."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(f"{BASE_URL}/repos/{GITHUB_USER}/{repo}", headers=HEADERS)
            r.raise_for_status()
            return r.json().get("default_branch") or "main"
    except Exception as e:
        logger.warning(f"get_default_branch({repo}) ошибка: {e}")
        return "main"

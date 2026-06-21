"""
github_tools.py — общий модуль для работы с GitHub API
Репо: unperson22-alt/ai-office-shared

АУТЕНТИФИКАЦИЯ (важно — корень бага «401 Unauthorized при деплое фикса»):
  Для ЗАПИСИ в репозитории нужен ВАЛИДНЫЙ креденшел с правами Contents: write
  (+ Pull requests: write для PR). Историческая причина инцидента: GitHub-write-
  креденшел (`GITHUB_TOKEN`/`GH_PAT`) стал НЕВАЛИДЕН (протух/битый → 401, а не 403).
  Чтение в coder.py идёт напрямую через GH_PAT (работало), запись — через этот
  модуль (`GITHUB_TOKEN or GH_PAT`) → 401. ВНИМАНИЕ: `RAILWAY_TOKEN_VLAD` — это
  токен Railway (редеплой/checkvar), НЕ годится для GitHub-записи. Это разные ключи.

  Поддерживаются два режима (выбирается автоматически):
    1) GitHub App (рекомендуется) — задать в окружении:
         GITHUB_APP_ID
         GITHUB_APP_PRIVATE_KEY        (PEM; допускаются литеральные \n)
         GITHUB_APP_INSTALLATION_ID
       Модуль сам минтит installation-token (через JWT) и кэширует его (~55 мин).
       Токен не истекает «насовсем», права узкие — самый надёжный путь.
    2) Личный токен (fallback) — GITHUB_TOKEN или GH_PAT. Для записи токен ОБЯЗАН
       быть валиден и иметь scope Contents: write. Протухший токен → 401.

  Проверить доступ на запись на старте: await verify_write_access(repo).
"""

import httpx
import base64
import os
import time
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

GITHUB_USER = "unperson22-alt"
BASE_URL = "https://api.github.com"

# ── Личный токен (legacy / fallback) ─────────────────────────────────────────
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") or os.getenv("GH_PAT")

# ── GitHub App (предпочтительный путь для записи) ────────────────────────────
GITHUB_APP_ID = os.getenv("GITHUB_APP_ID")
GITHUB_APP_PRIVATE_KEY = os.getenv("GITHUB_APP_PRIVATE_KEY")
GITHUB_APP_INSTALLATION_ID = os.getenv("GITHUB_APP_INSTALLATION_ID")

# Статичные заголовки оставлены для обратной совместимости (legacy-импорт).
# ВНУТРИ модуля используйте await _auth_headers().
HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

TIMEOUT = httpx.Timeout(15.0)

_ACCEPT = "application/vnd.github+json"
_app_configured = bool(GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY and GITHUB_APP_INSTALLATION_ID)

# Кэш installation-токена: (token, expires_at_epoch)
_inst_token: Optional[str] = None
_inst_token_exp: float = 0.0


def _private_key() -> str:
    """PEM приватного ключа App. Поддерживает ключи, где переводы строк экранированы."""
    key = GITHUB_APP_PRIVATE_KEY or ""
    if "\\n" in key and "-----BEGIN" in key:
        key = key.replace("\\n", "\n")
    return key


def _make_app_jwt() -> str:
    """Сминтить короткоживущий JWT для аутентификации как GitHub App (RS256)."""
    import jwt  # PyJWT[crypto]; импорт ленивый — нужен только в App-режиме
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 540, "iss": str(GITHUB_APP_ID)}
    return jwt.encode(payload, _private_key(), algorithm="RS256")


async def _get_installation_token() -> str:
    """Вернуть (с кэшем) installation access token для GitHub App."""
    global _inst_token, _inst_token_exp
    if _inst_token and time.time() < _inst_token_exp - 300:  # запас 5 мин
        return _inst_token

    jwt_token = _make_app_jwt()
    url = f"{BASE_URL}/app/installations/{GITHUB_APP_INSTALLATION_ID}/access_tokens"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.post(
            url,
            headers={"Authorization": f"Bearer {jwt_token}", "Accept": _ACCEPT},
        )
        if r.status_code in (401, 403):
            raise PermissionError(
                f"GitHub App auth failed ({r.status_code}): проверь GITHUB_APP_ID / "
                f"GITHUB_APP_PRIVATE_KEY / GITHUB_APP_INSTALLATION_ID в Railway."
            )
        r.raise_for_status()
        data = r.json()
    _inst_token = data["token"]
    # expires_at в ISO; для простоты держим токен ~55 минут.
    _inst_token_exp = time.time() + 55 * 60
    logger.info("github_tools: получен installation-token (GitHub App)")
    return _inst_token


async def _auth_headers() -> dict:
    """Заголовки авторизации: GitHub App, если настроен, иначе личный токен."""
    if _app_configured:
        token = await _get_installation_token()
        return {"Authorization": f"Bearer {token}", "Accept": _ACCEPT}
    if not GITHUB_TOKEN:
        raise EnvironmentError(
            "Нет GitHub креденшела: задай GitHub App (GITHUB_APP_*) либо GITHUB_TOKEN/GH_PAT."
        )
    return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": _ACCEPT}


def _auth_mode() -> str:
    return "github_app" if _app_configured else ("token" if GITHUB_TOKEN else "none")


async def verify_write_access(repo: str = "ai-office-shared") -> Tuple[bool, str]:
    """
    Префлайт-проверка прав на ЗАПИСЬ. Зовётся на старте Силли.
    Возвращает (ok, detail). Не бросает — чтобы вызывающий сам решил, что делать.

    Проверяем permissions.push на репо: точный индикатор того, что креденшел
    реально сможет push_file/PR, а не только читать. Различает 401 (токен невалиден/
    протух) и read-only (push=false) — это разные причины с разным фиксом.
    """
    mode = _auth_mode()
    if mode == "none":
        return False, "нет креденшела (ни GITHUB_APP_*, ни GITHUB_TOKEN/GH_PAT)"
    try:
        headers = await _auth_headers()
        url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}"
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.get(url, headers=headers)
        if r.status_code in (401, 403):
            return False, f"{mode}: {r.status_code} — токен невалиден/протух или отозван (нужна ротация)"
        if r.status_code == 404:
            return False, f"{mode}: репо {repo} не найдено или нет доступа к нему"
        r.raise_for_status()
        perms = r.json().get("permissions", {})
        if perms.get("push") or perms.get("admin") or perms.get("maintain"):
            return True, f"{mode}: write OK"
        return False, (
            f"{mode}: креденшел только на ЧТЕНИЕ (push=false). Нужен Contents: write — "
            f"настрой GitHub App или выдай токену права на запись."
        )
    except PermissionError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, f"{mode}: ошибка проверки: {type(e).__name__}: {e}"


def _raise_auth(status: int):
    """Единый понятный текст при auth-сбое — чтобы Силли отличала это от обычной ошибки."""
    raise PermissionError(
        f"GitHub auth failed ({status}): креденшел невалиден или без прав на запись. "
        f"Режим={_auth_mode()}. Это НЕ ошибка задачи — нужен ВАЛИДНЫЙ write-креденшел: настрой "
        f"GitHub App (GITHUB_APP_ID/PRIVATE_KEY/INSTALLATION_ID, Contents+PR: write) либо "
        f"положи валидный токен с Contents: write в GITHUB_TOKEN (RAILWAY_TOKEN_VLAD — это "
        f"Railway-токен, для GitHub-записи НЕ годится)."
    )


async def create_repo(repo: str, description: str = "", private: bool = True) -> dict:
    """
    Создать новый репозиторий на GitHub.
    Возвращает {"url": html_url, "clone_url": clone_url}
    """
    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.post(
            f"{BASE_URL}/user/repos",
            headers=headers,
            json={"name": repo, "description": description, "private": private, "auto_init": False}
        )
        if r.status_code in (401, 403):
            _raise_auth(r.status_code)
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

    headers = await _auth_headers()
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/contents/{path}"
    encoded = base64.b64encode(content.encode()).decode()
    sha = await _get_file_sha(repo, path)
    payload = {"message": commit_msg, "content": encoded}
    if sha:
        payload["sha"] = sha

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.put(url, headers=headers, json=payload)
        if r.status_code in (401, 403):
            _raise_auth(r.status_code)
        r.raise_for_status()
        data = r.json()
        logger.info(f"push_file OK: {repo}/{path}")
        return {
            "url": data.get("content", {}).get("html_url", ""),
            "action": "updated" if sha else "created"
        }


async def read_file(repo: str, path: str) -> str:
    """Прочитать содержимое файла из репо."""
    headers = await _auth_headers()
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/contents/{path}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        data = r.json()
        return base64.b64decode(data["content"]).decode()


async def list_files(repo: str, path: str = "") -> list:
    """Список файлов в папке репо."""
    headers = await _auth_headers()
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/contents/{path}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.get(url, headers=headers)
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
    headers = await _auth_headers()
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/contents/{path}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.delete(url, headers=headers, json={"message": commit_msg, "sha": sha})
        if r.status_code in (401, 403):
            _raise_auth(r.status_code)
        r.raise_for_status()
        return True


async def _get_file_sha(repo: str, path: str) -> Optional[str]:
    """Получить sha файла (нужен для обновления/удаления)."""
    headers = await _auth_headers()
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/contents/{path}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.get(url, headers=headers)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("sha")


# ── Branch & PR operations ────────────────────────────────────────────────────

async def get_default_branch_sha(repo: str, branch: str = "main") -> str:
    """Возвращает SHA последнего коммита в ветке."""
    headers = await _auth_headers()
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/git/refs/heads/{branch}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(url, headers=headers)
        r.raise_for_status()
    return r.json()["object"]["sha"]


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

    headers = await _auth_headers()
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/git/refs"
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(url, headers=headers,
                         json={"ref": f"refs/heads/{branch_name}", "sha": sha})
        if r.status_code in (401, 403):
            _raise_auth(r.status_code)
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
    if path.endswith(".py"):
        try:
            import ast as _ast
            _ast.parse(content)
        except SyntaxError as e:
            raise ValueError(f"SyntaxError в {path}: {e.msg} (строка {e.lineno}). Файл НЕ запушен.")

    headers = await _auth_headers()
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/contents/{path}"
    encoded = base64.b64encode(content.encode()).decode()

    # Получаем SHA текущей версии в ЭТОЙ ветке
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(url, headers=headers, params={"ref": branch})
        sha = r.json().get("sha") if r.status_code == 200 else None

    payload = {"message": commit_msg, "content": encoded, "branch": branch}
    if sha:
        payload["sha"] = sha

    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.put(url, headers=headers, json=payload)
        if r.status_code in (401, 403):
            _raise_auth(r.status_code)
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
    headers = await _auth_headers()
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/pulls"
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(url, headers=headers, json={
            "title": title,
            "body": body,
            "head": head_branch,
            "base": base_branch,
        })
        if r.status_code in (401, 403):
            _raise_auth(r.status_code)
        r.raise_for_status()
    data = r.json()
    logger.info(f"create_pull_request OK: {repo} #{data['number']}")
    return {"number": data["number"], "url": data["url"], "html_url": data["html_url"]}


async def merge_pull_request(repo: str, pr_number: int, commit_msg: str = "") -> bool:
    """
    Мержит PR squash-методом. Возвращает True при успехе.
    """
    headers = await _auth_headers()
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/pulls/{pr_number}/merge"
    payload = {"merge_method": "squash"}
    if commit_msg:
        payload["commit_title"] = commit_msg
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.put(url, headers=headers, json=payload)
        if r.status_code in (401, 403):
            _raise_auth(r.status_code)
        if r.status_code == 405:
            logger.warning(f"merge_pull_request: PR #{pr_number} not mergeable")
            return False
        r.raise_for_status()
    logger.info(f"merge_pull_request OK: {repo} #{pr_number}")
    return True


async def deploy_via_pr(
    repo: str,
    path: str,
    content: str,
    commit_msg: str,
    *,
    branch: str,
    pr_title: str = "",
    pr_body: str = "",
    base_branch: str = "main",
    auto_merge: bool = True,
) -> dict:
    """
    Безопасный деплой фикса: ветка → файл в ветку → PR → (опц.) squash-мёрж.
    Заменяет прямой push_file в main. Возвращает сводку с url PR.

    Auth-сбой (PermissionError) пробрасывается наверх — вызывающий (Силли) должен
    распознать его как «креденшел мёртв» и эскалировать, а не блокировать задачу.
    """
    await create_branch(repo, branch, from_branch=base_branch)
    push = await push_file_to_branch(repo, path, content, commit_msg, branch)
    pr = await create_pull_request(
        repo,
        title=pr_title or commit_msg,
        body=pr_body or commit_msg,
        head_branch=branch,
        base_branch=base_branch,
    )
    merged = False
    if auto_merge:
        merged = await merge_pull_request(repo, pr["number"], commit_msg)
    return {
        "action": push["action"],
        "pr_number": pr["number"],
        "pr_url": pr["html_url"],
        "merged": merged,
        "branch": branch,
    }


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
    headers = await _auth_headers()
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/pulls/{number}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(url, headers=headers)
        r.raise_for_status()
    return {"repo": repo, "number": number, **r.json()}

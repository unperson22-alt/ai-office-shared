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

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
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
    """
    if not GITHUB_TOKEN:
        raise EnvironmentError("GITHUB_TOKEN не задан в переменных окружения Railway")

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


async def read_file(repo: str, path: str) -> str:
    """Прочитать содержимое файла из репо."""
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/contents/{path}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.get(url, headers=HEADERS)
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

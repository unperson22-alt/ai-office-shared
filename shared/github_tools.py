"""
github_tools.py — общий модуль для работы с GitHub API
Репо: unperson22-alt/ai-office-shared
"""

import httpx
import base64
import os
from typing import Optional

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_USER = "unperson22-alt"
BASE_URL = "https://api.github.com"

HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json"
}


async def push_file(repo: str, path: str, content: str, commit_msg: str) -> dict:
    """
    Создать или обновить файл в репо.
    repo: имя репо (например 'ai-office-shared')
    path: путь внутри репо (например 'scripts/test.py')
    content: содержимое файла (текст)
    commit_msg: сообщение коммита
    """
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/contents/{path}"
    encoded = base64.b64encode(content.encode()).decode()

    # Проверяем существует ли файл (нужен sha для обновления)
    sha = await _get_file_sha(repo, path)

    payload = {
        "message": commit_msg,
        "content": encoded
    }
    if sha:
        payload["sha"] = sha

    async with httpx.AsyncClient() as client:
        r = await client.put(url, headers=HEADERS, json=payload)
        r.raise_for_status()
        data = r.json()
        return {
            "url": data.get("content", {}).get("html_url", ""),
            "action": "updated" if sha else "created"
        }


async def read_file(repo: str, path: str) -> str:
    """
    Прочитать содержимое файла из репо.
    Возвращает текст файла.
    """
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/contents/{path}"

    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
        content = base64.b64decode(data["content"]).decode()
        return content


async def list_files(repo: str, path: str = "") -> list:
    """
    Список файлов в папке репо.
    path: папка внутри репо (пустая строка = корень)
    """
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/contents/{path}"

    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=HEADERS)
        r.raise_for_status()
        items = r.json()
        return [
            {"name": i["name"], "type": i["type"], "path": i["path"]}
            for i in items
        ]


async def delete_file(repo: str, path: str, commit_msg: str) -> bool:
    """
    Удалить файл из репо.
    """
    sha = await _get_file_sha(repo, path)
    if not sha:
        return False

    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/contents/{path}"

    async with httpx.AsyncClient() as client:
        r = await client.delete(url, headers=HEADERS, json={
            "message": commit_msg,
            "sha": sha
        })
        r.raise_for_status()
        return True


async def _get_file_sha(repo: str, path: str) -> Optional[str]:
    """
    Внутренняя функция: получить sha файла (нужен для обновления/удаления).
    Возвращает None если файл не существует.
    """
    url = f"{BASE_URL}/repos/{GITHUB_USER}/{repo}/contents/{path}"

    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=HEADERS)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("sha")

"""
coder.py — агент Кодер (Cilly)
Генерирует код, пушит на GitHub, мониторит логи всех ботов и автофиксит баги.
"""

import asyncio
import os
import sys
import json
import time
import httpx
import logging
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.weekly_report import register_weekly_handlers

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, MessageReactionUpdated
from aiogram.filters import CommandStart
from anthropic import AsyncAnthropic
import redis.asyncio as aioredis
from ai_office_shared.shared.logging import log_event, read_logs

from shared.github_tools import (
    push_file, read_file, list_files, create_repo,
    create_branch, push_file_to_branch, create_pull_request, merge_pull_request, get_pr_by_url,
)
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetDialogFiltersRequest, UpdateDialogFilterRequest
from telethon.tl.functions.channels import InviteToChannelRequest, EditAdminRequest
from telethon.tl.functions.messages import EditChatAdminRequest
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import DialogFilter, InputPeerUser, InputPeerChannel, ChatAdminRights

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.environ.get("CODER_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN", "")
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_KEY") or ""
LESSONS_CHAT_ID = os.getenv("LESSONS_CHAT_ID")
OFFICE_CHAT_ID  = os.getenv("OFFICE_CHAT_ID")

# Ollama — локальная модель для лёгких задач (Haiku-tier classification)
OLLAMA_HOST     = os.getenv("OLLAMA_HOST", "").strip().rstrip("/\\")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "qwen3:8b")
OLLAMA_ENABLED  = os.getenv("OLLAMA_ENABLED", "").lower() in ("1", "true", "yes")
RAILWAY_TOKEN   = os.getenv("RAILWAY_TOKEN_VLAD") or os.getenv("RAILWAY_TOKEN")  # VLAD-token приоритет (audit fix)
RAILWAY_PROJECT = "271b40b7-199a-429a-88ef-ca417f26a638"
RAILWAY_ENV_ID  = "2efaaf60-ba39-492c-bf86-007fd505493f"  # BUILD:20260518-1803
GITHUB_USER     = "unperson22-alt"
LESSONS_FILE    = "lessons/lessons.json"

MONITOR_INTERVAL   = 300  # секунд между проверками логов
TEMPLATE_BOTS_FILE = "shared/template_bots.json"  # реестр ботов созданных по шаблону

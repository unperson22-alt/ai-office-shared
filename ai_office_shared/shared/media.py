"""
ai_office_shared.shared.media — единый приём изображений из Telegram.

Зачем: до 2026-07-25 НИ ОДИН бот офиса не обрабатывал `filters.Document.IMAGE`.
Картинка, отправленная файлом (Telegram Desktop «отправить без сжатия», пересланный
скриншот, png/webp), молча пропадала — и в группе, и в ЛС. Симптом из логов:
«кинул картинку — а ты молчишь». Vision-код у ботов был рабочий, ломался приём.

Второй источник молчания — групповой путь: `handle_photo`/`handle_message` делали голый
`return` на group/supergroup. Здесь же живёт общий предикат `addressed_in_group`, чтобы
текст и фото в группе вели себя одинаково.

ИСПОЛЬЗОВАНИЕ:
    from ai_office_shared.shared.media import (
        IMAGE_FILTER, extract_image, addressed_in_group, ImageError,
    )

    # регистрация хендлера
    ptb.add_handler(MessageHandler(IMAGE_FILTER, handle_photo))

    # в хендлере
    img = await extract_image(update.message, context.bot)
    if isinstance(img, ImageError):
        await update.message.reply_text(img.user_message)   # НИКОГДА не молчим
        return
    ... process(img.b64, img.media_type) ...

КОНТРАКТ: `extract_image` не бросает исключений — всегда либо `ImagePayload`,
либо `ImageError` с готовым текстом для пользователя.
"""

from __future__ import annotations

import base64
import logging
from typing import NamedTuple, Optional, Union

logger = logging.getLogger("ai_office_shared.media")

# Что принимает Claude Vision. Всё остальное (heic, tiff, bmp, svg) — отказ с текстом.
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

# Практический потолок vision API по одному изображению (до base64-раздувания).
MAX_IMAGE_BYTES = 5 * 1024 * 1024

_EXT_TO_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "jpe": "image/jpeg",
    "png": "image/png", "gif": "image/gif", "webp": "image/webp",
}


class ImagePayload(NamedTuple):
    """Готовое к отправке в vision изображение."""
    b64: str
    media_type: str
    caption: str
    source: str          # "photo" | "document" — для логов


class ImageError(NamedTuple):
    """Отказ с готовым текстом пользователю. Молчать нельзя ни в одном случае."""
    reason: str          # машинный код для логов
    user_message: str    # что показать пользователю


def _mime_from_name(file_name: str) -> Optional[str]:
    if not file_name or "." not in file_name:
        return None
    return _EXT_TO_MIME.get(file_name.rsplit(".", 1)[-1].lower())


def _resolve_document_mime(document) -> Optional[str]:
    """mime_type документа, с откатом на расширение имени файла."""
    mime = (getattr(document, "mime_type", "") or "").lower().split(";")[0].strip()
    if mime:
        return mime
    return _mime_from_name(getattr(document, "file_name", "") or "")


def has_image(message) -> bool:
    """Есть ли в сообщении картинка — фотографией ИЛИ файлом."""
    if message is None:
        return False
    if getattr(message, "photo", None):
        return True
    document = getattr(message, "document", None)
    if document is None:
        return False
    mime = _resolve_document_mime(document)
    return bool(mime and mime.startswith("image/"))


async def extract_image(message, bot) -> Union[ImagePayload, ImageError]:
    """
    Достаёт изображение из сообщения (photo ИЛИ document с image-mime).

    Никогда не бросает исключение — при любой проблеме возвращает `ImageError`
    с текстом, который можно отправить пользователю как есть.
    """
    if message is None:
        return ImageError("no_message", "⚠️ Не вижу сообщения с картинкой.")

    caption = (getattr(message, "caption", None) or "").strip()

    photo = getattr(message, "photo", None)
    document = getattr(message, "document", None)

    if photo:
        target = photo[-1]           # самый большой размер
        media_type = "image/jpeg"    # Telegram всегда перекодирует photo в jpeg
        source = "photo"
    elif document is not None:
        mime = _resolve_document_mime(document)
        if not mime or not mime.startswith("image/"):
            return ImageError(
                "not_an_image",
                "⚠️ Это не картинка — я умею читать только изображения "
                "(jpg, png, gif, webp).",
            )
        if mime not in SUPPORTED_IMAGE_TYPES:
            return ImageError(
                f"unsupported_mime={mime}",
                f"⚠️ Формат {mime} я прочитать не могу. "
                "Пришли, пожалуйста, в jpg, png, gif или webp.",
            )
        target = document
        media_type = mime
        source = "document"
    else:
        return ImageError("no_image", "⚠️ В сообщении нет картинки.")

    size = getattr(target, "file_size", None) or 0
    if size and size > MAX_IMAGE_BYTES:
        mb = MAX_IMAGE_BYTES // (1024 * 1024)
        return ImageError(
            f"too_large={size}",
            f"⚠️ Картинка слишком большая (лимит {mb} МБ). "
            "Пришли поменьше или сожми.",
        )

    try:
        file_obj = await bot.get_file(target.file_id)
        raw = bytes(await file_obj.download_as_bytearray())
    except Exception as e:
        logger.warning("extract_image download failed (%s): %s", source, e)
        return ImageError(
            "download_failed",
            "⚠️ Не смог скачать картинку из Telegram. Попробуй прислать ещё раз.",
        )

    if not raw:
        return ImageError("empty_file", "⚠️ Картинка пришла пустой. Попробуй ещё раз.")
    if len(raw) > MAX_IMAGE_BYTES:
        mb = MAX_IMAGE_BYTES // (1024 * 1024)
        return ImageError(
            f"too_large={len(raw)}",
            f"⚠️ Картинка слишком большая (лимит {mb} МБ). Пришли поменьше или сожми.",
        )

    return ImagePayload(
        b64=base64.b64encode(raw).decode(),
        media_type=media_type,
        caption=caption,
        source=source,
    )


def bot_username(bot) -> str:
    """
    Безопасно достаёт username бота.

    ВАЖНО: telegram.Bot.username — property, которое бросает RuntimeError, если бот
    ещё не инициализирован (`Bot.bot` → `_bot_user is None`). getattr(bot, "username", "")
    от этого НЕ спасает: он глушит только AttributeError. В хендлерах бот уже
    инициализирован, но полагаться на это в общем хелпере нельзя.
    """
    try:
        return bot.username or ""
    except Exception:
        return ""


def addressed_in_group(message, username: str = "", bot_name: str = "") -> bool:
    """
    Обращались ли к боту в группе: reply на его сообщение, @-меншн или имя в тексте.

    Общий предикат для текста и фото — чтобы группа не вела себя асимметрично
    («на текст с меншном отвечаю, на фото с меншном молчу»).

    username — берётся через bot_username(context.bot) (см. выше, там try/except).
    """
    if message is None:
        return False

    uname_norm = username.lower().lstrip("@") if username else ""

    reply = getattr(message, "reply_to_message", None)
    if reply is not None:
        sender = getattr(reply, "from_user", None)
        if sender is not None and getattr(sender, "is_bot", False):
            sender_uname = (getattr(sender, "username", "") or "").lower()
            if uname_norm and sender_uname == uname_norm:
                return True

    text = (getattr(message, "text", None) or getattr(message, "caption", None) or "")
    if not text:
        return False
    low = text.lower()

    if uname_norm and "@" + uname_norm in low:
        return True
    if bot_name and bot_name.lower() in low:
        return True
    return False


# ── Готовый фильтр для регистрации хендлеров ────────────────────────────────────
# Импорт telegram отложен и защищён: shared-пакет ставится и в окружения без PTB
# (скрипты, тесты, Силли), и падать на импорте модуля он не должен.
try:  # pragma: no cover — зависит от окружения
    from telegram.ext import filters as _filters

    IMAGE_FILTER = _filters.PHOTO | _filters.Document.IMAGE
except Exception:  # pragma: no cover
    IMAGE_FILTER = None

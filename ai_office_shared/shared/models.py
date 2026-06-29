"""
ai_office_shared.shared.models
Единый источник правды по id моделей Anthropic для всех ботов офиса.

Использование:
    from ai_office_shared.shared.models import MODEL_SONNET, MODEL_HAIKU

    r = await client.messages.create(model=MODEL_SONNET, ...)

Каждую константу можно переопределить через переменную окружения с тем же именем
(например MODEL_SONNET=claude-sonnet-4-7), чтобы менять модель без правки кода.
"""
import os

# Основная модель ботов (диалог, синтез).
MODEL_SONNET = os.getenv("MODEL_SONNET", "claude-sonnet-4-6")

# Лёгкая/быстрая модель (роутинг, enhance, extract).
MODEL_HAIKU = os.getenv("MODEL_HAIKU", "claude-haiku-4-5-20251001")

# Тяжёлая модель для сложных рассуждений.
MODEL_OPUS = os.getenv("MODEL_OPUS", "claude-opus-4-8")

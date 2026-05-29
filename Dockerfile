FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

RUN pip install --no-cache-dir \
    aiogram==3.7.0 \
    "anthropic>=0.40.0" \
    "aiohttp>=3.9.0" \
    "redis>=4.5.0" \
    "telethon>=1.34.0" \
    "httpx>=0.26.0" \
    "python-telegram-bot>=20.0" \
    "requests>=2.31.0"

CMD ["python", "agents/coder.py"]

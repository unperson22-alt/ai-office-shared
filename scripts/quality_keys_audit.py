#!/usr/bin/env python3
"""
quality_keys_audit.py — аудит Redis quality ключей AI офиса.

Читает все office:quality:* ключи из Redis и сравнивает с
ожидаемым набором из SYSTEM_STATE.md.

Находит:
  - Ключи которые есть в Redis но не ожидаются (rogue бот?)
  - Ожидаемые ключи которых нет (бот ни разу не получал реакцию)
  - Рассинхрон AGENT (health key) vs quality key (Доктор/Дилли)

Запуск:
    REDIS_URL=redis://... python quality_keys_audit.py
"""
import asyncio, os, json
import redis.asyncio as aioredis

# Ожидаемые quality ключи (lowercase, как пишет каждый бот)
EXPECTED_QUALITY_KEYS = {
    "билли",
    "тилли",
    "милли",
    "доктор",   # dilly-bot пишет "доктор"
    "крисс",
    "эллис",
    "вилли",
    "гослинг",
    "силли",
    "фили",
}

# Маппинг quality_key → health_agent (UPPERCASE)
# Если они расходятся — потенциальная проблема атрибуции
QUALITY_TO_HEALTH = {
    "билли":    "БИЛЛИ",
    "тилли":    "ТИЛЛИ",
    "милли":    "МИЛЛИ",
    "доктор":   "ДИЛЛИ",   # ⚠️ РАССИНХРОН: quality="доктор", health="ДИЛЛИ"
    "крисс":    "КРИС",    # ⚠️ Uppercase agent КРИС (одна С), quality крисс (две С)
    "эллис":    "ЭЛЛИС",
    "вилли":    "ВИЛЛИ",
    "гослинг":  "ГОСЛИНГ",
    "силли":    "СИЛЛИ",
    "фили":     "ФИЛИ",
}


async def run_audit():
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    redis = aioredis.from_url(redis_url, decode_responses=True)

    print("=== Quality Keys Audit ===\n")

    # 1. Сканируем все office:quality:* ключи
    actual_keys = set()
    async for key in redis.scan_iter("office:quality:*", count=100):
        bot_name = key.split(":")[-1]
        actual_keys.add(bot_name)

    print(f"Найдено в Redis: {sorted(actual_keys)}")
    print(f"Ожидается:       {sorted(EXPECTED_QUALITY_KEYS)}\n")

    # 2. Rogue ключи (есть в Redis, нет в ожидаемых)
    rogue = actual_keys - EXPECTED_QUALITY_KEYS
    if rogue:
        print(f"⚠️  ROGUE ключи (неожиданные): {rogue}")
        for k in rogue:
            data = await redis.hgetall(f"office:quality:{k}")
            print(f"   office:quality:{k} = {data}")
    else:
        print("✅ Нет неожиданных ключей")

    # 3. Отсутствующие ожидаемые ключи
    missing = EXPECTED_QUALITY_KEYS - actual_keys
    if missing:
        print(f"\nℹ️  Отсутствуют (нет реакций): {missing}")
    else:
        print("✅ Все ожидаемые ключи присутствуют")

    # 4. Значения для каждого бота
    print("\n=== Статистика качества ===")
    for bot_key in sorted(EXPECTED_QUALITY_KEYS):
        data = await redis.hgetall(f"office:quality:{bot_key}")
        up   = int(data.get("up",   0))
        down = int(data.get("down", 0))
        total = up + down
        pct = f"{up/total*100:.0f}%" if total else "n/a"
        health_key = QUALITY_TO_HEALTH.get(bot_key, "?")
        health_val = await redis.get(f"office:health:{health_key}")
        marker = "⚠️ " if bot_key == "доктор" else "  "
        print(f"  {marker}{bot_key:<10} quality={up}↑{down}↓ ({pct}) | health:{health_key}={health_val or 'none'}")

    # 5. Специальная проверка Доктора (DATA-001)
    print("\n=== DATA-001: Доктор/Дилли рассинхрон ===")
    q_dok = await redis.hgetall("office:quality:доктор")
    q_dil = await redis.hgetall("office:quality:дилли")
    h_dil = await redis.get("office:health:ДИЛЛИ")
    h_dok = await redis.get("office:health:ДОКТОР")
    print(f"  office:quality:доктор = {q_dok}")
    print(f"  office:quality:дилли  = {q_dil}")
    print(f"  office:health:ДИЛЛИ   = {h_dil}")
    print(f"  office:health:ДОКТОР  = {h_dok}")
    if q_dil:
        print("  ⚠️  Есть записи под ключом :дилли — рассинхрон подтверждён!")
    else:
        print("  ✅ Записей под :дилли нет — бот пишет как :доктор")

    await redis.aclose()
    print("\n=== Аудит завершён ===")


if __name__ == "__main__":
    asyncio.run(run_audit())

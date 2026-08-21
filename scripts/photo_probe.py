#!/usr/bin/env python3
"""
Проверка обработки фото ДО внедрения в бота: гоняет локальный движок на реальном
файле и стучится в подключённые внешние API.

Смысл: не верить обещаниям сайтов «бесплатный API», а посмотреть на HTTP-код и
байты. Половина «бесплатных» провайдеров на проверке 2026-08-15 оказалась
платной (Pollinations kontext → 500 «only available on enter.pollinations.ai»,
Gemini image → нет free tier в прайсе, ZSky → только план $99/мес).

ЗАПУСК:
    python scripts/photo_probe.py                 # своя тестовая картинка
    python scripts/photo_probe.py photo.jpg       # на своём фото
    python scripts/photo_probe.py photo.jpg --out /tmp/probe   # сохранить результаты

Выход: 0 — локальный движок здоров (внешние API опциональны), 1 — сломан.
"""
import argparse
import asyncio
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_office_shared.shared import photo  # noqa: E402

OPS = ["", "сочное", "винтаж", "чб", "портрет", "ретушь", "прибери недоліки",
       "квадрат", "стори", "на аву", "увеличь", "сожми", "убери фон",
       "размой фон", "стикер"]


def _sample() -> bytes:
    """Градиент с деталями — на нём видно и цвет, и резкость."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (900, 1200))
    px = im.load()
    for y in range(1200):
        for x in range(900):
            px[x, y] = (40 + x * 180 // 900, 60 + y * 150 // 1200, 120 - x * 90 // 900)
    d = ImageDraw.Draw(im)
    for i in range(0, 900, 40):
        d.line((i, 0, i + 200, 1200), fill=(230, 230, 220), width=1)
    d.ellipse((300, 400, 600, 700), fill=(220, 190, 170))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=95)
    return buf.getvalue()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image", nargs="?", help="путь к фото (по умолчанию — синтетика)")
    ap.add_argument("--out", help="куда сложить результаты")
    ap.add_argument("--ai", help="промпт для проверки AI-перерисовки (нужны ключи CF_*)")
    args = ap.parse_args()

    from importlib.util import find_spec
    if find_spec("PIL") is None:
        print("❌ Pillow не установлен — локальный движок работать не будет.")
        print("   pip install pillow")
        return 1

    raw = open(args.image, "rb").read() if args.image else _sample()
    print(f"вход: {len(raw)/1024:.1f} КБ\n")
    if args.out:
        os.makedirs(args.out, exist_ok=True)

    print(f"{'запрос':<14} {'операция':<16} {'время':>8} {'вес':>9}  результат")
    print("-" * 74)
    failed = []
    for req in OPS:
        t = time.time()
        res = await photo.process_photo(raw, req)
        dt = (time.time() - t) * 1000
        size = f"{len(res.data)/1024:.0f} КБ" if res.data else "—"
        mark = "✅" if res.ok else "❌"
        print(f"{(req or '(пусто)'):<14} {res.op:<16} {dt:>6.0f}мс {size:>9}  "
              f"{mark} {res.caption or res.error}")
        if not res.ok:
            failed.append(req)
        if args.out and res.data:
            ext = "png" if res.fmt == "png" else "jpg"
            name = res.op.replace(":", "_").replace("/", "_")
            open(os.path.join(args.out, f"{name}.{ext}"), "wb").write(res.data)

    print()
    print(f"rembg (вырез фона локально): {'✅ доступен' if photo.rembg_available() else '❌ нет'}")
    acc, tok = await photo._cf_creds()
    print(f"Cloudflare Workers AI:       {'✅ ключи есть' if acc and tok else '❌ ключей нет'}")

    if args.ai:
        t = time.time()
        res = await photo.ai_restyle(raw, args.ai)
        print(f"AI-перерисовка: {'✅' if res.ok else '❌'} {(time.time()-t):.1f}с "
              f"{res.caption or res.error}")
        if args.out and res.data:
            open(os.path.join(args.out, "ai.png"), "wb").write(res.data)

    if failed:
        print(f"\n❌ упало: {failed}")
        return 1
    print("\n✅ локальный движок здоров")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

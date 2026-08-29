"""Недочитанный трейсбек откладывается, а не режется пополам.

Инцидент 27–29.08.2026. Монитор читает логи Railway по водяному знаку, а
Railway отдаёт каждую строку стека отдельной записью со своей меткой времени.
Чтение, попавшее в середину выгрузки, забирало заголовок с кадрами БЕЗ строки
исключения — и терялись обе половины:

  * огрызок уходил на разбор без имени исключения → classify_fault не видела
    ни одного внешнего паттерна (все они — имена исключений) и возвращала
    «наш баг» → 11 ложных заметок в офис за трое суток;
  * хвост в следующем цикле приезжал сиротой, без заголовка, и его отбрасывал
    ERROR_PATTERNS: там есть «Error:»/«Exception:», но не «httpx.ReadTimeout:».

Здесь проверяется сама проводка в `get_service_logs`, а не её пересказ:
функция достаётся из coder.py через AST (файл не импортируется — на уровне
модуля читается os.environ и поднимается aiogram) и исполняется с заглушками.
"""

import ast
import asyncio
import logging
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ai_office_shared.shared.fault_class import classify  # noqa: E402
from ai_office_shared.shared.traceback_scan import (  # noqa: E402
    error_lines, unterminated_tail,
)

TB = [
    "INFO | fetching market data",
    "Traceback (most recent call last):",
    '  File "/app/molly_trader/trader.py", line 214, in fetch_market_data',
    "    response = await client.get(url, timeout=25.0)",
    "httpx.ReadTimeout: timed out while reading response after 25.0 seconds",
]
RECORDS = [{"message": m, "timestamp": f"2026-08-29T10:15:0{i}+00:00"}
           for i, m in enumerate(TB)]


class _FakeRedis:
    def __init__(self):
        self.d = {}

    async def get(self, k):
        return self.d.get(k)

    async def set(self, k, v):
        self.d[k] = v

    async def setex(self, k, ttl, v):
        self.d[k] = v

    async def delete(self, k):
        self.d.pop(k, None)


def _load_get_service_logs(redis, available):
    """Настоящая get_service_logs из coder.py, с заглушками вокруг."""
    with open(os.path.join(ROOT, "agents", "coder.py"), encoding="utf-8") as fh:
        node = next(n for n in ast.parse(fh.read()).body
                    if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "get_service_logs")

    async def railway_query(query, variables=None):
        if "deployments(" in query:
            return {"data": {"deployments": {"edges": [{"node": {"id": "dep1"}}]}}}
        return {"data": {"deploymentLogs": RECORDS[:available["n"]]}}

    async def get_redis():
        return redis

    ns = {
        "railway_query": railway_query, "get_redis": get_redis,
        "redact": lambda s: s, "time": __import__("time"),
        "logger": logging.getLogger("test"), "last_seen": {}, "_tb_holdback": {},
        "unterminated_tail": unterminated_tail, "asyncio": asyncio,
    }
    exec(compile(ast.Module([node], []), "<coder>", "exec"), ns)
    return ns["get_service_logs"]


class TestHoldback(unittest.TestCase):
    def setUp(self):
        self.redis = _FakeRedis()
        self.available = {"n": len(RECORDS) - 1}   # строка исключения ещё в пути
        self.gsl = _load_get_service_logs(self.redis, self.available)

    def test_incomplete_block_is_not_delivered(self):
        """Цикл 1: огрызок не отдаётся — именно он и порождал ложный диагноз."""
        out = asyncio.run(self.gsl("svc"))
        self.assertEqual(out, ["INFO | fetching market data"])
        self.assertNotIn("Traceback (most recent call last):", out)
        self.assertTrue(self.redis.d.get("tb_holdback:svc"))

    def test_next_read_delivers_the_whole_stack(self):
        """Цикл 2: стек дописался — приходит целиком, вместе с заголовком."""
        asyncio.run(self.gsl("svc"))
        self.available["n"] = len(RECORDS)
        out = asyncio.run(self.gsl("svc"))
        self.assertEqual(out, TB[1:])
        self.assertIn("httpx.ReadTimeout: timed out while reading response "
                      "after 25.0 seconds", out)

    def test_reassembled_stack_classifies_as_external(self):
        """Итог всей цепочки: сбой снова опознаётся как внешний → офис молчит.

        Ровно это и не срабатывало 27–29.08: по половине стека вердикт был
        "internal", и заметка уходила в группу.
        """
        asyncio.run(self.gsl("svc"))
        self.available["n"] = len(RECORDS)
        out = asyncio.run(self.gsl("svc"))
        self.assertEqual(classify(error_lines(out, ignore=[])), "external")

    def test_holdback_is_released_after_one_cycle(self):
        """Инвариант №6: у петли есть потолок.

        Процесс мог умереть посреди печати стека (SIGKILL/OOM) — дописывать
        некому. Держать такой блок вечно значит ослепить монитор по сервису,
        поэтому со второго чтения он отдаётся как есть, а неполноту дальше
        называет classify (вердикт "unknown"), а не догадка.
        """
        asyncio.run(self.gsl("svc"))                    # придержали
        out = asyncio.run(self.gsl("svc"))              # стек так и не дописан
        self.assertIn("Traceback (most recent call last):", out)
        self.assertFalse(self.redis.d.get("tb_holdback:svc"))
        self.assertEqual(classify(error_lines(out, ignore=[])), "unknown")

    def test_complete_log_is_passed_through_untouched(self):
        """Целый стек ничего не задерживает — придержка не должна тормозить норму."""
        self.available["n"] = len(RECORDS)
        out = asyncio.run(self.gsl("svc"))
        self.assertEqual(out, TB)
        self.assertFalse(self.redis.d.get("tb_holdback:svc"))

    def test_watermark_does_not_pass_the_held_header(self):
        """Отметка встаёт ПЕРЕД заголовком, иначе хвост уехал бы вместе с ним."""
        asyncio.run(self.gsl("svc"))
        watermark = float(self.redis.d["last_seen:svc"])
        header_ts = float(self.redis.d["tb_holdback:svc"])
        self.assertLess(watermark, header_ts)


if __name__ == "__main__":
    unittest.main()

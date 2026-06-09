"""
ai_office_shared.shared.currency
Курсы валют через Frankfurter API (бесплатно, данные ЕЦБ, без ключа).

Использование:
    from ai_office_shared.shared.currency import get_rate, get_rates_text

    rate = await get_rate("USD", "EUR")
    text = await get_rates_text("EUR")
"""
import logging
import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.frankfurter.app"
_TIMEOUT = 10.0

DEFAULT_PAIRS = ["USD", "UAH", "THB", "VND", "RUB"]


async def get_rate(from_: str, to: str) -> float | None:
    """Курс одной валюты."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_BASE}/latest", params={"from": from_, "to": to})
            r.raise_for_status()
            return r.json().get("rates", {}).get(to)
    except Exception as e:
        logger.warning(f"[currency] get_rate {from_}/{to}: {e}")
        return None


async def get_rates(base: str = "EUR", targets: list[str] | None = None) -> dict[str, float]:
    """Несколько курсов от одной базовой валюты."""
    pairs = targets or DEFAULT_PAIRS
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_BASE}/latest", params={"from": base, "to": ",".join(pairs)})
            r.raise_for_status()
            return r.json().get("rates", {})
    except Exception as e:
        logger.warning(f"[currency] get_rates {base}: {e}")
        return {}


async def get_rates_text(base: str = "EUR", targets: list[str] | None = None) -> str:
    """Форматированная строка для инжекта в промпт. Пример: '1 EUR = USD 1.08 | UAH 44.2'"""
    rates = await get_rates(base, targets)
    if not rates:
        return f"Не удалось получить курсы для {base}."
    parts = [f"{cur} {rate:.4g}" for cur, rate in rates.items()]
    return f"1 {base} = " + " | ".join(parts)

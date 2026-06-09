"""
ai_office_shared.shared.crypto
Данные о криптовалютах через CoinGecko API (бесплатно, без ключа).

Использование:
    from ai_office_shared.shared.crypto import get_price, get_prices_text

    price = await get_price("bitcoin")
    text = await get_prices_text()
"""
import logging
import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.coingecko.com/api/v3"
_TIMEOUT = 10.0

COINS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "binancecoin",
    "TON": "the-open-network",
}


async def get_price(coin_id: str, vs: str = "usd") -> dict | None:
    """
    Цена одной монеты.

    Returns:
        {"price": float, "change_24h": float} или None
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(
                f"{_BASE}/simple/price",
                params={
                    "ids": coin_id,
                    "vs_currencies": vs,
                    "include_24hr_change": "true",
                },
            )
            r.raise_for_status()
            data = r.json().get(coin_id, {})
            return {
                "price": data.get(vs, 0),
                "change_24h": round(data.get(f"{vs}_24h_change", 0), 2),
            }
    except Exception as e:
        logger.warning(f"[crypto] get_price {coin_id}: {e}")
        return None


async def get_prices(coins: list[str] | None = None) -> dict[str, dict]:
    """
    Цены нескольких монет одним запросом.

    Returns:
        {"BTC": {"price": ..., "change_24h": ...}, ...}
    """
    tickers = coins or list(COINS.keys())
    ids = [COINS[t] for t in tickers if t in COINS]
    if not ids:
        return {}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(
                f"{_BASE}/simple/price",
                params={
                    "ids": ",".join(ids),
                    "vs_currencies": "usd",
                    "include_24hr_change": "true",
                },
            )
            r.raise_for_status()
            data = r.json()
        result = {}
        for ticker in tickers:
            cg_id = COINS.get(ticker)
            if cg_id and cg_id in data:
                d = data[cg_id]
                result[ticker] = {
                    "price": d.get("usd", 0),
                    "change_24h": round(d.get("usd_24h_change", 0), 2),
                }
        return result
    except Exception as e:
        logger.warning(f"[crypto] get_prices: {e}")
        return {}


async def get_prices_text(coins: list[str] | None = None) -> str:
    """
    Форматированная строка для инжекта в промпт.

    Пример: "BTC $65,432 (+2.3%) | ETH $3,210 (-0.8%)"
    """
    prices = await get_prices(coins)
    if not prices:
        return "Не удалось получить данные о ценах."
    parts = []
    for ticker, d in prices.items():
        sign = "+" if d["change_24h"] >= 0 else ""
        parts.append(f"{ticker} ${d['price']:,.0f} ({sign}{d['change_24h']}%)")
    return " | ".join(parts)

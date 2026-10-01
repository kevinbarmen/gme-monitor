"""Цена за день — бесплатный chart-эндпоинт Yahoo Finance (тот же, что внутри yfinance)."""
from __future__ import annotations

from datetime import datetime, timezone


def fetch_day_move(http, ticker: str) -> dict:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=5d&interval=1d"
    data = http.get_json(url, cache=False)
    result = data["chart"]["result"][0]
    meta = result["meta"]
    price = meta["regularMarketPrice"]
    market_ts = meta["regularMarketTime"]
    market_day = datetime.fromtimestamp(market_ts, timezone.utc).date()

    # предыдущее закрытие = close последнего дневного бара раньше дня текущей цены
    closes = result["indicators"]["quote"][0]["close"]
    prev_close = None
    for ts, close in zip(result["timestamp"], closes):
        if close is not None and datetime.fromtimestamp(ts, timezone.utc).date() < market_day:
            prev_close = close
    if prev_close is None:
        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
    if not prev_close:
        raise RuntimeError("нет предыдущего закрытия в ответе Yahoo")
    return {
        "ticker": ticker,
        "price": price,
        "prev_close": prev_close,
        "change_pct": (price / prev_close - 1) * 100,
        "market_day": market_day.isoformat(),
    }

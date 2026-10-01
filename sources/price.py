"""Цена — бесплатный chart-эндпоинт Yahoo Finance (тот же, что внутри yfinance)."""
from __future__ import annotations

from datetime import datetime, timezone


def _chart(http, ticker: str, range_: str) -> dict:
    # range=max Yahoo отдаёт помесячно — для всей истории берём period1/period2
    span = "period1=0&period2=9999999999" if range_ == "max" else f"range={range_}"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?{span}&interval=1d"
    return http.get_json(url, cache=False)["chart"]["result"][0]


def _bars(result: dict) -> list[dict]:
    q = result["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(result.get("timestamp") or []):
        if q["close"][i] is None:
            continue
        out.append({
            "date": datetime.fromtimestamp(ts, timezone.utc).date().isoformat(),
            "open": q["open"][i], "high": q["high"][i], "low": q["low"][i],
            "close": q["close"][i], "volume": q["volume"][i],
        })
    return out


def fetch_history(http, ticker: str, range_: str = "max") -> list[dict]:
    """Дневные бары за период (max — вся история, для первичного заполнения дневника)."""
    return _bars(_chart(http, ticker, range_))


def fetch_day_move(http, ticker: str) -> dict:
    """Текущая цена vs предыдущее закрытие + дневные бары за последний месяц."""
    result = _chart(http, ticker, "1mo")
    meta = result["meta"]
    price = meta["regularMarketPrice"]
    market_day = datetime.fromtimestamp(meta["regularMarketTime"], timezone.utc).date()
    bars = _bars(result)

    # предыдущее закрытие = close последнего бара раньше дня текущей цены
    prev_close = None
    for b in bars:
        if b["date"] < market_day.isoformat():
            prev_close = b["close"]
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
        "bars": bars,
    }

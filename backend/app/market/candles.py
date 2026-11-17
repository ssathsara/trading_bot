"""Candle ingestion, storage and data-quality checks."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MarketCandle

TIMEFRAMES: dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


@dataclass(frozen=True)
class Candle:
    open_time: int
    close_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trade_count: int

    @classmethod
    def from_rest(cls, row: list) -> "Candle":
        return cls(int(row[0]), int(row[6]), Decimal(row[1]), Decimal(row[2]), Decimal(row[3]),
                   Decimal(row[4]), Decimal(row[5]), Decimal(row[7]), int(row[8]))

    def is_valid(self, timeframe: str) -> bool:
        dur = TIMEFRAMES[timeframe]
        return (
            self.low > 0
            and self.low <= min(self.open, self.close)
            and self.high >= max(self.open, self.close)
            and self.volume >= 0
            and self.close_time == self.open_time + dur - 1
        )


def is_closed(c: Candle, now_ms: int | None = None) -> bool:
    return c.close_time < (now_ms if now_ms is not None else int(time.time() * 1000))


def store_candles(session: Session, symbol: str, timeframe: str, candles: list[Candle],
                  now_ms: int | None = None) -> int:
    """Insert CLOSED, valid candles that are not already stored. Returns rows inserted."""
    good = [c for c in candles if is_closed(c, now_ms) and c.is_valid(timeframe)]
    if not good:
        return 0
    lo, hi = ms_to_dt(min(c.open_time for c in good)), ms_to_dt(max(c.open_time for c in good))
    existing = {
        dt_to_ms(t)
        for t in session.scalars(
            select(MarketCandle.open_time).where(
                MarketCandle.symbol == symbol, MarketCandle.timeframe == timeframe,
                MarketCandle.open_time >= lo, MarketCandle.open_time <= hi,
            )
        )
    }
    inserted = 0
    seen: set[int] = set()
    for c in sorted(good, key=lambda c: c.open_time):
        if c.open_time in existing or c.open_time in seen:
            continue
        seen.add(c.open_time)
        session.add(MarketCandle(
            symbol=symbol, timeframe=timeframe, open_time=ms_to_dt(c.open_time), close_time=ms_to_dt(c.close_time),
            open_price=c.open, high_price=c.high, low_price=c.low, close_price=c.close,
            volume=c.volume, quote_volume=c.quote_volume, trade_count=c.trade_count,
        ))
        inserted += 1
    session.flush()
    return inserted


def load_frame(session: Session, symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
    """Most recent ``limit`` stored candles as a float DataFrame (oldest first).

    Floats are used ONLY for indicator mathematics; accounting uses Decimal.
    """
    rows = session.execute(
        select(MarketCandle).where(MarketCandle.symbol == symbol, MarketCandle.timeframe == timeframe)
        .order_by(MarketCandle.open_time.desc()).limit(limit)
    ).scalars().all()
    return candles_to_frame([
        Candle(dt_to_ms(r.open_time), dt_to_ms(r.close_time), r.open_price, r.high_price, r.low_price,
               r.close_price, r.volume, r.quote_volume, r.trade_count) for r in reversed(rows)
    ])


def candles_to_frame(candles: list[Candle]) -> pd.DataFrame:
    cols = ["open_time", "close_time", "open", "high", "low", "close", "volume", "quote_volume", "trades"]
    if not candles:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(
        [[c.open_time, c.close_time, float(c.open), float(c.high), float(c.low), float(c.close),
          float(c.volume), float(c.quote_volume), c.trade_count] for c in candles],
        columns=cols,
    )
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df.reset_index(drop=True)


def check_quality(df: pd.DataFrame, timeframe: str, now: datetime | None = None) -> list[str]:
    """Return a list of data-quality issues (empty list = clean)."""
    issues: list[str] = []
    if df.empty:
        return ["no candles"]
    dur = pd.Timedelta(milliseconds=TIMEFRAMES[timeframe])
    if df["open_time"].duplicated().any():
        issues.append("duplicate candles")
    if not df["open_time"].is_monotonic_increasing:
        issues.append("candles out of order")
    gaps = df["open_time"].diff().dropna()
    missing = int(((gaps / dur).round() - 1).clip(lower=0).sum())
    if missing:
        issues.append(f"{missing} missing candle(s)")
    if ((df[["open", "high", "low", "close"]] <= 0).any(axis=1)).any():
        issues.append("non-positive prices")
    bad_hl = (df["high"] < df[["open", "close"]].max(axis=1)) | (df["low"] > df[["open", "close"]].min(axis=1))
    if bad_hl.any():
        issues.append("inconsistent OHLC values")
    expected_close = df["open_time"] + dur - pd.Timedelta(milliseconds=1)
    if (df["close_time"] != expected_close).any():
        issues.append("inconsistent candle timestamps")
    now = now or datetime.now(timezone.utc)
    last_close = df["close_time"].iloc[-1].to_pydatetime()
    if now - last_close > 2 * dur.to_pytimedelta():
        issues.append(f"stale data: last candle closed {last_close.isoformat()}")
    return issues


async def fetch_closed_candles(client, symbol: str, timeframe: str, limit: int) -> list[Candle]:
    """Fetch the latest ``limit`` CLOSED candles via REST (paginates beyond 1000)."""
    out: list[Candle] = []
    end_ms: int | None = None
    remaining = limit + 1  # +1 because the newest candle is usually still open
    while remaining > 0:
        req = min(remaining, 1000)
        rows = await client.klines(symbol, timeframe, limit=req, end_ms=end_ms)
        if not rows:
            break
        batch = [Candle.from_rest(r) for r in rows]
        out = batch + out
        remaining -= len(batch)
        end_ms = batch[0].open_time - 1
        if len(rows) < req:  # no more history available
            break
    now_ms = int(time.time() * 1000)
    closed = [c for c in out if is_closed(c, now_ms)]
    return closed[-limit:]


async def fetch_range(client, symbol: str, timeframe: str, start_ms: int, end_ms: int) -> list[Candle]:
    """Fetch closed candles in [start_ms, end_ms] chronologically (for backtests)."""
    out: list[Candle] = []
    cursor = start_ms
    now_ms = int(time.time() * 1000)
    while cursor <= end_ms:
        rows = await client.klines(symbol, timeframe, limit=1000, start_ms=cursor, end_ms=end_ms)
        if not rows:
            break
        batch = [Candle.from_rest(r) for r in rows]
        out.extend(batch)
        nxt = batch[-1].open_time + TIMEFRAMES[timeframe]
        if nxt <= cursor:
            break
        cursor = nxt
        if len(rows) < 1000:
            break
    seen: set[int] = set()
    uniq = []
    for c in out:
        if c.open_time not in seen and is_closed(c, now_ms):
            seen.add(c.open_time)
            uniq.append(c)
    return uniq

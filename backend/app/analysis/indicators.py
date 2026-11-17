"""Technical indicators (TA-Lib compatible definitions).

Every indicator is *causal*: the value at row i depends only on rows <= i, so the
same functions are safe to use in backtests without look-ahead bias.

Floats are acceptable here: indicators are analytical signals, not accounting.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(x: pd.Series, n: int) -> pd.Series:
    return x.rolling(n, min_periods=n).mean()


def ema(x: pd.Series, n: int) -> pd.Series:
    """EMA seeded with the SMA of the first ``n`` valid values (TA-Lib style)."""
    v = x.to_numpy(dtype=float)
    out = np.full(len(v), np.nan)
    valid = np.flatnonzero(~np.isnan(v))
    if len(valid) < n:
        return pd.Series(out, index=x.index)
    start = valid[0]
    seed_end = start + n - 1
    if np.isnan(v[start:seed_end + 1]).any():
        return pd.Series(out, index=x.index)
    alpha = 2.0 / (n + 1)
    out[seed_end] = v[start:seed_end + 1].mean()
    for i in range(seed_end + 1, len(v)):
        out[i] = out[i - 1] + alpha * (v[i] - out[i - 1]) if not np.isnan(v[i]) else out[i - 1]
    return pd.Series(out, index=x.index)


def _wilder(values: np.ndarray, n: int, first: int) -> np.ndarray:
    """Wilder smoothing; seed = mean(values[first-n+1 .. first])."""
    out = np.full(len(values), np.nan)
    if len(values) <= first:
        return out
    out[first] = values[first - n + 1:first + 1].mean()
    for i in range(first + 1, len(values)):
        out[i] = (out[i - 1] * (n - 1) + values[i]) / n
    return out


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    c = close.to_numpy(dtype=float)
    out = np.full(len(c), np.nan)
    if len(c) <= n:
        return pd.Series(out, index=close.index)
    delta = np.diff(c, prepend=np.nan)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    ag = _wilder(gain, n, n)
    al = _wilder(loss, n, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = ag / al
        out = 100.0 - 100.0 / (1.0 + rs)
    out = np.where((al == 0) & (ag > 0), 100.0, out)
    out = np.where((al == 0) & (ag == 0), 50.0, out)
    out[:n] = np.nan
    return pd.Series(out, index=close.index)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    return mid + k * sd, mid, mid - k * sd


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> np.ndarray:
    h, l, c = (s.to_numpy(dtype=float) for s in (high, low, close))
    pc = np.roll(c, 1)
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    tr[0] = h[0] - l[0]
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    tr = true_range(high, low, close)
    return pd.Series(_wilder(tr, n, n), index=close.index)


def adx(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    h, l = high.to_numpy(dtype=float), low.to_numpy(dtype=float)
    size = len(h)
    nan = np.full(size, np.nan)
    if size < 2 * n + 1:
        return (pd.Series(nan, index=close.index),) * 3  # type: ignore[return-value]
    up = np.diff(h, prepend=np.nan)
    down = -np.diff(l, prepend=np.nan)
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    plus_dm[0] = minus_dm[0] = 0.0
    tr = true_range(high, low, close)

    def wilder_sum(x: np.ndarray) -> np.ndarray:
        out = np.full(size, np.nan)
        out[n] = x[1:n + 1].sum()
        for i in range(n + 1, size):
            out[i] = out[i - 1] - out[i - 1] / n + x[i]
        return out

    s_tr, s_p, s_m = wilder_sum(tr), wilder_sum(plus_dm), wilder_sum(minus_dm)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100 * s_p / s_tr
        mdi = 100 * s_m / s_tr
        dx = 100 * np.abs(pdi - mdi) / (pdi + mdi)
    dx = np.where(np.isnan(dx) & ~np.isnan(pdi), 0.0, dx)
    adx_v = np.full(size, np.nan)
    first = 2 * n - 1
    adx_v[first] = dx[n:first + 1].mean()
    for i in range(first + 1, size):
        adx_v[i] = (adx_v[i - 1] * (n - 1) + dx[i]) / n
    idx = close.index
    return pd.Series(adx_v, index=idx), pd.Series(pdi, index=idx), pd.Series(mdi, index=idx)


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of ``df`` with all strategy indicators appended."""
    out = df.copy()
    if out.empty:
        for col in ("rsi", "ema_9", "ema_20", "ema_50", "ema_200", "macd", "macd_signal", "macd_hist",
                    "bb_upper", "bb_mid", "bb_lower", "atr", "adx", "plus_di", "minus_di", "volume_ma"):
            out[col] = pd.Series(dtype=float)
        return out
    c = out["close"]
    out["rsi"] = rsi(c, 14)
    for n in (9, 20, 50, 200):
        out[f"ema_{n}"] = ema(c, n)
    out["macd"], out["macd_signal"], out["macd_hist"] = macd(c)
    out["bb_upper"], out["bb_mid"], out["bb_lower"] = bollinger(c)
    out["atr"] = atr(out["high"], out["low"], c, 14)
    out["adx"], out["plus_di"], out["minus_di"] = adx(out["high"], out["low"], c, 14)
    out["volume_ma"] = sma(out["volume"], 20)
    return out


def support_resistance(df: pd.DataFrame, lookback: int = 120, window: int = 2, tolerance: float = 0.004) -> dict:
    """Nearest support/resistance from clustered fractal pivots over recent candles."""
    d = df.tail(lookback)
    if len(d) < 2 * window + 1:
        return {"support": None, "resistance": None}
    highs, lows = d["high"].to_numpy(), d["low"].to_numpy()
    piv_h, piv_l = [], []
    # Pivots require `window` candles on BOTH sides, so the newest `window` candles
    # can never be pivots: this uses only completed information.
    for i in range(window, len(d) - window):
        if highs[i] == highs[i - window:i + window + 1].max():
            piv_h.append(highs[i])
        if lows[i] == lows[i - window:i + window + 1].min():
            piv_l.append(lows[i])
    price = float(d["close"].iloc[-1])

    def cluster(levels: list[float]) -> list[float]:
        levels = sorted(levels)
        groups: list[list[float]] = []
        for lv in levels:
            if groups and abs(lv - np.mean(groups[-1])) / lv <= tolerance:
                groups[-1].append(lv)
            else:
                groups.append([lv])
        return [float(np.mean(g)) for g in groups]

    sup = [lv for lv in cluster(piv_l) if lv < price]
    res = [lv for lv in cluster(piv_h) if lv > price]
    return {"support": max(sup) if sup else None, "resistance": min(res) if res else None}

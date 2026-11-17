"""Multi-timeframe trend-following strategy with weighted scoring.

Design rules:
* BUY requires ALL hard gates AND a composite score >= ``min_score``.
  A low RSI alone never triggers a buy; a high RSI alone never triggers a sell.
* The confidence score is a strategy score (0-100), NOT a probability of profit.
* Explanations are generated only from conditions that were actually evaluated.
* The risk engine still has the final say on every BUY.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Literal

import pandas as pd

from app.analysis.indicators import support_resistance
from app.settings_store import StrategySettings

Signal = Literal["BUY", "SELL", "HOLD"]
REQUIRED = ("close", "ema_9", "ema_20", "ema_50", "rsi", "macd", "macd_signal", "atr", "adx", "volume", "volume_ma")


@dataclass
class Condition:
    name: str
    passed: bool
    detail: str
    gate: bool = False  # hard requirement for entry


@dataclass
class MarketCondition:
    trend: Literal["BULLISH", "BEARISH", "SIDEWAYS", "UNKNOWN"]
    rsi_state: Literal["OVERBOUGHT", "OVERSOLD", "NEUTRAL", "UNKNOWN"]
    volume_state: Literal["INCREASING", "DECREASING", "NORMAL", "UNKNOWN"]
    volatility_state: Literal["EXPANDING", "CONTRACTING", "STABLE", "UNKNOWN"]
    support: float | None
    resistance: float | None
    atr_pct: float | None


@dataclass
class SignalResult:
    symbol: str
    signal: Signal
    score: float
    reason: str
    strategy_name: str
    entry_conditions: list[Condition] = field(default_factory=list)
    exit_conditions: list[Condition] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)
    market: dict[str, dict] = field(default_factory=dict)
    snapshot: dict[str, dict] = field(default_factory=dict)
    candle_time: str | None = None
    atr: float | None = None
    price: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else v


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def classify_market(df: pd.DataFrame) -> MarketCondition:
    """Describe market conditions from the latest CLOSED candle of ``df``."""
    if df.empty or len(df) < 60:
        return MarketCondition("UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN", None, None, None)
    last = df.iloc[-1]
    close, e20, e50, rsi_v, adx_v = (_f(last.get(k)) for k in ("close", "ema_20", "ema_50", "rsi", "adx"))
    e50_prev = _f(df["ema_50"].iloc[-6]) if len(df) >= 6 else None
    trend = "UNKNOWN"
    if None not in (close, e20, e50, e50_prev):
        slope = e50 - e50_prev  # type: ignore[operator]
        if adx_v is not None and adx_v < 18:
            trend = "SIDEWAYS"
        elif close > e50 and e20 > e50 and slope > 0:  # type: ignore[operator]
            trend = "BULLISH"
        elif close < e50 and e20 < e50 and slope < 0:  # type: ignore[operator]
            trend = "BEARISH"
        else:
            trend = "SIDEWAYS"
    rsi_state = "UNKNOWN" if rsi_v is None else "OVERBOUGHT" if rsi_v >= 70 else "OVERSOLD" if rsi_v <= 30 else "NEUTRAL"
    vol, vma = _f(last.get("volume")), _f(last.get("volume_ma"))
    volume_state = "UNKNOWN"
    if vol is not None and vma:
        volume_state = "INCREASING" if vol > 1.2 * vma else "DECREASING" if vol < 0.8 * vma else "NORMAL"
    atr_v = _f(last.get("atr"))
    atr_mean = _f(df["atr"].tail(50).mean())
    vol_state = "UNKNOWN"
    if atr_v and atr_mean:
        vol_state = "EXPANDING" if atr_v > 1.2 * atr_mean else "CONTRACTING" if atr_v < 0.8 * atr_mean else "STABLE"
    sr = support_resistance(df)
    return MarketCondition(trend, rsi_state, volume_state, vol_state, sr["support"], sr["resistance"],
                           (atr_v / close * 100) if atr_v and close else None)


def _snapshot(df: pd.DataFrame) -> dict:
    last = df.iloc[-1]
    keys = ("close", "rsi", "ema_9", "ema_20", "ema_50", "ema_200", "macd", "macd_signal", "macd_hist",
            "bb_upper", "bb_lower", "atr", "adx", "plus_di", "minus_di", "volume", "volume_ma")
    snap = {k: (round(v, 8) if (v := _f(last.get(k))) is not None else None) for k in keys}
    snap["candle_open_time"] = str(last.get("open_time"))
    return snap


def evaluate(
    symbol: str,
    frames: dict[str, pd.DataFrame],
    settings: StrategySettings,
    *,
    max_atr_pct: float,
    has_position: bool = False,
    data_issues: list[str] | None = None,
    ml_probability: float | None = None,
    ml_available: bool = False,
    detail: bool = True,
) -> SignalResult:
    """Evaluate the strategy on frames keyed by role: ``trend``, ``setup``, ``entry``.

    Each frame must already contain indicators and ONLY closed candles.
    """
    name = settings.name
    issues = list(data_issues or [])
    for role in ("trend", "setup", "entry"):
        df = frames.get(role)
        if df is None or len(df) < 60:
            issues.append(f"{role} timeframe: insufficient candles")
        elif any(_f(df.iloc[-1].get(k)) is None for k in REQUIRED):
            issues.append(f"{role} timeframe: indicators not yet available")
    if issues:
        return SignalResult(symbol, "HOLD", 0.0, "HOLD: data not usable - " + "; ".join(issues), name,
                            entry_conditions=[Condition("data_quality", False, "; ".join(issues), gate=True)])

    t, s, e = frames["trend"].iloc[-1], frames["setup"].iloc[-1], frames["entry"].iloc[-1]
    tdf, sdf = frames["trend"], frames["setup"]
    g = lambda row, k: _f(row.get(k))  # noqa: E731
    w = settings.weights

    # ---- Component scores (0..1) --------------------------------------------
    t_e20, t_e50, t_close, t_e200 = g(t, "ema_20"), g(t, "ema_50"), g(t, "close"), g(t, "ema_200")
    t_e50_prev = _f(tdf["ema_50"].iloc[-6])
    trend_bull = t_e20 > t_e50  # type: ignore[operator]
    trend_score = (0.4 * trend_bull + 0.3 * (t_close > t_e50)  # type: ignore[operator]
                   + (0.15 * (t_e50 > t_e200) if t_e200 is not None else 0.075)  # type: ignore[operator]
                   + 0.15 * (t_e50_prev is not None and t_e50 > t_e50_prev))  # type: ignore[operator]

    s_close, s_e9, s_e20, s_e50 = g(s, "close"), g(s, "ema_9"), g(s, "ema_20"), g(s, "ema_50")
    e_close, e_e9 = g(e, "close"), g(e, "ema_9")
    price_confirms = s_close > s_e20 and e_close > e_e9  # type: ignore[operator]
    momentum_score = 0.35 * (s_close > s_e20) + 0.35 * (s_e9 > s_e20) + 0.3 * (e_close > e_e9)  # type: ignore[operator]

    vol_ratio = g(s, "volume") / g(s, "volume_ma") if g(s, "volume_ma") else 0.0  # type: ignore[operator]
    volume_score = _clip((vol_ratio - 0.5) / 1.0)

    rsi_v = g(s, "rsi")
    lo, hi = float(settings.rsi_entry_min), float(settings.rsi_entry_max)
    rsi_ok = lo <= rsi_v <= hi  # type: ignore[operator]
    rsi_score = 1.0 if rsi_ok else (_clip(1 - (lo - rsi_v) / 10) if rsi_v < lo else 0.0)  # type: ignore[operator]

    m, ms, mh = g(s, "macd"), g(s, "macd_signal"), g(s, "macd_hist")
    mh_prev = _f(sdf["macd_hist"].iloc[-2])
    macd_ok = m > ms  # type: ignore[operator]
    macd_score = 0.5 * macd_ok + 0.25 * (mh_prev is not None and mh > mh_prev) + 0.25 * (m > 0)  # type: ignore[operator]

    adx_v, atr_v = g(s, "adx"), g(s, "atr")
    atr_pct = atr_v / s_close * 100  # type: ignore[operator]
    adx_ok = adx_v >= float(settings.min_adx)  # type: ignore[operator]
    vol_safe = atr_pct <= max_atr_pct
    volatility_score = 0.5 * adx_ok + 0.5 * vol_safe

    components = {
        "trend": round(trend_score, 4), "momentum": round(momentum_score, 4), "volume": round(volume_score, 4),
        "rsi": round(rsi_score, 4), "macd": round(macd_score, 4), "volatility": round(volatility_score, 4),
    }
    score = (float(w.trend) * trend_score + float(w.momentum) * momentum_score + float(w.volume) * volume_score
             + float(w.rsi) * rsi_score + float(w.macd) * macd_score + float(w.volatility) * volatility_score)
    score = round(_clip(score, 0, 100), 2)

    tf = settings
    entry = [
        Condition("trend_bullish", trend_bull, f"{tf.trend_timeframe} EMA20 {t_e20:.6g} {'>' if trend_bull else '<='} EMA50 {t_e50:.6g}", True),
        Condition("price_momentum", price_confirms,
                  f"{tf.setup_timeframe} close {s_close:.6g} vs EMA20 {s_e20:.6g}; {tf.entry_timeframe} close {e_close:.6g} vs EMA9 {e_e9:.6g}", True),
        Condition("macd_positive", macd_ok, f"{tf.setup_timeframe} MACD {m:.6g} {'>' if macd_ok else '<='} signal {ms:.6g}", True),
        Condition("rsi_in_range", rsi_ok, f"{tf.setup_timeframe} RSI {rsi_v:.2f} (entry range {lo:g}-{hi:g})", True),
        Condition("volume_sufficient", vol_ratio >= float(settings.min_volume_ratio),
                  f"volume/MA20 ratio {vol_ratio:.2f} (min {settings.min_volume_ratio})", True),
        Condition("trend_strength", adx_ok, f"ADX {adx_v:.2f} (min {settings.min_adx})", True),
        Condition("volatility_safe", vol_safe, f"ATR {atr_pct:.2f}% of price (max {max_atr_pct:g}%)", True),
        Condition("score_threshold", score >= float(settings.min_score), f"score {score:.1f} (min {settings.min_score})", True),
    ]
    if settings.ml_filter_enabled:
        if not ml_available or ml_probability is None:
            entry.append(Condition("ml_filter", False, "ML filter enabled but no validated model is available", True))
        else:
            ok = ml_probability >= float(settings.ml_min_probability)
            entry.append(Condition("ml_filter", ok, f"ML P(up) {ml_probability:.2f} (min {settings.ml_min_probability})", True))

    s_rev = s_close < s_e50 and not macd_ok  # type: ignore[operator]
    exits = [
        Condition("trend_reversal", not trend_bull, f"{tf.trend_timeframe} EMA20 below EMA50" if not trend_bull else f"{tf.trend_timeframe} trend intact"),
        Condition("setup_breakdown", s_rev, f"{tf.setup_timeframe} close below EMA50 with bearish MACD" if s_rev else f"{tf.setup_timeframe} structure intact"),
    ]

    # Market description/snapshots are for the dashboard; backtests skip them for speed.
    market = {role: asdict(classify_market(df)) for role, df in frames.items()} if detail else {}
    snapshot = {role: _snapshot(df) for role, df in frames.items()} if detail else {}
    snapshot["components"] = components  # type: ignore[assignment]
    result = SignalResult(symbol, "HOLD", score, "", name, entry, exits, components, market, snapshot,
                          candle_time=str(e.get("open_time")), atr=atr_v, price=e_close)

    if has_position:
        triggered = [c for c in exits if c.passed]
        if settings.exit_on_trend_reversal and triggered:
            result.signal = "SELL"
            result.score = round(100 - score, 2)
            result.reason = "SELL: exit signal - " + "; ".join(c.detail for c in triggered)
        else:
            result.reason = "HOLD: position open, no exit signal (" + "; ".join(c.detail for c in exits) + ")"
        return result

    failed = [c for c in entry if c.gate and not c.passed]
    if not failed:
        result.signal = "BUY"
        result.reason = f"BUY: all entry conditions met (score {score:.1f}). " + "; ".join(c.detail for c in entry)
    else:
        result.reason = f"HOLD: {len(failed)} entry condition(s) not met - " + "; ".join(f"{c.name}: {c.detail}" for c in failed)
    return result

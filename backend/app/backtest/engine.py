"""Historical backtesting engine.

Bias controls
-------------
* Candles are processed strictly chronologically.
* Indicators are causal (see indicators.py) so precomputing them is safe.
* The higher "trend" timeframe is built by resampling the base candles, and at
  bar i only higher-timeframe candles that have fully CLOSED by bar i's close
  are visible.
* A signal generated at the close of bar i executes at the OPEN of bar i+1
  (plus slippage) - never at the close of the signal bar.
* Stop-loss/take-profit use intrabar high/low. If both are touched in the same
  bar the stop-loss is assumed to hit first (conservative). Gaps through the
  stop fill at the bar open.
* Fees are charged on every fill; slippage is applied adversely on every fill.

Backtest results are hypothetical and are reported separately from paper and
testnet performance.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import numpy as np
import pandas as pd

from app.analysis.indicators import compute_indicators
from app.analysis.strategy import evaluate
from app.exchange.filters import SymbolFilters
from app.risk.engine import AccountSnapshot, EntryRequest, adjust_exits, check_hard_limits, evaluate_entry
from app.settings_store import ExecutionSettings, RiskSettings, StrategySettings

HIGHER_TF = {"1m": ("5m", 5), "5m": ("15m", 3), "15m": ("1h", 4), "1h": ("4h", 4), "4h": ("1d", 6)}
BARS_PER_YEAR = {"1m": 525_600, "5m": 105_120, "15m": 35_040, "1h": 8_760, "4h": 2_190, "1d": 365}
TF_PANDAS = {"5m": "5min", "15m": "15min", "1h": "1h", "4h": "4h", "1d": "1D"}
ZERO = Decimal("0")


def default_filters(symbol: str) -> SymbolFilters:
    return SymbolFilters(symbol=symbol, base_asset=symbol.removesuffix("USDT"), quote_asset="USDT", status="TRADING",
                         spot_allowed=True, order_types=("LIMIT", "MARKET"), tick_size=Decimal("0.00000001"),
                         min_price=Decimal("0.00000001"), max_price=Decimal("10000000"), step_size=Decimal("0.00000001"),
                         min_qty=Decimal("0.00000001"), max_qty=Decimal("1e12"), market_step_size=Decimal("0.00000001"),
                         market_min_qty=ZERO, market_max_qty=Decimal("1e12"), min_notional=Decimal("5"),
                         max_notional=Decimal("1e12"), apply_min_to_market=True, base_precision=8, quote_precision=8)


def resample_closed(df: pd.DataFrame, higher: str, factor: int) -> pd.DataFrame:
    """Aggregate base candles into complete higher-timeframe candles only."""
    g = df.set_index("open_time").resample(TF_PANDAS[higher], label="left", closed="left")
    out = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last(),
        "volume": g["volume"].sum(), "quote_volume": g["quote_volume"].sum(), "count": g["close"].count(),
        "close_time": g["close_time"].last(),
    })
    out = out[out["count"] == factor].drop(columns="count").reset_index()
    return out


@dataclass
class BacktestParams:
    symbol: str
    timeframe: str
    initial_balance: Decimal = Decimal("10000")
    fee_rate: Decimal = Decimal("0.001")
    slippage_bps: Decimal = Decimal("5")
    risk_pct: Decimal | None = None
    apply_hard_limits: bool = True


def run_backtest(candles: pd.DataFrame, p: BacktestParams, strategy: StrategySettings, risk: RiskSettings,
                 filters: SymbolFilters | None = None) -> dict:
    if p.timeframe not in HIGHER_TF:
        raise ValueError(f"unsupported backtest timeframe {p.timeframe}")
    if len(candles) < 300:
        raise ValueError(f"need at least 300 candles, got {len(candles)}")
    candles = candles.sort_values("open_time").drop_duplicates("open_time").reset_index(drop=True)
    higher, factor = HIGHER_TF[p.timeframe]
    base = compute_indicators(candles)
    trend = compute_indicators(resample_closed(candles, higher, factor))
    # model_copy does not re-validate, so the labels below reflect the real backtest timeframes.
    strat = strategy.model_copy(update={"trend_timeframe": higher, "setup_timeframe": p.timeframe,
                                        "entry_timeframe": p.timeframe, "ml_filter_enabled": False})
    if p.risk_pct is not None:
        risk = risk.model_copy(update={"risk_per_trade_pct": p.risk_pct})
    ex = ExecutionSettings(estimated_fee_rate=p.fee_rate, estimated_slippage_bps=p.slippage_bps)
    f = filters or default_filters(p.symbol)
    slip = p.slippage_bps / Decimal("10000")

    trend_close = trend["close_time"].to_numpy()
    base_close = base["close_time"].to_numpy()
    opens, highs, lows, closes = (base[c].to_numpy() for c in ("open", "high", "low", "close"))

    cash = p.initial_balance
    peak = day_start = p.initial_balance
    current_day = None
    pos: dict | None = None
    pending: str | None = None  # "BUY" or "SELL" to execute at next open
    pending_decision = None
    signal_time: str | None = None
    trades: list[dict] = []
    equity_curve: list[tuple[str, float]] = []
    total_fees = ZERO
    halted_at = None
    consecutive_losses, last_loss_at = 0, None
    warmup = 210
    exposure_bars = 0

    def fill(side: str, ref_price: float) -> Decimal:
        px = Decimal(str(ref_price))
        return px * (1 + slip) if side == "BUY" else px * (1 - slip)

    def close_pos(i: int, price: Decimal, reason: str) -> None:
        nonlocal cash, pos, total_fees, consecutive_losses, last_loss_at
        assert pos is not None
        proceeds = pos["qty"] * price
        fee = proceeds * p.fee_rate
        cash += proceeds - fee
        total_fees += fee
        pnl = proceeds - fee - pos["cost"] - pos["entry_fee"]
        trades.append({"signal_time": pos["signal_time"], "entry_time": pos["time"], "exit_time": str(base["open_time"].iloc[i]), "entry_price": float(pos["entry"]),
                       "exit_price": float(price), "quantity": float(pos["qty"]), "fees": float(pos["entry_fee"] + fee),
                       "pnl": float(pnl), "return_pct": float(pnl / (pos["cost"]) * 100), "exit_reason": reason})
        if pnl < 0:
            consecutive_losses += 1
            last_loss_at = base["open_time"].iloc[i].to_pydatetime()
        else:
            consecutive_losses = 0
        pos = None

    for i in range(warmup, len(base)):
        ts = base["open_time"].iloc[i]
        # ---- 1. execute orders decided at the previous bar's close (at this bar's open)
        if pending == "BUY" and pos is None and pending_decision is not None:
            price = fill("BUY", opens[i])
            qty = pending_decision.quantity
            cost = qty * price
            fee = cost * p.fee_rate
            if cost + fee <= cash and qty > 0:
                cash -= cost + fee
                total_fees += fee
                pos = {"qty": qty, "entry": price, "cost": cost, "entry_fee": fee, "stop": pending_decision.stop_loss,
                       "target": pending_decision.take_profit, "time": str(ts), "signal_time": signal_time, "high": price}
        elif pending == "SELL" and pos is not None:
            close_pos(i, fill("SELL", opens[i]), "STRATEGY_EXIT")
        pending, pending_decision = None, None

        # ---- 2. intrabar stop-loss / take-profit
        if pos is not None:
            o, h, lo = Decimal(str(opens[i])), Decimal(str(highs[i])), Decimal(str(lows[i]))
            if o <= pos["stop"]:
                close_pos(i, o * (1 - slip), "STOP_LOSS_GAP")
            elif lo <= pos["stop"]:
                close_pos(i, pos["stop"] * (1 - slip), "STOP_LOSS")
            elif o >= pos["target"]:
                close_pos(i, o * (1 - slip), "TAKE_PROFIT")
            elif h >= pos["target"]:
                close_pos(i, pos["target"] * (1 - slip), "TAKE_PROFIT")
        if pos is not None:
            exposure_bars += 1
            pos["high"] = max(pos["high"], Decimal(str(highs[i])))
            # dynamic exits: re-evaluated at this bar's close, effective from the next bar
            if risk.dynamic_exits_enabled:
                row = base.iloc[i]
                dv = lambda k: Decimal(str(float(row[k]))) if k in row and row[k] == row[k] else None  # noqa: E731
                adj = adjust_exits(entry=pos["entry"], stop=pos["stop"], target=pos["target"], highest=pos["high"],
                                   close=Decimal(str(closes[i])), atr=dv("atr"), adx=dv("adx"), ema_20=dv("ema_20"),
                                   rsi=dv("rsi"), rs=risk, tick=f.tick_size, cost_rate=p.fee_rate + slip)
                pos["stop"], pos["target"] = adj.stop, adj.target

        # ---- 3. mark to market at close
        c = Decimal(str(closes[i]))
        equity = cash + (pos["qty"] * c if pos else ZERO)
        day = ts.date()
        if day != current_day:
            current_day, day_start = day, equity
        peak = max(peak, equity)
        equity_curve.append((str(ts), float(equity)))
        acct = AccountSnapshot(equity=equity, cash=cash, invested=pos["qty"] * c if pos else ZERO,
                               open_positions=1 if pos else 0, day_start_equity=day_start, peak_equity=peak,
                               consecutive_losses=consecutive_losses, last_loss_at=last_loss_at, halted=halted_at is not None)
        if p.apply_hard_limits and halted_at is None and check_hard_limits(acct, risk):
            halted_at = str(ts)
        if i == len(base) - 1:
            break

        # ---- 4. signal at the close of bar i (only data <= i is visible)
        t_idx = int(np.searchsorted(trend_close, base_close[i], side="right")) - 1
        if t_idx < 60:
            continue
        frames = {"trend": trend.iloc[max(0, t_idx - 150): t_idx + 1],
                  "setup": base.iloc[i - 150: i + 1], "entry": base.iloc[i - 150: i + 1]}
        r = evaluate(p.symbol, frames, strat, max_atr_pct=float(risk.max_atr_pct), has_position=pos is not None,
                     detail=False)
        if r.signal == "SELL" and pos is not None:
            pending = "SELL"
        elif r.signal == "BUY" and pos is None and halted_at is None:
            d = evaluate_entry(EntryRequest(symbol=p.symbol, entry_price=Decimal(str(closes[i])) * (1 + slip),
                                            atr=Decimal(str(r.atr)) if r.atr else None, filters=f,
                                            now=base["close_time"].iloc[i].to_pydatetime()), acct, risk, ex)
            if d.approved:
                pending, pending_decision, signal_time = "BUY", d, str(ts)

    # Mark any open position at the final close (reported as open, not realised)
    final_equity = Decimal(str(equity_curve[-1][1])) if equity_curve else p.initial_balance
    return {"metrics": compute_metrics(p, equity_curve, trades, total_fees, final_equity, exposure_bars,
                                       len(base) - warmup, halted_at, open_position=pos is not None),
            "equity_curve": downsample(equity_curve, 1000), "trades": trades}


def downsample(curve: list[tuple[str, float]], n: int) -> list[dict]:
    if len(curve) <= n:
        idx = range(len(curve))
    else:
        idx = sorted(set(np.linspace(0, len(curve) - 1, n).astype(int).tolist()))
    return [{"time": curve[i][0], "equity": round(curve[i][1], 4)} for i in idx]


def compute_metrics(p: BacktestParams, curve, trades, fees: Decimal, final_equity: Decimal, exposure_bars: int,
                    total_bars: int, halted_at, open_position: bool) -> dict:
    eq = np.array([e for _, e in curve], dtype=float) if curve else np.array([float(p.initial_balance)])
    peak = np.maximum.accumulate(eq)
    max_dd = float(((peak - eq) / peak).max() * 100) if len(eq) else 0.0
    rets = np.diff(eq) / eq[:-1] if len(eq) > 1 else np.array([])
    sharpe = None
    if len(rets) > 2 and rets.std(ddof=1) > 0:
        sharpe = float(rets.mean() / rets.std(ddof=1) * math.sqrt(BARS_PER_YEAR[p.timeframe]))
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    gross_loss = -sum(losses)
    init = float(p.initial_balance)
    return {
        "initial_balance": init,
        "final_equity": round(float(final_equity), 4),
        "net_profit": round(float(final_equity) - init, 4),
        "total_return_pct": round((float(final_equity) / init - 1) * 100, 4),
        "number_of_trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else None,
        "profit_factor": round(sum(wins) / gross_loss, 4) if gross_loss > 0 else None,
        "average_win": round(sum(wins) / len(wins), 4) if wins else None,
        "average_loss": round(sum(losses) / len(losses), 4) if losses else None,
        "max_drawdown_pct": round(max_dd, 4),
        "sharpe_ratio": round(sharpe, 4) if sharpe is not None else None,
        "total_fees": round(float(fees), 4),
        "exposure_pct": round(exposure_bars / total_bars * 100, 2) if total_bars else 0,
        "halted_at": halted_at,
        "open_position_at_end": open_position,
        "disclaimer": "Hypothetical backtest result. Not indicative of future, paper or testnet performance.",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

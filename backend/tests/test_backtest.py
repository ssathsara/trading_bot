from decimal import Decimal as D

import numpy as np
import pandas as pd
import pytest

from app.backtest.engine import BacktestParams, resample_closed, run_backtest
from app.settings_store import RiskSettings, StrategySettings
from tests.conftest import make_ohlcv

LOOSE = StrategySettings(rsi_entry_min=D("0"), rsi_entry_max=D("100"), min_adx=D("0"), min_score=D("40"),
                         min_volume_ratio=D("0"))
RISK = RiskSettings(max_atr_pct=D("50"))


def test_resample_only_complete_candles():
    df = make_ohlcv(10, freq="15min")
    r = resample_closed(df, "1h", 4)
    assert len(r) == 2  # 10 x 15m = 2 complete hours + 1 partial (dropped)
    assert r["high"].iloc[0] == df["high"].iloc[:4].max() and r["close"].iloc[1] == df["close"].iloc[7]


def test_backtest_runs_and_accounting_consistent():
    df = make_ohlcv(1500, drift=0.0006, vol=0.006, seed=11)
    res = run_backtest(df, BacktestParams("TESTUSDT", "15m"), LOOSE, RISK)
    m = res["metrics"]
    assert m["number_of_trades"] > 0
    pnl_sum = sum(t["pnl"] for t in res["trades"])
    if not m["open_position_at_end"]:
        assert m["net_profit"] == pytest.approx(pnl_sum, abs=1e-3)
    assert m["total_fees"] > 0
    assert 0 <= m["max_drawdown_pct"] < 100
    for t in res["trades"]:
        assert t["entry_time"] > t["signal_time"]  # fills strictly after the signal bar
        assert t["exit_time"] >= t["entry_time"]


def test_backtest_has_no_lookahead():
    """Changing FUTURE candles must not change trades that completed before them."""
    df = make_ohlcv(1500, drift=0.0006, vol=0.006, seed=11)
    res_a = run_backtest(df, BacktestParams("TESTUSDT", "15m"), LOOSE, RISK)
    cut = 1200
    df_b = df.copy()
    rng = np.random.default_rng(99)
    scale = np.exp(np.cumsum(rng.normal(-0.01, 0.02, len(df) - cut)))
    for c in ("open", "high", "low", "close"):
        df_b.loc[cut:, c] = df_b.loc[cut:, c].to_numpy() * scale
    res_b = run_backtest(df_b, BacktestParams("TESTUSDT", "15m"), LOOSE, RISK)
    cutoff = str(df["open_time"].iloc[cut - 1])
    before_a = [t for t in res_a["trades"] if t["exit_time"] < cutoff]
    before_b = [t for t in res_b["trades"] if t["exit_time"] < cutoff]
    assert before_a and before_a == before_b


def test_fees_and_slippage_are_charged_on_every_fill():
    df = make_ohlcv(1500, drift=0.0006, vol=0.006, seed=11)
    free = run_backtest(df, BacktestParams("T", "15m", fee_rate=D("0"), slippage_bps=D("0")), LOOSE, RISK)
    assert free["metrics"]["total_fees"] == 0
    res = run_backtest(df, BacktestParams("T", "15m", fee_rate=D("0.001"), slippage_bps=D("10")), LOOSE, RISK)
    for t in res["trades"]:
        notional = t["quantity"] * (t["entry_price"] + t["exit_price"])
        assert t["fees"] == pytest.approx(notional * 0.001, rel=1e-9)
    assert res["metrics"]["total_fees"] > 0


def test_prohibitive_costs_block_trades():
    """When costs dominate the reward/risk, the risk engine rejects every entry."""
    df = make_ohlcv(1500, drift=0.0006, vol=0.006, seed=11)
    res = run_backtest(df, BacktestParams("T", "15m", fee_rate=D("0.002"), slippage_bps=D("20")), LOOSE, RISK)
    assert res["metrics"]["number_of_trades"] == 0


def test_backtest_rejects_bad_input():
    with pytest.raises(ValueError):
        run_backtest(make_ohlcv(100), BacktestParams("T", "15m"), LOOSE, RISK)
    with pytest.raises(ValueError):
        run_backtest(make_ohlcv(400), BacktestParams("T", "3m"), LOOSE, RISK)

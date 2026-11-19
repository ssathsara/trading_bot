from decimal import Decimal

import numpy as np

from app.analysis.indicators import compute_indicators
from app.analysis.strategy import evaluate
from app.settings_store import StrategySettings, StrategyWeights
from tests.conftest import make_ohlcv


def frames(drift=0.002, vol=0.003, seed=4, boost_last_volume=True):
    out = {}
    for role, freq in (("trend", "1h"), ("setup", "15min"), ("entry", "5min")):
        df = make_ohlcv(300, drift=drift, vol=vol, seed=seed, freq=freq)
        if boost_last_volume:
            df.loc[df.index[-1], "volume"] = df["volume"].mean() * 2
        out[role] = compute_indicators(df)
    return out


def loosened(**kw):
    base = dict(rsi_entry_min=Decimal("0"), rsi_entry_max=Decimal("100"), min_adx=Decimal("0"), min_score=Decimal("50"),
                market_filter_enabled=False)
    base.update(kw)
    return StrategySettings(**base)


def test_bullish_market_generates_buy_with_explanation():
    r = evaluate("BTCUSDT", frames(), loosened(), max_atr_pct=10)
    assert r.signal == "BUY", r.reason
    assert 50 <= r.score <= 100
    assert all(c.passed for c in r.entry_conditions if c.gate)
    assert "BUY" in r.reason and "EMA20" in r.reason


def test_bearish_market_holds():
    r = evaluate("BTCUSDT", frames(drift=-0.002), loosened(), max_atr_pct=10)
    assert r.signal == "HOLD"
    assert any(c.name == "trend_bullish" and not c.passed for c in r.entry_conditions)


def test_low_rsi_alone_never_buys():
    """Oversold RSI in a downtrend must not produce a BUY."""
    s = StrategySettings(rsi_entry_min=Decimal("0"), rsi_entry_max=Decimal("35"), min_adx=Decimal("0"), min_score=Decimal("0"),
                         market_filter_enabled=False)
    r = evaluate("BTCUSDT", frames(drift=-0.003), s, max_atr_pct=10)
    assert r.signal == "HOLD"


def test_high_rsi_alone_never_sells_open_position():
    r = evaluate("BTCUSDT", frames(drift=0.004), loosened(), max_atr_pct=10, has_position=True)
    assert r.signal == "HOLD", r.reason  # uptrend intact -> no sell even if RSI is high


def test_trend_reversal_generates_sell_for_open_position():
    r = evaluate("BTCUSDT", frames(drift=-0.003), loosened(), max_atr_pct=10, has_position=True)
    assert r.signal == "SELL"
    assert "exit signal" in r.reason


def test_stale_or_missing_data_holds():
    f = frames()
    r = evaluate("BTCUSDT", f, loosened(), max_atr_pct=10, data_issues=["stale data: last candle closed ..."])
    assert r.signal == "HOLD" and "stale" in r.reason
    f["entry"] = f["entry"].iloc[:30]
    assert evaluate("BTCUSDT", f, loosened(), max_atr_pct=10).signal == "HOLD"


def test_excessive_volatility_holds():
    r = evaluate("BTCUSDT", frames(), loosened(), max_atr_pct=0.01)
    assert r.signal == "HOLD"
    assert any(c.name == "volatility_safe" and not c.passed for c in r.entry_conditions)


def test_score_respects_weights_and_threshold():
    f = frames()
    hi = evaluate("BTCUSDT", f, loosened(min_score=Decimal("100")), max_atr_pct=10)
    assert hi.signal == "HOLD" or hi.score == 100
    w = StrategyWeights(trend=Decimal("100"), momentum=Decimal("0"), volume=Decimal("0"), rsi=Decimal("0"),
                        macd=Decimal("0"), volatility=Decimal("0"))
    r = evaluate("BTCUSDT", f, loosened(weights=w), max_atr_pct=10)
    assert np.isclose(r.score, 100 * r.components["trend"], atol=0.01)


def test_ml_filter_fails_closed_without_model():
    r = evaluate("BTCUSDT", frames(), loosened(ml_filter_enabled=True), max_atr_pct=10, ml_available=False)
    assert r.signal == "HOLD"
    assert any(c.name == "ml_filter" and not c.passed for c in r.entry_conditions)


# ---- market-regime filter -------------------------------------------------------
from app.analysis.strategy import MarketRegime, market_regime  # noqa: E402


def test_market_regime_classifies_btc_trend():
    up = compute_indicators(make_ohlcv(300, drift=0.003, vol=0.003, seed=4, freq="4h"))
    down = compute_indicators(make_ohlcv(300, drift=-0.003, vol=0.003, seed=4, freq="4h"))
    assert market_regime(up, "BTCUSDT", "4h").bearish is False
    r = market_regime(down, "BTCUSDT", "4h")
    assert r.bearish is True and "downtrend" in r.detail
    assert market_regime(up.head(30), "BTCUSDT", "4h").bearish is None  # too little data
    assert market_regime(None, "BTCUSDT", "4h").bearish is None


def test_market_filter_blocks_buy_when_btc_bearish():
    s = loosened(market_filter_enabled=True)
    ok = evaluate("ETHUSDT", frames(), s, max_atr_pct=10, market=MarketRegime(False, "BTC fine"))
    assert ok.signal == "BUY", ok.reason
    bad = evaluate("ETHUSDT", frames(), s, max_atr_pct=10, market=MarketRegime(True, "BTCUSDT 4h ... downtrend - no new buys"))
    assert bad.signal == "HOLD" and "market_regime" in bad.reason and "downtrend" in bad.reason


def test_market_filter_fails_safe_without_data():
    s = loosened(market_filter_enabled=True)
    assert evaluate("ETHUSDT", frames(), s, max_atr_pct=10, market=None).signal == "HOLD"
    assert evaluate("ETHUSDT", frames(), s, max_atr_pct=10, market=MarketRegime(None, "no data")).signal == "HOLD"


def test_market_filter_never_blocks_exits():
    s = loosened(market_filter_enabled=True)
    r = evaluate("ETHUSDT", frames(drift=-0.003), s, max_atr_pct=10, has_position=True, market=MarketRegime(True, "bear"))
    assert r.signal == "SELL"

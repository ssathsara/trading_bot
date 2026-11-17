import math

import numpy as np
import pandas as pd
import pytest

from app.analysis import indicators as ind
from tests.conftest import make_ohlcv


def ref_ema(values, n):
    out = [math.nan] * len(values)
    out[n - 1] = sum(values[:n]) / n
    a = 2 / (n + 1)
    for i in range(n, len(values)):
        out[i] = out[i - 1] + a * (values[i] - out[i - 1])
    return out


def ref_rsi(values, n=14):
    gains = [max(values[i] - values[i - 1], 0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0) for i in range(1, len(values))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    out = [math.nan] * len(values)
    out[n] = 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(values)):
        ag = (ag * (n - 1) + gains[i - 1]) / n
        al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = 100 - 100 / (1 + ag / al)
    return out


def test_ema_matches_reference():
    df = make_ohlcv(300)
    got = ind.ema(df["close"], 20).to_numpy()
    exp = ref_ema(df["close"].tolist(), 20)
    assert np.isnan(got[:19]).all()
    np.testing.assert_allclose(got[19:], exp[19:], rtol=1e-12)


def test_rsi_matches_reference_and_textbook_value():
    df = make_ohlcv(200)
    got = ind.rsi(df["close"]).to_numpy()
    np.testing.assert_allclose(got[14:], ref_rsi(df["close"].tolist())[14:], rtol=1e-10)
    # Wilder's classic example data (StockCharts RSI tutorial). Their spreadsheet rounds the
    # intermediate averages to 2dp (0.24/0.10 -> 70.53); exact arithmetic gives 70.46.
    closes = pd.Series([44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03,
                        45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64])
    r = ind.rsi(closes)
    assert r.iloc[14] == pytest.approx(70.53, abs=0.1)
    assert r.iloc[15] == pytest.approx(66.32, abs=0.1)


def test_rsi_edge_cases():
    up = pd.Series(np.arange(1, 40, dtype=float))
    assert ind.rsi(up).iloc[-1] == 100.0
    flat = pd.Series([5.0] * 40)
    assert ind.rsi(flat).iloc[-1] == 50.0


def test_macd_and_bollinger():
    df = make_ohlcv(300)
    line, sig, hist = ind.macd(df["close"])
    e12, e26 = ref_ema(df["close"].tolist(), 12), ref_ema(df["close"].tolist(), 26)
    exp_line = np.array(e12) - np.array(e26)
    np.testing.assert_allclose(line.to_numpy()[25:], exp_line[25:], rtol=1e-12)
    exp_sig = ref_ema(exp_line[25:].tolist(), 9)
    np.testing.assert_allclose(sig.to_numpy()[33:], exp_sig[8:], rtol=1e-10)
    np.testing.assert_allclose(hist.dropna(), (line - sig).dropna())
    up, mid, lo = ind.bollinger(df["close"])
    window = df["close"].iloc[-20:].to_numpy()
    assert mid.iloc[-1] == pytest.approx(window.mean())
    assert up.iloc[-1] == pytest.approx(window.mean() + 2 * window.std(ddof=0))
    assert lo.iloc[-1] == pytest.approx(window.mean() - 2 * window.std(ddof=0))


def test_atr_reference():
    df = make_ohlcv(100)
    h, l, c = df["high"].tolist(), df["low"].tolist(), df["close"].tolist()
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    a = sum(tr[1:15]) / 14
    exp = [a]
    for i in range(15, len(c)):
        a = (a * 13 + tr[i]) / 14
        exp.append(a)
    np.testing.assert_allclose(ind.atr(df["high"], df["low"], df["close"]).to_numpy()[14:], exp, rtol=1e-12)


def test_adx_bounds_and_trend_response():
    trending = make_ohlcv(300, drift=0.004, vol=0.002)
    adx, pdi, mdi = ind.adx(trending["high"], trending["low"], trending["close"])
    valid = adx.dropna()
    assert len(valid) == 300 - 27
    assert ((valid >= 0) & (valid <= 100)).all()
    assert valid.iloc[-1] > 25 and pdi.iloc[-1] > mdi.iloc[-1]


def test_indicators_are_causal_no_lookahead():
    """Values at row i must not change when future rows are appended."""
    df = make_ohlcv(400)
    full = ind.compute_indicators(df)
    part = ind.compute_indicators(df.iloc[:300])
    cols = ["rsi", "ema_9", "ema_20", "ema_50", "ema_200", "macd", "macd_signal", "bb_upper", "atr", "adx", "volume_ma"]
    pd.testing.assert_frame_equal(full[cols].iloc[:300], part[cols], check_exact=False, rtol=1e-12)


def test_support_resistance_brackets_price():
    df = ind.compute_indicators(make_ohlcv(300, drift=0.0, vol=0.01, seed=3))
    sr = ind.support_resistance(df)
    price = df["close"].iloc[-1]
    if sr["support"] is not None:
        assert sr["support"] < price
    if sr["resistance"] is not None:
        assert sr["resistance"] > price

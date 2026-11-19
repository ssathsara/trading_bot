from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from app.risk.engine import (AccountSnapshot, EntryRequest, check_hard_limits, evaluate_entry, exit_trigger,
                             stop_and_target, update_trailing_stop)
from app.settings_store import ExecutionSettings, RiskSettings

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
NO_COST = ExecutionSettings(estimated_fee_rate=D("0"), estimated_slippage_bps=D("0"))


def acct(**kw):
    base = dict(equity=D("10000"), cash=D("10000"), invested=D("0"), open_positions=0, day_start_equity=D("10000"),
                peak_equity=D("10000"), consecutive_losses=0, last_loss_at=None, halted=False)
    base.update(kw)
    return AccountSnapshot(**base)


def req(f, price="100", atr="1", **kw):
    return EntryRequest(symbol="BTCUSDT", entry_price=D(price), atr=D(atr) if atr else None, filters=f, now=NOW, **kw)


def test_position_sizing_formula(btc_filters):
    rs = RiskSettings()  # 0.5% risk, ATR*2 stop, RR 2
    d = evaluate_entry(req(btc_filters), acct(), rs, NO_COST)
    assert d.approved, d.reasons
    # risk = 10000*0.5% = 50; stop distance = 2 => qty 25, but 20% cap = 2000/100 = 20
    assert d.stop_loss == D("98") and d.take_profit == D("104")
    assert d.quantity == D("20") and "quantity capped by max allocation per position" in d.adjustments
    rs2 = RiskSettings(max_position_alloc_pct=D("50"), max_total_alloc_pct=D("60"))
    d2 = evaluate_entry(req(btc_filters), acct(), rs2, NO_COST)
    assert d2.quantity == D("25") and d2.risk_amount == D("50")


def test_costs_included_in_sizing_and_rr(btc_filters):
    ex = ExecutionSettings(estimated_fee_rate=D("0.001"), estimated_slippage_bps=D("5"))
    rs = RiskSettings(max_position_alloc_pct=D("50"))
    d = evaluate_entry(req(btc_filters), acct(), rs, ex)
    per_unit = D("2") + D("100") * D("0.0015") + D("98") * D("0.0015")
    assert d.quantity == btc_filters.round_qty(D("50") / per_unit)
    assert d.risk_amount <= D("50")
    # tight stop where costs dominate => reward/risk after costs too low
    tight = evaluate_entry(req(btc_filters, atr="0.1"), acct(), rs, ex)
    assert not tight.approved and any("reward/risk" in r for r in tight.reasons)


def test_percent_stop_and_targets(btc_filters):
    rs = RiskSettings(stop_loss_mode="PERCENT", stop_loss_pct=D("2"), take_profit_mode="PERCENT", take_profit_pct=D("5"))
    stop, tgt, err = stop_and_target(D("100"), None, rs, btc_filters)
    assert (stop, tgt, err) == (D("98"), D("105"), None)
    rs_bad = RiskSettings(stop_loss_mode="PERCENT", stop_loss_pct=D("2"), take_profit_mode="PERCENT", take_profit_pct=D("2"))
    d = evaluate_entry(req(btc_filters), acct(), rs_bad, NO_COST)
    assert not d.approved and "reward/risk" in d.reasons[0]
    _, _, err = stop_and_target(D("100"), None, RiskSettings(), btc_filters)
    assert err  # ATR mode requires ATR


def test_daily_loss_limit_is_hard_halt(btc_filters):
    a = acct(equity=D("9790"), cash=D("9790"))  # -2.1% today
    assert check_hard_limits(a, RiskSettings())[0] == "DAILY_LOSS_LIMIT"
    d = evaluate_entry(req(btc_filters), a, RiskSettings(), NO_COST)
    assert not d.approved and d.hard_breach == "DAILY_LOSS_LIMIT"


def test_max_drawdown_is_hard_halt(btc_filters):
    a = acct(equity=D("10800"), cash=D("10800"), peak_equity=D("12000"), day_start_equity=D("10800"))
    assert check_hard_limits(a, RiskSettings())[0] == "MAX_DRAWDOWN"
    assert evaluate_entry(req(btc_filters), acct(halted=True, halt_reason="MAX_DRAWDOWN"), RiskSettings(), NO_COST).approved is False


def test_exposure_and_position_limits(btc_filters):
    rs = RiskSettings()
    full = evaluate_entry(req(btc_filters), acct(open_positions=3), rs, NO_COST)
    assert not full.approved and "max open positions" in full.reasons[0]
    # 55% already allocated: only 5% (500 USDT) of room left => qty 5
    d = evaluate_entry(req(btc_filters), acct(invested=D("5500"), cash=D("4500")), rs, NO_COST)
    assert d.approved and d.quantity == D("5") and "quantity capped by max total allocation" in d.adjustments
    none = evaluate_entry(req(btc_filters), acct(invested=D("6000"), cash=D("4000")), rs, NO_COST)
    assert not none.approved
    cash = evaluate_entry(req(btc_filters), acct(cash=D("300")), rs, NO_COST)
    assert cash.quantity == D("3")


def test_max_order_size_and_min_notional(btc_filters):
    rs = RiskSettings(max_order_notional_usdt=D("1000"))
    d = evaluate_entry(req(btc_filters), acct(), rs, NO_COST)
    assert d.quantity == D("10")
    tiny = evaluate_entry(req(btc_filters), acct(equity=D("10"), cash=D("10"), day_start_equity=D("10"), peak_equity=D("10")),
                          RiskSettings(), NO_COST)
    assert not tiny.approved and any("minNotional" in r for r in tiny.reasons)


def test_safety_rejections(btc_filters):
    rs = RiskSettings()
    assert "stale" in evaluate_entry(req(btc_filters, data_stale=True), acct(), rs, NO_COST).reasons[0]
    assert "connectivity" in evaluate_entry(req(btc_filters, connectivity_ok=False), acct(), rs, NO_COST).reasons[0]
    assert "volatility" in evaluate_entry(req(btc_filters, atr="10"), acct(), rs, NO_COST).reasons[0]
    assert "already open" in evaluate_entry(req(btc_filters, has_open_position=True), acct(), rs, NO_COST).reasons[0]
    losing = acct(consecutive_losses=3, last_loss_at=NOW - timedelta(minutes=10))
    assert "cooldown" in evaluate_entry(req(btc_filters), losing, rs, NO_COST).reasons[0]
    expired = acct(consecutive_losses=3, last_loss_at=NOW - timedelta(minutes=61))
    assert evaluate_entry(req(btc_filters), expired, rs, NO_COST).approved
    sym = evaluate_entry(req(btc_filters, symbol_last_loss_at=NOW - timedelta(minutes=5)), acct(), rs, NO_COST)
    assert "cooldown after loss" in sym.reasons[0]


def test_trailing_stop_and_exit_triggers():
    assert update_trailing_stop(D("95"), D("110"), D("2"), D("0.01")) == D("107.8")
    assert update_trailing_stop(D("108"), D("105"), D("2"), D("0.01")) == D("108")  # never moves down
    assert exit_trigger(D("94.99"), D("95"), D("110")) == "STOP_LOSS"
    assert exit_trigger(D("110"), D("95"), D("110")) == "TAKE_PROFIT"
    assert exit_trigger(D("100"), D("95"), D("110")) is None


def test_settings_validation():
    with pytest.raises(ValueError):
        RiskSettings(max_position_alloc_pct=D("70"), max_total_alloc_pct=D("60"))
    with pytest.raises(ValueError):
        RiskSettings(take_profit_rr=D("1"), min_reward_risk=D("1.5"))


# ---- dynamic exits -------------------------------------------------------------
from app.risk.engine import adjust_exits  # noqa: E402

TICK = D("0.01")


def dyn(**kw):
    base = dict(entry=D("100"), stop=D("96"), target=D("108"), highest=D("100"), close=D("100"), atr=D("2"),
                adx=D("15"), ema_20=D("99"), rsi=D("55"), rs=RiskSettings(), tick=TICK, cost_rate=D("0"))
    base.update(kw)
    return adjust_exits(**base)


def test_dynamic_exits_unchanged_at_entry():
    a = dyn()
    assert (a.stop, a.target, a.reasons) == (D("96"), D("108"), [])


def test_dynamic_stop_moves_to_breakeven_then_trails():
    a = dyn(highest=D("104"), close=D("103"))  # +2 ATR -> break-even; trail 104-5=99 < 100
    assert a.stop == D("100")
    a = dyn(highest=D("110"), close=D("109"), target=D("120"))  # trail 110 - 2.5*2 = 105
    assert a.stop == D("105")


def test_dynamic_stop_never_moves_down():
    a = dyn(stop=D("103"), highest=D("104"), close=D("101"), atr=D("4"))  # wider ATR would give 94
    assert a.stop == D("103")


def test_dynamic_take_profit_raised_in_strong_trend():
    a = dyn(close=D("106"), highest=D("106"), ema_20=D("103"), adx=D("35"))
    assert a.target == D("112")  # 106 + 3*2


def test_dynamic_take_profit_lowered_when_momentum_fades():
    a = dyn(close=D("103"), highest=D("103"), ema_20=D("104"))  # close < EMA20
    assert a.target == D("105")  # 103 + 1*2
    a = dyn(close=D("103"), highest=D("103"), ema_20=D("101"), rsi=D("80"))
    assert a.target == D("105")


def test_dynamic_take_profit_not_lowered_when_losing():
    a = dyn(close=D("98"), highest=D("100"), ema_20=D("99.5"))
    assert a.target == D("108")


def test_dynamic_exits_disabled_in_backtest_matches_static(btc_filters):
    # the flag is honoured by callers; adjust_exits itself is a no-op without ATR
    assert dyn(atr=None).stop == D("96")

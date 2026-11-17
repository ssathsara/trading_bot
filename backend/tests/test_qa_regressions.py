"""Regression tests for defects found in system QA, plus property tests for the money invariants."""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal as D

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.exchange.filters import SymbolFilters
from app.execution.adapters import OrderRequest, PaperExecutionAdapter
from app.models import AccountState, OrderFill, PortfolioSnapshot, Position, SystemSetting, Trade, TradingOrder
from app.portfolio import service as pf
from app.risk.engine import AccountSnapshot, EntryRequest, evaluate_entry
from app.settings_store import ExecutionSettings, RiskSettings, get_control, save
from app.worker import engine as engine_mod
from tests.conftest import exchange_symbol
from tests.test_api import auth, client  # noqa: F401  (shared API fixtures)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _position(symbol="BTCUSDT", **kw) -> Position:
    base = dict(symbol=symbol, execution_mode="PAPER", quantity=D("1"), average_entry_price=D("100"), cost_basis=D("100"),
                entry_fees_open=D("0"), unrealised_pnl=D("0"), realised_pnl=D("0"), status="OPEN", current_price=D("100"))
    return Position(**{**base, **kw})


# ------------------------------------------------------------------ API
def test_backtest_naive_datetimes_are_treated_as_utc_not_500(client, auth):
    # Previously: comparing a naive `end` with an aware "now" raised TypeError -> HTTP 500.
    r = client.post("/api/backtest/run", headers=auth,
                    json={"symbol": "BTCUSDT", "timeframe": "1h", "start": "2099-01-01T00:00:00", "end": "2099-03-01T00:00:00"})
    assert r.status_code == 422 and "future" in r.text

    from app.schemas import BacktestRequest
    req = BacktestRequest(symbol="BTCUSDT", start="2025-01-01T00:00:00", end="2025-01-02T05:30:00+05:30")
    assert req.start == datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert req.end == datetime(2025, 1, 2, tzinfo=timezone.utc)


def test_unknown_binance_symbol_is_404_not_502():
    from app.api.routes import upstream_error
    from app.exchange.rest import BinanceAPIError, BinanceNetworkError

    assert upstream_error(BinanceAPIError(400, -1121, "Invalid symbol.")).status_code == 404
    assert upstream_error(BinanceAPIError(400, -1100, "Illegal characters")).status_code == 502
    assert upstream_error(BinanceNetworkError("timeout")).status_code == 502


def test_manual_close_requests_are_queued_not_overwritten(client, auth):
    with session_scope() as s:
        ids = []
        for sym in ("BTCUSDT", "ETHUSDT"):
            p = _position(sym)
            s.add(p)
            s.flush()
            ids.append(p.id)
    for pid in ids:
        assert client.post(f"/api/trading/close-position/{pid}", headers=auth).status_code == 202
    with session_scope() as s:
        row = s.scalar(select(SystemSetting).where(SystemSetting.setting_key == engine_mod.CLOSE_REQUESTS_KEY))
        assert [r["position_id"] for r in row.setting_value] == ids
    # NOTE: the concurrent API-append vs worker-drain race is prevented by SELECT ... FOR UPDATE
    # on both sides; that only has an effect on PostgreSQL and cannot be exercised on SQLite.


# ------------------------------------------------------------------ worker lease
def test_lease_loss_stands_down_locally_without_stopping_the_lease_holder():
    a = engine_mod.TradingEngine(get_settings(), public=object(), stream=object())
    assert a._acquire_lease()
    a.has_lease = True
    a.publish_status = lambda: None
    with session_scope() as s:
        ctl = get_control(s)
        ctl.state = "RUNNING"
        save(s, "control", ctl)
        # Worker B legitimately took over after A stalled past the lease TTL.
        row = s.scalar(select(SystemSetting).where(SystemSetting.setting_key == engine_mod.LEASE_KEY))
        row.setting_value = {"owner": "worker-B", "expires": 9e12}

    asyncio.run(a.lease_tick())

    with session_scope() as s:
        assert get_control(s).state == "RUNNING", "the shared control state must not be touched (B keeps trading)"
        lease = s.scalar(select(SystemSetting).where(SystemSetting.setting_key == engine_mod.LEASE_KEY)).setting_value
        assert lease["owner"] == "worker-B"
    assert not a.has_lease and a._stopping and a.lease_lost.is_set()

    # A must not place any order after losing the lease.
    class ExplodingOM:
        async def place(self, **kw):
            raise AssertionError("order placed by a worker without the lease")
    a.om = ExplodingOM()
    a.filters = {"BTCUSDT": SymbolFilters.from_exchange_info(exchange_symbol())}
    with session_scope() as s:
        p = _position()
        s.add(p)
        s.flush()
        pid = p.id
    assert asyncio.run(a.close_position(pid, "STOP_LOSS")) is None


# ------------------------------------------------------------------ portfolio statistics
def test_max_drawdown_is_numeric_max_on_sqlite():
    # Money is stored as text on SQLite: MAX("9.5", "10.2") used to return "9.5".
    with session_scope() as s:
        pf.get_account(s, "PAPER", D("10000"))
        for dd in ("9.5", "10.2", "2"):
            s.add(PortfolioSnapshot(execution_mode="PAPER", total_equity=D("9000"), available_balance=D("9000"),
                                    invested_capital=D("0"), realised_pnl=D("0"), unrealised_pnl=D("0"), drawdown=D(dd)))
    with session_scope() as s:
        assert pf.performance(s, "PAPER", lambda _: None)["max_drawdown_pct"] == D("10.2")


def test_breakeven_trade_is_neither_win_nor_loss():
    with session_scope() as s:
        acct = pf.get_account(s, "PAPER", D("10000"))
        acct.consecutive_losses = 2
        pos = _position(quantity=D("0"), cost_basis=D("0"))
        s.add(pos)
        s.flush()
        o = TradingOrder(client_order_id="z1", idempotency_key="z1", symbol="BTCUSDT", side="BUY", order_type="MARKET",
                         quantity=D("1"), filled_quantity=D("1"), cumulative_quote_qty=D("100"), fee_quote=D(0),
                         status="FILLED", execution_mode="PAPER", purpose="ENTRY", position_id=pos.id, order_meta={})
        s.add(o)
        s.flush()
        s.add(Trade(order_id=o.id, position_id=pos.id, symbol="BTCUSDT", side="BUY", entry_price=D("100"), quantity=D("1"),
                    entry_fee=D("0"), exit_fee=D("0"), status="OPEN", execution_mode="PAPER"))
        s.flush()
        pf.close_position(s, pos, D("100"), "TEST")
        perf = pf.performance(s, "PAPER", lambda _: None)
        assert acct.consecutive_losses == 0
        assert (perf["winning_trades"], perf["losing_trades"], perf["closed_trades"]) == (0, 0, 1)


# ------------------------------------------------------------------ paper simulator
async def test_paper_limit_cannot_reuse_the_same_displayed_liquidity():
    async def depth(_):
        return {"bids": [["99", "1"]], "asks": [["101", "1"]]}  # not marketable at 100 -> rests
    p = PaperExecutionAdapter(depth, D("0.001"), D("0"))
    req = OrderRequest("cid1", "BTCUSDT", "BUY", "LIMIT", D("10"), D("100"), "BTC", "USDT")
    await p.submit(req)
    for _ in range(10):  # the same level (ask 100 x 1) re-published on every tick
        p.on_quote("BTCUSDT", D("99"), D("1"), D("100"), D("1"))
    assert (await p.query(req)).filled_qty == D("1")

    p.on_quote("BTCUSDT", D("99"), D("1"), D("100"), D("3"))  # replenished at the same price: +2 new
    assert (await p.query(req)).filled_qty == D("3")
    p.on_quote("BTCUSDT", D("98"), D("1"), D("99.5"), D("2"))  # new, better level: fresh liquidity
    rep = await p.query(req)
    assert rep.filled_qty == D("5") and rep.status == "PARTIALLY_FILLED"


async def test_paper_resting_orders_share_one_level():
    async def depth(_):
        return {"bids": [["99", "1"]], "asks": [["101", "1"]]}
    p = PaperExecutionAdapter(depth, D("0.001"), D("0"))
    a = OrderRequest("a", "BTCUSDT", "BUY", "LIMIT", D("2"), D("100"), "BTC", "USDT")
    b = OrderRequest("b", "BTCUSDT", "BUY", "LIMIT", D("2"), D("100"), "BTC", "USDT")
    await p.submit(a)
    await p.submit(b)
    p.on_quote("BTCUSDT", D("99"), D("1"), D("100"), D("3"))
    assert (await p.query(a)).filled_qty + (await p.query(b)).filled_qty == D("3")


# ------------------------------------------------------------------ property tests
def _rand_filters(rng: random.Random) -> SymbolFilters:
    step = rng.choice(["0.00001", "0.001", "0.1", "1"])
    tick = rng.choice(["0.00000001", "0.0001", "0.01"])
    return SymbolFilters.from_exchange_info(
        exchange_symbol(step=step, min_qty=step, tick=tick, min_notional=str(rng.choice([1, 5, 10]))))


def test_property_risk_engine_never_exceeds_limits():
    rng = random.Random(1234)
    ex = ExecutionSettings()
    approved = 0
    for i in range(3000):
        rs = RiskSettings(risk_per_trade_pct=D(str(rng.choice([0.1, 0.5, 1, 2, 5]))), max_open_positions=rng.randint(1, 5),
                          max_position_alloc_pct=D(str(rng.choice([5, 10, 20, 50]))),
                          stop_loss_mode=rng.choice(["ATR", "PERCENT"]), stop_loss_pct=D(str(rng.choice([0.5, 1, 2, 5]))),
                          take_profit_mode=rng.choice(["RR", "PERCENT"]), take_profit_pct=D(str(rng.choice([1, 4, 10]))),
                          max_order_notional_usdt=D(str(rng.choice([50, 500, 2500, 100000]))), max_atr_pct=D("100"))
        f = _rand_filters(rng)
        price = D(str(round(rng.uniform(0.01, 90000), 2))).quantize(f.tick_size, ROUND_DOWN) or f.tick_size
        equity = D(str(rng.choice([50, 1000, 10000, 1_000_000])))
        invested = (equity * D(str(rng.uniform(0, 0.7)))).quantize(D("0.01"))
        acct = AccountSnapshot(equity=equity, cash=equity - invested, invested=invested, open_positions=rng.randint(0, 4),
                               day_start_equity=equity, peak_equity=equity, consecutive_losses=0, last_loss_at=None,
                               halted=False)
        d = evaluate_entry(EntryRequest("BTCUSDT", price, price * D(str(rng.uniform(0.001, 0.05))), f, NOW), acct, rs, ex)
        if not d.approved:
            continue
        approved += 1
        eps, ctx = D("1e-8"), f"case {i}: price={price} equity={equity} invested={invested} decision={d.to_dict()}"
        assert d.stop_loss < price < d.take_profit, ctx
        assert d.quantity > 0 and (d.quantity / f.step_size) == (d.quantity / f.step_size).to_integral_value(), ctx
        assert d.notional <= rs.max_order_notional_usdt + eps, ctx
        assert d.notional <= equity * rs.max_position_alloc_pct / 100 + eps, ctx
        assert invested + d.notional <= equity * rs.max_total_alloc_pct / 100 + eps, ctx
        assert d.notional * (1 + ex.estimated_fee_rate + ex.estimated_slippage_bps / 10000) <= acct.cash + eps, ctx
        assert d.risk_amount <= equity * rs.risk_per_trade_pct / 100 + eps, ctx
        assert d.reward_risk >= rs.min_reward_risk - D("0.01"), ctx
        assert acct.open_positions < rs.max_open_positions, ctx
    assert approved > 200, f"only {approved} approvals - the property test is not exercising sizing"


def test_property_portfolio_accounting_identity():
    """equity - initial == realised + unrealised across random partial fills, quote/base fees and partial exits."""
    rng = random.Random(99)
    for trial in range(60):
        with session_scope() as s:
            for model in (OrderFill, Trade, TradingOrder, Position, AccountState):
                s.query(model).delete()
        with session_scope() as s:
            pf.get_account(s, "PAPER", D("10000"))
            entry = TradingOrder(client_order_id=f"e{trial}", idempotency_key=f"e{trial}", symbol="BTCUSDT", side="BUY",
                                 order_type="MARKET", quantity=D("1"), filled_quantity=D(0), cumulative_quote_qty=D(0),
                                 fee_quote=D(0), status="NEW", execution_mode="PAPER", purpose="ENTRY", order_meta={})
            s.add(entry)
            s.flush()
            for _ in range(rng.randint(1, 4)):
                px, q = D(str(round(rng.uniform(90, 110), 4))), D(str(round(rng.uniform(0.01, 0.5), 5)))
                base_fee = rng.random() < 0.5
                fee = q * D("0.001") if base_fee else (px * q * D("0.001")).quantize(D("1e-8"))
                fq, kind = pf.fee_to_quote(fee, "BTC" if base_fee else "USDT", "BTC", "USDT", px)
                pos = pf.apply_entry_fill(s, entry, px, q, fq, kind, fee if kind == "BASE" else D(0))
            exit_ = TradingOrder(client_order_id=f"x{trial}", idempotency_key=f"x{trial}", symbol="BTCUSDT", side="SELL",
                                 order_type="MARKET", quantity=pos.quantity, filled_quantity=D(0), cumulative_quote_qty=D(0),
                                 fee_quote=D(0), status="NEW", execution_mode="PAPER", purpose="EXIT", position_id=pos.id,
                                 order_meta={})
            s.add(exit_)
            s.flush()
            for _ in range(rng.randint(0, 3)):
                if pos.quantity <= D("0.0001"):
                    break
                px = D(str(round(rng.uniform(80, 120), 4)))
                q = (pos.quantity * D(str(rng.uniform(0.1, 0.9)))).quantize(D("1e-5"))
                pf.apply_exit_fill(s, exit_, px, q, (px * q * D("0.001")).quantize(D("1e-8")), "QUOTE")
            mark = D(str(round(rng.uniform(80, 120), 4)))
            v = pf.mark_to_market(s, "PAPER", lambda _: mark)
            acct = pf.get_account(s, "PAPER")
            lhs, rhs = v.equity - acct.initial_balance, acct.realised_pnl + v.unrealised
            assert abs(lhs - rhs) <= D("0.000001"), f"trial {trial}: equity-initial={lhs} realised+unrealised={rhs}"

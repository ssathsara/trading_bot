import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from sqlalchemy import func, select

from app.db import session_scope
from app.execution.adapters import ExecutionReport, Fill, OrderRequest, PaperExecutionAdapter
from app.execution.order_manager import OrderManager
from app.models import AccountState, OrderFill, Position, Trade, TradingOrder
from app.portfolio import service as pf
from tests.conftest import FakeAdapter

BOOK = {"bids": [["99.9", "5"], ["99.8", "10"]], "asks": [["100.1", "5"], ["100.2", "10"]]}


async def depth(symbol):
    return BOOK


def manager(adapter, btc_filters):
    with session_scope() as s:
        pf.get_account(s, "PAPER", D("10000"))
    return OrderManager(adapter, "PAPER", lambda sym: btc_filters)


async def entry(om, qty="2", key="ENTRY:BTCUSDT:1", **kw):
    return await om.place(symbol="BTCUSDT", side="BUY", order_type=kw.pop("order_type", "MARKET"), quantity=D(qty),
                          purpose="ENTRY", idempotency_key=key, expected_price=D("100.1"),
                          meta={"stop_loss": "95", "take_profit": "110", "strategy_name": "t"}, **kw)


# ------------------------------------------------------------------ paper engine
async def test_paper_market_order_walks_book_with_fees_and_slippage():
    a = PaperExecutionAdapter(depth, fee_rate=D("0.001"), slippage_bps=D("10"))
    r = await a.submit(OrderRequest("c1", "BTCUSDT", "BUY", "MARKET", D("7")))
    assert r.status == "FILLED" and r.simulated
    assert [(f.qty, f.price) for f in r.fills] == [(D("5"), D("100.2001")), (D("2"), D("100.3002"))]
    assert r.fills[0].fee == (D("100.2001") * 5 * D("0.001")).quantize(D("0.00000001"))
    again = await a.submit(OrderRequest("c1", "BTCUSDT", "BUY", "MARKET", D("7")))
    assert len(again.fills) == 2  # idempotent resubmission


async def test_paper_market_order_partial_when_book_exhausted():
    a = PaperExecutionAdapter(depth, D("0.001"), D("0"))
    r = await a.submit(OrderRequest("c2", "BTCUSDT", "SELL", "MARKET", D("20")))
    assert r.status == "EXPIRED" and r.filled_qty == D("15")


async def test_paper_limit_order_rests_then_partially_fills():
    a = PaperExecutionAdapter(depth, D("0.001"), D("0"))
    req = OrderRequest("c3", "BTCUSDT", "BUY", "LIMIT", D("4"), price=D("99.5"))
    r = await a.submit(req)
    assert r.status == "NEW" and not r.fills
    a.on_quote("BTCUSDT", D("99.3"), D("1"), D("99.4"), D("1.5"))
    r = await a.query(req)
    assert r.status == "PARTIALLY_FILLED" and r.filled_qty == D("1.5") and r.fills[0].price == D("99.4")
    a.on_quote("BTCUSDT", D("99.3"), D("1"), D("99.5"), D("10"))
    r = await a.query(req)
    assert r.status == "FILLED" and r.filled_qty == D("4")


# ------------------------------------------------------------------ order manager + accounting
async def test_entry_and_exit_accounting_identity(btc_filters):
    om = manager(PaperExecutionAdapter(depth, D("0.001"), D("0")), btc_filters)
    oid = await entry(om)
    with session_scope() as s:
        o = s.get(TradingOrder, oid)
        assert o.status == "FILLED" and o.filled_quantity == D("2") and o.average_fill_price == D("100.1")
        pos = s.scalar(select(Position))
        assert pos.quantity == D("2") and pos.stop_loss == D("95") and pos.take_profit == D("110")
        acct = pf.get_account(s, "PAPER")
        fee_in = D("200.2") * D("0.001")
        assert acct.cash_balance == D("10000") - D("200.2") - fee_in
        v = pf.mark_to_market(s, "PAPER", lambda sym: D("105"))
        assert v.unrealised == D("2") * D("105") - D("200.2") - fee_in
        assert v.equity - acct.initial_balance == acct.realised_pnl + v.unrealised
        pos_id = pos.id
    oid2 = await om.place(symbol="BTCUSDT", side="SELL", order_type="MARKET", quantity=D("2"), purpose="EXIT",
                          idempotency_key=f"EXIT:{pos_id}:0", expected_price=D("99.9"), position_id=pos_id,
                          meta={"exit_reason": "TAKE_PROFIT"})
    with session_scope() as s:
        pos = s.get(Position, pos_id)
        assert pos.status == "CLOSED" and pos.quantity == 0 and pos.exit_reason == "TAKE_PROFIT"
        trade = s.scalar(select(Trade))
        fee_out = D("199.8") * D("0.001")
        expected = D("199.8") - fee_out - D("200.2") - fee_in
        assert trade.realised_pnl == expected and trade.status == "CLOSED"
        assert trade.entry_fee == fee_in and trade.exit_fee == fee_out
        acct = pf.get_account(s, "PAPER")
        assert acct.cash_balance == D("10000") + expected
        assert acct.realised_pnl == expected and acct.total_fees == fee_in + fee_out
        assert acct.consecutive_losses == 1  # it was a loss
        perf = pf.performance(s, "PAPER", lambda sym: None)
        assert perf["net_profit"] == expected and perf["losing_trades"] == 1 and perf["win_rate"] == D("0")
        assert s.get(TradingOrder, oid2).slippage_bps == D("0")


async def test_duplicate_order_prevention(btc_filters):
    om = manager(PaperExecutionAdapter(depth, D("0.001"), D("0")), btc_filters)
    results = await asyncio.gather(*[entry(om) for _ in range(5)])  # same signal fired 5x concurrently
    assert sum(r is not None for r in results) == 1
    assert await entry(om, key="ENTRY:BTCUSDT:2") is None  # new candle, but position already open
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(TradingOrder)) == 1
        assert s.scalar(select(func.count()).select_from(Position)) == 1


async def test_rejected_order_changes_nothing(btc_filters):
    fa = FakeAdapter()
    fa.reports["submit"] = ExecutionReport("REJECTED", error="testnet rejected order (-2010): insufficient balance")
    om = manager(fa, btc_filters)
    oid = await entry(om)
    with session_scope() as s:
        o = s.get(TradingOrder, oid)
        assert o.status == "REJECTED" and "insufficient" in o.error_message
        assert s.scalar(select(func.count()).select_from(Position)) == 0
        assert pf.get_account(s, "PAPER").cash_balance == D("10000")


async def test_partial_fills_applied_incrementally_and_idempotently(btc_filters):
    fa = FakeAdapter()
    f1, f2 = Fill("t1", D("100"), D("0.5"), D("0.05"), "USDT"), Fill("t2", D("101"), D("1.5"), D("0.1515"), "USDT")
    fa.reports["submit"] = ExecutionReport("PARTIALLY_FILLED", "X1", [f1])
    om = manager(fa, btc_filters)
    oid = await entry(om, order_type="LIMIT", price=D("101"), timeout_seconds=300)
    fa.query_reports["*"] = ExecutionReport("FILLED", "X1", [f1, f2])  # cumulative report
    await om.reconcile()
    await om.reconcile()  # second reconcile must not double count
    with session_scope() as s:
        o = s.get(TradingOrder, oid)
        assert o.status == "FILLED" and o.filled_quantity == D("2")
        assert o.average_fill_price == D("100.75")
        assert s.scalar(select(func.count()).select_from(OrderFill)) == 2
        pos = s.scalar(select(Position))
        assert pos.quantity == D("2") and pos.cost_basis == D("201.5")
        assert pf.get_account(s, "PAPER").cash_balance == D("10000") - D("201.5") - D("0.2015")


async def test_unknown_outcome_is_reconciled_not_resubmitted(btc_filters):
    fa = FakeAdapter()
    fa.reports["submit"] = ExecutionReport("UNKNOWN", error="submission outcome unknown: ReadTimeout")
    om = manager(fa, btc_filters)
    oid = await entry(om)
    with session_scope() as s:
        assert s.get(TradingOrder, oid).status == "UNKNOWN"
        assert s.scalar(select(func.count()).select_from(Position)) == 0
    # Venue says it actually filled
    fa.query_reports["*"] = ExecutionReport("FILLED", "X9", [Fill("t9", D("100"), D("2"), D("0.2"), "USDT")])
    await om.reconcile()
    assert len(fa.submitted) == 1  # never resubmitted
    with session_scope() as s:
        assert s.get(TradingOrder, oid).status == "FILLED"
        assert s.scalar(select(Position)).quantity == D("2")


async def test_not_found_after_grace_marks_rejected(btc_filters):
    fa = FakeAdapter()
    fa.reports["submit"] = ExecutionReport("UNKNOWN", error="timeout")
    om = manager(fa, btc_filters)
    oid = await entry(om)
    fa.query_reports["*"] = ExecutionReport("NOT_FOUND")
    await om.reconcile()
    with session_scope() as s:
        assert s.get(TradingOrder, oid).status == "UNKNOWN"  # within grace: keep waiting
        s.get(TradingOrder, oid).created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    await om.reconcile()
    with session_scope() as s:
        assert s.get(TradingOrder, oid).status == "REJECTED"


async def test_base_asset_commission_and_dust_writeoff(btc_filters):
    """Testnet-style: buy commission charged in BTC; the sell leaves dust below minQty."""
    fa = FakeAdapter()
    fa.reports["submit"] = ExecutionReport("FILLED", "X", [Fill("b1", D("100"), D("1"), D("0.000001"), "BTC")])
    om = manager(fa, btc_filters)
    await entry(om, qty="1")
    with session_scope() as s:
        pos = s.scalar(select(Position))
        assert pos.quantity == D("0.999999") and pos.entry_fees_open == 0
        assert pf.get_account(s, "PAPER").cash_balance == D("9900")
        pid = pos.id
    sell_qty = btc_filters.round_qty(D("0.999999"))  # 0.99999 -> 0.000009 dust remains
    fa.reports["submit"] = ExecutionReport("FILLED", "Y", [Fill("s1", D("110"), sell_qty, D("0.109"), "USDT")])
    await om.place(symbol="BTCUSDT", side="SELL", order_type="MARKET", quantity=sell_qty, purpose="EXIT",
                   idempotency_key=f"EXIT:{pid}:0", expected_price=D("110"), position_id=pid)
    with session_scope() as s:
        pos = s.get(Position, pid)
        acct = pf.get_account(s, "PAPER")
        assert pos.status == "CLOSED" and pos.quantity == 0
        # identity: equity (all cash now) - initial == realised
        assert acct.cash_balance - acct.initial_balance == acct.realised_pnl


async def test_incomplete_exit_returns_position_to_open(btc_filters):
    fa = FakeAdapter()
    fa.reports["submit"] = ExecutionReport("FILLED", "X", [Fill("b1", D("100"), D("2"), D("0.2"), "USDT")])
    om = manager(fa, btc_filters)
    await entry(om)
    with session_scope() as s:
        pid = s.scalar(select(Position.id))
    fa.reports["submit"] = ExecutionReport("EXPIRED", "Y", [Fill("s1", D("99"), D("0.5"), D("0.0495"), "USDT")])
    await om.place(symbol="BTCUSDT", side="SELL", order_type="MARKET", quantity=D("2"), purpose="EXIT",
                   idempotency_key=f"EXIT:{pid}:0", expected_price=D("99"), position_id=pid)
    with session_scope() as s:
        pos = s.get(Position, pid)
        assert pos.status == "OPEN" and pos.quantity == D("1.5") and pos.realised_pnl < 0


async def test_restart_recovery_restores_resting_paper_orders(btc_filters):
    a1 = PaperExecutionAdapter(depth, D("0.001"), D("0"))
    om1 = manager(a1, btc_filters)
    oid = await entry(om1, order_type="LIMIT", price=D("99"), timeout_seconds=600)
    # "restart": brand-new simulator + manager instance
    a2 = PaperExecutionAdapter(depth, D("0.001"), D("0"))
    om2 = OrderManager(a2, "PAPER", lambda s: btc_filters)
    assert om2.restore_paper_orders() == 1
    a2.on_quote("BTCUSDT", D("98.9"), D("5"), D("99"), D("5"))
    await om2.reconcile()
    with session_scope() as s:
        assert s.get(TradingOrder, oid).status == "FILLED"
        assert s.scalar(select(func.count()).select_from(Position)) == 1
    # a SUBMITTING order lost in a crash is resolved as not executed (nothing moved)
    with session_scope() as s:
        s.add(TradingOrder(client_order_id="tbp-lost", idempotency_key="lost", symbol="ETHUSDT", side="BUY",
                           order_type="MARKET", quantity=D("1"), status="SUBMITTING", execution_mode="PAPER",
                           purpose="ENTRY", filled_quantity=D("0"), cumulative_quote_qty=D("0"), fee_quote=D("0")))
    await om2.reconcile()
    with session_scope() as s:
        assert s.scalar(select(TradingOrder).where(TradingOrder.client_order_id == "tbp-lost")).status == "EXPIRED"


async def test_limit_order_timeout_cancels(btc_filters):
    a = PaperExecutionAdapter(depth, D("0.001"), D("0"))
    om = manager(a, btc_filters)
    oid = await entry(om, order_type="LIMIT", price=D("90"), timeout_seconds=30)
    with session_scope() as s:
        s.get(TradingOrder, oid).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await om.reconcile()
    with session_scope() as s:
        assert s.get(TradingOrder, oid).status == "CANCELED"
        assert s.scalar(select(func.count()).select_from(Position)) == 0

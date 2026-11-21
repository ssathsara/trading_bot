"""The trading engine in LIVE mode, end to end, against a MOCKED api.binance.com (respx).
No real credentials or network calls are used."""
import dataclasses
from decimal import Decimal as D
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.models import AISignal, Position, TradingOrder
from app.portfolio import service as pf
from app.settings_store import get_control, save
from tests.test_engine import configured, make_engine, quote  # noqa: F401  (fixture re-export)

L = "https://api.binance.com"


class MockBinance:
    """Records signed requests; fills MARKET orders at a fixed price, rests stops."""

    def __init__(self, usdt="1000"):
        self.usdt = D(usdt)
        self.orders: list[dict] = []
        self.signed_hosts: set[str] = set()

    def order(self, request: httpx.Request) -> httpx.Response:
        self.signed_hosts.add(request.url.host)
        p = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        assert "signature" in p and request.headers["X-MBX-APIKEY"] == "live-key"
        self.orders.append(p)
        n = len(self.orders)
        if p["type"] == "MARKET":
            qty = D(p["quantity"])
            return httpx.Response(200, json={"orderId": n, "status": "FILLED", "executedQty": p["quantity"], "fills": [
                {"price": "100.01", "qty": p["quantity"], "commission": str((qty * D("100.01") * D("0.001")).quantize(D("0.00000001"))),
                 "commissionAsset": "USDT", "tradeId": 1000 + n}]})
        return httpx.Response(200, json={"orderId": n, "status": "NEW", "executedQty": "0", "fills": []})

    def account(self, request: httpx.Request) -> httpx.Response:
        self.signed_hosts.add(request.url.host)
        return httpx.Response(200, json={"balances": [{"asset": "USDT", "free": str(self.usdt), "locked": "0"}]})


@pytest.fixture
def live_env(monkeypatch, configured):  # noqa: F811
    for k, v in {"LIVE_TRADING_ENABLED": "true", "BINANCE_LIVE_API_KEY": "live-key",
                 "BINANCE_LIVE_API_SECRET": "live-secret", "LIVE_MAX_CAPITAL_USDT": "500"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    with session_scope() as s:
        ctl = get_control(s)
        ctl.execution_mode = "LIVE"
        save(s, "control", ctl)
    yield
    get_settings.cache_clear()


def mock_venue(mock: MockBinance):
    respx.post(f"{L}/api/v3/order").mock(side_effect=mock.order)
    respx.get(f"{L}/api/v3/account").mock(side_effect=mock.account)
    respx.get(f"{L}/api/v3/openOrders").mock(return_value=httpx.Response(200, json=[]))


@respx.mock
async def test_live_entry_uses_capped_ledger_real_balance_and_places_exchange_stop(live_env):
    mock = MockBinance(usdt="1000")
    mock_venue(mock)
    eng = await make_engine()
    await eng.start()
    try:
        assert eng.mode == "LIVE" and type(eng.om.adapter).__name__ == "LiveExecutionAdapter"
        with session_scope() as s:
            assert pf.get_account(s, "LIVE").initial_balance == D("500")  # ledger = cap, not INITIAL_BALANCE_USDT
        await eng.on_market_event(quote("BTCUSDT", "99.99", "100.01"))
        result = await eng.analyse_and_act("BTCUSDT")
        assert result.signal == "BUY", result.reason
        with session_scope() as s:
            sig = s.scalar(select(AISignal).order_by(AISignal.id.desc()))
            assert sig.action_status == "ORDER_PLACED", sig.action_status
            o = s.scalar(select(TradingOrder).where(TradingOrder.purpose == "ENTRY"))
            assert o.execution_mode == "LIVE" and o.status == "FILLED" and o.client_order_id.startswith("tbl-")
            pos = s.scalar(select(Position))
            assert pos.status == "OPEN" and pos.execution_mode == "LIVE"
            notional = o.quantity * D("100.01")
        assert notional <= D("500"), f"entry notional {notional} exceeds the live capital cap"
        assert mock.signed_hosts == {"api.binance.com"}

        # exchange-side stop-loss placed on the live venue
        eng.filters["BTCUSDT"] = dataclasses.replace(eng.filters["BTCUSDT"], order_types=("LIMIT", "MARKET", "STOP_LOSS"))
        await eng.protect_tick()
        assert any(p["type"] == "STOP_LOSS" for p in mock.orders), mock.orders
    finally:
        await eng.stop()


@respx.mock
async def test_live_entry_blocked_when_real_balance_too_low(live_env):
    mock = MockBinance(usdt="1")
    mock_venue(mock)
    eng = await make_engine()
    await eng.start()
    try:
        await eng.on_market_event(quote("BTCUSDT", "99.99", "100.01"))
        await eng.analyse_and_act("BTCUSDT")
        with session_scope() as s:
            sig = s.scalar(select(AISignal).order_by(AISignal.id.desc()))
            assert sig.action_status == "INSUFFICIENT_BALANCE", sig.action_status
        assert not mock.orders
    finally:
        await eng.stop()


@respx.mock
async def test_live_capital_cap_blocks_entry_beyond_cap(live_env):
    mock = MockBinance(usdt="100000")
    mock_venue(mock)
    eng = await make_engine()
    await eng.start()
    try:
        with session_scope() as s:  # pretend 495 of the 500 cap is already invested
            acct = pf.get_account(s, "LIVE")
            s.add(Position(symbol="ETHUSDT", execution_mode="LIVE", quantity=D("4.95"), average_entry_price=D("100"),
                           cost_basis=D("495"), current_price=D("100"), status="OPEN", stop_loss=D("90"), take_profit=D("120")))
            acct.cash_balance -= D("495")
        await eng.on_market_event(quote("BTCUSDT", "99.99", "100.01"))
        await eng.analyse_and_act("BTCUSDT")
        with session_scope() as s:
            sig = s.scalar(select(AISignal).where(AISignal.symbol == "BTCUSDT").order_by(AISignal.id.desc()))
            assert sig.action_status in ("LIVE_CAPITAL_CAP", "REJECTED_BY_RISK"), sig.action_status
        assert not mock.orders
    finally:
        await eng.stop()


async def test_worker_refuses_live_when_not_enabled(configured, monkeypatch):  # noqa: F811
    with session_scope() as s:
        ctl = get_control(s)
        ctl.execution_mode = "LIVE"
        save(s, "control", ctl)
    eng = await make_engine()
    with pytest.raises(RuntimeError, match="LIVE mode requested"):
        await eng.start()
    eng._release_lease()

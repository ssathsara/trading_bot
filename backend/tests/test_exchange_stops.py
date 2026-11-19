"""Exchange-side protective stops (TESTNET): placed, maintained, honoured while offline,
and cancelled before the bot sells."""
import asyncio
import itertools
import time
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.events import EventBus
from app.exchange.filters import SymbolFilters
from app.exchange.websocket import BookTickerEvent
from app.execution.adapters import TERMINAL, ExecutionReport, Fill
from app.execution.order_manager import OrderManager
from app.market.price_cache import price_cache
from app.models import Position, TradingOrder
from app.portfolio import service as pf
from app.settings_store import ExecutionSettings, save
from app.worker import engine as engine_mod
from tests.conftest import exchange_symbol
from tests.test_engine import FakePublic, FakeStream, NoTestnet

SYM = "BTCUSDT"


class FakeVenue:
    """Minimal Binance spot model: MARKET fills at the quote, stops rest until triggered,
    and a resting stop locks the coins (a sell beyond the free balance is rejected)."""

    mode = "TESTNET"

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.ids = itertools.count(1)
        self.base = D("0")

    def _locked(self):
        return sum((o["req"].quantity for o in self.orders.values()
                    if o["req"].order_type.startswith("STOP") and o["status"] == "NEW"), D("0"))

    def _report(self, o):
        return ExecutionReport(o["status"], o["id"], list(o["fills"]))

    def _fill(self, o, price):
        req = o["req"]
        o["fills"].append(Fill(f"{o['id']}-1", price, req.quantity, D("0"), "USDT"))
        o["status"] = "FILLED"
        self.base += req.quantity if req.side == "BUY" else -req.quantity

    async def submit(self, req):
        o = {"req": req, "id": str(next(self.ids)), "status": "NEW", "fills": []}
        if req.side == "SELL" and req.quantity > self.base - self._locked():
            return ExecutionReport("REJECTED", error="insufficient balance")
        self.orders[req.client_order_id] = o
        if req.order_type == "MARKET":
            q = price_cache.get(req.symbol)
            self._fill(o, q.ask if req.side == "BUY" else q.bid)
        return self._report(o)

    async def query(self, req):
        o = self.orders.get(req.client_order_id)
        return self._report(o) if o else ExecutionReport("NOT_FOUND")

    async def cancel(self, req):
        o = self.orders.get(req.client_order_id)
        if o is None:
            return ExecutionReport("NOT_FOUND")
        if o["status"] not in TERMINAL:
            o["status"] = "CANCELED"
        return self._report(o)

    def trigger_stops(self, price):
        """The market trades through the stop while the bot is not looking."""
        for o in self.orders.values():
            if o["req"].order_type == "STOP_LOSS" and o["status"] == "NEW" and price <= o["req"].stop_price:
                self._fill(o, price)

    def resting_stops(self):
        return [o["req"] for o in self.orders.values() if o["req"].order_type.startswith("STOP") and o["status"] == "NEW"]


def quote(bid, ask):
    return BookTickerEvent(SYM, int(time.time() * 1e6), D(bid), D("50"), D(ask), D("50"))


@pytest.fixture
async def eng(monkeypatch):
    monkeypatch.setattr(engine_mod, "PublicTestnetProbe", NoTestnet)
    price_cache._q.clear()
    e = engine_mod.TradingEngine(get_settings(), public=FakePublic(), stream=FakeStream())
    e.has_lease = True
    info = exchange_symbol(SYM, "BTC", step="0.0001", tick="0.01")
    info["orderTypes"] = ["LIMIT", "MARKET", "STOP_LOSS", "STOP_LOSS_LIMIT"]
    e.filters = {SYM: SymbolFilters.from_exchange_info(info)}
    e.mode = "TESTNET"
    e.venue = FakeVenue()
    e.om = OrderManager(e.venue, "TESTNET", e.filters.get)
    with session_scope() as s:
        pf.get_account(s, "TESTNET", D("10000"))
    yield e
    await e.public.close()


async def open_position(e, stop="95", qty="1"):
    await e.on_market_event(quote("99.99", "100.01"))
    await e.om.place(symbol=SYM, side="BUY", order_type="MARKET", quantity=D(qty), purpose="ENTRY",
                     idempotency_key="ENTRY:1", expected_price=D("100.01"),
                     meta={"stop_loss": stop, "take_profit": "110"})
    with session_scope() as s:
        return s.scalar(select(Position)).id


def stops(status=None):
    with session_scope() as s:
        q = select(TradingOrder).where(TradingOrder.purpose == "STOP").order_by(TradingOrder.id)
        return [(o.status, o.order_type, o.quantity, D(o.order_meta["stop_price"])) for o in s.scalars(q)
                if status is None or o.status == status]


def position():
    with session_scope() as s:
        p = s.scalar(select(Position))
        return p.status, p.exit_reason, p.quantity, p.realised_pnl


async def test_stop_placed_below_bot_stop_once(eng):
    await open_position(eng)
    await eng.protect_tick()
    await eng.protect_tick()  # idempotent: still exactly one
    assert stops() == [("NEW", "STOP_LOSS", D("1"), D("94.52"))]  # 95 * (1 - 0.5%), rounded down to tick
    assert position()[0] == "OPEN"  # a protective stop does not mark the position as closing
    assert len(eng.venue.resting_stops()) == 1


async def test_stop_fills_while_bot_offline_closes_position_on_restart(eng):
    await open_position(eng)
    await eng.protect_tick()
    eng.venue.trigger_stops(D("94.00"))  # gap down while the worker is off
    eng.om._stop_polled.clear()  # a fresh process polls every active order at startup
    await eng.om.reconcile()
    status, reason, qty, pnl = position()
    assert (status, reason, qty) == ("CLOSED", "STOP_LOSS_EXCHANGE", D("0"))
    assert pnl == D("94.00") - D("100.01")


async def test_bot_exit_cancels_exchange_stop_first(eng):
    await open_position(eng)
    await eng.protect_tick()
    await eng.on_market_event(quote("94.90", "94.92"))  # below the bot stop, above the exchange stop
    await eng.monitor_positions()
    assert position()[:2] == ("CLOSED", "STOP_LOSS")
    assert stops() == [("CANCELED", "STOP_LOSS", D("1"), D("94.52"))]
    assert eng.venue.resting_stops() == [] and eng.venue.base == 0
    await eng.protect_tick()
    assert len(stops()) == 1  # no new stop for a closed position


async def test_stop_filled_just_before_bot_exit_is_not_sold_twice(eng):
    await open_position(eng)
    await eng.protect_tick()
    eng.venue.trigger_stops(D("94.50"))  # exchange fills; the bot has not polled yet
    await eng.on_market_event(quote("94.50", "94.52"))
    await eng.monitor_positions()
    assert position()[:2] == ("CLOSED", "STOP_LOSS_EXCHANGE")
    with session_scope() as s:
        assert s.scalar(select(TradingOrder).where(TradingOrder.purpose == "EXIT")) is None
    assert eng.venue.base == 0


async def test_trailing_stop_moves_exchange_stop_up(eng):
    pid = await open_position(eng)
    await eng.protect_tick()
    with session_scope() as s:
        s.get(Position, pid).stop_loss = D("95.10")  # +0.1%: below the replace threshold
    await eng.protect_tick()
    assert len(stops()) == 1
    with session_scope() as s:
        s.get(Position, pid).stop_loss = D("98")
    await eng.protect_tick()
    assert stops() == [("CANCELED", "STOP_LOSS", D("1"), D("94.52")), ("NEW", "STOP_LOSS", D("1"), D("97.51"))]
    assert len(eng.venue.resting_stops()) == 1


async def test_no_stop_when_price_already_below_or_disabled(eng):
    await open_position(eng)
    await eng.on_market_event(quote("94.00", "94.02"))
    await eng.protect_tick()
    assert stops() == []  # would trigger immediately; the bot's monitor exits instead
    await eng.on_market_event(quote("99.99", "100.01"))
    await eng.protect_tick()
    assert len(stops("NEW")) == 1
    with session_scope() as s:
        save(s, "execution", ExecutionSettings(exchange_stop_enabled=False))
    await eng.protect_tick()
    assert stops("NEW") == [] and eng.venue.resting_stops() == []


async def test_rejected_stop_backs_off(eng):
    await open_position(eng)
    eng.venue.base = D("0")  # coins not there (e.g. moved manually): the venue rejects the stop
    await eng.protect_tick()
    await eng.protect_tick()
    assert [st for st, *_ in stops()] == ["REJECTED"]  # not retried every tick


async def test_paper_mode_never_places_exchange_stops(eng):
    eng.mode = "PAPER"
    await open_position(eng)
    await eng.protect_tick()
    assert stops() == []


# ---------------------------------------------------------------- event bus / redis
class FakePipe:
    def __init__(self, redis):
        self.redis, self.cmds = redis, []

    async def __aenter__(self):
        self.redis.open_conns += 1
        self.redis.max_conns = max(self.redis.max_conns, self.redis.open_conns)
        return self

    async def __aexit__(self, *a):
        self.redis.open_conns -= 1

    def publish(self, channel, payload):
        self.cmds.append((channel, payload))

    async def execute(self):
        await asyncio.sleep(0)
        self.redis.sent.extend(self.cmds)


class FakeRedis:
    def __init__(self):
        self.sent, self.open_conns, self.max_conns = [], 0, 0

    def pipeline(self, transaction=False):
        return FakePipe(self)

    async def aclose(self):
        pass


async def test_redis_publishes_go_through_one_sender():
    bus = EventBus()
    bus.bind_loop(asyncio.get_running_loop())
    bus._redis = FakeRedis()
    bus._outbox = asyncio.Queue(maxsize=10_000)
    bus._sender_task = asyncio.create_task(bus._sender())
    for i in range(2000):  # a burst like a stream reconnect
        bus.publish("market", "ticker", {"i": i})
    for _ in range(50):
        await asyncio.sleep(0)
        if len(bus._redis.sent) == 2000:
            break
    assert len(bus._redis.sent) == 2000
    assert bus._redis.max_conns == 1
    await bus.close()

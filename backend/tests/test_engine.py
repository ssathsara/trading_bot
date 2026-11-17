"""End-to-end paper-trading flow through the real engine with mocked Binance I/O."""
import time
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db import session_scope
from app.exchange.websocket import BookTickerEvent, KlineEvent
from app.market.candles import TIMEFRAMES
from app.market.price_cache import price_cache
from app.models import AISignal, Position, RiskEvent, Trade, TradingOrder, TradingPair
from app.portfolio import service as pf
from app.settings_store import MarketSettings, RiskSettings, StrategySettings, get_control, save
from app.worker import engine as engine_mod
from tests.conftest import exchange_symbol, make_ohlcv


class FakePublic:
    last_error = None
    used_weight_1m = 0

    def __init__(self):
        self.symbols = [exchange_symbol("BTCUSDT", "BTC", step="0.0001", tick="0.01"),
                        exchange_symbol("ETHUSDT", "ETH", step="0.0001", tick="0.01"),
                        exchange_symbol("USDCUSDT", "USDC")]

    async def exchange_info(self, symbols=None):
        return {"symbols": self.symbols}

    async def ticker_24h(self, symbol=None):
        return [{"symbol": s["symbol"], "lastPrice": "100", "priceChangePercent": "2", "quoteVolume": str(10 ** 9 - i)}
                for i, s in enumerate(self.symbols)]

    async def klines(self, symbol, interval, limit=500, start_ms=None, end_ms=None):
        dur = TIMEFRAMES[interval]
        now = int(time.time() * 1000)
        cur_open = now - now % dur
        n = 301
        df = make_ohlcv(n, start=80, drift=0.002, vol=0.003, seed=4)
        df.loc[df.index[-2], "volume"] = df["volume"].mean() * 2
        rows = []
        for i, r in df.iterrows():
            ot = cur_open - (n - 1 - i) * dur
            rows.append([ot, f"{r.open:.4f}", f"{r.high:.4f}", f"{r.low:.4f}", f"{r.close:.4f}", f"{r.volume:.4f}",
                         ot + dur - 1, f"{r.quote_volume:.4f}", 100, "0", "0", "0"])
        if end_ms is not None:
            rows = [r for r in rows if r[0] <= end_ms]
        return rows[-limit:]

    async def order_book(self, symbol, limit=20):
        q = price_cache.get(symbol)
        return {"bids": [[str(q.bid), "50"]], "asks": [[str(q.ask), "50"]]}

    async def close(self):
        pass


class FakeStream:
    connected = True
    reconnects = 0
    invalid_messages = 0

    def __init__(self):
        self.subs = None

    def set_subscriptions(self, symbols, tfs):
        self.subs = (symbols, tfs)

    def is_healthy(self, max_silence=None):
        return True

    def start(self):
        import asyncio

        return asyncio.create_task(asyncio.sleep(3600))

    async def stop(self):
        pass


class NoTestnet:
    def __init__(self, url):
        pass

    async def exchange_info(self):
        return {"symbols": [exchange_symbol("BTCUSDT", "BTC")]}  # ETH "not on testnet"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(engine_mod, "PublicTestnetProbe", NoTestnet)
    with session_scope() as s:
        save(s, "strategy", StrategySettings(rsi_entry_min=D("0"), rsi_entry_max=D("100"), min_adx=D("0"),
                                             min_score=D("40"), min_volume_ratio=D("0")))
        save(s, "market", MarketSettings(top_n=10, min_history_candles=250, history_candles=300))
        save(s, "risk", RiskSettings(max_atr_pct=D("50")))
        ctl = get_control(s)
        ctl.state = "RUNNING"
        save(s, "control", ctl)
    price_cache._q.clear()


async def make_engine():
    eng = engine_mod.TradingEngine(get_settings(), public=FakePublic(), stream=FakeStream())
    eng.stream.on_event = eng.on_market_event
    return eng


def quote(symbol, bid, ask):
    return BookTickerEvent(symbol, int(time.time() * 1e6), D(bid), D("50"), D(ask), D("50"))


async def test_full_paper_trading_cycle_and_restart(configured):
    eng = await make_engine()
    await eng.start()
    try:
        # discovery: stablecoin excluded, testnet availability flagged but not substituted
        assert eng.top_symbols == ["BTCUSDT", "ETHUSDT"]
        with session_scope() as s:
            eth = s.scalar(select(TradingPair).where(TradingPair.symbol == "ETHUSDT"))
            assert eth.market_rank == 2 and eth.testnet_supported is False and eth.enabled is True  # paper OK
        assert "BTCUSDT" in eng.stream.subs[0]

        await eng.on_market_event(quote("BTCUSDT", "99.99", "100.01"))
        result = await eng.analyse_and_act("BTCUSDT")
        assert result.signal == "BUY", result.reason
        with session_scope() as s:
            order = s.scalar(select(TradingOrder))
            assert order.status == "FILLED" and order.execution_mode == "PAPER" and order.purpose == "ENTRY"
            pos = s.scalar(select(Position))
            assert pos.status == "OPEN" and pos.stop_loss < D("100.01") < pos.take_profit
            sig = s.scalar(select(AISignal).order_by(AISignal.id.desc()))
            assert sig.action_status == "ORDER_PLACED"
            stop = pos.stop_loss

        # same candle analysed again (duplicate event) => no second order
        await eng.analyse_and_act("BTCUSDT")
        with session_scope() as s:
            assert s.scalar(select(func.count()).select_from(TradingOrder)) == 1

        # ---- restart: a new engine instance must recover state without new orders
        await eng.stop()
        eng = await make_engine()
        await eng.start()
        with session_scope() as s:
            assert s.scalar(select(func.count()).select_from(TradingOrder)) == 1
            assert len(pf.open_positions(s, "PAPER")) == 1
        assert "BTCUSDT" in eng.watch_symbols()

        # ---- price collapses through the stop-loss => automatic exit
        low = stop - D("1")
        await eng.on_market_event(quote("BTCUSDT", str(low), str(low + D("0.02"))))
        await eng.monitor_positions()
        with session_scope() as s:
            pos = s.scalar(select(Position))
            assert pos.status == "CLOSED" and pos.exit_reason == "STOP_LOSS"
            trade = s.scalar(select(Trade))
            assert trade.status == "CLOSED" and trade.realised_pnl < 0
            acct = pf.get_account(s, "PAPER")
            assert acct.cash_balance - acct.initial_balance == acct.realised_pnl  # flat => equity identity
            assert acct.consecutive_losses == 1
    finally:
        await eng.stop()


async def test_stale_data_blocks_entries(configured):
    eng = await make_engine()
    await eng.start()
    try:
        await eng.on_market_event(quote("BTCUSDT", "99.99", "100.01"))
        price_cache.get("BTCUSDT").updated_at -= 3600  # an hour old
        await eng.analyse_and_act("BTCUSDT")
        with session_scope() as s:
            assert s.scalar(select(func.count()).select_from(TradingOrder)) == 0
            ev = s.scalar(select(RiskEvent).where(RiskEvent.event_type == "TRADE_REJECTED"))
            assert "stale" in ev.description
    finally:
        await eng.stop()


async def test_hard_limit_halts_trading(configured):
    eng = await make_engine()
    await eng.start()
    try:
        with session_scope() as s:
            acct = pf.get_account(s, "PAPER")
            acct.cash_balance = D("9000")  # -10% vs peak 10000 => drawdown limit
            acct.day_start_equity = D("9000")  # isolate from the daily-loss limit
        await eng.risk_tick()
        with session_scope() as s:
            acct = pf.get_account(s, "PAPER")
            assert acct.halted and "DRAWDOWN" in acct.halt_reason
        await eng.on_market_event(quote("BTCUSDT", "99.99", "100.01"))
        await eng.analyse_and_act("BTCUSDT")
        with session_scope() as s:
            assert s.scalar(select(func.count()).select_from(TradingOrder)) == 0
    finally:
        await eng.stop()


async def test_paused_state_blocks_entries_but_closed_candle_triggers_analysis(configured):
    eng = await make_engine()
    await eng.start()
    try:
        with session_scope() as s:
            ctl = get_control(s)
            ctl.state = "PAUSED"
            save(s, "control", ctl)
        await eng.on_market_event(quote("BTCUSDT", "99.99", "100.01"))
        r = await eng.analyse_and_act("BTCUSDT")
        assert r.signal == "BUY"
        with session_scope() as s:
            assert s.scalar(select(func.count()).select_from(TradingOrder)) == 0
            assert s.scalar(select(AISignal).order_by(AISignal.id.desc())).action_status == "NOT_EXECUTED_PAUSED"
        now = int(time.time() * 1000)
        dur = TIMEFRAMES["5m"]
        ot = now - now % dur - dur
        await eng.on_market_event(KlineEvent("BTCUSDT", "5m", ot, ot + dur - 1, D("100"), D("101"), D("99"), D("100.5"),
                                             D("10"), D("1000"), 5, True))
        assert "BTCUSDT" in eng._queued
    finally:
        await eng.stop()


async def test_single_worker_lease(configured):
    a = await make_engine()
    await a.start()
    try:
        b = await make_engine()
        with pytest.raises(RuntimeError, match="another worker"):
            await b.start()
    finally:
        await a.stop()

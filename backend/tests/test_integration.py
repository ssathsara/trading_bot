"""Opt-in integration tests against REAL Binance endpoints.

    RUN_INTEGRATION=1 pytest -m integration                 # public market data + paper
    RUN_INTEGRATION=1 TESTNET_KEY=... TESTNET_SECRET=... pytest -m integration   # + testnet

Public tests use read-only market-data hosts. Testnet tests use ONLY
testnet.binance.vision with dedicated testnet credentials and place a validated
test order via /api/v3/order/test (no fill), never a production endpoint.
"""
import asyncio
import json
import os
from decimal import Decimal as D

import pytest

from app.exchange.filters import SymbolFilters
from app.exchange.rest import PublicMarketClient, TestnetClient
from app.exchange.websocket import MarketStreamManager
from app.execution.adapters import OrderRequest, PaperExecutionAdapter
from app.market.candles import check_quality, candles_to_frame, fetch_closed_candles
from app.market.discovery import rank_pairs

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(os.environ.get("RUN_INTEGRATION") != "1", reason="set RUN_INTEGRATION=1")]
PUBLIC = "https://data-api.binance.vision"


async def test_public_market_data_and_top10():
    c = PublicMarketClient(PUBLIC)
    try:
        assert await c.ping()
        info, tickers = await c.exchange_info(), await c.ticker_24h()
        selected, _ = rank_pairs(info["symbols"], tickers, top_n=10)
        assert len(selected) == 10 and all(s.symbol.endswith("USDT") for s in selected)
        candles = await fetch_closed_candles(c, selected[0].symbol, "15m", 300)
        assert len(candles) == 300
        assert not [i for i in check_quality(candles_to_frame(candles), "15m") if "stale" in i or "OHLC" in i]
    finally:
        await c.close()


async def test_live_websocket_stream():
    got = []

    async def on_event(ev):
        got.append(ev)

    m = MarketStreamManager("wss://data-stream.binance.vision", on_event)
    m.set_subscriptions(["BTCUSDT"], ["1m"])
    m.start()
    for _ in range(100):
        if len(got) >= 5:
            break
        await asyncio.sleep(0.1)
    await m.stop()
    assert len(got) >= 5


async def test_paper_order_against_live_order_book():
    c = PublicMarketClient(PUBLIC)
    try:
        a = PaperExecutionAdapter(lambda s: c.order_book(s, 20), D("0.001"), D("5"))
        r = await a.submit(OrderRequest("int-1", "BTCUSDT", "BUY", "MARKET", D("0.01")))
        assert r.status == "FILLED" and r.simulated and r.fills
    finally:
        await c.close()


@pytest.mark.skipif(not (os.environ.get("TESTNET_KEY") and os.environ.get("TESTNET_SECRET")), reason="testnet keys not set")
async def test_testnet_account_and_test_order():
    c = TestnetClient("https://testnet.binance.vision", os.environ["TESTNET_KEY"], os.environ["TESTNET_SECRET"])
    try:
        await c.sync_time()
        acct = await c.account()
        assert "balances" in acct
        info = await c.exchange_info()
        f = SymbolFilters.from_exchange_info(next(s for s in info["symbols"] if s["symbol"] == "BTCUSDT"))
        price = D((await c._request("GET", "/api/v3/ticker/price", {"symbol": "BTCUSDT"}))["price"])
        qty = f.round_qty(D("20") / price)
        assert f.validate(qty, price, market=True) == []
        resp = await c.new_order(symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity=qty,
                                 client_order_id="tbt-integration-test", test=True)
        assert resp == {} or isinstance(resp, dict)
    finally:
        await c.close()

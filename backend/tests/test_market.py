import asyncio
import json
from decimal import Decimal as D

import httpx
import pytest
import respx

from app.exchange.filters import SymbolFilters
from app.exchange.rest import BinanceAPIError, BinanceNetworkError, BinanceRateLimitError, PublicMarketClient, TestnetClient
from app.exchange.websocket import (BookTickerEvent, Deduplicator, InvalidPayload, KlineEvent, MarketStreamManager,
                                    parse_message)
from app.market.candles import Candle, check_quality, load_frame, store_candles
from app.market.discovery import is_leveraged_token, is_stablecoin, rank_pairs
from tests.conftest import exchange_symbol, make_ohlcv

BASE = "https://data-api.binance.vision"


# ---------------------------------------------------------------- filters
def test_filters_parse_and_round(btc_filters: SymbolFilters):
    f = btc_filters
    assert f.tradable and f.min_notional == D("5") and f.market_step_size == D("0.00001")
    assert f.round_qty(D("0.123456789")) == D("0.12345")
    assert f.round_price(D("100.019"), "BUY") == D("100.01")
    assert f.round_price(D("100.011"), "SELL") == D("100.02")
    assert f.validate(D("0.0001"), D("60000"), market=True) == []
    assert any("minNotional" in e for e in f.validate(D("0.00001"), D("60000"), market=True))
    assert any("stepSize" in e for e in f.validate(D("0.000015"), D("60000"), market=True))
    assert SymbolFilters.from_json(f.to_json()) == f


# ---------------------------------------------------------------- discovery
def _ticker(sym, qv, price="10"):
    return {"symbol": sym, "lastPrice": price, "priceChangePercent": "1.5", "quoteVolume": str(qv)}


def test_top_pairs_selection_rules():
    symbols = [
        exchange_symbol("BTCUSDT", "BTC"), exchange_symbol("ETHUSDT", "ETH"),
        exchange_symbol("USDCUSDT", "USDC"), exchange_symbol("FDUSDUSDT", "FDUSD"),
        exchange_symbol("BTCUPUSDT", "BTCUP"), exchange_symbol("JUPUSDT", "JUP"),
        exchange_symbol("DEADUSDT", "DEAD", status="BREAK"), exchange_symbol("ETHBTC", "ETH", quote="BTC"),
        exchange_symbol("HUGEUSDT", "HUGE", min_notional="500"), exchange_symbol("NOHISTUSDT", "NOHIST"),
        exchange_symbol("SOLUSDT", "SOL"),
    ]
    tickers = [_ticker(s["symbol"], 1000 - i) for i, s in enumerate(symbols)]
    history = {s["symbol"]: True for s in symbols} | {"NOHISTUSDT": False}
    selected, rejected = rank_pairs(symbols, tickers, top_n=10, testnet_symbols={"BTCUSDT"}, history_ok=history)
    assert [c.symbol for c in selected] == ["BTCUSDT", "ETHUSDT", "JUPUSDT", "SOLUSDT"]
    assert [c.rank for c in selected] == [1, 2, 3, 4]
    reasons = {c.symbol: c.exclusion_reason for c in rejected}
    assert reasons["USDCUSDT"] == reasons["FDUSDUSDT"] == "stablecoin base asset"
    assert reasons["BTCUPUSDT"] == "leveraged token"
    assert "suspended" in reasons["DEADUSDT"]
    assert "minimum trade size" in reasons["HUGEUSDT"]
    assert reasons["NOHISTUSDT"] == "insufficient historical data"
    assert "ETHBTC" not in reasons  # non-USDT quote simply not considered
    # testnet availability is reported, never substituted
    assert selected[0].testnet_supported and not selected[1].testnet_supported


def test_tokenized_equities_excluded():
    nvda = exchange_symbol("NVDABUSDT", "NVDAB")
    nvda["permissionSets"] = [["SPOT", "MARGIN", "TRD_GRP_261"]]
    symbols = [nvda, exchange_symbol("BTCUSDT", "BTC")]
    tickers = [_ticker("NVDABUSDT", 2000), _ticker("BTCUSDT", 1000)]
    selected, rejected = rank_pairs(symbols, tickers, excluded_permission_groups={"TRD_GRP_261"})
    assert [c.symbol for c in selected] == ["BTCUSDT"]
    assert rejected[0].exclusion_reason == "tokenized stock/ETF (non-crypto asset)"


def test_top_n_limit_and_volume_order():
    symbols = [exchange_symbol(f"C{i}USDT", f"C{i}") for i in range(15)]
    tickers = [_ticker(f"C{i}USDT", i * 10) for i in range(15)]
    selected, _ = rank_pairs(symbols, tickers, top_n=10)
    assert len(selected) == 10 and selected[0].symbol == "C14USDT"
    assert all(a.quote_volume >= b.quote_volume for a, b in zip(selected, selected[1:]))


def test_pinned_symbols_added_beyond_top_n():
    symbols = [exchange_symbol(f"C{i}USDT", f"C{i}") for i in range(15)] + [exchange_symbol("USDCUSDT", "USDC")]
    tickers = [_ticker(f"C{i}USDT", (i + 1) * 10) for i in range(15)] + [_ticker("USDCUSDT", 1)]
    selected, _ = rank_pairs(symbols, tickers, top_n=3, pinned={"C0USDT", "USDCUSDT"})
    assert [c.symbol for c in selected] == ["C14USDT", "C13USDT", "C12USDT", "C0USDT"]  # ineligible pin ignored
    assert selected[-1].rank == 4


def test_all_pairs_mode_and_liquidity_floor():
    symbols = [exchange_symbol(f"C{i}USDT", f"C{i}") for i in range(1, 51)]
    tickers = [_ticker(f"C{i}USDT", i * 100_000) for i in range(1, 51)]  # 0.1M .. 5M
    selected, _ = rank_pairs(symbols, tickers, top_n=0)
    assert len(selected) == 50 and [c.rank for c in selected] == list(range(1, 51))
    selected, rejected = rank_pairs(symbols, tickers, top_n=0, min_quote_volume=D("1000000"))
    assert len(selected) == 41 and all(c.quote_volume >= D("1000000") for c in selected)
    assert {c.symbol for c in rejected} == {f"C{i}USDT" for i in range(1, 10)}
    assert rejected[0].exclusion_reason.startswith("low liquidity")


async def test_sharded_stream_splits_connections_and_aggregates_status():
    from app.exchange.websocket import ShardedStreamManager

    made = []

    class FakeShard:
        def __init__(self, url, on_event, on_status, **kw):
            self.on_status, self.include_trades, self._streams = on_status, True, []
            self.connected, self.reconnects, self.invalid_messages = False, 1, 0
            self.started = self.stopped = False
            made.append(self)

        def set_subscriptions(self, symbols, tfs):
            self.symbols, self._streams = symbols, [f"{s}@x" for s in symbols]

        def is_healthy(self, max_silence=None):
            return self.connected

        def start(self):
            self.started = True

        async def stop(self):
            self.stopped = True

    statuses = []

    async def on_status(st, info):
        statuses.append(st)

    m = ShardedStreamManager("wss://x", None, on_status, max_streams=50, factory=FakeShard)
    syms = [f"S{i:03d}USDT" for i in range(120)]
    m.set_subscriptions(syms, ["15m", "1h", "4h"])  # 5 streams/symbol (no trades) => 10 symbols/shard
    assert len(m.shards) == 12 and all(not s.include_trades for s in m.shards)
    assert sorted(x for s in m.shards for x in s.symbols) == sorted(syms)
    task = m.start()
    await asyncio.sleep(0)
    assert all(s.started for s in m.shards) and m.reconnects == 12 and not m.connected
    for i, s in enumerate(m.shards):
        s.connected = True
        await s.on_status("connected", {})
    assert m.connected and m.is_healthy() and statuses[-1] == "connected"
    await m.shards[3].on_status("reconnecting", {})
    assert statuses[-1] == "reconnecting"  # one bad shard => aggregate is degraded
    await m.shards[3].on_status("connected", {})
    assert statuses[-1] == "connected"
    # universe shrinks => surplus connections closed; small universe re-enables trades
    m.set_subscriptions(syms[:5], ["15m", "1h", "4h"])
    await asyncio.sleep(0)
    assert len(m.shards) == 1 and m.shards[0].include_trades and made[5].stopped
    task.cancel()
    await m.stop()


def test_token_classifiers():
    assert is_stablecoin("USDT") and is_stablecoin("USD1") and not is_stablecoin("BTC")
    assert is_leveraged_token("ETHDOWN") and is_leveraged_token("BTCUP") and not is_leveraged_token("JUP")


# ---------------------------------------------------------------- candles
def _c(t, o=100, h=101, l=99, c=100.5, tf_ms=60_000):
    return Candle(t, t + tf_ms - 1, D(o), D(h), D(l), D(c), D("10"), D("1000"), 5)


def test_candle_storage_dedup_and_closed_only():
    from app.db import session_scope

    now = 10 * 60_000 + 30_000  # mid-way through the 11th minute
    candles = [_c(i * 60_000) for i in range(11)]  # last one still open
    with session_scope() as s:
        assert store_candles(s, "BTCUSDT", "1m", candles, now_ms=now) == 10
        assert store_candles(s, "BTCUSDT", "1m", candles, now_ms=now) == 0  # duplicates ignored
        bad = _c(20 * 60_000, h=98)  # high below open: invalid
        assert store_candles(s, "BTCUSDT", "1m", [bad], now_ms=10 ** 12) == 0
        df = load_frame(s, "BTCUSDT", "1m")
    assert len(df) == 10 and df["open_time"].is_monotonic_increasing


def test_data_quality_checks():
    df = make_ohlcv(50, freq="1min")
    assert check_quality(df, "1m", now=df["close_time"].iloc[-1]) == []
    gappy = df.drop(index=[10, 11])
    assert any("missing" in i for i in check_quality(gappy, "1m", now=df["close_time"].iloc[-1]))
    stale_now = df["close_time"].iloc[-1] + __import__("pandas").Timedelta(minutes=10)
    assert any("stale" in i for i in check_quality(df, "1m", now=stale_now))
    bad = df.copy()
    bad.loc[5, "high"] = bad.loc[5, "low"] - 1
    assert "inconsistent OHLC values" in check_quality(bad, "1m", now=df["close_time"].iloc[-1])


# ---------------------------------------------------------------- websocket parsing
def kline_msg(sym="BTCUSDT", t=0, closed=True, o="100", h="110", l="90", c="105"):
    return json.dumps({"stream": f"{sym.lower()}@kline_1m", "data": {"e": "kline", "E": t + 1, "s": sym, "k": {
        "t": t, "T": t + 59_999, "s": sym, "i": "1m", "o": o, "c": c, "h": h, "l": l, "v": "1", "q": "100",
        "n": 3, "x": closed}}})


def test_parse_messages_and_validation():
    bt = parse_message(json.dumps({"stream": "btcusdt@bookTicker", "data": {"u": 5, "s": "BTCUSDT", "b": "100", "B": "1", "a": "101", "A": "2"}}))
    assert isinstance(bt, BookTickerEvent) and bt.ask == D("101")
    k = parse_message(kline_msg())
    assert isinstance(k, KlineEvent) and k.closed and k.close == D("105")
    with pytest.raises(InvalidPayload):
        parse_message(kline_msg(h="95"))  # high below close
    with pytest.raises(InvalidPayload):
        parse_message(json.dumps({"stream": "x@bookTicker", "data": {"u": 1, "s": "X", "b": "102", "B": "1", "a": "101", "A": "1"}}))
    assert parse_message(json.dumps({"result": None, "id": 1})) is None


def test_deduplicator():
    d = Deduplicator()
    k = parse_message(kline_msg(t=0))
    assert d.accept(k) and not d.accept(k)
    assert not d.accept(parse_message(kline_msg(t=0, closed=False)))  # stale in-progress update
    b1 = BookTickerEvent("X", 10, D(1), D(1), D(2), D(1))
    assert d.accept(b1) and not d.accept(b1) and not d.accept(BookTickerEvent("X", 9, D(1), D(1), D(2), D(1)))


class FakeWS:
    def __init__(self, messages, then="close"):
        self.messages = list(messages)
        self.then = then

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def recv(self):
        if self.messages:
            return self.messages.pop(0)
        if self.then == "hang":
            await asyncio.sleep(3600)
        raise ConnectionError("socket closed")


async def test_websocket_reconnects_with_backoff_and_heartbeat():
    events, statuses = [], []
    sessions = [FakeWS([kline_msg(t=0)], then="hang"), FakeWS([kline_msg(t=0), kline_msg(t=60_000)])]
    calls = []

    def connect(url):
        calls.append(url)
        return sessions.pop(0) if sessions else FakeWS([], then="hang")

    async def on_event(ev):
        events.append(ev)

    async def on_status(st, info):
        statuses.append(st)

    m = MarketStreamManager("wss://data-stream.binance.vision", on_event, on_status, heartbeat_timeout=0.2,
                            max_backoff=0.05, connect=connect)
    m.set_subscriptions(["BTCUSDT"], ["1m"])
    task = m.start()
    for _ in range(100):  # wait for the condition rather than a fixed sleep (robust under load)
        if len(calls) >= 3 and len(events) >= 2:
            break
        await asyncio.sleep(0.05)
    await m.stop()
    assert len(calls) >= 2, "must reconnect"
    assert "stale" in statuses and "reconnecting" in statuses  # heartbeat watchdog fired
    assert [e.open_time for e in events] == [0, 60_000]  # duplicate across reconnect dropped
    assert "btcusdt@kline_1m" in calls[0] and "btcusdt@bookTicker" in calls[0]
    assert task.done()


def trade_msg(trade_id, t_ms):
    return json.dumps({"stream": "btcusdt@trade", "data": {"e": "trade", "E": t_ms, "s": "BTCUSDT", "t": trade_id,
                                                          "p": "100", "q": "1", "T": t_ms}})


async def test_websocket_detects_lagging_data_with_hysteresis():
    now = 1_000_000.0  # local clock, seconds
    # trade times relative to now: fresh, 12s late (lagging), 7s late (still lagging), 2s late (recovered)
    lags = [0.5, 12, 7, 2]
    statuses, seen = [], []

    async def on_event(ev):
        seen.append((m.lagging, m.is_healthy()))

    async def on_status(st, info):
        statuses.append((st, info.get("lag")))

    msgs = [trade_msg(i + 1, int((now - lag) * 1000)) for i, lag in enumerate(lags)]
    m = MarketStreamManager("wss://x", on_event, on_status, heartbeat_timeout=5, max_backoff=0.05,
                            connect=lambda url: FakeWS(msgs, then="hang"), max_lag=10, clock=lambda: now)
    m.set_subscriptions(["BTCUSDT"], ["1m"])
    m.start()
    for _ in range(100):
        if len(seen) >= 4:
            break
        await asyncio.sleep(0.02)
    await m.stop()
    assert [st for st, _ in statuses][:3] == ["connected", "lagging", "connected"]  # then "disconnected" on stop
    assert statuses[1][1] == 12.0
    assert seen == [(False, True), (True, False), (True, False), (False, True)]


# ---------------------------------------------------------------- REST
@respx.mock
async def test_public_client_parsing_and_rate_limit():
    respx.get(f"{BASE}/api/v3/ticker/price").mock(return_value=httpx.Response(200, json={"symbol": "BTCUSDT", "price": "64000.10"}))
    respx.get(f"{BASE}/api/v3/depth").mock(return_value=httpx.Response(429, headers={"Retry-After": "7"}, json={"code": -1003, "msg": "Too many requests"}))
    c = PublicMarketClient(BASE)
    assert await c.ticker_price("BTCUSDT") == D("64000.10")
    with pytest.raises(BinanceRateLimitError) as ei:
        await c.order_book("BTCUSDT")
    assert ei.value.retry_after == 7
    with pytest.raises(BinanceRateLimitError):  # client backs off without calling the API
        await c.ticker_price("BTCUSDT")
    await c.close()


@respx.mock
async def test_public_client_retries_reads_on_5xx_and_reports_downtime():
    route = respx.get(f"{BASE}/api/v3/ping").mock(side_effect=[httpx.Response(503), httpx.Response(200, json={})])
    c = PublicMarketClient(BASE)
    assert await c.ping() is True and route.call_count == 2
    respx.get(f"{BASE}/api/v3/time").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(BinanceNetworkError):
        await c._request("GET", "/api/v3/time", retries=0)
    await c.close()


def test_clients_refuse_non_allowlisted_hosts():
    with pytest.raises(ValueError):
        TestnetClient("https://api.binance.com", "k", "s")  # production trading host
    with pytest.raises(ValueError):
        PublicMarketClient("https://evil.example.com")


@respx.mock
async def test_testnet_signed_request_and_errors():
    T = "https://testnet.binance.vision"
    route = respx.post(f"{T}/api/v3/order").mock(return_value=httpx.Response(400, json={"code": -2010, "msg": "Account has insufficient balance"}))
    c = TestnetClient(T, "key", "secret")
    with pytest.raises(BinanceAPIError) as ei:
        await c.new_order(symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity=D("0.001"), client_order_id="abc")
    assert ei.value.code == -2010
    req = route.calls.last.request
    assert req.headers["X-MBX-APIKEY"] == "key"
    assert "signature=" in str(req.url) and "newClientOrderId=abc" in str(req.url)
    assert "secret" not in repr(c)
    respx.post(f"{T}/api/v3/order").mock(side_effect=httpx.ReadTimeout("t"))
    with pytest.raises(BinanceNetworkError):  # never auto-retried
        await c.new_order(symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity=D("0.001"), client_order_id="abc")
    await c.close()

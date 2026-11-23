"""Binance market-data WebSocket stream manager.

Features: combined streams, automatic reconnection with exponential backoff and
jitter, heartbeat watchdog (forces reconnect when the socket goes silent),
lag detection (data arriving late, e.g. on a slow connection), payload
validation, duplicate/out-of-order protection, and proactive reconnect before
Binance's 24h connection limit.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BookTickerEvent:
    symbol: str
    update_id: int
    bid: Decimal
    bid_qty: Decimal
    ask: Decimal
    ask_qty: Decimal


@dataclass(frozen=True)
class TickerEvent:
    symbol: str
    event_time: int
    last_price: Decimal
    change_pct: Decimal
    quote_volume: Decimal
    volume: Decimal


@dataclass(frozen=True)
class TradeEvent:
    symbol: str
    trade_id: int
    price: Decimal
    qty: Decimal
    trade_time: int


@dataclass(frozen=True)
class KlineEvent:
    symbol: str
    interval: str
    open_time: int
    close_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trade_count: int
    closed: bool


MarketEvent = BookTickerEvent | TickerEvent | TradeEvent | KlineEvent


class InvalidPayload(ValueError):
    pass


def _dec(v: Any, positive: bool = True) -> Decimal:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, TypeError) as exc:
        raise InvalidPayload(f"bad decimal {v!r}") from exc
    if not d.is_finite() or (positive and d < 0):
        raise InvalidPayload(f"invalid numeric value {v!r}")
    return d


def parse_message(raw: str | bytes) -> MarketEvent | None:
    """Parse one combined-stream message. Returns None for non-market messages."""
    msg = json.loads(raw)
    if not isinstance(msg, dict):
        raise InvalidPayload("message is not an object")
    if "result" in msg and "id" in msg:  # subscription ack
        return None
    stream, data = msg.get("stream"), msg.get("data", msg)
    if not isinstance(data, dict):
        raise InvalidPayload("data is not an object")
    if stream and stream.endswith("@bookTicker") or ("u" in data and "b" in data and "a" in data and "e" not in data):
        ev = BookTickerEvent(data["s"], int(data["u"]), _dec(data["b"]), _dec(data["B"]), _dec(data["a"]), _dec(data["A"]))
        if ev.bid > 0 and ev.ask > 0 and ev.bid > ev.ask:
            raise InvalidPayload(f"crossed book for {ev.symbol}: bid {ev.bid} > ask {ev.ask}")
        return ev
    etype = data.get("e")
    if etype == "24hrTicker":
        return TickerEvent(data["s"], int(data["E"]), _dec(data["c"]), _dec(data["P"], positive=False),
                           _dec(data["q"]), _dec(data["v"]))
    if etype == "trade":
        ev = TradeEvent(data["s"], int(data["t"]), _dec(data["p"]), _dec(data["q"]), int(data["T"]))
        if ev.price <= 0:
            raise InvalidPayload("non-positive trade price")
        return ev
    if etype == "kline":
        k = data["k"]
        ev = KlineEvent(k["s"], k["i"], int(k["t"]), int(k["T"]), _dec(k["o"]), _dec(k["h"]), _dec(k["l"]),
                        _dec(k["c"]), _dec(k["v"]), _dec(k["q"]), int(k["n"]), bool(k["x"]))
        if ev.close_time <= ev.open_time:
            raise InvalidPayload("kline close_time <= open_time")
        if not (ev.low <= min(ev.open, ev.close) and ev.high >= max(ev.open, ev.close) and ev.low > 0):
            raise InvalidPayload(f"inconsistent OHLC for {ev.symbol} {ev.interval}")
        return ev
    return None


class Deduplicator:
    """Drops duplicate or out-of-order events per stream key."""

    def __init__(self) -> None:
        self._last: dict[tuple, int] = {}
        self._closed_klines: dict[tuple, int] = {}

    def accept(self, ev: MarketEvent) -> bool:
        if isinstance(ev, KlineEvent):
            key = (ev.symbol, ev.interval)
            if ev.closed:
                if self._closed_klines.get(key, -1) >= ev.open_time:
                    return False
                self._closed_klines[key] = ev.open_time
                return True
            return ev.open_time > self._closed_klines.get(key, -1)
        if isinstance(ev, BookTickerEvent):
            key, seq = ("book", ev.symbol), ev.update_id
        elif isinstance(ev, TradeEvent):
            key, seq = ("trade", ev.symbol), ev.trade_id
        else:
            key, seq = ("ticker", ev.symbol), ev.event_time
        if self._last.get(key, -1) >= seq:
            return False
        self._last[key] = seq
        return True


Handler = Callable[[MarketEvent], Awaitable[None]]
StatusHandler = Callable[[str, dict], Awaitable[None]]


class MarketStreamManager:
    def __init__(self, base_url: str, on_event: Handler, on_status: StatusHandler | None = None, *,
                 heartbeat_timeout: float = 30.0, max_backoff: float = 60.0,
                 max_connection_age: float = 23 * 3600, connect: Callable | None = None,
                 include_trades: bool = True, max_lag: float = 10.0,
                 clock: Callable[[], float] = time.time) -> None:
        self.base_url = base_url.rstrip("/")
        self.on_event = on_event
        self.on_status = on_status
        self.heartbeat_timeout = heartbeat_timeout
        self.max_backoff = max_backoff
        self.max_connection_age = max_connection_age
        self.include_trades = include_trades
        self._connect = connect
        self._streams: list[str] = []
        self._restart = asyncio.Event()
        self._stop = False
        self.connected = False
        self.last_message_at: float | None = None
        self.reconnects = 0
        self.invalid_messages = 0
        self.dedup = Deduplicator()
        self._task: asyncio.Task | None = None
        # Lag = local clock minus the exchange's event time. A live socket can still
        # deliver data late when the connection is slower than the feed: messages queue
        # up and fall further behind (the heartbeat never fires because data keeps
        # arriving). Above ``max_lag`` the stream reports "lagging" and is unhealthy;
        # it recovers below half of that (hysteresis). Lag includes local clock skew.
        self.max_lag = max_lag
        self._clock = clock
        self.lag_s: float | None = None
        self.lagging = False

    def _observe_lag(self, ev: MarketEvent) -> bool:
        """Update lag from an event's exchange timestamp. True if the lagging state changed."""
        if isinstance(ev, TickerEvent):
            event_ms = ev.event_time
        elif isinstance(ev, TradeEvent):
            event_ms = ev.trade_time
        else:
            return False
        self.lag_s = max(0.0, self._clock() - event_ms / 1000)
        if not self.lagging and self.lag_s > self.max_lag:
            self.lagging = True
            return True
        if self.lagging and self.lag_s < self.max_lag / 2:
            self.lagging = False
            return True
        return False

    def build_streams(self, symbols: list[str], timeframes: list[str]) -> list[str]:
        streams: list[str] = []
        for s in symbols:
            low = s.lower()
            streams += [f"{low}@bookTicker", f"{low}@ticker"]
            if self.include_trades:
                streams.append(f"{low}@trade")
            streams += [f"{low}@kline_{tf}" for tf in timeframes]
        return streams

    def set_subscriptions(self, symbols: list[str], timeframes: list[str]) -> None:
        streams = self.build_streams(symbols, timeframes)
        if streams != self._streams:
            self._streams = streams
            self._restart.set()

    @property
    def url(self) -> str:
        return f"{self.base_url}/stream?streams={'/'.join(self._streams)}"

    def is_healthy(self, max_silence: float | None = None) -> bool:
        if not self.connected or self.last_message_at is None or self.lagging:
            return False
        return time.monotonic() - self.last_message_at <= (max_silence or self.heartbeat_timeout)

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self.run(), name="market-ws")
        return self._task

    async def stop(self) -> None:
        self._stop = True
        self._restart.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _status(self, status: str, **info: Any) -> None:
        if self.on_status:
            try:
                await self.on_status(status, info)
            except Exception:  # pragma: no cover
                log.exception("status handler failed")

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop:
            if not self._streams:
                self._restart.clear()
                await self._restart.wait()
                continue
            self._restart.clear()
            started = time.monotonic()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("market websocket error: %s", exc)
                await self._status("error", error=str(exc)[:200])
            finally:
                if self.connected:
                    self.connected = False
                    await self._status("disconnected")
            if self._stop:
                break
            if self._restart.is_set():
                backoff = 1.0
                continue
            # Reset backoff after a connection that stayed up for a while.
            if time.monotonic() - started > 60:
                backoff = 1.0
            delay = backoff + random.uniform(0, backoff / 2)
            self.reconnects += 1
            await self._status("reconnecting", delay=round(delay, 1), attempt=self.reconnects)
            try:
                await asyncio.wait_for(self._restart.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, self.max_backoff)

    async def _session(self) -> None:
        connect = self._connect
        if connect is None:
            import websockets

            connect = lambda url: websockets.connect(url, ping_interval=20, ping_timeout=20,  # noqa: E731
                                                     max_size=2 ** 22, open_timeout=15)
        async with connect(self.url) as ws:
            self.connected = True
            self.lagging, self.lag_s = False, None
            self.last_message_at = time.monotonic()
            opened = time.monotonic()
            await self._status("connected", streams=len(self._streams))
            while not self._stop and not self._restart.is_set():
                if time.monotonic() - opened > self.max_connection_age:
                    log.info("proactively recycling websocket before 24h limit")
                    return
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=self.heartbeat_timeout)
                except asyncio.TimeoutError:
                    # Heartbeat failure: never assume a silent socket is live.
                    await self._status("stale", silence=self.heartbeat_timeout)
                    raise ConnectionError(f"no market data for {self.heartbeat_timeout}s")
                self.last_message_at = time.monotonic()
                try:
                    ev = parse_message(raw)
                except (InvalidPayload, KeyError, ValueError, TypeError) as exc:
                    self.invalid_messages += 1
                    log.debug("dropping invalid message: %s", exc)
                    continue
                if ev is None or not self.dedup.accept(ev):
                    continue
                if self._observe_lag(ev):
                    lag = round(self.lag_s or 0.0, 1)
                    if self.lagging:
                        log.warning("market data is %.1fs behind real time (max %.0fs)", lag, self.max_lag)
                    await self._status("lagging" if self.lagging else "connected", lag=lag)
                try:
                    await self.on_event(ev)
                except Exception:
                    log.exception("market event handler failed")


_STATUS_SEVERITY = {"connected": 0, "init": 1, "reconnecting": 2, "lagging": 3, "disconnected": 4, "stale": 5, "error": 6}


class ShardedStreamManager:
    """Spreads many symbols over several combined-stream connections.

    Binance allows at most 1024 streams per connection (and very long URLs are
    fragile), so each shard carries at most ``max_streams`` streams. Symbols are
    sharded alphabetically so a re-ranking of the universe rarely moves a symbol
    to another connection. Exposes the same interface as ``MarketStreamManager``;
    the reported status is the worst status of any shard.
    """

    # The per-trade stream only refines the "last price" (the 1s ticker also has it)
    # and is by far the busiest stream; only subscribe to it for small universes.
    TRADES_MAX_SYMBOLS = 20

    def __init__(self, base_url: str, on_event: Handler, on_status: StatusHandler | None = None, *,
                 max_streams: int = 200, factory: Callable[..., MarketStreamManager] | None = None,
                 **child_kwargs: Any) -> None:
        self.base_url = base_url
        self.on_event = on_event
        self.on_status = on_status
        self.max_streams = max_streams
        self._factory = factory or MarketStreamManager
        self._child_kwargs = child_kwargs
        self.shards: list[MarketStreamManager] = []
        self._shard_status: list[str] = []
        self._reported = "init"
        self._started = False
        self._task: asyncio.Task | None = None

    # ---- aggregate interface
    @property
    def connected(self) -> bool:
        return bool(self.shards) and all(s.connected for s in self.shards)

    @property
    def reconnects(self) -> int:
        return sum(s.reconnects for s in self.shards)

    @property
    def invalid_messages(self) -> int:
        return sum(s.invalid_messages for s in self.shards)

    @property
    def lag_s(self) -> float | None:
        lags = [s.lag_s for s in self.shards if s.lag_s is not None]
        return max(lags) if lags else None

    def is_healthy(self, max_silence: float | None = None) -> bool:
        return bool(self.shards) and all(s.is_healthy(max_silence) for s in self.shards)

    def _dispatch(self, ev: MarketEvent) -> Awaitable[None]:
        return self.on_event(ev)  # resolved at call time so on_event can be rebound

    def _make_status_handler(self, idx: int) -> StatusHandler:
        async def handler(status: str, info: dict) -> None:
            if idx >= len(self._shard_status):
                return
            self._shard_status[idx] = status
            agg = max(self._shard_status, key=lambda st: _STATUS_SEVERITY.get(st, 6))
            if agg != self._reported:
                self._reported = agg
                if self.on_status:
                    await self.on_status(agg, {**info, "shard": idx, "shards": len(self.shards)})
        return handler

    def set_subscriptions(self, symbols: list[str], timeframes: list[str]) -> None:
        include_trades = len(symbols) <= self.TRADES_MAX_SYMBOLS
        per_symbol = 2 + len(timeframes) + (1 if include_trades else 0)
        chunk = max(1, self.max_streams // per_symbol)
        ordered = sorted(dict.fromkeys(symbols))
        groups = [ordered[i:i + chunk] for i in range(0, len(ordered), chunk)]
        for idx, group in enumerate(groups):
            if idx < len(self.shards):
                shard = self.shards[idx]
            else:
                shard = self._factory(self.base_url, self._dispatch, self._make_status_handler(idx),
                                      **self._child_kwargs)
                self.shards.append(shard)
                self._shard_status.append("init")
                if self._started:
                    shard.start()
            if shard.include_trades != include_trades:
                shard.include_trades = include_trades
                shard._streams = []  # force set_subscriptions to restart with the new stream set
            shard.set_subscriptions(group, timeframes)
        for shard in self.shards[len(groups):]:  # universe shrank: close surplus connections
            asyncio.get_running_loop().create_task(shard.stop())
        del self.shards[len(groups):]
        del self._shard_status[len(groups):]

    async def _run(self) -> None:
        self._started = True
        for shard in self.shards:
            shard.start()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.gather(*(s.stop() for s in self.shards), return_exceptions=True)

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self._run(), name="market-ws-shards")
        return self._task

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await asyncio.gather(*(s.stop() for s in self.shards), return_exceptions=True)

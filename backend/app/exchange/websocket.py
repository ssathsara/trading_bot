"""Binance market-data WebSocket stream manager.

Features: combined streams, automatic reconnection with exponential backoff and
jitter, heartbeat watchdog (forces reconnect when the socket goes silent),
payload validation, duplicate/out-of-order protection, and proactive reconnect
before Binance's 24h connection limit.
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
                 include_trades: bool = True) -> None:
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
        if not self.connected or self.last_message_at is None:
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
                try:
                    await self.on_event(ev)
                except Exception:
                    log.exception("market event handler failed")

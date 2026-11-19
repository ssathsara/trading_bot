"""Event broadcasting between the trading worker and WebSocket clients.

* Without Redis: an in-process pub/sub (worker runs inside the API process).
* With REDIS_URL: events are published to Redis and every API process relays
  them to its local WebSocket subscribers (worker runs as its own container).

``publish`` is safe to call from any thread.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any

log = logging.getLogger(__name__)
CHANNELS = ("market", "dashboard", "trading")
REDIS_PREFIX = "tradingbot:"
# Outgoing Redis events are queued and sent by ONE task over one connection. A task
# per publish used one pooled connection each, so bursts (stream reconnects, universe
# refreshes) exhausted the pool with "Too many connections". Events are UI updates;
# when the queue is full the oldest are dropped instead of stalling trading.
REDIS_OUTBOX_MAX = 5000
REDIS_BATCH_MAX = 500


def _default(o: Any) -> Any:
    if isinstance(o, Decimal):
        from app.db import clean_decimal

        return format(clean_decimal(o), "f")
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"not serialisable: {type(o)}")


def encode(event: dict) -> str:
    return json.dumps(event, default=_default)


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[str]]] = defaultdict(set)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._redis = None
        self._redis_task: asyncio.Task | None = None
        self._outbox: asyncio.Queue[tuple[str, str]] | None = None
        self._sender_task: asyncio.Task | None = None
        self.dropped_events = 0

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def enable_redis(self, url: str, relay: bool) -> None:
        """Connect to Redis. ``relay`` = subscribe and fan out to local clients."""
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(url, decode_responses=True)
        await self._redis.ping()
        self._outbox = asyncio.Queue(maxsize=REDIS_OUTBOX_MAX)
        self._sender_task = asyncio.create_task(self._sender(), name="redis-publisher")
        if relay:
            self._redis_task = asyncio.create_task(self._relay())

    def _enqueue(self, channel: str, payload: str) -> None:
        """Runs on the event loop thread."""
        q = self._outbox
        if q is None:
            return
        if q.full():
            try:
                q.get_nowait()
                self.dropped_events += 1
            except asyncio.QueueEmpty:
                pass
        q.put_nowait((channel, payload))

    async def _sender(self) -> None:
        assert self._redis is not None and self._outbox is not None
        q = self._outbox
        backoff = 1.0
        while True:
            batch = [await q.get()]
            while len(batch) < REDIS_BATCH_MAX and not q.empty():
                batch.append(q.get_nowait())
            try:
                async with self._redis.pipeline(transaction=False) as pipe:
                    for channel, payload in batch:
                        pipe.publish(REDIS_PREFIX + channel, payload)
                    await pipe.execute()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - network dependent
                # This batch is lost (UI events only); never let the sender die.
                self.dropped_events += len(batch)
                log.warning("redis publish failed (%s); %d event(s) dropped, retrying in %.0fs", exc, len(batch), backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _relay(self) -> None:
        assert self._redis is not None
        backoff = 1.0
        while True:
            try:
                pubsub = self._redis.pubsub()
                await pubsub.subscribe(*[REDIS_PREFIX + c for c in CHANNELS])
                backoff = 1.0
                async for msg in pubsub.listen():
                    if msg.get("type") == "message":
                        self._fanout(msg["channel"].removeprefix(REDIS_PREFIX), msg["data"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - network dependent
                log.warning("redis relay error: %s; retrying in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def subscribe(self, channel: str) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        with self._lock:
            self._subs[channel].add(q)
        return q

    def unsubscribe(self, channel: str, q: asyncio.Queue[str]) -> None:
        with self._lock:
            self._subs[channel].discard(q)

    def _fanout(self, channel: str, data: str) -> None:
        with self._lock:
            queues = list(self._subs.get(channel, ()))
        for q in queues:
            if q.full():  # slow consumer: drop oldest rather than block trading
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(data)

    def publish(self, channel: str, event_type: str, data: Any) -> None:
        payload = encode({"type": event_type, "channel": channel, "data": data})
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        target = self._enqueue if self._redis is not None else self._fanout
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            target(channel, payload)
        else:
            loop.call_soon_threadsafe(target, channel, payload)

    async def close(self) -> None:
        for task in (self._redis_task, self._sender_task):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._redis_task = self._sender_task = None
        if self._redis is not None:
            await self._redis.aclose()


bus = EventBus()

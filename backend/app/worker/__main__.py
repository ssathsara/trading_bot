"""Standalone trading worker: ``python -m app.worker``.

Used by docker-compose (service ``trading-worker``) together with Redis so the
API container can relay events. Graceful shutdown on SIGINT/SIGTERM.
"""
from __future__ import annotations

import asyncio
import logging
import signal

from app.config import get_settings
from app.db import init_engine
from app.events import bus
from app.main import configure_logging, ensure_admin, verify_schema
from app.worker.engine import TradingEngine

log = logging.getLogger("worker")


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    init_engine(settings.database_url)
    verify_schema()
    ensure_admin()
    loop = asyncio.get_running_loop()
    bus.bind_loop(loop)
    if settings.redis_url:
        await bus.enable_redis(settings.redis_url, relay=False)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))

    delay = 5.0
    engine: TradingEngine | None = None
    while not stop.is_set():
        engine = TradingEngine(settings)
        try:
            await engine.start()
            break
        except Exception as exc:
            log.error("engine start failed: %s; retrying in %.0fs", exc, delay)
            await engine.stop()
            engine = None
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 120)
    exit_code = 0
    if engine is not None:
        # Wake on shutdown OR on losing the lease to another instance.
        waiters = [asyncio.create_task(stop.wait()), asyncio.create_task(engine.lease_lost.wait())]
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        for w in waiters:
            w.cancel()
        if engine.lease_lost.is_set() and not stop.is_set():
            log.error("lease lost to another worker; exiting so the supervisor can restart and re-contend")
            exit_code = 3
    log.info("shutdown requested")
    if engine is not None:
        await engine.stop()
    await bus.close()
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    asyncio.run(main())

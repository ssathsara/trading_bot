"""FastAPI application factory."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import inspect, select

from app.config import get_settings
from app.db import get_engine, init_engine, session_scope
from app.events import bus
from app.models import User
from app.security import hash_password

log = logging.getLogger("app")
REQUIRED_TABLES = {"users", "system_settings", "trading_orders", "positions", "trades", "account_state", "order_fills"}


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs full URLs (signed query strings)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def verify_schema() -> None:
    tables = set(inspect(get_engine()).get_table_names())
    missing = REQUIRED_TABLES - tables
    if missing:
        raise RuntimeError(f"database schema missing tables {sorted(missing)}; run `alembic upgrade head` first")


def ensure_admin() -> None:
    s = get_settings()
    with session_scope() as db:
        if db.scalar(select(User).where(User.username == s.admin_username)) is None:
            db.add(User(username=s.admin_username, email=s.admin_email,
                        hashed_password=hash_password(s.admin_password.get_secret_value()), is_admin=True))
            log.info("created admin user %r", s.admin_username)


async def _safe_stop(engine) -> None:
    try:
        await engine.stop()
    except Exception:
        log.exception("error while stopping engine")


async def start_engine_with_retry(app: FastAPI) -> None:
    from app.worker.engine import TradingEngine

    delay = 5.0
    while True:
        engine = TradingEngine(get_settings())
        try:
            await engine.start()
            app.state.engine = engine
            delay = 5.0
            # Supervise: if another instance takes the lease, retire this engine and re-contend.
            await engine.lease_lost.wait()
            log.error("embedded trading engine lost its lease; restarting it in standby")
            app.state.engine = None
            await _safe_stop(engine)
            await asyncio.sleep(delay)
            continue
        except asyncio.CancelledError:
            # Shutdown (during startup or supervision): release the lease so the next start is not blocked.
            app.state.engine = None
            await asyncio.shield(_safe_stop(engine))
            raise
        except Exception as exc:
            log.error("trading engine failed to start (%s); retrying in %.0fs", exc, delay)
            await _safe_stop(engine)
            # "another worker" may be a live dedicated worker OR a stale lease from a crash;
            # keep retrying - a stale lease expires after LEASE_TTL seconds.
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()  # raises (fail closed) on invalid configuration
    init_engine(settings.database_url)
    verify_schema()
    ensure_admin()
    bus.bind_loop(asyncio.get_running_loop())
    if settings.redis_url:
        await bus.enable_redis(settings.redis_url, relay=True)
    app.state.engine = None
    task = asyncio.create_task(start_engine_with_retry(app)) if settings.run_worker_in_api else None
    log.info("API ready (execution mode default=%s, live trading DISABLED)", settings.execution_mode.value)
    yield
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if app.state.engine is not None:
        await app.state.engine.stop()
    await bus.close()


def _exact_decimals() -> None:
    """Serialise Decimal as an exact string in plain-dict responses (FastAPI's default is float).

    Pydantic response models and WebSocket events already emit strings; this keeps
    every API payload consistent and free of float rounding.
    """
    from decimal import Decimal

    from fastapi.encoders import ENCODERS_BY_TYPE

    from app.db import clean_decimal

    ENCODERS_BY_TYPE[Decimal] = lambda d: format(clean_decimal(d), "f")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    _exact_decimals()
    app = FastAPI(
        title="AI Spot Trading Agent (Simulation)",
        description="Paper-trading and Binance Spot TESTNET trading agent. Live trading is disabled in this release.",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=False,
                       allow_methods=["GET", "POST", "PUT"], allow_headers=["Authorization", "Content-Type"])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail if isinstance(exc.detail, str) else "error", "detail": exc.detail},
                            status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        errors = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
        return JSONResponse({"error": "validation error", "detail": errors}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception):
        log.exception("unhandled error")
        return JSONResponse({"error": "internal server error", "detail": None}, status_code=500)

    from app.api.routes import api, public
    from app.api.ws import router as ws_router

    app.include_router(public)
    app.include_router(api)
    app.include_router(ws_router)
    return app


app = create_app()  # uvicorn app.main:app

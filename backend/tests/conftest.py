"""Shared fixtures. Tests NEVER use real credentials or production order endpoints."""
from __future__ import annotations

import os
import tempfile
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_TMP = Path(tempfile.mkdtemp(prefix="tradingbot-tests-"))
os.environ.update({
    "APP_ENV": "test",
    "JWT_SECRET": "test-secret-0123456789-abcdefghijklmnopqrstuvwxyz",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "test-password-123",
    "DATABASE_URL": f"sqlite:///{(_TMP / 'test.db').as_posix()}",
    "REDIS_URL": "",
    "RUN_WORKER_IN_API": "false",
    "EXECUTION_MODE": "PAPER",
    "BINANCE_TESTNET_API_KEY": "",
    "BINANCE_TESTNET_API_SECRET": "",
})

from app import db as dbmod  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.exchange.filters import SymbolFilters  # noqa: E402

get_settings.cache_clear()


@pytest.fixture(autouse=True)
def fresh_db():
    from app.models import Base

    engine = dbmod.init_engine(os.environ["DATABASE_URL"])
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    engine.dispose()


def exchange_symbol(symbol="BTCUSDT", base="BTC", quote="USDT", status="TRADING", step="0.00001",
                    min_qty="0.00001", tick="0.01", min_notional="5", spot=True) -> dict:
    return {
        "symbol": symbol, "status": status, "baseAsset": base, "quoteAsset": quote,
        "baseAssetPrecision": 8, "quoteAssetPrecision": 8, "isSpotTradingAllowed": spot,
        "orderTypes": ["LIMIT", "LIMIT_MAKER", "MARKET"], "permissions": [], "permissionSets": [["SPOT"]],
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": tick, "maxPrice": "1000000", "tickSize": tick},
            {"filterType": "LOT_SIZE", "minQty": min_qty, "maxQty": "9000", "stepSize": step},
            {"filterType": "MARKET_LOT_SIZE", "minQty": "0", "maxQty": "100", "stepSize": "0"},
            {"filterType": "NOTIONAL", "minNotional": min_notional, "applyMinToMarket": True,
             "maxNotional": "9000000", "applyMaxToMarket": False, "avgPriceMins": 5},
        ],
    }


@pytest.fixture
def btc_filters() -> SymbolFilters:
    return SymbolFilters.from_exchange_info(exchange_symbol())


def make_ohlcv(n: int = 400, start: float = 100.0, drift: float = 0.001, vol: float = 0.004, seed: int = 1,
               freq: str = "15min") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = drift + vol * rng.standard_normal(n)
    close = start * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[start], close[:-1]])
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, vol / 2, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, vol / 2, n)))
    volume = rng.uniform(50, 150, n)
    t = pd.date_range("2025-01-01", periods=n, freq=freq, tz="UTC")
    dur = pd.Timedelta(freq)
    return pd.DataFrame({"open_time": t, "close_time": t + dur - pd.Timedelta(milliseconds=1), "open": open_,
                         "high": high, "low": low, "close": close, "volume": volume,
                         "quote_volume": volume * close, "trades": 100})


class FakeAdapter:
    """Scriptable execution adapter for order-manager tests."""

    mode = "PAPER"

    def __init__(self):
        self.submitted = []
        self.reports = {}
        self.query_reports = {}

    async def submit(self, req):
        self.submitted.append(req)
        fn = self.reports.get("submit")
        return fn(req) if callable(fn) else fn

    async def query(self, req):
        fn = self.query_reports.get(req.client_order_id) or self.query_reports.get("*")
        return fn(req) if callable(fn) else fn

    async def cancel(self, req):
        from app.execution.adapters import ExecutionReport
        return ExecutionReport("CANCELED")


D = Decimal

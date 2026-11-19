"""Binance Spot REST clients.

``PublicMarketClient`` — unauthenticated market data (read-only host).
``TestnetClient``      — signed requests; refuses to talk to anything but the
                          Spot Testnet host (defence in depth on top of config
                          validation).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from app.config import MARKET_DATA_REST_HOSTS, TESTNET_REST_HOSTS

log = logging.getLogger(__name__)


class BinanceError(Exception):
    """Base error."""


class BinanceAPIError(BinanceError):
    """Exchange returned an error response (request definitely processed / rejected)."""

    def __init__(self, status: int, code: int | None, msg: str):
        super().__init__(f"HTTP {status} code={code}: {msg}")
        self.status, self.code, self.msg = status, code, msg


class BinanceRateLimitError(BinanceAPIError):
    def __init__(self, status: int, code: int | None, msg: str, retry_after: float):
        super().__init__(status, code, msg)
        self.retry_after = retry_after


class BinanceNetworkError(BinanceError):
    """Network failure/timeout: the request outcome is UNKNOWN (an order may exist!)."""


class _BaseClient:
    def __init__(self, base_url: str, allowed_hosts: set[str], timeout: float = 10.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        host = (urlparse(base_url).hostname or "").lower()
        if host not in allowed_hosts:
            raise ValueError(f"refusing to use endpoint {base_url!r}: host not in allowlist")
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=transport)
        self.used_weight_1m = 0
        self._banned_until = 0.0
        self.last_ok_at: float | None = None
        self.last_error: str | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, params: dict | None = None,
                       headers: dict | None = None, retries: int = 2) -> Any:
        if time.monotonic() < self._banned_until:
            raise BinanceRateLimitError(429, None, "client is backing off after rate limit",
                                        self._banned_until - time.monotonic())
        attempt = 0
        while True:
            try:
                resp = await self._client.request(method, path, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                self.last_error = f"network: {type(exc).__name__}"
                # Never transparently retry non-idempotent calls: caller must reconcile.
                if method != "GET" or attempt >= retries:
                    raise BinanceNetworkError(f"{method} {path}: {type(exc).__name__}") from exc
                attempt += 1
                await asyncio.sleep(0.5 * 2 ** attempt)
                continue
            self.used_weight_1m = int(resp.headers.get("x-mbx-used-weight-1m", self.used_weight_1m) or 0)
            if resp.status_code in (418, 429):
                retry_after = float(resp.headers.get("retry-after", "60"))
                self._banned_until = time.monotonic() + retry_after
                body = _safe_json(resp)
                self.last_error = f"rate limited ({resp.status_code})"
                raise BinanceRateLimitError(resp.status_code, body.get("code"), body.get("msg", "rate limited"), retry_after)
            if resp.status_code >= 500:
                self.last_error = f"server {resp.status_code}"
                # 5xx: outcome unknown for writes; retry only reads.
                if method != "GET" or attempt >= retries:
                    raise BinanceNetworkError(f"{method} {path}: HTTP {resp.status_code} (status unknown)")
                attempt += 1
                await asyncio.sleep(0.5 * 2 ** attempt)
                continue
            body = _safe_json(resp)
            if resp.status_code >= 400:
                self.last_error = f"api {body.get('code')}: {body.get('msg')}"
                raise BinanceAPIError(resp.status_code, body.get("code"), body.get("msg", resp.text[:200]))
            self.last_ok_at = time.time()
            self.last_error = None
            return body


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {"msg": resp.text[:200]}


class PublicMarketClient(_BaseClient):
    """Unauthenticated market data. Never carries credentials."""

    def __init__(self, base_url: str, **kw: Any) -> None:
        super().__init__(base_url, MARKET_DATA_REST_HOSTS, **kw)

    async def ping(self) -> bool:
        await self._request("GET", "/api/v3/ping")
        return True

    async def exchange_info(self, symbols: list[str] | None = None) -> dict:
        params = {"symbols": "[" + ",".join(f'"{s}"' for s in symbols) + "]"} if symbols else None
        return await self._request("GET", "/api/v3/exchangeInfo", params)

    async def ticker_24h(self, symbol: str | None = None) -> Any:
        return await self._request("GET", "/api/v3/ticker/24hr", {"symbol": symbol} if symbol else None)

    async def ticker_price(self, symbol: str) -> Decimal:
        data = await self._request("GET", "/api/v3/ticker/price", {"symbol": symbol})
        return Decimal(data["price"])

    async def book_ticker(self, symbol: str) -> dict:
        return await self._request("GET", "/api/v3/ticker/bookTicker", {"symbol": symbol})

    async def book_tickers(self) -> list[dict]:
        """Best bid/ask for every symbol in one request (weight 4)."""
        return await self._request("GET", "/api/v3/ticker/bookTicker")

    async def order_book(self, symbol: str, limit: int = 20) -> dict:
        return await self._request("GET", "/api/v3/depth", {"symbol": symbol, "limit": limit})

    async def klines(self, symbol: str, interval: str, limit: int = 500,
                     start_ms: int | None = None, end_ms: int | None = None) -> list[list]:
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        return await self._request("GET", "/api/v3/klines", params)


TIMESTAMP_OUTSIDE_RECV_WINDOW = -1021


class TestnetClient(_BaseClient):
    """Signed client for the Binance Spot TESTNET only."""

    __test__ = False  # not a pytest test class

    def __init__(self, base_url: str, api_key: str, api_secret: str, recv_window: int = 5000, **kw: Any) -> None:
        super().__init__(base_url, TESTNET_REST_HOSTS, **kw)
        if not api_key or not api_secret:
            raise ValueError("testnet credentials are required")
        self._key = api_key
        self._secret = api_secret.encode()
        self._recv_window = recv_window
        self._time_offset_ms = 0

    def __repr__(self) -> str:  # never leak credentials via repr
        return f"TestnetClient(base_url={self.base_url!r})"

    async def sync_time(self) -> None:
        data = await self._request("GET", "/api/v3/time")
        self._time_offset_ms = int(data["serverTime"]) - int(time.time() * 1000)

    def _sign(self, params: dict) -> dict:
        p = {k: v for k, v in params.items() if v is not None}
        p["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
        p["recvWindow"] = self._recv_window
        query = urlencode(p)
        p["signature"] = hmac.new(self._secret, query.encode(), hashlib.sha256).hexdigest()
        return p

    async def _signed(self, method: str, path: str, params: dict) -> Any:
        # Defence in depth: re-check the host at every signed call.
        if (urlparse(self.base_url).hostname or "").lower() not in TESTNET_REST_HOSTS:
            raise BinanceError("signed request blocked: non-testnet host")
        retries = 0 if method != "GET" else 2
        try:
            return await self._request(method, path, self._sign(params), headers={"X-MBX-APIKEY": self._key}, retries=retries)
        except BinanceAPIError as exc:
            # -1021: our clock drifted (e.g. the Docker VM after the host slept). The exchange
            # rejected the request BEFORE processing it, so re-syncing and re-signing once is
            # safe even for order placement.
            if exc.code != TIMESTAMP_OUTSIDE_RECV_WINDOW:
                raise
        await self.sync_time()
        return await self._request(method, path, self._sign(params), headers={"X-MBX-APIKEY": self._key}, retries=retries)

    async def exchange_info(self) -> dict:
        return await self._request("GET", "/api/v3/exchangeInfo")

    async def account(self) -> dict:
        return await self._signed("GET", "/api/v3/account", {"omitZeroBalances": "true"})

    async def new_order(self, *, symbol: str, side: str, order_type: str, quantity: Decimal,
                        client_order_id: str, price: Decimal | None = None, time_in_force: str | None = None,
                        stop_price: Decimal | None = None, test: bool = False) -> dict:
        params: dict[str, Any] = {
            "symbol": symbol, "side": side, "type": order_type,
            "quantity": format(quantity, "f"), "newClientOrderId": client_order_id,
            "newOrderRespType": "FULL",
        }
        if order_type in ("LIMIT", "STOP_LOSS_LIMIT"):
            params["price"] = format(price, "f")  # type: ignore[arg-type]
            params["timeInForce"] = time_in_force or "GTC"
        if order_type in ("STOP_LOSS", "STOP_LOSS_LIMIT"):
            if stop_price is None:
                raise ValueError(f"{order_type} requires a stop price")
            params["stopPrice"] = format(stop_price, "f")
        path = "/api/v3/order/test" if test else "/api/v3/order"
        return await self._signed("POST", path, params)

    async def get_order(self, symbol: str, client_order_id: str) -> dict:
        return await self._signed("GET", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_order_id})

    async def cancel_order(self, symbol: str, client_order_id: str) -> dict:
        return await self._signed("DELETE", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_order_id})

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        return await self._signed("GET", "/api/v3/openOrders", {"symbol": symbol} if symbol else {})

    async def my_trades(self, symbol: str, order_id: int) -> list[dict]:
        return await self._signed("GET", "/api/v3/myTrades", {"symbol": symbol, "orderId": order_id})

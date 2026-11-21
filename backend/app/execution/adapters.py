"""Execution adapters: local paper engine, Binance Spot Testnet and Binance Spot LIVE.

Both expose the same async interface and report *cumulative* fills with stable
fill ids, so the order manager can apply them idempotently.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal

from app.exchange.rest import BinanceAPIError, BinanceNetworkError, BinanceRateLimitError, LiveClient, SignedClient, TestnetClient

log = logging.getLogger(__name__)
BPS = Decimal("10000")
TERMINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str
    symbol: str
    side: str  # BUY | SELL
    order_type: str  # MARKET | LIMIT | STOP_LOSS | STOP_LOSS_LIMIT (stops: testnet only)
    quantity: Decimal
    price: Decimal | None = None
    base_asset: str = ""
    quote_asset: str = "USDT"
    stop_price: Decimal | None = None


@dataclass(frozen=True)
class Fill:
    fill_id: str
    price: Decimal
    qty: Decimal
    fee: Decimal
    fee_asset: str


@dataclass
class ExecutionReport:
    status: str  # NEW | PARTIALLY_FILLED | FILLED | CANCELED | REJECTED | EXPIRED | UNKNOWN
    exchange_order_id: str | None = None
    fills: list[Fill] = field(default_factory=list)
    error: str | None = None
    simulated: bool = False

    @property
    def filled_qty(self) -> Decimal:
        return sum((f.qty for f in self.fills), Decimal("0"))


DepthProvider = Callable[[str], Awaitable[dict]]


class PaperExecutionAdapter:
    """Local simulator. Uses LIVE public order-book data; never touches an exchange account.

    * MARKET orders walk the real order-book depth (partial fill + EXPIRED if the
      visible book is exhausted), plus configurable extra slippage.
    * LIMIT orders fill immediately if marketable, otherwise rest and are matched
      against subsequent best bid/ask updates, limited by the displayed quantity
      (which naturally produces partial fills).
    * Fees are charged in the quote asset at ``fee_rate``.
    """

    mode = "PAPER"

    def __init__(self, depth_provider: DepthProvider, fee_rate: Decimal, slippage_bps: Decimal) -> None:
        self.depth_provider = depth_provider
        self.fee_rate = fee_rate
        self.slippage_bps = slippage_bps
        self._orders: dict[str, dict] = {}
        self._lock = threading.Lock()
        # (symbol, "bid"|"ask") -> (level price, qty consumed there by simulated fills)
        self._consumed: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}

    def configure(self, fee_rate: Decimal, slippage_bps: Decimal) -> None:
        self.fee_rate, self.slippage_bps = fee_rate, slippage_bps

    def _fill(self, o: dict, price: Decimal, qty: Decimal) -> Fill:
        o["seq"] += 1
        f = Fill(f"{o['req'].client_order_id}-{o['seq']}", price, qty,
                 (price * qty * self.fee_rate).quantize(Decimal("0.00000001")), o["req"].quote_asset)
        o["fills"].append(f)
        o["filled"] += qty
        return f

    def _report(self, o: dict) -> ExecutionReport:
        return ExecutionReport(o["status"], o["id"], list(o["fills"]), o.get("error"), simulated=True)

    async def submit(self, req: OrderRequest) -> ExecutionReport:
        with self._lock:
            if req.client_order_id in self._orders:  # idempotent resubmission
                return self._report(self._orders[req.client_order_id])
        if req.order_type not in ("MARKET", "LIMIT"):
            # Exchange-side stops protect real venue positions while the bot is offline;
            # a paper position only exists while the bot runs, so there is nothing to simulate.
            return ExecutionReport("REJECTED", None, error=f"paper: {req.order_type} orders not simulated", simulated=True)
        try:
            book = await self.depth_provider(req.symbol)
        except Exception as exc:
            return ExecutionReport("REJECTED", None, error=f"paper: order book unavailable ({exc})", simulated=True)
        o = {"req": req, "id": f"PAPER-{req.client_order_id}", "status": "NEW", "fills": [], "filled": Decimal("0"),
             "seq": 0, "created": time.time()}
        levels = book.get("asks" if req.side == "BUY" else "bids") or []
        slip = self.slippage_bps / BPS
        remaining = req.quantity
        for lp, lq in levels:
            if remaining <= 0:
                break
            lp, lq = Decimal(str(lp)), Decimal(str(lq))
            if req.order_type == "LIMIT" and ((req.side == "BUY" and lp > req.price) or (req.side == "SELL" and lp < req.price)):
                break
            take = min(remaining, lq)
            if req.order_type == "MARKET":
                px = lp * (1 + slip) if req.side == "BUY" else lp * (1 - slip)
            else:
                px = lp  # limit orders never fill worse than the limit (lp already within limit)
            self._fill(o, px.quantize(Decimal("0.00000001")), take)
            remaining -= take
        if remaining <= 0:
            o["status"] = "FILLED"
        elif req.order_type == "MARKET":
            o["status"] = "EXPIRED" if o["fills"] else "REJECTED"
            if not o["fills"]:
                o["error"] = "paper: no liquidity in order book"
        else:
            o["status"] = "PARTIALLY_FILLED" if o["fills"] else "NEW"
        with self._lock:
            self._orders[req.client_order_id] = o
        return self._report(o)

    def _available(self, symbol: str, side: str, price: Decimal, displayed: Decimal) -> Decimal:
        """Displayed quantity at this level not yet consumed by earlier simulated fills.

        Book-ticker updates frequently re-publish the SAME level; without this, one
        unit of displayed liquidity could fill an order of any size over a few ticks.
        Consumption resets when the level's price changes. While the price is unchanged,
        simulated fills are conservatively assumed to still be part of the displayed size
        (so only growth beyond what we already took becomes fillable).
        """
        key = (symbol, side)
        prev = self._consumed.get(key)
        if prev is None or prev[0] != price:
            self._consumed[key] = (price, Decimal("0"))
            return displayed
        return max(Decimal("0"), displayed - prev[1])

    def _consume(self, symbol: str, side: str, qty: Decimal) -> None:
        price, used = self._consumed[(symbol, side)]
        self._consumed[(symbol, side)] = (price, used + qty)

    def on_quote(self, symbol: str, bid: Decimal, bid_qty: Decimal, ask: Decimal, ask_qty: Decimal) -> None:
        """Match resting paper LIMIT orders against the latest top of book."""
        with self._lock:
            avail = {"ask": self._available(symbol, "ask", ask, ask_qty) if ask > 0 else Decimal("0"),
                     "bid": self._available(symbol, "bid", bid, bid_qty) if bid > 0 else Decimal("0")}
            for o in self._orders.values():
                req = o["req"]
                if req.symbol != symbol or o["status"] not in ("NEW", "PARTIALLY_FILLED"):
                    continue
                remaining = req.quantity - o["filled"]
                if req.side == "BUY" and ask > 0 and ask <= req.price and avail["ask"] > 0:
                    level, px = "ask", ask
                elif req.side == "SELL" and bid > 0 and bid >= req.price and avail["bid"] > 0:
                    level, px = "bid", bid
                else:
                    continue
                take = min(remaining, avail[level])
                self._fill(o, px, take)
                avail[level] -= take
                self._consume(symbol, level, take)
                o["status"] = "FILLED" if o["filled"] >= req.quantity else "PARTIALLY_FILLED"

    def restore(self, req: OrderRequest, filled: Decimal, fill_count: int) -> None:
        """Re-register a resting paper order after restart (fills already recorded in DB)."""
        with self._lock:
            if req.client_order_id not in self._orders:
                self._orders[req.client_order_id] = {
                    "req": req, "id": f"PAPER-{req.client_order_id}", "fills": [], "filled": filled,
                    "seq": fill_count, "created": time.time(),
                    "status": "PARTIALLY_FILLED" if filled > 0 else "NEW",
                }

    async def query(self, req: OrderRequest) -> ExecutionReport:
        with self._lock:
            o = self._orders.get(req.client_order_id)
        if o is None:
            # The paper engine never executed it (e.g. crash before recording): nothing moved.
            return ExecutionReport("EXPIRED", None, error="paper: order not found in simulator (not executed)", simulated=True)
        return self._report(o)

    async def cancel(self, req: OrderRequest) -> ExecutionReport:
        with self._lock:
            o = self._orders.get(req.client_order_id)
            if o is None:
                return ExecutionReport("CANCELED", None, simulated=True)
            if o["status"] not in TERMINAL:
                o["status"] = "CANCELED"
            return self._report(o)

    def forget(self, client_order_id: str) -> None:
        with self._lock:
            self._orders.pop(client_order_id, None)


# Binance error codes where the order outcome is unknown
UNKNOWN_CODES = {-1006, -1007}
ORDER_NOT_FOUND = -2013

PriceLookup = Callable[[str], Awaitable[Decimal | None]]


class ExchangeExecutionAdapter:
    """Execution on a Binance Spot account. Never falls back to any other endpoint."""

    mode = ""
    venue = ""
    client_type: type[SignedClient] = SignedClient

    def __init__(self, client: SignedClient) -> None:
        if not isinstance(client, self.client_type):  # live keys never reach the testnet adapter and vice versa
            raise TypeError(f"{type(self).__name__} requires a {self.client_type.__name__}")
        self.client = client

    @staticmethod
    def _fills_from_full(resp: dict) -> list[Fill]:
        return [Fill(str(f.get("tradeId")), Decimal(f["price"]), Decimal(f["qty"]), Decimal(f["commission"]),
                     f["commissionAsset"]) for f in resp.get("fills", [])]

    async def submit(self, req: OrderRequest) -> ExecutionReport:
        try:
            resp = await self.client.new_order(symbol=req.symbol, side=req.side, order_type=req.order_type,
                                               quantity=req.quantity, client_order_id=req.client_order_id,
                                               price=req.price, stop_price=req.stop_price,
                                               time_in_force="GTC" if req.order_type in ("LIMIT", "STOP_LOSS_LIMIT") else None)
        except BinanceNetworkError as exc:
            return ExecutionReport("UNKNOWN", error=f"submission outcome unknown: {exc}")
        except BinanceRateLimitError as exc:
            return ExecutionReport("REJECTED", error=f"rate limited: {exc.msg}")
        except BinanceAPIError as exc:
            if exc.code in UNKNOWN_CODES:
                return ExecutionReport("UNKNOWN", error=f"exchange reported unknown status: {exc.msg}")
            return ExecutionReport("REJECTED", error=f"{self.venue} rejected order ({exc.code}): {exc.msg}")
        return ExecutionReport(resp.get("status", "UNKNOWN"), str(resp.get("orderId")), self._fills_from_full(resp))

    async def query(self, req: OrderRequest) -> ExecutionReport:
        try:
            o = await self.client.get_order(req.symbol, req.client_order_id)
        except BinanceAPIError as exc:
            if exc.code == ORDER_NOT_FOUND:
                return ExecutionReport("NOT_FOUND", error=f"order not found on {self.venue}")
            return ExecutionReport("UNKNOWN", error=str(exc))
        except BinanceNetworkError as exc:
            return ExecutionReport("UNKNOWN", error=str(exc))
        fills: list[Fill] = []
        if Decimal(o.get("executedQty", "0")) > 0:
            try:
                trades = await self.client.my_trades(req.symbol, int(o["orderId"]))
                fills = [Fill(str(t["id"]), Decimal(t["price"]), Decimal(t["qty"]), Decimal(t["commission"]),
                              t["commissionAsset"]) for t in trades]
            except (BinanceAPIError, BinanceNetworkError) as exc:
                return ExecutionReport("UNKNOWN", str(o.get("orderId")), error=f"could not load fills: {exc}")
        return ExecutionReport(o["status"], str(o["orderId"]), fills)

    async def cancel(self, req: OrderRequest) -> ExecutionReport:
        try:
            await self.client.cancel_order(req.symbol, req.client_order_id)
        except BinanceAPIError as exc:
            if exc.code != ORDER_NOT_FOUND:
                return ExecutionReport("UNKNOWN", error=str(exc))
        except BinanceNetworkError as exc:
            return ExecutionReport("UNKNOWN", error=str(exc))
        return await self.query(req)

    async def free_balance(self, asset: str) -> Decimal:
        acct = await self.client.account()
        for b in acct.get("balances", []):
            if b["asset"] == asset:
                return Decimal(b["free"])
        return Decimal("0")


class TestnetExecutionAdapter(ExchangeExecutionAdapter):
    """Binance Spot Testnet execution."""

    __test__ = False
    mode = "TESTNET"
    venue = "testnet"
    client_type = TestnetClient


class LiveExecutionAdapter(ExchangeExecutionAdapter):
    """Binance Spot production execution. REAL MONEY."""

    mode = "LIVE"
    venue = "live"
    client_type = LiveClient

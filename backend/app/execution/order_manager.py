"""Order lifecycle management.

Safety properties
-----------------
1. **Idempotency**: every order has a unique ``idempotency_key`` (DB unique
   constraint per mode) and a unique ``client_order_id``. A repeated signal for
   the same candle cannot create a second order.
2. **Persist-before-submit**: the order row is committed (status SUBMITTING)
   BEFORE the request is sent. If the process dies mid-flight, startup
   reconciliation queries the venue by client order id instead of resubmitting.
3. **No assumed fills**: positions change only from reported fills, applied
   idempotently by fill id. An accepted order is not a filled order.
4. **Per-symbol locks** serialise decisions so concurrent market events cannot
   race; the partial unique index on open positions is the DB-level backstop.
5. **Unknown outcomes** (timeouts, 5xx) mark the order UNKNOWN and are
   reconciled; they are never treated as failed and never retried blindly.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import log_event
from app.db import session_scope
from app.events import bus
from app.exchange.filters import SymbolFilters
from app.execution.adapters import TERMINAL, ExecutionReport, OrderRequest
from app.models import OrderFill, Position, TradingOrder
from app.portfolio import service as pf

log = logging.getLogger(__name__)
ACTIVE = ("SUBMITTING", "UNKNOWN", "NEW", "PARTIALLY_FILLED", "PENDING_CANCEL")
STATUS_MAP = {"EXPIRED_IN_MATCH": "EXPIRED", "PENDING_NEW": "NEW"}
NOT_FOUND_GRACE = timedelta(seconds=90)
ZERO = Decimal("0")


def new_client_order_id(mode: str) -> str:
    return f"tb{mode[0].lower()}-{uuid.uuid4().hex[:28]}"


def order_to_dict(o: TradingOrder) -> dict:
    return {
        "id": o.id, "client_order_id": o.client_order_id, "exchange_order_id": o.exchange_order_id,
        "symbol": o.symbol, "side": o.side, "order_type": o.order_type, "quantity": o.quantity, "price": o.price,
        "filled_quantity": o.filled_quantity, "average_fill_price": o.average_fill_price, "status": o.status,
        "execution_mode": o.execution_mode, "purpose": o.purpose, "position_id": o.position_id,
        "fee_quote": o.fee_quote, "slippage_bps": o.slippage_bps, "reason": o.reason,
        "error_message": o.error_message, "created_at": o.created_at, "updated_at": o.updated_at,
    }


class OrderManager:
    def __init__(self, adapter, mode: str, filters_lookup: Callable[[str], SymbolFilters | None],
                 fee_rate_lookup: Callable[[str], Decimal | None] | None = None) -> None:
        self.adapter = adapter
        self.mode = mode
        self.filters_lookup = filters_lookup
        self.fee_rate_lookup = fee_rate_lookup
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def lock(self, symbol: str) -> asyncio.Lock:
        return self._locks[symbol]

    # ------------------------------------------------------------------ queries
    def has_active_entry(self, s: Session, symbol: str) -> bool:
        return bool(s.scalar(select(func.count()).select_from(TradingOrder).where(
            TradingOrder.execution_mode == self.mode, TradingOrder.symbol == symbol,
            TradingOrder.purpose == "ENTRY", TradingOrder.status.in_(ACTIVE))))

    def pending_entry_notional(self, s: Session) -> tuple[int, Decimal]:
        rows = s.scalars(select(TradingOrder).where(TradingOrder.execution_mode == self.mode,
                                                    TradingOrder.purpose == "ENTRY", TradingOrder.status.in_(ACTIVE),
                                                    TradingOrder.position_id.is_(None)))
        n, total = 0, ZERO
        for o in rows:
            n += 1
            total += (o.quantity - o.filled_quantity) * (o.price or o.expected_price or ZERO)
        return n, total

    # ------------------------------------------------------------------ placing
    async def place(self, *, symbol: str, side: str, order_type: str, quantity: Decimal, purpose: str,
                    idempotency_key: str, expected_price: Decimal, price: Decimal | None = None,
                    position_id: int | None = None, signal_id: int | None = None, meta: dict | None = None,
                    reason: str | None = None, timeout_seconds: int | None = None) -> int | None:
        """Persist, submit and record an order. Returns the order id, or None if blocked as a duplicate."""
        f = self.filters_lookup(symbol)
        async with self.lock(symbol):
            # ---- (A) persist intent atomically
            try:
                with session_scope() as s:
                    if s.scalar(select(TradingOrder.id).where(TradingOrder.execution_mode == self.mode,
                                                              TradingOrder.idempotency_key == idempotency_key)):
                        log.info("duplicate order blocked: %s", idempotency_key)
                        return None
                    if purpose == "ENTRY":
                        if self.has_active_entry(s, symbol) or s.scalar(select(Position.id).where(
                                Position.execution_mode == self.mode, Position.symbol == symbol,
                                Position.status.in_(("OPEN", "CLOSING")))):
                            log_event(s, "orders", f"Entry for {symbol} blocked: position or entry order already exists",
                                      "WARNING", {"idempotency_key": idempotency_key}, category="order_rejected")
                            return None
                    else:
                        pos = s.get(Position, position_id) if position_id else None
                        if pos is None or pos.status == "CLOSED":
                            return None
                        if s.scalar(select(TradingOrder.id).where(TradingOrder.position_id == position_id,
                                                                  TradingOrder.purpose == "EXIT",
                                                                  TradingOrder.status.in_(ACTIVE))):
                            return None
                        pos.status = "CLOSING"
                    now = datetime.now(timezone.utc)
                    order = TradingOrder(
                        client_order_id=new_client_order_id(self.mode), idempotency_key=idempotency_key,
                        symbol=symbol, side=side, order_type=order_type, quantity=quantity, price=price,
                        filled_quantity=ZERO, cumulative_quote_qty=ZERO, fee_quote=ZERO,
                        expected_price=expected_price, status="SUBMITTING", execution_mode=self.mode,
                        purpose=purpose, position_id=position_id, signal_id=signal_id, reason=reason,
                        order_meta=meta or {},
                        expires_at=now + timedelta(seconds=timeout_seconds) if order_type == "LIMIT" and timeout_seconds else None,
                    )
                    s.add(order)
                    s.flush()
                    order_id, client_id = order.id, order.client_order_id
                    log_event(s, "orders", f"{self.mode} {purpose} {side} {order_type} {quantity} {symbol} submitted"
                              + (f" @ {price}" if price else ""), "INFO",
                              {"client_order_id": client_id, "reason": reason, "simulated": self.mode == "PAPER"},
                              category="order_request")
            except IntegrityError:
                log.warning("duplicate order prevented by database constraint: %s", idempotency_key)
                return None

            # ---- (B) submit (no DB transaction held)
            req = OrderRequest(client_id, symbol, side, order_type, quantity, price,
                               base_asset=f.base_asset if f else "", quote_asset=f.quote_asset if f else "USDT")
            try:
                report = await self.adapter.submit(req)
            except Exception as exc:  # defensive: unexpected adapter failure => outcome unknown
                log.exception("adapter submit failed")
                report = ExecutionReport("UNKNOWN", error=f"adapter error: {exc}")

            # ---- (C) record outcome
            with session_scope() as s:
                self.apply_report(s, order_id, report)
            return order_id

    # ------------------------------------------------------------------ applying reports
    def apply_report(self, s: Session, order_id: int, report: ExecutionReport) -> TradingOrder:
        order = s.get(TradingOrder, order_id)
        assert order is not None
        prev_status = order.status
        if order.status in TERMINAL:
            return order
        status = STATUS_MAP.get(report.status, report.status)
        if status == "NOT_FOUND":
            age = datetime.now(timezone.utc) - order.created_at
            if order.status in ("SUBMITTING", "UNKNOWN") and age > NOT_FOUND_GRACE:
                status = "REJECTED"
                report.error = "order never reached the exchange (not found after grace period)"
            else:
                return order
        if status == "UNKNOWN":
            order.status = "UNKNOWN"
            order.error_message = report.error
            if prev_status != "UNKNOWN":
                log_event(s, "orders", f"Order {order.client_order_id} status UNKNOWN - will reconcile: {report.error}",
                          "WARNING", category="order_unknown")
            return order

        if report.exchange_order_id:
            order.exchange_order_id = report.exchange_order_id
        f = self.filters_lookup(order.symbol)
        base = f.base_asset if f else order.symbol.removesuffix("USDT")
        quote = f.quote_asset if f else "USDT"
        known = {x.fill_id for x in order.fills}
        for fill in report.fills:
            if fill.fill_id in known:
                continue  # idempotent: already applied
            known.add(fill.fill_id)
            fee_q, kind = pf.fee_to_quote(fill.fee, fill.fee_asset, base, quote, fill.price, self.fee_rate_lookup)
            s.add(OrderFill(order_id=order.id, fill_id=fill.fill_id, price=fill.price, quantity=fill.qty,
                            fee=fill.fee, fee_asset=fill.fee_asset, fee_quote=fee_q))
            fee_base = fill.fee if kind == "BASE" else ZERO
            if order.purpose == "ENTRY":
                pf.apply_entry_fill(s, order, fill.price, fill.qty, fee_q, kind, fee_base)
            else:
                pf.apply_exit_fill(s, order, fill.price, fill.qty, fee_q, kind, fee_base)
            order.filled_quantity += fill.qty
            order.cumulative_quote_qty += fill.qty * fill.price
            order.fee_quote += fee_q
        s.flush()
        if order.filled_quantity > 0:
            order.average_fill_price = (order.cumulative_quote_qty / order.filled_quantity).quantize(Decimal("0.00000001"))
            if order.expected_price:
                diff = order.average_fill_price - order.expected_price
                if order.side == "SELL":
                    diff = -diff
                order.slippage_bps = (diff / order.expected_price * 10000).quantize(Decimal("0.01"))
        order.status = status
        if report.error:
            order.error_message = report.error

        if status != prev_status or report.fills:
            level = "WARNING" if status in ("REJECTED", "EXPIRED") and order.filled_quantity == 0 else "INFO"
            cat = "order_rejected" if status == "REJECTED" else "order_execution"
            log_event(s, "orders", f"{self.mode} order {order.client_order_id} {order.side} {order.symbol}: {status}, "
                      f"filled {order.filled_quantity}/{order.quantity}"
                      + (f" @ {order.average_fill_price}" if order.average_fill_price else "")
                      + (f" ({report.error})" if report.error else ""), level,
                      {"order_id": order.id, "simulated": report.simulated}, category=cat)
        if order.purpose == "EXIT":
            self._after_exit(s, order)
        elif status in TERMINAL and order.position_id:
            pos = s.get(Position, order.position_id)
            if pos is not None:
                bus.publish("trading", "position", {"id": pos.id, "symbol": pos.symbol, "status": pos.status})
        bus.publish("trading", "order", order_to_dict(order))
        return order

    def _after_exit(self, s: Session, order: TradingOrder) -> None:
        pos = s.get(Position, order.position_id) if order.position_id else None
        if pos is None or pos.status == "CLOSED":
            return
        reason = (order.order_meta or {}).get("exit_reason", order.reason or "EXIT")
        f = self.filters_lookup(order.symbol)
        if pos.quantity <= 0:
            pf.close_position(s, pos, order.average_fill_price, reason)
        elif order.status in TERMINAL:
            px = order.average_fill_price or pos.current_price or pos.average_entry_price
            is_dust = f is not None and (pos.quantity < max(f.min_qty, f.market_min_qty) or
                                         (f.apply_min_to_market and pos.quantity * px < f.min_notional))
            if is_dust:
                lost = pf.write_off_dust(s, pos, reason)
                log_event(s, "portfolio", f"{pos.symbol}: residual dust below exchange minimums written off ({lost} USDT)", "WARNING")
                pf.close_position(s, pos, order.average_fill_price, reason)
            else:
                pos.status = "OPEN"  # exit incomplete; monitor will retry
                log_event(s, "orders", f"Exit for {pos.symbol} incomplete ({order.status}); {pos.quantity} remaining, will retry",
                          "WARNING", category="order_rejected")
        if pos.status == "CLOSED":
            log_event(s, "trading", f"Position {pos.symbol} closed ({reason}); realised P&L {pos.realised_pnl} USDT",
                      "INFO", {"position_id": pos.id, "simulated": self.mode == "PAPER"}, category="position_closed")
        bus.publish("trading", "position", {"id": pos.id, "symbol": pos.symbol, "status": pos.status,
                                            "realised_pnl": pos.realised_pnl})

    # ------------------------------------------------------------------ reconciliation
    async def reconcile(self) -> int:
        """Query every non-terminal order and apply its true state. Cancels expired limit orders."""
        with session_scope() as s:
            pending = [(o.id, o.symbol, o.side, o.order_type, o.quantity, o.price, o.client_order_id, o.expires_at, o.status)
                       for o in s.scalars(select(TradingOrder).where(TradingOrder.execution_mode == self.mode,
                                                                     TradingOrder.status.in_(ACTIVE)))]
        now = datetime.now(timezone.utc)
        for oid, symbol, side, otype, qty, price, cid, expires_at, status in pending:
            f = self.filters_lookup(symbol)
            req = OrderRequest(cid, symbol, side, otype, qty, price, base_asset=f.base_asset if f else "",
                               quote_asset=f.quote_asset if f else "USDT")
            async with self.lock(symbol):
                try:
                    if expires_at and now > expires_at and status in ("NEW", "PARTIALLY_FILLED"):
                        report = await self.adapter.cancel(req)
                        if report.status not in TERMINAL and report.status != "UNKNOWN":
                            report.status = "CANCELED"
                    else:
                        report = await self.adapter.query(req)
                except Exception as exc:
                    log.warning("reconcile query failed for %s: %s", cid, exc)
                    continue
                with session_scope() as s:
                    self.apply_report(s, oid, report)
        return len(pending)

    def restore_paper_orders(self) -> int:
        """After restart, re-register resting paper LIMIT orders with the simulator."""
        if not hasattr(self.adapter, "restore"):
            return 0
        n = 0
        with session_scope() as s:
            for o in s.scalars(select(TradingOrder).where(TradingOrder.execution_mode == self.mode,
                                                          TradingOrder.status.in_(("NEW", "PARTIALLY_FILLED")))):
                f = self.filters_lookup(o.symbol)
                self.adapter.restore(OrderRequest(o.client_order_id, o.symbol, o.side, o.order_type, o.quantity, o.price,
                                                  base_asset=f.base_asset if f else "", quote_asset=f.quote_asset if f else "USDT"),
                                     o.filled_quantity, len(o.fills))
                n += 1
        return n

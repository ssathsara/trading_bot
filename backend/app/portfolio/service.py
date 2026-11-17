"""Portfolio accounting (Decimal only).

Accounting model (per execution mode, quote asset USDT):
* BUY fill:  cash -= qty*price + fee_cash;  position.cost_basis += qty*price
* SELL fill: cash += qty*price - fee_cash;  realised = proceeds - fee - allocated(cost + entry fees)
* Fees charged in the BASE asset reduce the quantity received instead of cash.
* Fees charged in a third asset (e.g. BNB on testnet) are converted to quote and
  deducted from the USDT ledger (conservative).
* unrealised = qty*price - cost_basis - entry_fees_open

This keeps the invariant:  equity - initial == sum(realised) + sum(unrealised).
Unrealised profit is never counted as realised.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import numeric
from app.models import AccountState, PortfolioSnapshot, Position, Trade, TradingOrder

ZERO = Decimal("0")
Q8 = Decimal("0.00000001")


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_account(session: Session, mode: str, initial_balance: Decimal | None = None, lock: bool = False) -> AccountState:
    stmt = select(AccountState).where(AccountState.execution_mode == mode)
    if lock:
        stmt = stmt.with_for_update()
    acct = session.scalar(stmt)
    if acct is None:
        bal = initial_balance if initial_balance is not None else Decimal("10000")
        acct = AccountState(execution_mode=mode, initial_balance=bal, cash_balance=bal, total_fees=ZERO,
                            realised_pnl=ZERO, peak_equity=bal, day_start_equity=bal, day_start_date=today_utc(),
                            consecutive_losses=0, halted=False)
        session.add(acct)
        session.flush()
    return acct


def open_positions(session: Session, mode: str) -> list[Position]:
    return list(session.scalars(select(Position).where(Position.execution_mode == mode,
                                                       Position.status.in_(("OPEN", "CLOSING")))))


PriceFn = Callable[[str], Decimal | None]


@dataclass
class Valuation:
    cash: Decimal
    invested: Decimal  # market value of open positions
    equity: Decimal
    unrealised: Decimal
    realised: Decimal
    cost_basis: Decimal


def mark_to_market(session: Session, mode: str, price_fn: PriceFn) -> Valuation:
    acct = get_account(session, mode)
    invested = unreal = cost = ZERO
    for p in open_positions(session, mode):
        px = price_fn(p.symbol) or p.current_price or p.average_entry_price
        p.current_price = px
        p.unrealised_pnl = (p.quantity * px - p.cost_basis - p.entry_fees_open).quantize(Q8)
        if p.highest_price is None or px > p.highest_price:
            p.highest_price = px
        invested += p.quantity * px
        unreal += p.unrealised_pnl
        cost += p.cost_basis
    return Valuation(acct.cash_balance, invested.quantize(Q8), (acct.cash_balance + invested).quantize(Q8),
                     unreal.quantize(Q8), acct.realised_pnl, cost)


def fee_to_quote(fee: Decimal, fee_asset: str, base: str, quote: str, price: Decimal,
                 convert: Callable[[str], Decimal | None] | None = None) -> tuple[Decimal, str]:
    """Return (fee in quote, where it was charged: QUOTE|BASE|OTHER)."""
    if fee == 0 or fee_asset == quote:
        return fee, "QUOTE"
    if fee_asset == base:
        return (fee * price).quantize(Q8), "BASE"
    rate = convert(fee_asset) if convert else None
    return ((fee * rate).quantize(Q8) if rate else ZERO), "OTHER"


def apply_entry_fill(session: Session, order: TradingOrder, price: Decimal, qty: Decimal, fee_quote: Decimal,
                     fee_kind: str, fee_base_qty: Decimal = ZERO) -> Position:
    acct = get_account(session, order.execution_mode, lock=True)
    net_qty = qty - fee_base_qty if fee_kind == "BASE" else qty
    cash_fee = fee_quote if fee_kind in ("QUOTE", "OTHER") else ZERO
    gross = qty * price
    acct.cash_balance = (acct.cash_balance - gross - cash_fee).quantize(Q8)
    acct.total_fees = (acct.total_fees + fee_quote).quantize(Q8)

    pos = session.get(Position, order.position_id) if order.position_id else None
    meta = order.order_meta or {}
    if pos is None:
        pos = Position(
            symbol=order.symbol, execution_mode=order.execution_mode, quantity=ZERO, average_entry_price=ZERO,
            cost_basis=ZERO, entry_fees_open=ZERO, unrealised_pnl=ZERO, realised_pnl=ZERO, status="OPEN",
            stop_loss=Decimal(meta["stop_loss"]) if meta.get("stop_loss") else None,
            take_profit=Decimal(meta["take_profit"]) if meta.get("take_profit") else None,
            trailing_stop_pct=Decimal(meta["trailing_stop_pct"]) if meta.get("trailing_stop_pct") else None,
            strategy_name=meta.get("strategy_name"), highest_price=price, current_price=price,
        )
        session.add(pos)
        session.flush()
        order.position_id = pos.id
        session.add(Trade(order_id=order.id, position_id=pos.id, symbol=order.symbol, side="BUY", entry_price=price,
                          quantity=ZERO, entry_fee=ZERO, exit_fee=ZERO, status="OPEN",
                          strategy_name=meta.get("strategy_name"), execution_mode=order.execution_mode))
        session.flush()
    pos.quantity = pos.quantity + net_qty
    pos.cost_basis = (pos.cost_basis + gross).quantize(Q8)
    pos.entry_fees_open = (pos.entry_fees_open + cash_fee).quantize(Q8)
    pos.average_entry_price = (pos.cost_basis / pos.quantity).quantize(Q8) if pos.quantity > 0 else price
    trade = session.scalar(select(Trade).where(Trade.position_id == pos.id))
    if trade is not None:
        trade.quantity = pos.quantity
        trade.entry_price = pos.average_entry_price
        trade.entry_fee = (trade.entry_fee + fee_quote).quantize(Q8)
    session.flush()
    return pos


def apply_exit_fill(session: Session, order: TradingOrder, price: Decimal, qty: Decimal, fee_quote: Decimal,
                    fee_kind: str, fee_base_qty: Decimal = ZERO) -> tuple[Position, Decimal]:
    """Apply a SELL fill. Returns (position, realised pnl of this fill)."""
    acct = get_account(session, order.execution_mode, lock=True)
    pos = session.get(Position, order.position_id)
    if pos is None or pos.quantity <= 0:
        raise ValueError(f"exit fill for order {order.client_order_id} has no open position")
    qty = min(qty, pos.quantity)
    proceeds = qty * price
    cash_fee = fee_quote if fee_kind in ("QUOTE", "OTHER") else ZERO
    # A base-asset fee on a sell consumes extra base from the position.
    consumed = min(pos.quantity, qty + (fee_base_qty if fee_kind == "BASE" else ZERO))
    frac = consumed / pos.quantity
    cost_alloc = (pos.cost_basis * frac).quantize(Q8)
    fee_alloc = (pos.entry_fees_open * frac).quantize(Q8)
    realised = (proceeds - cash_fee - cost_alloc - fee_alloc).quantize(Q8)

    acct.cash_balance = (acct.cash_balance + proceeds - cash_fee).quantize(Q8)
    acct.total_fees = (acct.total_fees + fee_quote).quantize(Q8)
    acct.realised_pnl = (acct.realised_pnl + realised).quantize(Q8)
    pos.quantity -= consumed
    pos.cost_basis = (pos.cost_basis - cost_alloc).quantize(Q8)
    pos.entry_fees_open = (pos.entry_fees_open - fee_alloc).quantize(Q8)
    pos.realised_pnl = (pos.realised_pnl + realised).quantize(Q8)
    trade = session.scalar(select(Trade).where(Trade.position_id == pos.id))
    if trade is not None:
        trade.exit_fee = (trade.exit_fee + fee_quote).quantize(Q8)
        trade.exit_order_id = order.id
    session.flush()
    return pos, realised


def write_off_dust(session: Session, pos: Position, reason: str) -> Decimal:
    """Close a residual quantity too small to sell; its remaining cost is realised as a loss."""
    acct = get_account(session, pos.execution_mode, lock=True)
    loss = -(pos.cost_basis + pos.entry_fees_open)
    acct.realised_pnl = (acct.realised_pnl + loss).quantize(Q8)
    pos.realised_pnl = (pos.realised_pnl + loss).quantize(Q8)
    pos.quantity = ZERO
    pos.cost_basis = ZERO
    pos.entry_fees_open = ZERO
    return loss


def close_position(session: Session, pos: Position, exit_price: Decimal | None, reason: str) -> Trade | None:
    now = datetime.now(timezone.utc)
    pos.status = "CLOSED"
    pos.exit_reason = reason
    pos.closed_at = now
    pos.unrealised_pnl = ZERO
    trade = session.scalar(select(Trade).where(Trade.position_id == pos.id))
    acct = get_account(session, pos.execution_mode, lock=True)
    if trade is not None:
        trade.status = "CLOSED"
        trade.exit_price = exit_price
        trade.realised_pnl = pos.realised_pnl
        trade.exit_reason = reason
        trade.closed_at = now
    if pos.realised_pnl < 0:
        acct.consecutive_losses += 1
        acct.last_loss_at = now
    else:
        acct.consecutive_losses = 0
    session.flush()
    return trade


def roll_day_and_peak(session: Session, mode: str, equity: Decimal) -> AccountState:
    acct = get_account(session, mode, lock=True)
    today = today_utc()
    if acct.day_start_date != today:
        acct.day_start_date = today
        acct.day_start_equity = equity
    if equity > acct.peak_equity:
        acct.peak_equity = equity
    return acct


def record_snapshot(session: Session, mode: str, price_fn: PriceFn) -> PortfolioSnapshot:
    v = mark_to_market(session, mode, price_fn)
    acct = roll_day_and_peak(session, mode, v.equity)
    dd = ((acct.peak_equity - v.equity) / acct.peak_equity * 100).quantize(Decimal("0.0001")) if acct.peak_equity > 0 else ZERO
    snap = PortfolioSnapshot(execution_mode=mode, total_equity=v.equity, available_balance=v.cash,
                             invested_capital=v.invested, realised_pnl=acct.realised_pnl,
                             unrealised_pnl=v.unrealised, drawdown=max(dd, ZERO))
    session.add(snap)
    session.flush()
    return snap


def performance(session: Session, mode: str, price_fn: PriceFn) -> dict:
    acct = get_account(session, mode)
    v = mark_to_market(session, mode, price_fn)
    closed = list(session.scalars(select(Trade).where(Trade.execution_mode == mode, Trade.status == "CLOSED")))
    wins = [t.realised_pnl for t in closed if t.realised_pnl is not None and t.realised_pnl > 0]
    # Break-even (0) trades are neither wins nor losses - consistent with close_position(),
    # which does not count them toward the consecutive-loss streak. They still count in win_rate's denominator.
    losses = [t.realised_pnl for t in closed if t.realised_pnl is not None and t.realised_pnl < 0]
    gross_win, gross_loss = sum(wins, ZERO), -sum(losses, ZERO)
    max_dd = session.scalar(select(func.max(numeric(PortfolioSnapshot.drawdown)))
                            .where(PortfolioSnapshot.execution_mode == mode))
    max_dd = Decimal(str(max_dd)) if max_dd is not None else ZERO
    cur_dd = ((acct.peak_equity - v.equity) / acct.peak_equity * 100) if acct.peak_equity > 0 else ZERO
    n = len(closed)
    return {
        "execution_mode": mode,
        "initial_balance": acct.initial_balance,
        "available_balance": v.cash,
        "invested_capital": v.invested,
        "total_equity": v.equity,
        "realised_pnl": acct.realised_pnl,
        "unrealised_pnl": v.unrealised,
        "net_profit": (v.equity - acct.initial_balance).quantize(Q8),
        "daily_pnl": (v.equity - acct.day_start_equity).quantize(Q8),
        "total_fees": acct.total_fees,
        "closed_trades": n,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": (Decimal(len(wins)) / n * 100).quantize(Decimal("0.01")) if n else None,
        "average_win": (gross_win / len(wins)).quantize(Q8) if wins else None,
        "average_loss": (-gross_loss / len(losses)).quantize(Q8) if losses else None,
        "profit_factor": (gross_win / gross_loss).quantize(Decimal("0.0001")) if gross_loss > 0 else None,
        "max_drawdown_pct": max(Decimal(max_dd), max(cur_dd, ZERO)).quantize(Decimal("0.0001")),
        "current_drawdown_pct": max(cur_dd, ZERO).quantize(Decimal("0.0001")),
        "open_positions": len(open_positions(session, mode)),
        "halted": acct.halted,
        "halt_reason": acct.halt_reason,
    }

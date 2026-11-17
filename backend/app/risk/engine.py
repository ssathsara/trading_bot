"""Independent risk-management engine.

The risk engine has final authority: every entry proposed by the strategy (or
the ML filter) must be approved here. All arithmetic is Decimal.

Hard safety limits (daily loss, max drawdown) HALT trading and require an
explicit reset. Soft limits (open positions, exposure, cooldowns, stale data,
volatility) reject the individual trade.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from app.exchange.filters import SymbolFilters, round_step
from app.settings_store import ExecutionSettings, RiskSettings

ZERO = Decimal("0")
HUNDRED = Decimal("100")
BPS = Decimal("10000")


@dataclass
class AccountSnapshot:
    equity: Decimal
    cash: Decimal
    invested: Decimal  # market value of open positions + notional of pending entry orders
    open_positions: int
    day_start_equity: Decimal
    peak_equity: Decimal
    consecutive_losses: int
    last_loss_at: datetime | None
    halted: bool
    halt_reason: str | None = None


@dataclass
class EntryRequest:
    symbol: str
    entry_price: Decimal  # expected execution price (e.g. best ask)
    atr: Decimal | None
    filters: SymbolFilters
    now: datetime
    has_open_position: bool = False
    has_pending_order: bool = False
    data_stale: bool = False
    connectivity_ok: bool = True
    symbol_last_loss_at: datetime | None = None
    market_order: bool = True


@dataclass
class RiskDecision:
    approved: bool
    symbol: str
    quantity: Decimal = ZERO
    entry_price: Decimal = ZERO
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    notional: Decimal = ZERO
    risk_amount: Decimal = ZERO
    reward_risk: Decimal | None = None
    reasons: list[str] = field(default_factory=list)
    adjustments: list[str] = field(default_factory=list)
    hard_breach: str | None = None

    def to_dict(self) -> dict:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in self.__dict__.items()}


def check_hard_limits(acct: AccountSnapshot, rs: RiskSettings) -> tuple[str, str] | None:
    """Return (breach_type, description) if a HARD limit is breached."""
    if acct.day_start_equity > 0:
        daily_loss_pct = (acct.day_start_equity - acct.equity) / acct.day_start_equity * HUNDRED
        if daily_loss_pct >= rs.max_daily_loss_pct:
            return ("DAILY_LOSS_LIMIT",
                    f"Daily loss {daily_loss_pct:.2f}% reached limit {rs.max_daily_loss_pct}%")
    if acct.peak_equity > 0:
        dd_pct = (acct.peak_equity - acct.equity) / acct.peak_equity * HUNDRED
        if dd_pct >= rs.max_drawdown_pct:
            return ("MAX_DRAWDOWN", f"Drawdown {dd_pct:.2f}% reached limit {rs.max_drawdown_pct}%")
    return None


def stop_and_target(entry: Decimal, atr: Decimal | None, rs: RiskSettings, f: SymbolFilters) -> tuple[Decimal | None, Decimal | None, str | None]:
    if rs.stop_loss_mode == "ATR":
        if atr is None or atr <= 0:
            return None, None, "ATR unavailable for ATR-based stop-loss"
        stop = entry - rs.stop_loss_atr_mult * atr
    else:
        stop = entry * (1 - rs.stop_loss_pct / HUNDRED)
    stop = round_step(stop, f.tick_size, ROUND_DOWN)
    if stop <= 0 or stop >= entry:
        return None, None, f"invalid stop-loss {stop} for entry {entry}"
    if rs.take_profit_mode == "RR":
        target = entry + rs.take_profit_rr * (entry - stop)
    else:
        target = entry * (1 + rs.take_profit_pct / HUNDRED)
    target = round_step(target, f.tick_size, ROUND_UP)
    return stop, target, None


def evaluate_entry(req: EntryRequest, acct: AccountSnapshot, rs: RiskSettings, ex: ExecutionSettings) -> RiskDecision:
    d = RiskDecision(approved=False, symbol=req.symbol, entry_price=req.entry_price)
    rej = d.reasons

    # ---- Hard limits -----------------------------------------------------------
    if acct.halted:
        rej.append(f"trading halted: {acct.halt_reason or 'hard limit'} (explicit reset required)")
    breach = check_hard_limits(acct, rs)
    if breach:
        d.hard_breach = breach[0]
        rej.append(breach[1])

    # ---- Soft limits -------------------------------------------------------------
    if req.data_stale:
        rej.append("market data is stale")
    if not req.connectivity_ok:
        rej.append("exchange connectivity degraded")
    if req.has_open_position:
        rej.append(f"position already open for {req.symbol}")
    if req.has_pending_order:
        rej.append(f"order already pending for {req.symbol}")
    if acct.open_positions >= rs.max_open_positions:
        rej.append(f"max open positions reached ({acct.open_positions}/{rs.max_open_positions})")
    if acct.consecutive_losses >= rs.max_consecutive_losses and acct.last_loss_at is not None:
        until = acct.last_loss_at + timedelta(minutes=rs.cooldown_minutes)
        if req.now < until:
            rej.append(f"{acct.consecutive_losses} consecutive losses: cooldown until {until.isoformat()}")
    if req.symbol_last_loss_at is not None:
        until = req.symbol_last_loss_at + timedelta(minutes=rs.cooldown_minutes)
        if req.now < until:
            rej.append(f"cooldown after loss on {req.symbol} until {until.isoformat()}")
    if not req.filters.tradable:
        rej.append(f"{req.symbol} not tradable on exchange")
    if req.entry_price <= 0:
        rej.append("invalid entry price")
    if req.atr is not None and req.entry_price > 0:
        atr_pct = req.atr / req.entry_price * HUNDRED
        if atr_pct > rs.max_atr_pct:
            rej.append(f"volatility too high: ATR {atr_pct:.2f}% > {rs.max_atr_pct}%")
    if acct.equity <= 0:
        rej.append("non-positive equity")
    if rej:
        return d

    # ---- Stop-loss / take-profit -------------------------------------------------
    entry = req.entry_price
    stop, target, err = stop_and_target(entry, req.atr, rs, req.filters)
    if err:
        rej.append(err)
        return d
    assert stop is not None and target is not None
    cost_rate = ex.estimated_fee_rate + ex.estimated_slippage_bps / BPS
    cost_per_unit = entry * cost_rate + stop * cost_rate  # entry + exit costs
    risk_per_unit = (entry - stop) + cost_per_unit
    reward_per_unit = (target - entry) - (entry * cost_rate + target * cost_rate)
    rr = reward_per_unit / risk_per_unit
    d.stop_loss, d.take_profit, d.reward_risk = stop, target, rr.quantize(Decimal("0.01"))
    if rr < rs.min_reward_risk:
        rej.append(f"reward/risk after costs {rr:.2f} below minimum {rs.min_reward_risk}")
        return d

    # ---- Position sizing ---------------------------------------------------------
    risk_amount = acct.equity * rs.risk_per_trade_pct / HUNDRED
    qty = risk_amount / risk_per_unit
    caps = {
        "max allocation per position": acct.equity * rs.max_position_alloc_pct / HUNDRED / entry,
        "max total allocation": (acct.equity * rs.max_total_alloc_pct / HUNDRED - acct.invested) / entry,
        "available balance": acct.cash / (entry * (1 + cost_rate)),
        "max order size": rs.max_order_notional_usdt / entry,
        "exchange max quantity": req.filters.market_max_qty if req.market_order else req.filters.max_qty,
    }
    for label, cap in caps.items():
        if cap < qty:
            d.adjustments.append(f"quantity capped by {label}")
            qty = cap
    if qty <= 0:
        rej.append("no capacity: allocation/balance limits leave no room for a new position")
        return d
    qty = req.filters.round_qty(qty, market=req.market_order)
    errors = req.filters.validate(qty, entry, market=req.market_order)
    if errors:
        rej.append("exchange filters: " + "; ".join(errors))
        return d

    d.quantity = qty
    d.notional = (qty * entry).quantize(Decimal("0.00000001"))
    d.risk_amount = (qty * risk_per_unit).quantize(Decimal("0.00000001"))
    d.approved = True
    return d


def update_trailing_stop(stop: Decimal, highest: Decimal, trailing_pct: Decimal, tick: Decimal) -> Decimal:
    """Trailing stop only ever moves UP."""
    candidate = round_step(highest * (1 - trailing_pct / HUNDRED), tick, ROUND_DOWN)
    return max(stop, candidate)


def exit_trigger(price: Decimal, stop: Decimal | None, target: Decimal | None) -> str | None:
    """Spot long exits: price at/below stop -> STOP_LOSS; at/above target -> TAKE_PROFIT."""
    if stop is not None and price <= stop:
        return "STOP_LOSS"
    if target is not None and price >= target:
        return "TAKE_PROFIT"
    return None

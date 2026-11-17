"""SQLAlchemy ORM models.

All monetary / quantity columns use the exact ``Money`` type (Decimal); no floats
are used for balances, prices, quantities or accounting.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, Money, UTCDateTime, utcnow


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SystemSetting(Base):
    __tablename__ = "system_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    setting_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    setting_value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class TradingPair(Base):
    __tablename__ = "trading_pairs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    base_asset: Mapped[str] = mapped_column(String(32))
    quote_asset: Mapped[str] = mapped_column(String(16))
    market_rank: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    testnet_supported: Mapped[bool] = mapped_column(Boolean, default=False)
    quote_volume_24h: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    price_change_pct_24h: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    last_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    filters: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    exclusion_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class MarketCandle(Base):
    __tablename__ = "market_candles"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "open_time", name="uq_candle_symbol_tf_open"),
        Index("ix_candle_symbol_tf_time", "symbol", "timeframe", "open_time"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(8))
    open_time: Mapped[datetime] = mapped_column(UTCDateTime)
    close_time: Mapped[datetime] = mapped_column(UTCDateTime)
    open_price: Mapped[Decimal] = mapped_column(Money)
    high_price: Mapped[Decimal] = mapped_column(Money)
    low_price: Mapped[Decimal] = mapped_column(Money)
    close_price: Mapped[Decimal] = mapped_column(Money)
    volume: Mapped[Decimal] = mapped_column(Money)
    quote_volume: Mapped[Decimal] = mapped_column(Money)
    trade_count: Mapped[int] = mapped_column(Integer, default=0)


class MarketIndicator(Base):
    __tablename__ = "market_indicators"
    __table_args__ = (UniqueConstraint("symbol", "timeframe", "candle_time", name="uq_ind_symbol_tf_time"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(8))
    candle_time: Mapped[datetime] = mapped_column(UTCDateTime)
    rsi: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    ema_9: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    ema_20: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    ema_50: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    ema_200: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    macd: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    macd_signal: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    bollinger_upper: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    bollinger_lower: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    atr: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    adx: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    volume_ma: Mapped[Decimal | None] = mapped_column(Money, nullable=True)


class AISignal(Base):
    __tablename__ = "ai_signals"
    __table_args__ = (Index("ix_signal_symbol_created", "symbol", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    signal: Mapped[str] = mapped_column(String(8))  # BUY | SELL | HOLD
    confidence_score: Mapped[Decimal] = mapped_column(Money)
    strategy_name: Mapped[str] = mapped_column(String(64))
    indicator_snapshot: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(Text)
    candle_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    action_status: Mapped[str] = mapped_column(String(48), default="NONE")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AccountState(Base):
    """Per-execution-mode ledger and risk state (one row per mode)."""

    __tablename__ = "account_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    execution_mode: Mapped[str] = mapped_column(String(16), unique=True)
    quote_asset: Mapped[str] = mapped_column(String(16), default="USDT")
    initial_balance: Mapped[Decimal] = mapped_column(Money)
    cash_balance: Mapped[Decimal] = mapped_column(Money)
    total_fees: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    realised_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    peak_equity: Mapped[Decimal] = mapped_column(Money)
    day_start_equity: Mapped[Decimal] = mapped_column(Money)
    day_start_date: Mapped[str] = mapped_column(String(10))
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0)
    last_loss_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    halted: Mapped[bool] = mapped_column(Boolean, default=False)
    halt_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    halted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class TradingOrder(Base):
    __tablename__ = "trading_orders"
    __table_args__ = (
        UniqueConstraint("execution_mode", "idempotency_key", name="uq_order_idempotency"),
        Index("ix_order_symbol_status", "symbol", "status"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(4))  # BUY | SELL
    order_type: Mapped[str] = mapped_column(String(16))  # MARKET | LIMIT
    quantity: Mapped[Decimal] = mapped_column(Money)
    price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    filled_quantity: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    average_fill_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    cumulative_quote_qty: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    fee_quote: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    expected_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    slippage_bps: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    # NEW, SUBMITTING, UNKNOWN, OPEN, PARTIALLY_FILLED, FILLED, CANCELED, REJECTED, EXPIRED
    status: Mapped[str] = mapped_column(String(24), index=True)
    execution_mode: Mapped[str] = mapped_column(String(16), index=True)
    purpose: Mapped[str] = mapped_column(String(16))  # ENTRY | EXIT
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"), nullable=True, index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("ai_signals.id"), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    order_meta: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    fills: Mapped[list["OrderFill"]] = relationship(back_populates="order", cascade="all, delete-orphan")


class OrderFill(Base):
    """Individual executions. The unique fill id makes fill application idempotent."""

    __tablename__ = "order_fills"
    __table_args__ = (UniqueConstraint("order_id", "fill_id", name="uq_fill_order_fillid"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("trading_orders.id"), index=True)
    fill_id: Mapped[str] = mapped_column(String(64))
    price: Mapped[Decimal] = mapped_column(Money)
    quantity: Mapped[Decimal] = mapped_column(Money)
    fee: Mapped[Decimal] = mapped_column(Money)
    fee_asset: Mapped[str] = mapped_column(String(16))
    fee_quote: Mapped[Decimal] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    order: Mapped[TradingOrder] = relationship(back_populates="fills")


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        # At most ONE open position per symbol per execution mode (DB-level guard
        # against duplicate entries).
        Index(
            "uq_position_open_symbol_mode",
            "symbol",
            "execution_mode",
            unique=True,
            postgresql_where=text("status IN ('OPEN', 'CLOSING')"),
            sqlite_where=text("status IN ('OPEN', 'CLOSING')"),
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    execution_mode: Mapped[str] = mapped_column(String(16), index=True)
    quantity: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    average_entry_price: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    cost_basis: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))  # qty*entry for open qty
    entry_fees_open: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))  # entry fees on open qty
    current_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    stop_loss: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    trailing_stop_pct: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    highest_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    unrealised_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    realised_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(16), default="OPEN", index=True)  # OPEN | CLOSING | CLOSED
    exit_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    strategy_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Trade(Base):
    """A round trip (entry -> exit). Created on the first entry fill."""

    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("trading_orders.id"), index=True)
    exit_order_id: Mapped[int | None] = mapped_column(ForeignKey("trading_orders.id"), nullable=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"), unique=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(4), default="BUY")  # entry side (spot long only)
    entry_price: Mapped[Decimal] = mapped_column(Money)
    exit_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Money)
    entry_fee: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    exit_fee: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    realised_pnl: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    exit_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    strategy_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_mode: Mapped[str] = mapped_column(String(16), index=True)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    execution_mode: Mapped[str] = mapped_column(String(16), index=True)
    total_equity: Mapped[Decimal] = mapped_column(Money)
    available_balance: Mapped[Decimal] = mapped_column(Money)
    invested_capital: Mapped[Decimal] = mapped_column(Money)
    realised_pnl: Mapped[Decimal] = mapped_column(Money)
    unrealised_pnl: Mapped[Decimal] = mapped_column(Money)
    drawdown: Mapped[Decimal] = mapped_column(Money)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class RiskEvent(Base):
    __tablename__ = "risk_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16))  # INFO | WARNING | CRITICAL
    description: Mapped[str] = mapped_column(Text)
    event_meta: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class SystemLog(Base):
    __tablename__ = "system_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    level: Mapped[str] = mapped_column(String(16), index=True)
    component: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text)
    log_meta: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(8))
    strategy_name: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")  # PENDING|RUNNING|DONE|FAILED
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    equity_curve: Mapped[list | None] = mapped_column(JSON, nullable=True)
    trades: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

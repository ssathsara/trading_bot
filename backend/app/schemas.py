"""Pydantic request/response schemas. Decimals serialise as JSON strings (no precision loss)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.settings_store import ExecutionSettings, MarketSettings, RiskSettings, StrategySettings

T = TypeVar("T")


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


class ErrorResponse(BaseModel):
    error: str
    detail: Any = None


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_minutes: int


class UserOut(ORM):
    id: int
    username: str
    email: str
    created_at: datetime


class OrderOut(ORM):
    id: int
    client_order_id: str
    exchange_order_id: str | None
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None
    filled_quantity: Decimal
    average_fill_price: Decimal | None
    fee_quote: Decimal
    slippage_bps: Decimal | None
    status: str
    execution_mode: str
    purpose: str
    position_id: int | None
    reason: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class FillOut(ORM):
    fill_id: str
    price: Decimal
    quantity: Decimal
    fee: Decimal
    fee_asset: str
    fee_quote: Decimal
    created_at: datetime


class OrderDetail(OrderOut):
    fills: list[FillOut]


class PositionOut(ORM):
    id: int
    symbol: str
    execution_mode: str
    quantity: Decimal
    average_entry_price: Decimal
    current_price: Decimal | None
    stop_loss: Decimal | None
    take_profit: Decimal | None
    trailing_stop_pct: Decimal | None
    unrealised_pnl: Decimal
    realised_pnl: Decimal
    cost_basis: Decimal
    entry_fees_open: Decimal
    status: str
    exit_reason: str | None
    strategy_name: str | None
    opened_at: datetime
    closed_at: datetime | None
    invested_amount: Decimal | None = None
    duration_seconds: int | None = None


class TradeOut(ORM):
    id: int
    symbol: str
    side: str
    entry_price: Decimal
    exit_price: Decimal | None
    quantity: Decimal
    entry_fee: Decimal
    exit_fee: Decimal
    realised_pnl: Decimal | None
    status: str
    exit_reason: str | None
    strategy_name: str | None
    execution_mode: str
    opened_at: datetime
    closed_at: datetime | None


class SignalOut(ORM):
    id: int
    symbol: str
    signal: str
    confidence_score: Decimal
    strategy_name: str
    reason: str
    action_status: str
    candle_time: datetime | None
    created_at: datetime


class SignalDetail(SignalOut):
    indicator_snapshot: dict


class SnapshotOut(ORM):
    id: int
    execution_mode: str
    total_equity: Decimal
    available_balance: Decimal
    invested_capital: Decimal
    realised_pnl: Decimal
    unrealised_pnl: Decimal
    drawdown: Decimal
    recorded_at: datetime


class LogOut(ORM):
    id: int
    level: str
    component: str
    message: str
    log_meta: dict | None = Field(default=None, serialization_alias="metadata")
    created_at: datetime


class RiskEventOut(ORM):
    id: int
    event_type: str
    severity: str
    description: str
    event_meta: dict | None = Field(default=None, serialization_alias="metadata")
    created_at: datetime


class SettingsBundle(BaseModel):
    risk: RiskSettings
    strategy: StrategySettings
    market: MarketSettings
    execution: ExecutionSettings
    execution_mode: Literal["PAPER", "TESTNET", "LIVE"]
    auto_trading: bool
    state: str
    testnet_configured: bool
    live_trading_available: bool = False
    live_max_capital_usdt: Decimal | None = None


class SettingsUpdate(BaseModel):
    strategy: StrategySettings | None = None
    market: MarketSettings | None = None
    execution: ExecutionSettings | None = None
    auto_trading: bool | None = None
    execution_mode: Literal["PAPER", "TESTNET", "LIVE"] | None = None
    # Switching to LIVE (real money) requires this exact phrase.
    confirm_live: str | None = Field(None, max_length=64)


LIVE_CONFIRMATION_PHRASE = "I UNDERSTAND THIS USES REAL MONEY"


class BacktestRequest(BaseModel):
    symbol: str = Field(pattern=r"^[A-Z0-9]{2,20}USDT$")
    timeframe: Literal["5m", "15m", "1h", "4h"] = "15m"
    start: datetime
    end: datetime
    initial_balance: Decimal = Field(Decimal("10000"), gt=0, le=Decimal("100000000"))
    fee_rate: Decimal = Field(Decimal("0.001"), ge=0, le=Decimal("0.01"))
    slippage_bps: Decimal = Field(Decimal("5"), ge=0, le=500)
    risk_pct: Decimal = Field(Decimal("0.5"), gt=0, le=5)
    strategy: StrategySettings | None = None

    @field_validator("start", "end")
    @classmethod
    def _assume_utc(cls, v: datetime) -> datetime:
        """Timezone-less timestamps are interpreted as UTC (all candle times are UTC)."""
        return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v.astimezone(timezone.utc)


class BacktestOut(ORM):
    id: int
    symbol: str
    timeframe: str
    strategy_name: str
    params: dict
    status: str
    metrics: dict | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class BacktestDetail(BacktestOut):
    equity_curve: list | None
    trades: list | None


class MLTrainRequest(BaseModel):
    timeframe: Literal["5m", "15m", "1h"] = "15m"
    candles: int = Field(3000, ge=1000, le=10000)
    model: Literal["gradient_boosting", "random_forest"] = "gradient_boosting"
    horizon: int = Field(12, ge=1, le=96)

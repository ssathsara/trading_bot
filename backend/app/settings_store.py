"""User-configurable runtime settings persisted in ``system_settings``.

Every group is a Pydantic model with conservative defaults and validation, so an
invalid value can never be persisted.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal, TypeVar

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SystemSetting

D = Decimal


class RiskSettings(BaseModel):
    risk_per_trade_pct: Decimal = Field(D("0.5"), gt=0, le=5)
    max_open_positions: int = Field(3, ge=1, le=20)
    max_daily_loss_pct: Decimal = Field(D("2"), gt=0, le=50)
    max_drawdown_pct: Decimal = Field(D("10"), gt=0, le=90)
    max_position_alloc_pct: Decimal = Field(D("20"), gt=0, le=100)
    max_total_alloc_pct: Decimal = Field(D("60"), gt=0, le=100)
    min_reward_risk: Decimal = Field(D("1.5"), ge=D("0.5"), le=20)
    stop_loss_mode: Literal["ATR", "PERCENT"] = "ATR"
    stop_loss_atr_mult: Decimal = Field(D("2"), gt=0, le=20)
    stop_loss_pct: Decimal = Field(D("2"), gt=0, le=50)
    take_profit_mode: Literal["RR", "PERCENT"] = "RR"
    take_profit_rr: Decimal = Field(D("2"), gt=0, le=20)
    take_profit_pct: Decimal = Field(D("4"), gt=0, le=200)
    trailing_stop_enabled: bool = False
    trailing_stop_pct: Decimal = Field(D("1.5"), gt=0, le=50)
    # Dynamic exits: re-evaluate each open position's stop-loss / take-profit on every
    # closed entry-timeframe candle, from that symbol's own ATR / ADX / EMA20 / RSI.
    dynamic_exits_enabled: bool = True
    dyn_trail_atr_mult: Decimal = Field(D("2.5"), gt=0, le=20)      # stop trails highest price by N x ATR
    dyn_breakeven_atr_mult: Decimal = Field(D("2"), gt=0, le=20)    # move stop to break-even after +N x ATR
    dyn_tp_extend_adx: Decimal = Field(D("25"), ge=0, le=100)       # trend this strong -> raise take-profit
    dyn_tp_extend_atr_mult: Decimal = Field(D("3"), gt=0, le=20)    # ... to close + N x ATR
    dyn_tp_tighten_rsi: Decimal = Field(D("75"), gt=50, le=100)     # overbought (or close < EMA20) -> lower take-profit
    dyn_tp_tighten_atr_mult: Decimal = Field(D("1"), gt=0, le=20)   # ... to close + N x ATR
    max_consecutive_losses: int = Field(3, ge=1, le=50)
    cooldown_minutes: int = Field(60, ge=0, le=10080)
    # wait this long after ANY exit on a symbol before buying it again (0 = off)
    reentry_cooldown_minutes: int = Field(0, ge=0, le=10080)
    max_order_notional_usdt: Decimal = Field(D("2500"), gt=0)
    max_atr_pct: Decimal = Field(D("5"), gt=0, le=100)  # volatility protection (ATR as % of price)
    stale_data_seconds: int = Field(30, ge=5, le=600)

    @model_validator(mode="after")
    def _check(self) -> "RiskSettings":
        if self.max_position_alloc_pct > self.max_total_alloc_pct:
            raise ValueError("max_position_alloc_pct cannot exceed max_total_alloc_pct")
        if self.take_profit_mode == "RR" and self.take_profit_rr < self.min_reward_risk:
            raise ValueError("take_profit_rr must be >= min_reward_risk")
        return self


class StrategyWeights(BaseModel):
    trend: Decimal = D("30")
    momentum: Decimal = D("25")
    volume: Decimal = D("15")
    rsi: Decimal = D("10")
    macd: Decimal = D("10")
    volatility: Decimal = D("10")

    @model_validator(mode="after")
    def _sum(self) -> "StrategyWeights":
        total = self.trend + self.momentum + self.volume + self.rsi + self.macd + self.volatility
        if total != D("100") or min(self.trend, self.momentum, self.volume, self.rsi, self.macd, self.volatility) < 0:
            raise ValueError("strategy weights must be non-negative and sum to 100")
        return self


class StrategySettings(BaseModel):
    name: str = "trend_following_v1"
    trend_timeframe: Literal["15m", "1h", "4h", "1d"] = "1h"
    setup_timeframe: Literal["5m", "15m", "1h"] = "15m"
    entry_timeframe: Literal["1m", "5m", "15m", "1h"] = "5m"
    rsi_entry_min: Decimal = Field(D("45"), ge=0, le=100)
    rsi_entry_max: Decimal = Field(D("70"), ge=0, le=100)
    min_adx: Decimal = Field(D("20"), ge=0, le=100)
    min_volume_ratio: Decimal = Field(D("1.0"), ge=0, le=20)
    min_score: Decimal = Field(D("65"), ge=0, le=100)
    weights: StrategyWeights = StrategyWeights()
    exit_on_trend_reversal: bool = True
    ml_filter_enabled: bool = False
    ml_min_probability: Decimal = Field(D("0.55"), ge=0, le=1)
    # Market-regime filter: no new long entries while the market leader (BTC) is in a
    # downtrend on the trend timeframe (close < EMA50 and EMA20 < EMA50). Exits unaffected.
    market_filter_enabled: bool = True
    market_filter_symbol: str = Field("BTCUSDT", pattern=r"^[A-Z0-9]{2,20}USDT$")

    @model_validator(mode="after")
    def _check(self) -> "StrategySettings":
        if self.rsi_entry_min >= self.rsi_entry_max:
            raise ValueError("rsi_entry_min must be < rsi_entry_max")
        return self


class MarketSettings(BaseModel):
    top_n: int = Field(10, ge=0, le=1000)  # 0 = every eligible pair
    # Liquidity floor: thin markets have wide spreads and slippage far above the
    # risk engine's estimates. 0 disables the floor.
    min_quote_volume_usdt: Decimal = Field(D("1000000"), ge=0)
    quote_asset: Literal["USDT"] = "USDT"
    ranking_refresh_minutes: int = Field(60, ge=5, le=1440)
    min_history_candles: int = Field(250, ge=50, le=1000)
    history_candles: int = Field(300, ge=100, le=1000)
    excluded_symbols: list[str] = []
    pinned_symbols: list[str] = []  # always in the universe (if eligible), on top of the top-N
    # Binance lists tokenized equities/ETFs (e.g. NVDAB, SOXLB - some leveraged) as ordinary
    # spot pairs with no documented flag. Empirically they all sit in this permission group.
    # It is an observation, not an API contract - review periodically.
    excluded_permission_groups: list[str] = ["TRD_GRP_261"]
    selected_symbols: list[str] = []  # empty = all top-N; else restrict trading to these


class ExecutionSettings(BaseModel):
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    allow_market_orders: bool = True
    limit_offset_bps: Decimal = Field(D("5"), ge=0, le=500)
    limit_timeout_seconds: int = Field(120, ge=10, le=3600)
    paper_fee_rate: Decimal = Field(D("0.001"), ge=0, le=D("0.01"))
    paper_slippage_bps: Decimal = Field(D("5"), ge=0, le=500)
    estimated_fee_rate: Decimal = Field(D("0.001"), ge=0, le=D("0.01"))
    estimated_slippage_bps: Decimal = Field(D("5"), ge=0, le=500)
    # TESTNET / LIVE: keep a resting stop-loss order on the exchange for every open position, so
    # the position stays protected while the bot is offline. It sits this far below the
    # bot's own stop, so the bot normally exits first and the exchange stop is the backstop.
    exchange_stop_enabled: bool = True
    exchange_stop_buffer_pct: Decimal = Field(D("0.5"), ge=0, le=10)

    @model_validator(mode="after")
    def _check(self) -> "ExecutionSettings":
        if self.order_type == "MARKET" and not self.allow_market_orders:
            raise ValueError("order_type MARKET requires allow_market_orders=true")
        return self


class TradingControl(BaseModel):
    """Desired worker state, written by the API and obeyed by the worker."""

    state: Literal["STOPPED", "RUNNING", "PAUSED"] = "STOPPED"
    auto_trading: bool = True
    execution_mode: Literal["PAPER", "TESTNET", "LIVE"] = "PAPER"


GROUPS: dict[str, type[BaseModel]] = {
    "risk": RiskSettings,
    "strategy": StrategySettings,
    "market": MarketSettings,
    "execution": ExecutionSettings,
    "control": TradingControl,
}

T = TypeVar("T", bound=BaseModel)


def load(session: Session, key: str, model: type[T]) -> T:
    row = session.scalar(select(SystemSetting).where(SystemSetting.setting_key == key))
    if row is None or row.setting_value is None:
        return model()
    return model.model_validate(row.setting_value)


def save(session: Session, key: str, value: BaseModel) -> None:
    payload = value.model_dump(mode="json")
    row = session.scalar(select(SystemSetting).where(SystemSetting.setting_key == key))
    if row is None:
        session.add(SystemSetting(setting_key=key, setting_value=payload))
    else:
        row.setting_value = payload
    session.flush()


def get_risk(s: Session) -> RiskSettings:
    return load(s, "risk", RiskSettings)


def get_strategy(s: Session) -> StrategySettings:
    return load(s, "strategy", StrategySettings)


def get_market(s: Session) -> MarketSettings:
    return load(s, "market", MarketSettings)


def get_execution(s: Session) -> ExecutionSettings:
    return load(s, "execution", ExecutionSettings)


def get_control(s: Session) -> TradingControl:
    return load(s, "control", TradingControl)

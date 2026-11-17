"""Binance symbol trading filters (PRICE_FILTER, LOT_SIZE, MARKET_LOT_SIZE, NOTIONAL/MIN_NOTIONAL)."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal

ZERO = Decimal("0")


def _d(v: object, default: str = "0") -> Decimal:
    return Decimal(str(v)) if v not in (None, "") else Decimal(default)


def round_step(value: Decimal, step: Decimal, rounding: str = ROUND_DOWN) -> Decimal:
    """Round ``value`` to a multiple of ``step`` (no-op if step is 0)."""
    if step <= 0:
        return value
    return ((value / step).to_integral_value(rounding=rounding) * step).normalize()


@dataclass(frozen=True)
class SymbolFilters:
    symbol: str
    base_asset: str
    quote_asset: str
    status: str
    spot_allowed: bool
    order_types: tuple[str, ...]
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    market_step_size: Decimal
    market_min_qty: Decimal
    market_max_qty: Decimal
    min_notional: Decimal
    max_notional: Decimal
    apply_min_to_market: bool
    base_precision: int
    quote_precision: int

    @property
    def tradable(self) -> bool:
        return self.status == "TRADING" and self.spot_allowed

    @classmethod
    def from_exchange_info(cls, s: dict) -> "SymbolFilters":
        f = {x["filterType"]: x for x in s.get("filters", [])}
        price = f.get("PRICE_FILTER", {})
        lot = f.get("LOT_SIZE", {})
        mlot = f.get("MARKET_LOT_SIZE", {})
        notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
        perms = set(s.get("permissions") or [])
        for ps in s.get("permissionSets") or []:
            perms.update(ps)
        spot_allowed = bool(s.get("isSpotTradingAllowed", "SPOT" in perms))
        step = _d(lot.get("stepSize"))
        mstep = _d(mlot.get("stepSize"))
        return cls(
            symbol=s["symbol"],
            base_asset=s["baseAsset"],
            quote_asset=s["quoteAsset"],
            status=s.get("status", "UNKNOWN"),
            spot_allowed=spot_allowed,
            order_types=tuple(s.get("orderTypes", [])),
            tick_size=_d(price.get("tickSize")),
            min_price=_d(price.get("minPrice")),
            max_price=_d(price.get("maxPrice")),
            step_size=step,
            min_qty=_d(lot.get("minQty")),
            max_qty=_d(lot.get("maxQty"), "1e30"),
            # A zero MARKET_LOT_SIZE step means "use LOT_SIZE".
            market_step_size=mstep if mstep > 0 else step,
            market_min_qty=_d(mlot.get("minQty")),
            market_max_qty=_d(mlot.get("maxQty"), "0") or _d(lot.get("maxQty"), "1e30"),
            min_notional=_d(notional.get("minNotional")),
            max_notional=_d(notional.get("maxNotional"), "1e30"),
            apply_min_to_market=bool(notional.get("applyMinToMarket", True)),
            base_precision=int(s.get("baseAssetPrecision", 8)),
            quote_precision=int(s.get("quoteAssetPrecision", s.get("quotePrecision", 8))),
        )

    def to_json(self) -> dict:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}

    @classmethod
    def from_json(cls, d: dict) -> "SymbolFilters":
        kwargs = {}
        for k, v in d.items():
            if k in cls.__dataclass_fields__:
                t = cls.__dataclass_fields__[k].type
                kwargs[k] = Decimal(v) if t == "Decimal" else (tuple(v) if k == "order_types" else v)
        return cls(**kwargs)

    # ------------------------------------------------------------------ helpers
    def round_qty(self, qty: Decimal, market: bool = True) -> Decimal:
        return round_step(qty, self.market_step_size if market else self.step_size, ROUND_DOWN)

    def round_price(self, price: Decimal, side: str) -> Decimal:
        # Buy limits round down (never pay more), sell limits round up (never sell lower).
        return round_step(price, self.tick_size, ROUND_DOWN if side == "BUY" else ROUND_UP)

    def validate(self, qty: Decimal, price: Decimal, market: bool) -> list[str]:
        """Return a list of filter violations (empty = valid)."""
        errors: list[str] = []
        if not self.tradable:
            errors.append(f"{self.symbol} is not tradable (status={self.status})")
        if market and "MARKET" not in self.order_types:
            errors.append("MARKET orders not supported for this symbol")
        if not market and "LIMIT" not in self.order_types:
            errors.append("LIMIT orders not supported for this symbol")
        if qty <= 0:
            errors.append("quantity must be positive")
            return errors
        step = self.market_step_size if market else self.step_size
        min_q = max(self.min_qty, self.market_min_qty if market else ZERO)
        max_q = min(self.max_qty, self.market_max_qty) if market else self.max_qty
        if qty < min_q:
            errors.append(f"quantity {qty} below minQty {min_q}")
        if qty > max_q:
            errors.append(f"quantity {qty} above maxQty {max_q}")
        if step > 0 and (qty / step) % 1 != 0:
            errors.append(f"quantity {qty} not a multiple of stepSize {step}")
        if not market:
            if self.tick_size > 0 and (price / self.tick_size) % 1 != 0:
                errors.append(f"price {price} not a multiple of tickSize {self.tick_size}")
            if price < self.min_price or (self.max_price > 0 and price > self.max_price):
                errors.append(f"price {price} outside [{self.min_price}, {self.max_price}]")
        notional = qty * price
        if (not market or self.apply_min_to_market) and notional < self.min_notional:
            errors.append(f"notional {notional:.8f} below minNotional {self.min_notional}")
        if notional > self.max_notional and not market:
            errors.append(f"notional {notional:.8f} above maxNotional {self.max_notional}")
        return errors

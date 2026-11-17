"""In-memory latest-price cache with explicit staleness tracking."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class Quote:
    symbol: str
    last: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    bid_qty: Decimal | None = None
    ask_qty: Decimal | None = None
    change_pct: Decimal | None = None
    quote_volume: Decimal | None = None
    updated_at: float = field(default_factory=time.time)  # wall clock (seconds)

    @property
    def mid(self) -> Decimal | None:
        if self.bid and self.ask:
            return (self.bid + self.ask) / 2
        return self.last

    def age(self, now: float | None = None) -> float:
        return (now or time.time()) - self.updated_at


class PriceCache:
    def __init__(self) -> None:
        self._q: dict[str, Quote] = {}
        self._lock = threading.Lock()

    def update(self, symbol: str, **fields: Decimal | None) -> Quote:
        with self._lock:
            q = self._q.get(symbol) or Quote(symbol)
            for k, v in fields.items():
                if v is not None:
                    setattr(q, k, v)
            q.updated_at = time.time()
            self._q[symbol] = q
            return q

    def get(self, symbol: str) -> Quote | None:
        with self._lock:
            return self._q.get(symbol)

    def price(self, symbol: str, max_age: float | None = None) -> Decimal | None:
        """Best available price, or None if missing or older than ``max_age`` seconds."""
        q = self.get(symbol)
        if q is None or (max_age is not None and q.age() > max_age):
            return None
        return q.mid

    def is_stale(self, symbol: str, max_age: float) -> bool:
        q = self.get(symbol)
        return q is None or q.age() > max_age

    def snapshot(self) -> dict[str, Quote]:
        with self._lock:
            return dict(self._q)


price_cache = PriceCache()

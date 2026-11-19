"""Dynamic discovery of the top-N eligible Binance Spot pairs.

The ranking function is pure (no I/O) so it can be unit-tested with fixtures.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from app.exchange.filters import SymbolFilters

STABLECOINS = {
    "USDT", "USDC", "BUSD", "TUSD", "FDUSD", "DAI", "USDP", "PAX", "PYUSD", "USDD", "UST", "USTC", "GUSD",
    "SUSD", "LUSD", "FRAX", "EURC", "EURI", "AEUR", "USD1", "RLUSD", "USDE", "XUSD", "BFUSD", "USDS",
    "PAXG", "XAUT", "EUR", "GBP", "TRY", "BRL", "AUD", "RUB", "UAH", "NGN", "ZAR", "IDRT", "BIDR", "BVND",
    "ARS", "JPY", "MXN", "PLN", "RON", "CZK", "COP", "U",
}
_LEVERAGED_MAJORS = {"BTC", "ETH", "BNB", "ADA", "XRP", "DOT", "LINK", "TRX", "LTC", "EOS", "XTZ", "FIL", "SXP",
                     "YFI", "UNI", "AAVE", "SUSHI", "XLM", "1INCH", "BCH"}
_LEVERAGED_SUFFIX = re.compile(r"^(?P<base>[A-Z0-9]+?)(UP|DOWN|BULL|BEAR)$")

COIN_NAMES = {
    "BTC": "Bitcoin", "ETH": "Ethereum", "BNB": "BNB", "SOL": "Solana", "XRP": "XRP", "DOGE": "Dogecoin",
    "ADA": "Cardano", "TRX": "TRON", "AVAX": "Avalanche", "LINK": "Chainlink", "DOT": "Polkadot",
    "LTC": "Litecoin", "BCH": "Bitcoin Cash", "SHIB": "Shiba Inu", "PEPE": "Pepe", "SUI": "Sui",
    "TON": "Toncoin", "NEAR": "NEAR Protocol", "APT": "Aptos", "UNI": "Uniswap", "ARB": "Arbitrum",
    "OP": "Optimism", "FIL": "Filecoin", "ATOM": "Cosmos", "ETC": "Ethereum Classic", "XLM": "Stellar",
    "WIF": "dogwifhat", "FET": "Fetch.ai", "AAVE": "Aave", "ENA": "Ethena", "TAO": "Bittensor",
    "HBAR": "Hedera", "ICP": "Internet Computer", "INJ": "Injective", "SEI": "Sei", "TIA": "Celestia",
    "BONK": "Bonk", "WLD": "Worldcoin", "RENDER": "Render", "ZEC": "Zcash", "POL": "Polygon",
}


def coin_name(base: str) -> str:
    return COIN_NAMES.get(base, base)


def is_stablecoin(base: str) -> bool:
    return base in STABLECOINS or (base.startswith("USD") and len(base) <= 6) or base.endswith("USD")


def is_leveraged_token(base: str) -> bool:
    m = _LEVERAGED_SUFFIX.match(base)
    if not m:
        return False
    return base.endswith(("DOWN", "BULL", "BEAR")) or m.group("base") in _LEVERAGED_MAJORS


@dataclass
class PairCandidate:
    symbol: str
    base_asset: str
    quote_asset: str
    last_price: Decimal
    change_pct: Decimal
    quote_volume: Decimal
    filters: SymbolFilters
    eligible: bool = True
    exclusion_reason: str | None = None
    rank: int | None = None
    testnet_supported: bool = False
    name: str = field(default="")


def rank_pairs(
    exchange_symbols: list[dict],
    tickers: list[dict],
    *,
    quote_asset: str = "USDT",
    top_n: int = 10,
    max_min_notional: Decimal = Decimal("50"),
    excluded: set[str] | None = None,
    testnet_symbols: set[str] | None = None,
    history_ok: dict[str, bool] | None = None,
    excluded_permission_groups: set[str] | None = None,
    min_quote_volume: Decimal = Decimal("0"),
) -> tuple[list[PairCandidate], list[PairCandidate]]:
    """Rank pairs by 24h quote volume.

    Returns ``(selected, rejected)``. ``selected`` holds at most ``top_n`` eligible
    pairs with ranks 1..N (``top_n <= 0`` = no limit). Pairs whose 24h quote volume
    is below ``min_quote_volume`` are rejected as illiquid.
    Symbols failing ``history_ok`` are skipped (that is part
    of eligibility, not a substitution). Testnet availability is *reported* via
    ``testnet_supported`` but never causes a substitution.
    """
    excluded = excluded or set()
    tick = {t["symbol"]: t for t in tickers}
    candidates: list[PairCandidate] = []
    for s in exchange_symbols:
        if s.get("quoteAsset") != quote_asset:
            continue
        t = tick.get(s["symbol"])
        if t is None:
            continue
        f = SymbolFilters.from_exchange_info(s)
        c = PairCandidate(
            symbol=s["symbol"], base_asset=s["baseAsset"], quote_asset=s["quoteAsset"],
            last_price=Decimal(t.get("lastPrice", "0")), change_pct=Decimal(t.get("priceChangePercent", "0")),
            quote_volume=Decimal(t.get("quoteVolume", "0")), filters=f, name=coin_name(s["baseAsset"]),
            testnet_supported=(testnet_symbols is not None and s["symbol"] in testnet_symbols),
        )
        reason = _exclusion_reason(c, max_min_notional, excluded)
        groups = {p for ps in s.get("permissionSets") or [] for p in ps}
        if not reason and excluded_permission_groups and groups & excluded_permission_groups:
            reason = "tokenized stock/ETF (non-crypto asset)"
        if not reason and c.quote_volume < min_quote_volume:
            reason = f"low liquidity: 24h volume {c.quote_volume:.0f} < {min_quote_volume:.0f} {quote_asset}"
        if reason:
            c.eligible, c.exclusion_reason = False, reason
        candidates.append(c)

    candidates.sort(key=lambda c: c.quote_volume, reverse=True)
    selected: list[PairCandidate] = []
    rejected: list[PairCandidate] = []
    for c in candidates:
        if c.eligible and history_ok is not None and not history_ok.get(c.symbol, False):
            c.eligible, c.exclusion_reason = False, "insufficient historical data"
        if c.eligible and (top_n <= 0 or len(selected) < top_n):
            c.rank = len(selected) + 1
            selected.append(c)
        elif not c.eligible:
            rejected.append(c)
    return selected, rejected


def _exclusion_reason(c: PairCandidate, max_min_notional: Decimal, excluded: set[str]) -> str | None:
    f = c.filters
    if c.symbol in excluded:
        return "excluded by user"
    if f.status != "TRADING":
        return f"market status {f.status} (suspended/delisted)"
    if not f.spot_allowed:
        return "spot trading not allowed"
    if is_stablecoin(c.base_asset):
        return "stablecoin base asset"
    if is_leveraged_token(c.base_asset):
        return "leveraged token"
    if "MARKET" not in f.order_types or "LIMIT" not in f.order_types:
        return "required order types unavailable"
    if f.step_size <= 0 or f.tick_size <= 0:
        return "invalid lot/price filters"
    if c.last_price <= 0 or c.quote_volume <= 0:
        return "no recent trading activity"
    min_trade = max(f.min_notional, f.min_qty * c.last_price)
    if min_trade > max_min_notional:
        return f"minimum trade size {min_trade:.2f} {c.quote_asset} exceeds limit {max_min_notional}"
    return None

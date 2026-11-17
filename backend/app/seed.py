"""Seed data for dashboard development.

    python -m app.seed            # admin user + default settings + PAPER account
    python -m app.seed --demo     # ALSO inserts clearly-labelled DEMO trades/snapshots

Demo records are written to a separate execution mode, ``DEMO``. They never touch
the PAPER or TESTNET ledgers, so they can't distort real simulated performance.
View them via the read-only endpoints, e.g. GET /api/trading/history?mode=DEMO.
"""
from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, select

from app.config import get_settings
from app.db import init_engine, session_scope
from app.main import ensure_admin, verify_schema
from app.models import PortfolioSnapshot, Position, SystemSetting, Trade, TradingOrder
from app.portfolio import service as pf
from app.settings_store import ExecutionSettings, MarketSettings, RiskSettings, StrategySettings, TradingControl, save

DEMO = "DEMO"


def seed_defaults() -> None:
    with session_scope() as s:
        for key, model in (("risk", RiskSettings), ("strategy", StrategySettings), ("market", MarketSettings),
                           ("execution", ExecutionSettings), ("control", TradingControl)):
            if s.scalar(select(SystemSetting).where(SystemSetting.setting_key == key)) is None:
                save(s, key, model())
        pf.get_account(s, "PAPER", get_settings().initial_balance_usdt)


def seed_demo(n_trades: int = 40) -> None:
    rng = random.Random(42)
    with session_scope() as s:
        for model in (Trade, TradingOrder, Position, PortfolioSnapshot):
            s.execute(delete(model).where(model.execution_mode == DEMO))
        acct = pf.get_account(s, DEMO, Decimal("10000"))
        acct.cash_balance, acct.realised_pnl, acct.total_fees = Decimal("10000"), Decimal("0"), Decimal("0")
        now = datetime.now(timezone.utc)
        equity = Decimal("10000")
        for i in range(n_trades):
            sym = rng.choice(["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"])
            opened = now - timedelta(hours=(n_trades - i) * 6)
            entry = Decimal(str(round(rng.uniform(1, 100), 4)))
            qty = (Decimal("1000") / entry).quantize(Decimal("0.0001"))
            exit_ = (entry * Decimal(str(1 + rng.gauss(0.002, 0.02)))).quantize(Decimal("0.0001"))
            fees = (qty * (entry + exit_) * Decimal("0.001")).quantize(Decimal("0.00000001"))
            pnl = (qty * (exit_ - entry) - fees).quantize(Decimal("0.00000001"))
            o = TradingOrder(client_order_id=f"demo-{i:04d}", idempotency_key=f"demo-{i}", symbol=sym, side="BUY",
                             order_type="MARKET", quantity=qty, filled_quantity=qty, average_fill_price=entry,
                             cumulative_quote_qty=qty * entry, fee_quote=fees / 2, status="FILLED", execution_mode=DEMO,
                             purpose="ENTRY", reason="DEMO SEED DATA", created_at=opened, updated_at=opened)
            s.add(o)
            s.flush()
            p = Position(symbol=sym, execution_mode=DEMO, quantity=Decimal("0"), average_entry_price=entry,
                         cost_basis=Decimal("0"), entry_fees_open=Decimal("0"), realised_pnl=pnl, unrealised_pnl=Decimal("0"),
                         status="CLOSED", exit_reason=rng.choice(["TAKE_PROFIT", "STOP_LOSS", "STRATEGY_EXIT"]),
                         strategy_name="demo-seed", opened_at=opened, closed_at=opened + timedelta(hours=3))
            s.add(p)
            s.flush()
            s.add(Trade(order_id=o.id, position_id=p.id, symbol=sym, entry_price=entry, exit_price=exit_, quantity=qty,
                        entry_fee=fees / 2, exit_fee=fees / 2, realised_pnl=pnl, status="CLOSED", exit_reason=p.exit_reason,
                        strategy_name="demo-seed", execution_mode=DEMO, opened_at=opened, closed_at=p.closed_at))
            equity += pnl
            acct.realised_pnl += pnl
            acct.total_fees += fees
            s.add(PortfolioSnapshot(execution_mode=DEMO, total_equity=equity, available_balance=equity,
                                    invested_capital=Decimal("0"), realised_pnl=acct.realised_pnl, unrealised_pnl=Decimal("0"),
                                    drawdown=Decimal("0"), recorded_at=p.closed_at))
        acct.cash_balance = equity
        acct.peak_equity = max(equity, Decimal("10000"))
    print(f"Inserted {n_trades} DEMO trades (execution_mode={DEMO}); PAPER/TESTNET ledgers untouched.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="insert labelled DEMO trades for UI development")
    args = ap.parse_args()
    init_engine(get_settings().database_url)
    verify_schema()
    ensure_admin()
    seed_defaults()
    print("Seeded admin user, default settings and PAPER account.")
    if args.demo:
        seed_demo()


if __name__ == "__main__":
    main()

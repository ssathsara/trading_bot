"""REST API. Every route except /api/auth/login and /api/health requires a bearer token."""
from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query, Request
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import schemas as sc
from app.analysis import ml
from app.analysis.indicators import compute_indicators
from app.audit import log_event, risk_event
from app.backtest.engine import BacktestParams, run_backtest
from app.config import get_settings
from app.db import get_db, numeric, session_scope
from app.exchange.rest import BinanceAPIError, BinanceError, PublicMarketClient
from app.market import candles as cd
from app.market.discovery import coin_name
from app.models import (AccountState, AISignal, BacktestRun, MarketCandle, MarketIndicator, PortfolioSnapshot, Position,
                        RiskEvent, SystemLog, SystemSetting, Trade, TradingOrder, TradingPair, User)
from app.portfolio import service as pf
from app.security import admin_user, create_token, current_user, rate_limit, verify_password
from app.settings_store import (RiskSettings, get_control, get_execution, get_market, get_risk, get_strategy, save)
from app.worker.engine import CLOSE_REQUESTS_KEY, STATUS_KEY

SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}$")
Symbol = Path(..., pattern=r"^[A-Z0-9]{2,20}$", description="Binance symbol, e.g. BTCUSDT")
# Integer primary keys: values outside the 32-bit column range are a 422, not a database error.
DbId = Annotated[int, Path(ge=1, le=2**31 - 1)]

public = APIRouter(prefix="/api")
api = APIRouter(prefix="/api", dependencies=[Depends(current_user)])
write_guard = [Depends(admin_user), Depends(rate_limit("write", 60))]

_public_client: PublicMarketClient | None = None


def market_client() -> PublicMarketClient:
    global _public_client
    if _public_client is None:
        _public_client = PublicMarketClient(get_settings().market_data_rest_url)
    return _public_client


def upstream_error(exc: BinanceError) -> HTTPException:
    """Map Binance failures: an unknown symbol is the caller's error (404), anything else is upstream (502)."""
    if isinstance(exc, BinanceAPIError) and exc.code == -1121:
        return HTTPException(404, "unknown symbol on Binance")
    return HTTPException(502, f"Binance error: {exc}")


def current_mode(db: Session) -> str:
    return get_control(db).execution_mode


def paginate(db: Session, stmt, page: int, page_size: int, schema) -> dict:
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = db.scalars(stmt.offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [schema.model_validate(r) for r in rows], "total": total, "page": page, "page_size": page_size}


def price_from_db(db: Session):
    prices = {p.symbol: p.last_price for p in db.scalars(select(TradingPair))}
    return lambda sym: prices.get(sym)


def worker_status(db: Session) -> dict:
    row = db.scalar(select(SystemSetting).where(SystemSetting.setting_key == STATUS_KEY))
    st = dict(row.setting_value) if row and row.setting_value else {}
    alive = bool(st) and time.time() - st.get("heartbeat", 0) < 45
    st["alive"] = alive
    return st


# ===================================================================== auth / health
@public.post("/auth/login", response_model=sc.TokenResponse, dependencies=[Depends(rate_limit("login", 10))])
def login(body: sc.LoginRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == body.username))
    if user is None or not verify_password(body.password, user.hashed_password):
        raise HTTPException(401, "invalid username or password")
    return sc.TokenResponse(access_token=create_token(user.username), expires_in_minutes=get_settings().jwt_expire_minutes)


@public.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok", "execution_mode": current_mode(db), "live_trading": current_mode(db) == "LIVE"}


@api.get("/auth/me", response_model=sc.UserOut)
def me(user: User = Depends(current_user)):
    return user


# ===================================================================== dashboard
@api.get("/dashboard/summary")
def dashboard_summary(db: Session = Depends(get_db)):
    mode = current_mode(db)
    ctl = get_control(db)
    perf = pf.performance(db, mode, price_from_db(db))
    latest = db.scalars(select(AISignal).order_by(AISignal.created_at.desc()).limit(10)).all()
    db.rollback()  # performance() marks positions to market; do not persist from a GET
    return {
        "portfolio": perf,
        "control": ctl.model_dump(),
        "execution_mode": mode,
        "simulated": True,
        "worker": worker_status(db),
        "latest_signals": [sc.SignalOut.model_validate(s) for s in latest],
    }


# ===================================================================== market
@api.get("/market/top-coins")
def top_coins(db: Session = Depends(get_db)):
    pairs = db.scalars(select(TradingPair).where(TradingPair.market_rank.is_not(None)).order_by(TradingPair.market_rank)).all()
    out = []
    strat = get_strategy(db)
    for p in pairs:
        sig = db.scalar(select(AISignal).where(AISignal.symbol == p.symbol).order_by(AISignal.created_at.desc()).limit(1))
        ind = db.scalar(select(MarketIndicator).where(MarketIndicator.symbol == p.symbol,
                                                      MarketIndicator.timeframe == strat.setup_timeframe)
                        .order_by(MarketIndicator.candle_time.desc()).limit(1))
        out.append({
            "rank": p.market_rank, "symbol": p.symbol, "name": coin_name(p.base_asset), "base_asset": p.base_asset,
            "price": p.last_price, "change_pct_24h": p.price_change_pct_24h, "quote_volume_24h": p.quote_volume_24h,
            "rsi": ind.rsi if ind else None, "macd": ind.macd if ind else None, "macd_signal": ind.macd_signal if ind else None,
            "signal": sig.signal if sig else None, "confidence": sig.confidence_score if sig else None,
            "action_status": sig.action_status if sig else None, "enabled": p.enabled,
            "testnet_supported": p.testnet_supported, "note": p.exclusion_reason, "updated_at": p.updated_at,
        })
    return {"items": out, "count": len(out)}


@api.get("/market/ticker/{symbol}")
async def ticker(symbol: str = Symbol):
    try:
        return await market_client().ticker_24h(symbol)
    except BinanceError as exc:
        raise upstream_error(exc) from exc


@api.get("/market/orderbook/{symbol}")
async def orderbook(symbol: str = Symbol, limit: int = Query(20, ge=5, le=100)):
    try:
        return await market_client().order_book(symbol, limit)
    except BinanceError as exc:
        raise upstream_error(exc) from exc


@api.get("/market/candles/{symbol}")
async def candles(symbol: str = Symbol, timeframe: Literal["1m", "5m", "15m", "1h", "4h", "1d"] = "15m",
                  limit: int = Query(300, ge=10, le=1000), indicators: bool = True, db: Session = Depends(get_db)):
    df = cd.load_frame(db, symbol, timeframe, limit)
    source = "database"
    if len(df) < min(limit, 100):
        try:
            df = cd.candles_to_frame(await cd.fetch_closed_candles(market_client(), symbol, timeframe, limit))
            source = "binance"
        except BinanceError as exc:
            raise upstream_error(exc) from exc
    issues = cd.check_quality(df, timeframe) if len(df) else ["no candles"]
    if indicators and len(df):
        df = compute_indicators(df)
    df = df.astype(object).where(df.notna(), None)
    rows = []
    for r in df.to_dict("records"):
        r["time"] = int(r.pop("open_time").timestamp())
        r["close_time"] = int(r["close_time"].timestamp())
        rows.append(r)
    return {"symbol": symbol, "timeframe": timeframe, "source": source, "closed_only": True,
            "quality_issues": issues, "candles": rows}


# ===================================================================== AI
@api.get("/ai/signals", response_model=sc.Page[sc.SignalOut])
def signals(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
            symbol: str | None = Query(None, pattern=r"^[A-Z0-9]{2,20}$"),
            signal: Literal["BUY", "SELL", "HOLD"] | None = None, db: Session = Depends(get_db)):
    stmt = select(AISignal).order_by(AISignal.created_at.desc())
    if symbol:
        stmt = stmt.where(AISignal.symbol == symbol)
    if signal:
        stmt = stmt.where(AISignal.signal == signal)
    return paginate(db, stmt, page, page_size, sc.SignalOut)


@api.get("/ai/signals/{symbol}", response_model=sc.SignalDetail)
def latest_signal(symbol: str = Symbol, db: Session = Depends(get_db)):
    sig = db.scalar(select(AISignal).where(AISignal.symbol == symbol).order_by(AISignal.created_at.desc()).limit(1))
    if sig is None:
        raise HTTPException(404, f"no signal for {symbol} yet")
    return sig


@api.get("/ai/analysis/{symbol}")
def analysis(symbol: str = Symbol, db: Session = Depends(get_db)):
    sig = db.scalar(select(AISignal).where(AISignal.symbol == symbol).order_by(AISignal.created_at.desc()).limit(1))
    if sig is None:
        raise HTTPException(404, f"no analysis for {symbol} yet")
    snap = sig.indicator_snapshot or {}
    strat = get_strategy(db)
    return {
        "symbol": symbol, "signal": sig.signal, "score": sig.confidence_score, "reason": sig.reason,
        "action_status": sig.action_status, "strategy": sig.strategy_name, "created_at": sig.created_at,
        "candle_time": sig.candle_time,
        "timeframes": {"trend": strat.trend_timeframe, "setup": strat.setup_timeframe, "entry": strat.entry_timeframe},
        "market": snap.get("market", {}), "indicators": snap.get("snapshot", {}),
        "entry_conditions": snap.get("entry_conditions", []), "exit_conditions": snap.get("exit_conditions", []),
        "score_note": "Strategy score 0-100 from weighted rule components; it is NOT a probability of profit.",
        "ml": ml.load_meta(symbol, strat.setup_timeframe),
    }


@api.post("/ai/ml/train/{symbol}", dependencies=write_guard)
async def train_ml(body: sc.MLTrainRequest, symbol: str = Symbol):
    try:
        candles = await cd.fetch_closed_candles(market_client(), symbol, body.timeframe, body.candles)
    except BinanceError as exc:
        raise upstream_error(exc) from exc
    df = compute_indicators(cd.candles_to_frame(candles))
    try:
        res = await asyncio.to_thread(ml.train, df, symbol, body.timeframe, kind=body.model, horizon=body.horizon)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return res.__dict__


# ===================================================================== trading
@api.get("/trading/orders", response_model=sc.Page[sc.OrderOut])
def orders(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
           symbol: str | None = Query(None, pattern=r"^[A-Z0-9]{2,20}$"), status: str | None = Query(None, max_length=24),
           mode: Literal["PAPER", "TESTNET", "LIVE", "DEMO"] | None = None, db: Session = Depends(get_db)):
    stmt = select(TradingOrder).where(TradingOrder.execution_mode == (mode or current_mode(db))).order_by(TradingOrder.created_at.desc())
    if symbol:
        stmt = stmt.where(TradingOrder.symbol == symbol)
    if status:
        stmt = stmt.where(TradingOrder.status == status)
    return paginate(db, stmt, page, page_size, sc.OrderOut)


@api.get("/trading/orders/{order_id}", response_model=sc.OrderDetail)
def order_detail(order_id: DbId, db: Session = Depends(get_db)):
    o = db.scalar(select(TradingOrder).options(selectinload(TradingOrder.fills)).where(TradingOrder.id == order_id))
    if o is None:
        raise HTTPException(404, "order not found")
    return o


@api.get("/trading/positions", response_model=list[sc.PositionOut])
def positions(db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    out = []
    for p in pf.open_positions(db, current_mode(db)):
        item = sc.PositionOut.model_validate(p)
        item.invested_amount = p.cost_basis
        item.duration_seconds = int((now - p.opened_at).total_seconds())
        out.append(item)
    return out


@api.get("/trading/history", response_model=sc.Page[sc.TradeOut])
def history(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
            symbol: str | None = Query(None, pattern=r"^[A-Z0-9]{2,20}$"),
            status: Literal["OPEN", "CLOSED"] | None = None, mode: Literal["PAPER", "TESTNET", "LIVE", "DEMO"] | None = None,
            db: Session = Depends(get_db)):
    stmt = select(Trade).where(Trade.execution_mode == (mode or current_mode(db))).order_by(Trade.opened_at.desc())
    if symbol:
        stmt = stmt.where(Trade.symbol == symbol)
    if status:
        stmt = stmt.where(Trade.status == status)
    return paginate(db, stmt, page, page_size, sc.TradeOut)


@api.post("/trading/close-position/{position_id}", status_code=202, dependencies=write_guard)
def close_position(position_id: DbId, db: Session = Depends(get_db), user: User = Depends(current_user)):
    pos = db.get(Position, position_id)
    if pos is None or pos.status != "OPEN":
        raise HTTPException(404, "open position not found")
    symbol, req = pos.symbol, {"position_id": position_id, "user": user.username, "at": time.time()}
    for attempt in (1, 2):
        # Row lock shared with the worker's drain (control_tick) so neither side loses the other's write.
        row = db.scalar(select(SystemSetting).where(SystemSetting.setting_key == CLOSE_REQUESTS_KEY).with_for_update())
        if row is None:
            db.add(SystemSetting(setting_key=CLOSE_REQUESTS_KEY, setting_value=[req]))
        else:
            row.setting_value = [*(row.setting_value or []), req]  # reassign so the JSON change is persisted
        log_event(db, "api", f"Manual close requested for position {position_id} ({symbol}) by {user.username}", category="manual")
        try:
            db.commit()
            break
        except IntegrityError:  # concurrent first-ever request created the row; append to it instead
            db.rollback()
            if attempt == 2:
                raise
    return {"accepted": True, "position_id": position_id,
            "note": "The trading worker will submit a reduce-only exit order and reconcile its fills."}


# ===================================================================== portfolio
@api.get("/portfolio/summary")
def portfolio_summary(db: Session = Depends(get_db)):
    perf = pf.performance(db, current_mode(db), price_from_db(db))
    db.rollback()
    return perf


@api.get("/portfolio/performance")
def portfolio_performance(db: Session = Depends(get_db)):
    mode = current_mode(db)
    trades = db.scalars(select(Trade).where(Trade.execution_mode == mode, Trade.status == "CLOSED").order_by(Trade.closed_at)).all()
    daily: dict[str, Decimal] = defaultdict(Decimal)
    monthly: dict[str, Decimal] = defaultdict(Decimal)
    by_symbol: dict[str, dict] = defaultdict(lambda: {"pnl": Decimal(0), "trades": 0, "wins": 0})
    by_strategy: dict[str, dict] = defaultdict(lambda: {"pnl": Decimal(0), "trades": 0, "wins": 0})
    for t in trades:
        pnl = t.realised_pnl or Decimal(0)
        daily[t.closed_at.strftime("%Y-%m-%d")] += pnl
        monthly[t.closed_at.strftime("%Y-%m")] += pnl
        for bucket, key in ((by_symbol, t.symbol), (by_strategy, t.strategy_name or "unknown")):
            bucket[key]["pnl"] += pnl
            bucket[key]["trades"] += 1
            bucket[key]["wins"] += int(pnl > 0)
    volume = db.execute(select(func.date(TradingOrder.created_at), TradingOrder.side, TradingOrder.cumulative_quote_qty)
                        .where(TradingOrder.execution_mode == mode, numeric(TradingOrder.filled_quantity) > 0)).all()
    vol_daily: dict[str, Decimal] = defaultdict(Decimal)
    for day, _side, q in volume:
        vol_daily[str(day)] += Decimal(str(q))
    snaps = db.scalars(select(PortfolioSnapshot).where(PortfolioSnapshot.execution_mode == mode)
                       .order_by(PortfolioSnapshot.recorded_at.desc()).limit(2000)).all()
    perf = pf.performance(db, mode, price_from_db(db))
    db.rollback()
    return {
        "execution_mode": mode,
        "summary": perf,
        "equity_curve": [{"time": s.recorded_at, "equity": s.total_equity, "drawdown": s.drawdown} for s in reversed(snaps)],
        "daily_pnl": [{"date": k, "pnl": v} for k, v in sorted(daily.items())],
        "monthly_pnl": [{"month": k, "pnl": v} for k, v in sorted(monthly.items())],
        "trading_volume": [{"date": k, "volume": v} for k, v in sorted(vol_daily.items())],
        "by_symbol": [{"symbol": k, **v} for k, v in sorted(by_symbol.items(), key=lambda kv: kv[1]["pnl"], reverse=True)],
        "by_strategy": [{"strategy": k, **v} for k, v in by_strategy.items()],
    }


@api.get("/portfolio/snapshots", response_model=sc.Page[sc.SnapshotOut])
def snapshots(page: int = Query(1, ge=1), page_size: int = Query(500, ge=1, le=2000), db: Session = Depends(get_db)):
    stmt = select(PortfolioSnapshot).where(PortfolioSnapshot.execution_mode == current_mode(db)).order_by(PortfolioSnapshot.recorded_at.desc())
    return paginate(db, stmt, page, page_size, sc.SnapshotOut)


# ===================================================================== settings
def _bundle(db: Session) -> sc.SettingsBundle:
    ctl = get_control(db)
    return sc.SettingsBundle(risk=get_risk(db), strategy=get_strategy(db), market=get_market(db), execution=get_execution(db),
                             execution_mode=ctl.execution_mode, auto_trading=ctl.auto_trading, state=ctl.state,
                             testnet_configured=get_settings().testnet_configured,
                             live_trading_available=get_settings().live_configured,
                             live_max_capital_usdt=get_settings().live_max_capital_usdt if get_settings().live_configured else None)


@api.get("/settings", response_model=sc.SettingsBundle)
def get_all_settings(db: Session = Depends(get_db)):
    return _bundle(db)


@api.put("/settings", response_model=sc.SettingsBundle, dependencies=write_guard)
def update_settings(body: sc.SettingsUpdate, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ctl = get_control(db)
    changed = []
    for key in ("strategy", "market", "execution"):
        val = getattr(body, key)
        if val is not None:
            save(db, key, val)
            changed.append(key)
    if body.auto_trading is not None:
        ctl.auto_trading = body.auto_trading
        changed.append("auto_trading")
    if body.execution_mode is not None and body.execution_mode != ctl.execution_mode:
        if ctl.state != "STOPPED":
            raise HTTPException(409, "stop the trading system before changing execution mode")
        if body.execution_mode == "TESTNET" and not get_settings().testnet_configured:
            raise HTTPException(422, "testnet credentials are not configured (BINANCE_TESTNET_API_KEY/SECRET)")
        if body.execution_mode == "LIVE":
            if not get_settings().live_configured:
                raise HTTPException(422, "live trading is not enabled (LIVE_TRADING_ENABLED, BINANCE_LIVE_API_KEY/SECRET, "
                                         "LIVE_MAX_CAPITAL_USDT)")
            if (body.confirm_live or "").strip() != sc.LIVE_CONFIRMATION_PHRASE:
                raise HTTPException(422, f"switching to LIVE requires confirm_live='{sc.LIVE_CONFIRMATION_PHRASE}'")
        if pf.open_positions(db, ctl.execution_mode):
            raise HTTPException(409, f"close all {ctl.execution_mode} positions before switching mode")
        ctl.execution_mode = body.execution_mode
        changed.append(f"execution_mode={body.execution_mode}" + (" (REAL MONEY)" if body.execution_mode == "LIVE" else ""))
    save(db, "control", ctl)
    log_event(db, "settings", f"Settings updated by {user.username}: {', '.join(changed) or 'no changes'}", category="settings")
    db.commit()
    return _bundle(db)


@api.put("/settings/risk", response_model=RiskSettings, dependencies=write_guard)
def update_risk(body: RiskSettings, db: Session = Depends(get_db), user: User = Depends(current_user)):
    save(db, "risk", body)
    log_event(db, "settings", f"Risk settings updated by {user.username}", meta=body.model_dump(mode="json"), category="settings")
    db.commit()
    return body


# ===================================================================== system
@api.get("/system/status")
def system_status(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    ctl = get_control(db)
    acct = pf.get_account(db, ctl.execution_mode, get_settings().initial_balance_for(ctl.execution_mode))
    db.commit()
    w = worker_status(db)
    return {
        "database": {"connected": db_ok, "dialect": db.get_bind().dialect.name},
        "control": ctl.model_dump(),
        "execution_mode": ctl.execution_mode,
        "environment_label": {"PAPER": "PAPER TRADING (SIMULATED)", "TESTNET": "BINANCE SPOT TESTNET",
                              "LIVE": "LIVE TRADING - REAL MONEY"}[ctl.execution_mode],
        "live_trading_enabled": get_settings().live_configured,
        "live": {"configured": get_settings().live_configured,
                 "max_capital_usdt": str(get_settings().live_max_capital_usdt),
                 "last_error": (w.get("live") or {}).get("last_error")},
        "worker": w,
        "market_data": {"stream": w.get("stream"), "rest": w.get("rest")},
        "testnet": {**(w.get("testnet") or {}), "configured": get_settings().testnet_configured},
        "risk": {"halted": acct.halted, "halt_reason": acct.halt_reason, "halted_at": acct.halted_at},
    }


def _set_state(db: Session, state: str, user: User, message: str) -> dict:
    ctl = get_control(db)
    ctl.state = state  # type: ignore[assignment]
    save(db, "control", ctl)
    log_event(db, "system", f"{message} by {user.username}", category="system")
    db.commit()
    return {"state": state, "control": ctl.model_dump()}


@api.post("/system/start", dependencies=write_guard)
def system_start(db: Session = Depends(get_db), user: User = Depends(current_user)):
    acct = pf.get_account(db, current_mode(db), get_settings().initial_balance_for(current_mode(db)))
    if acct.halted:
        raise HTTPException(409, f"hard risk limit active ({acct.halt_reason}); explicit reset required")
    return _set_state(db, "RUNNING", user, "Trading started")


@api.post("/system/stop", dependencies=write_guard)
def system_stop(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return _set_state(db, "STOPPED", user, "Trading stopped (stop-loss/take-profit protection stays active)")


@api.post("/system/pause", dependencies=write_guard)
def system_pause(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return _set_state(db, "PAUSED", user, "Trading paused (no new entries)")


@api.post("/system/resume", dependencies=write_guard)
def system_resume(db: Session = Depends(get_db), user: User = Depends(current_user)):
    acct = pf.get_account(db, current_mode(db), get_settings().initial_balance_for(current_mode(db)))
    if acct.halted:
        raise HTTPException(409, f"hard risk limit active ({acct.halt_reason}); explicit reset required")
    return _set_state(db, "RUNNING", user, "Trading resumed")


@api.post("/system/risk/reset", dependencies=write_guard)
def risk_reset(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Explicitly acknowledge a hard-limit halt. Daily/peak baselines restart from current equity."""
    mode = current_mode(db)
    acct = pf.get_account(db, mode)
    if not acct.halted:
        # Resetting without a halt would silently rebase the drawdown / daily-loss baselines
        # and so loosen those limits.
        raise HTTPException(409, "no hard-limit halt is active; nothing to reset")
    v = pf.mark_to_market(db, mode, price_from_db(db))
    prev = acct.halt_reason
    acct.halted, acct.halt_reason, acct.halted_at = False, None, None
    acct.day_start_equity = acct.peak_equity = v.equity
    acct.consecutive_losses = 0
    risk_event(db, "RISK_RESET", "INFO", f"Hard-limit halt reset by {user.username} (was: {prev}); "
               f"baselines reset to equity {v.equity}")
    db.commit()
    return {"reset": True, "previous_reason": prev, "equity": v.equity}


@api.get("/system/logs", response_model=sc.Page[sc.LogOut])
def logs(page: int = Query(1, ge=1), page_size: int = Query(100, ge=1, le=500),
         level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | None = None,
         component: str | None = Query(None, max_length=64), q: str | None = Query(None, max_length=100),
         db: Session = Depends(get_db)):
    stmt = select(SystemLog).order_by(SystemLog.created_at.desc())
    if level:
        stmt = stmt.where(SystemLog.level == level)
    if component:
        stmt = stmt.where(SystemLog.component == component)
    if q:
        stmt = stmt.where(SystemLog.message.ilike(f"%{q}%"))
    return paginate(db, stmt, page, page_size, sc.LogOut)


@api.get("/risk/events", response_model=sc.Page[sc.RiskEventOut])
def risk_events(page: int = Query(1, ge=1), page_size: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    return paginate(db, select(RiskEvent).order_by(RiskEvent.created_at.desc()), page, page_size, sc.RiskEventOut)


@api.get("/risk/status")
def risk_status(db: Session = Depends(get_db)):
    mode = current_mode(db)
    rs = get_risk(db)
    acct = pf.get_account(db, mode)
    v = pf.mark_to_market(db, mode, price_from_db(db))
    db.rollback()
    daily_loss = (acct.day_start_equity - v.equity) / acct.day_start_equity * 100 if acct.day_start_equity else Decimal(0)
    dd = (acct.peak_equity - v.equity) / acct.peak_equity * 100 if acct.peak_equity else Decimal(0)
    exposure = v.invested / v.equity * 100 if v.equity else Decimal(0)
    n_open = len(pf.open_positions(db, mode))
    return {
        "halted": acct.halted, "halt_reason": acct.halt_reason,
        "metrics": [
            {"name": "Daily loss", "value": max(daily_loss, Decimal(0)), "limit": rs.max_daily_loss_pct, "unit": "%", "hard": True},
            {"name": "Drawdown", "value": max(dd, Decimal(0)), "limit": rs.max_drawdown_pct, "unit": "%", "hard": True},
            {"name": "Total exposure", "value": exposure, "limit": rs.max_total_alloc_pct, "unit": "%", "hard": False},
            {"name": "Open positions", "value": n_open, "limit": rs.max_open_positions, "unit": "", "hard": False},
            {"name": "Consecutive losses", "value": acct.consecutive_losses, "limit": rs.max_consecutive_losses, "unit": "", "hard": False},
        ],
    }


# ===================================================================== backtest
async def _run_backtest_job(run_id: int, req: sc.BacktestRequest) -> None:
    with session_scope() as s:
        run = s.get(BacktestRun, run_id)
        run.status = "RUNNING"
        strategy = req.strategy or get_strategy(s)
        risk = get_risk(s)
    try:
        start_ms, end_ms = int(req.start.timestamp() * 1000), int(req.end.timestamp() * 1000)
        candles = await cd.fetch_range(market_client(), req.symbol, req.timeframe, start_ms, end_ms)
        df = cd.candles_to_frame(candles)
        market_df = None
        if strategy.market_filter_enabled and req.symbol != strategy.market_filter_symbol:
            market_df = cd.candles_to_frame(await cd.fetch_range(market_client(), strategy.market_filter_symbol,
                                                                 req.timeframe, start_ms, end_ms))
        params = BacktestParams(req.symbol, req.timeframe, req.initial_balance, req.fee_rate, req.slippage_bps, req.risk_pct)
        result = await asyncio.to_thread(run_backtest, df, params, strategy, risk, None, market_df)
        with session_scope() as s:
            run = s.get(BacktestRun, run_id)
            run.status, run.metrics = "DONE", {**result["metrics"], "candles": len(df)}
            run.equity_curve, run.trades = result["equity_curve"], result["trades"]
            run.finished_at = datetime.now(timezone.utc)
            log_event(s, "backtest", f"Backtest #{run_id} {req.symbol} {req.timeframe} finished: "
                      f"{result['metrics']['number_of_trades']} trades, return {result['metrics']['total_return_pct']}% "
                      "(hypothetical)", category="backtest")
    except Exception as exc:
        with session_scope() as s:
            run = s.get(BacktestRun, run_id)
            run.status, run.error, run.finished_at = "FAILED", str(exc)[:1000], datetime.now(timezone.utc)


@api.post("/backtest/run", response_model=sc.BacktestOut, status_code=202, dependencies=write_guard)
def backtest_run(body: sc.BacktestRequest, background: BackgroundTasks, db: Session = Depends(get_db)):
    if body.end <= body.start:
        raise HTTPException(422, "end must be after start")
    if body.end > datetime.now(timezone.utc):
        raise HTTPException(422, "end cannot be in the future")
    bars = (body.end - body.start).total_seconds() * 1000 / cd.TIMEFRAMES[body.timeframe]
    if bars > 50_000:
        raise HTTPException(422, f"range too large ({int(bars)} candles; max 50000)")
    if bars < 400:
        raise HTTPException(422, f"range too small ({int(bars)} candles; need at least 400 incl. warm-up)")
    run = BacktestRun(symbol=body.symbol, timeframe=body.timeframe,
                      strategy_name=(body.strategy or get_strategy(db)).name,
                      params=body.model_dump(mode="json"), status="PENDING")
    db.add(run)
    db.commit()
    background.add_task(_run_backtest_job, run.id, body)
    return run


@api.get("/backtest/results", response_model=sc.Page[sc.BacktestOut])
def backtest_results(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    return paginate(db, select(BacktestRun).order_by(BacktestRun.created_at.desc()), page, page_size, sc.BacktestOut)


@api.get("/backtest/results/{run_id}", response_model=sc.BacktestDetail)
def backtest_result(run_id: DbId, db: Session = Depends(get_db)):
    run = db.get(BacktestRun, run_id)
    if run is None:
        raise HTTPException(404, "backtest not found")
    return run

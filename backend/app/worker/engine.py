"""Automated trading engine (the background worker).

Lifecycle per symbol:
  market data -> closed candle -> indicators -> strategy signal -> risk engine
  -> balance/filter checks -> order manager -> fills -> portfolio -> monitoring -> exits

Control states (set via the API, persisted in system_settings):
  RUNNING  - full automation.
  PAUSED   - no new entries; strategy exits + stop-loss/take-profit stay active.
  STOPPED  - no new entries and no strategy exits; stop-loss/take-profit
             protection of already-open positions REMAINS active (safety).
Hard risk halts block new entries until an explicit reset, in any state.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select

from app.analysis import ml
from app.analysis.indicators import compute_indicators
from app.analysis.strategy import SignalResult, evaluate
from app.audit import log_event, risk_event
from app.config import ExecutionMode, Settings
from app.db import numeric, session_scope
from app.events import bus
from app.exchange.filters import SymbolFilters
from app.exchange.rest import BinanceError, PublicMarketClient, TestnetClient
from app.exchange.websocket import BookTickerEvent, KlineEvent, MarketStreamManager, TickerEvent, TradeEvent
from app.execution.adapters import PaperExecutionAdapter, TestnetExecutionAdapter
from app.execution.order_manager import ACTIVE, OrderManager
from app.market import candles as cd
from app.market.discovery import rank_pairs
from app.market.price_cache import price_cache
from app.models import AISignal, MarketCandle, MarketIndicator, Position, SystemSetting, TradingOrder, TradingPair
from app.portfolio import service as pf
from app.risk.engine import AccountSnapshot, EntryRequest, check_hard_limits, evaluate_entry, exit_trigger, update_trailing_stop
from app.settings_store import (get_control, get_execution, get_market, get_risk, get_strategy)

log = logging.getLogger("worker")
LEASE_KEY = "worker.lease"
STATUS_KEY = "worker.status"
CLOSE_REQUESTS_KEY = "commands.close_position"
LEASE_TTL = 30


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


class TradingEngine:
    def __init__(self, settings: Settings, public: PublicMarketClient | None = None,
                 stream: MarketStreamManager | None = None) -> None:
        self.settings = settings
        self.public = public or PublicMarketClient(settings.market_data_rest_url)
        self.testnet: TestnetClient | None = None
        if settings.testnet_configured:
            self.testnet = TestnetClient(settings.binance_testnet_rest_url,
                                         settings.binance_testnet_api_key.get_secret_value(),
                                         settings.binance_testnet_api_secret.get_secret_value())
        self.stream = stream or MarketStreamManager(settings.market_data_ws_url, self.on_market_event, self.on_stream_status)
        self.filters: dict[str, SymbolFilters] = {}
        self.testnet_symbols: set[str] | None = None
        self.top_symbols: list[str] = []
        self.mode: str = settings.execution_mode.value
        self.paper = PaperExecutionAdapter(self._depth, Decimal("0.001"), Decimal("5"))
        self.om: OrderManager | None = None
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.has_lease = False
        self.started_at: float | None = None
        self.last_discovery_at: float | None = None
        self.last_analysis: dict[str, float] = {}
        self.stream_status = "init"
        self.stale_reported: set[str] = set()
        self._tasks: list[asyncio.Task] = []
        self._ticker_pub: dict[str, float] = {}
        self._analysis_queue: asyncio.Queue[str] = asyncio.Queue()
        self._queued: set[str] = set()
        self._position_check = asyncio.Event()
        self._stopping = False
        # Set when another instance takes over the lease; the host process should
        # stop this engine and (optionally) start a fresh one that re-contends.
        self.lease_lost = asyncio.Event()

    # ================================================================== setup
    def _build_order_manager(self, mode: str) -> OrderManager:
        if mode == ExecutionMode.TESTNET.value:
            if self.testnet is None:
                raise RuntimeError("TESTNET mode requested but testnet credentials are not configured")
            adapter = TestnetExecutionAdapter(self.testnet)
        else:
            adapter = self.paper
        return OrderManager(adapter, mode, self.filters.get, self._fee_asset_rate)

    def _fee_asset_rate(self, asset: str) -> Decimal | None:
        return price_cache.price(f"{asset}USDT")

    async def _depth(self, symbol: str) -> dict:
        try:
            return await self.public.order_book(symbol, 20)
        except BinanceError:
            q = price_cache.get(symbol)
            if q is None or q.age() > 10 or not (q.bid and q.ask):
                raise
            return {"bids": [[str(q.bid), str(q.bid_qty or 0)]], "asks": [[str(q.ask), str(q.ask_qty or 0)]]}

    def _acquire_lease(self) -> bool:
        now = time.time()
        with session_scope() as s:
            row = s.scalar(select(SystemSetting).where(SystemSetting.setting_key == LEASE_KEY).with_for_update())
            val = row.setting_value if row else None
            if val and val.get("owner") != self.owner and val.get("expires", 0) > now:
                return False
            payload = {"owner": self.owner, "expires": now + LEASE_TTL}
            if row is None:
                s.add(SystemSetting(setting_key=LEASE_KEY, setting_value=payload))
            else:
                row.setting_value = payload
        return True

    def _release_lease(self) -> None:
        with session_scope() as s:
            row = s.scalar(select(SystemSetting).where(SystemSetting.setting_key == LEASE_KEY))
            if row and (row.setting_value or {}).get("owner") == self.owner:
                row.setting_value = {"owner": None, "expires": 0}

    async def start(self) -> None:
        bus.bind_loop(asyncio.get_running_loop())
        self.started_at = time.time()
        if not self._acquire_lease():
            with session_scope() as s:
                log_event(s, "worker", "Another trading worker holds the lease; this instance will not trade", "ERROR")
            raise RuntimeError("another worker instance is active")
        self.has_lease = True
        with session_scope() as s:
            ctl = get_control(s)
            self.mode = ctl.execution_mode
            pf.get_account(s, self.mode, self.settings.initial_balance_usdt)
            self._apply_exec_settings(s)
            log_event(s, "worker", f"Trading worker starting (mode={self.mode}, state={ctl.state}, "
                      f"{'SIMULATED' if self.mode == 'PAPER' else 'TESTNET'} execution)", category="system")
        self.om = self._build_order_manager(self.mode)
        await self.refresh_exchange_info()
        # ---- restart recovery: never re-place, always reconcile
        restored = self.om.restore_paper_orders() if self.mode == "PAPER" else 0
        n = await self.om.reconcile()
        with session_scope() as s:
            open_n = len(pf.open_positions(s, self.mode))
            log_event(s, "worker", f"Recovery: {open_n} open position(s), {n} outstanding order(s) reconciled, "
                      f"{restored} resting paper order(s) restored", category="system")
        await self.refresh_universe()
        self._tasks = [
            self.stream.start(),
            asyncio.create_task(self._loop(self.reconcile_tick, 5), name="reconcile"),
            asyncio.create_task(self._loop(self.risk_tick, 10), name="risk"),
            asyncio.create_task(self._loop(self.snapshot_tick, 60), name="snapshot"),
            asyncio.create_task(self._loop(self.lease_tick, 10), name="lease"),
            asyncio.create_task(self._loop(self.control_tick, 3), name="control"),
            asyncio.create_task(self._loop(self.discovery_tick, 60), name="discovery"),
            asyncio.create_task(self._position_loop(), name="positions"),
            asyncio.create_task(self._analysis_worker(), name="analysis"),
        ]
        for sym in self.watch_symbols():
            self.queue_analysis(sym)

    async def stop(self) -> None:
        self._stopping = True
        await self.stream.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            if self.has_lease:
                with session_scope() as s:
                    log_event(s, "worker", "Trading worker stopped gracefully", category="system")
                self._release_lease()
                self.has_lease = False
        except Exception:  # pragma: no cover
            log.exception("error during shutdown")
        await self.public.close()
        if self.testnet:
            await self.testnet.close()

    async def _loop(self, fn, interval: float) -> None:
        while not self._stopping:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("background task %s failed", getattr(fn, "__name__", fn))
            await asyncio.sleep(interval)

    def _apply_exec_settings(self, s) -> None:
        ex = get_execution(s)
        self.paper.configure(ex.paper_fee_rate, ex.paper_slippage_bps)

    # ================================================================== universe
    async def refresh_exchange_info(self) -> None:
        info = await self.public.exchange_info()
        self._exchange_symbols = info["symbols"]
        self.filters = {s["symbol"]: SymbolFilters.from_exchange_info(s) for s in info["symbols"]}
        try:  # testnet exchangeInfo is public; used only to flag availability
            tinfo = await (self.testnet.exchange_info() if self.testnet else
                           PublicTestnetProbe(self.settings.binance_testnet_rest_url).exchange_info())
            self.testnet_symbols = {s["symbol"] for s in tinfo["symbols"] if s.get("status") == "TRADING"}
            if self.mode == "TESTNET":  # testnet filters are authoritative for testnet orders
                for s in tinfo["symbols"]:
                    self.filters[s["symbol"]] = SymbolFilters.from_exchange_info(s)
        except Exception as exc:
            log.warning("testnet exchangeInfo unavailable: %s", exc)

    async def refresh_universe(self) -> None:
        with session_scope() as s:
            mk, strat = get_market(s), get_strategy(s)
        tickers = await self.public.ticker_24h()
        tfs = sorted({strat.trend_timeframe, strat.setup_timeframe, strat.entry_timeframe}, key=lambda t: cd.TIMEFRAMES[t])
        # Rank without history first, then verify history for the leading candidates.
        groups = set(mk.excluded_permission_groups)
        prelim, _ = rank_pairs(self._exchange_symbols, tickers, top_n=mk.top_n + 8, excluded=set(mk.excluded_symbols),
                               testnet_symbols=self.testnet_symbols, excluded_permission_groups=groups)
        history_ok: dict[str, bool] = {}
        for c in prelim:
            ok = True
            for tf in tfs:
                got = await self.backfill(c.symbol, tf, mk.history_candles)
                if got < mk.min_history_candles:
                    ok = False
            history_ok[c.symbol] = ok
        selected, rejected = rank_pairs(self._exchange_symbols, tickers, top_n=mk.top_n, excluded=set(mk.excluded_symbols),
                                        testnet_symbols=self.testnet_symbols, history_ok=history_ok,
                                        excluded_permission_groups=groups)
        with session_scope() as s:
            ranked = {c.symbol for c in selected}
            for row in s.scalars(select(TradingPair)):
                if row.symbol not in ranked:
                    row.market_rank, row.enabled = None, False
            for c in selected:
                row = s.scalar(select(TradingPair).where(TradingPair.symbol == c.symbol))
                if row is None:
                    row = TradingPair(symbol=c.symbol, base_asset=c.base_asset, quote_asset=c.quote_asset)
                    s.add(row)
                allowed = not mk.selected_symbols or c.symbol in mk.selected_symbols
                row.market_rank, row.testnet_supported = c.rank, c.testnet_supported
                row.enabled = allowed and (self.mode == "PAPER" or c.testnet_supported)
                row.quote_volume_24h, row.price_change_pct_24h, row.last_price = c.quote_volume, c.change_pct, c.last_price
                row.filters, row.exclusion_reason = c.filters.to_json(), None
                if self.mode == "TESTNET" and not c.testnet_supported:
                    row.exclusion_reason = "unavailable on Spot Testnet (paper trading only)"
            unavailable = [c.symbol for c in selected if not c.testnet_supported]
            log_event(s, "discovery", f"Top {len(selected)} pairs: {', '.join(c.symbol for c in selected)}"
                      + (f"; not on testnet: {', '.join(unavailable)}" if unavailable else ""), category="discovery",
                      meta={"rejected_sample": [(c.symbol, c.exclusion_reason) for c in rejected[:15]]})
        self.top_symbols = [c.symbol for c in selected]
        self.last_discovery_at = time.time()
        self.stream.set_subscriptions(self.watch_symbols(), tfs)
        bus.publish("market", "universe", {"symbols": self.top_symbols})

    def watch_symbols(self) -> list[str]:
        """Top-N plus any symbol with an open position (positions must always stay monitored)."""
        with session_scope() as s:
            held = [p.symbol for p in pf.open_positions(s, self.mode)]
        return list(dict.fromkeys(self.top_symbols + held))

    async def backfill(self, symbol: str, tf: str, n: int) -> int:
        """Ensure the most recent ``n`` closed candles are stored; returns how many are stored now."""
        with session_scope() as s:
            last = s.scalar(select(func.max(MarketCandle.open_time)).where(MarketCandle.symbol == symbol,
                                                                           MarketCandle.timeframe == tf))
            have = s.scalar(select(func.count()).select_from(MarketCandle).where(MarketCandle.symbol == symbol,
                                                                                MarketCandle.timeframe == tf)) or 0
        dur = cd.TIMEFRAMES[tf]
        if last is not None and have >= n:
            missing = int((time.time() * 1000 - cd.dt_to_ms(last)) // dur)
            if missing <= 1:
                return have
            fetch = min(missing + 2, 1000)
        else:
            fetch = n
        try:
            candles = await cd.fetch_closed_candles(self.public, symbol, tf, fetch)
        except BinanceError as exc:
            log.warning("backfill %s %s failed: %s", symbol, tf, exc)
            return have
        with session_scope() as s:
            cd.store_candles(s, symbol, tf, candles)
            return s.scalar(select(func.count()).select_from(MarketCandle).where(MarketCandle.symbol == symbol,
                                                                                MarketCandle.timeframe == tf)) or 0

    # ================================================================== market events
    async def on_stream_status(self, status: str, info: dict) -> None:
        prev, self.stream_status = self.stream_status, status
        bus.publish("dashboard", "stream_status", {"status": status, **info})
        if status == prev:
            return
        with session_scope() as s:
            level = "INFO" if status == "connected" else "WARNING"
            log_event(s, "websocket", f"Market stream {status} {info or ''}".strip(), level, category="websocket")
            if status == "stale":
                risk_event(s, "STALE_MARKET_DATA", "WARNING", "Market data stream went silent; new entries blocked until data resumes")
        if status == "connected" and prev in ("reconnecting", "disconnected", "stale", "error"):
            asyncio.create_task(self._gap_fill())

    async def _gap_fill(self) -> None:
        """After a reconnect, backfill candles missed while disconnected."""
        with session_scope() as s:
            strat = get_strategy(s)
        for sym in self.watch_symbols():
            for tf in {strat.trend_timeframe, strat.setup_timeframe, strat.entry_timeframe}:
                await self.backfill(sym, tf, 300)

    async def on_market_event(self, ev) -> None:
        if isinstance(ev, BookTickerEvent):
            price_cache.update(ev.symbol, bid=ev.bid, ask=ev.ask, bid_qty=ev.bid_qty, ask_qty=ev.ask_qty)
            if self.mode == "PAPER":
                self.paper.on_quote(ev.symbol, ev.bid, ev.bid_qty, ev.ask, ev.ask_qty)
            self._position_check.set()
        elif isinstance(ev, TradeEvent):
            price_cache.update(ev.symbol, last=ev.price)
        elif isinstance(ev, TickerEvent):
            price_cache.update(ev.symbol, last=ev.last_price, change_pct=ev.change_pct, quote_volume=ev.quote_volume)
            now = time.time()
            if now - self._ticker_pub.get(ev.symbol, 0) >= 1:
                self._ticker_pub[ev.symbol] = now
                q = price_cache.get(ev.symbol)
                bus.publish("market", "ticker", {"symbol": ev.symbol, "price": ev.last_price, "change_pct": ev.change_pct,
                                                 "quote_volume": ev.quote_volume, "bid": q.bid if q else None,
                                                 "ask": q.ask if q else None})
        elif isinstance(ev, KlineEvent):
            bus.publish("market", "kline", {"symbol": ev.symbol, "interval": ev.interval, "open_time": ev.open_time,
                                            "open": ev.open, "high": ev.high, "low": ev.low, "close": ev.close,
                                            "volume": ev.volume, "closed": ev.closed})
            if ev.closed:
                c = cd.Candle(ev.open_time, ev.close_time, ev.open, ev.high, ev.low, ev.close, ev.volume,
                              ev.quote_volume, ev.trade_count)
                with session_scope() as s:
                    cd.store_candles(s, ev.symbol, ev.interval, [c])
                    strat = get_strategy(s)
                if ev.interval == strat.entry_timeframe:
                    self.queue_analysis(ev.symbol)

    # ================================================================== analysis
    def queue_analysis(self, symbol: str) -> None:
        if symbol not in self._queued:
            self._queued.add(symbol)
            self._analysis_queue.put_nowait(symbol)

    async def _analysis_worker(self) -> None:
        while not self._stopping:
            symbol = await self._analysis_queue.get()
            self._queued.discard(symbol)
            try:
                await self.analyse_and_act(symbol)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("analysis failed for %s", symbol)

    def build_frames(self, symbol: str) -> tuple[dict, list[str], object]:
        with session_scope() as s:
            strat, risk = get_strategy(s), get_risk(s)
            raw = {role: cd.load_frame(s, symbol, getattr(strat, f"{role}_timeframe"), 300)
                   for role in ("trend", "setup", "entry")}
        issues: list[str] = []
        for role, df in raw.items():
            tf = getattr(strat, f"{role}_timeframe")
            for issue in cd.check_quality(df, tf):
                # Binance occasionally has 1-2 genuinely missing candles (maintenance);
                # tolerate tiny gaps, block on anything larger.
                if "missing" in issue and int(issue.split()[0]) <= 3:
                    continue
                issues.append(f"{tf}: {issue}")
        frames = {role: compute_indicators(df) for role, df in raw.items()}
        return frames, issues, (strat, risk)

    async def analyse_and_act(self, symbol: str) -> SignalResult:
        frames, issues, (strat, risk) = await asyncio.to_thread(self.build_frames, symbol)
        with session_scope() as s:
            has_pos = s.scalar(select(Position.id).where(Position.execution_mode == self.mode, Position.symbol == symbol,
                                                         Position.status == "OPEN")) is not None
        proba, available = (None, False)
        if strat.ml_filter_enabled and len(frames["setup"]):
            proba, available = await asyncio.to_thread(ml.predict_proba, frames["setup"], symbol, strat.setup_timeframe)
        result = evaluate(symbol, frames, strat, max_atr_pct=float(risk.max_atr_pct), has_position=has_pos,
                          data_issues=issues, ml_probability=proba, ml_available=available)
        signal_id = self._persist_signal(symbol, result, frames, strat)
        self.last_analysis[symbol] = time.time()
        with session_scope() as s:
            ctl = get_control(s)
        if result.signal == "BUY" and ctl.state == "RUNNING" and ctl.auto_trading:
            await self.attempt_entry(symbol, result, signal_id)
        elif result.signal == "SELL" and ctl.state in ("RUNNING", "PAUSED") and ctl.auto_trading:
            await self.exit_position_for(symbol, "STRATEGY_EXIT", signal_id)
        elif result.signal == "BUY":
            self._set_signal_status(signal_id, f"NOT_EXECUTED_{ctl.state}" if ctl.state != "RUNNING" else "AUTO_TRADING_OFF")
        return result

    def _persist_signal(self, symbol: str, r: SignalResult, frames: dict, strat) -> int:
        with session_scope() as s:
            sig = AISignal(symbol=symbol, signal=r.signal, confidence_score=Decimal(str(r.score)),
                           strategy_name=r.strategy_name, reason=r.reason,
                           indicator_snapshot={"snapshot": r.snapshot, "market": r.market,
                                               "entry_conditions": [c.__dict__ for c in r.entry_conditions],
                                               "exit_conditions": [c.__dict__ for c in r.exit_conditions]},
                           candle_time=datetime.fromisoformat(r.candle_time) if r.candle_time and r.candle_time != "None" else None)
            s.add(sig)
            for role, df in frames.items():
                if df.empty:
                    continue
                last = df.iloc[-1]
                tf = getattr(strat, f"{role}_timeframe")
                ct = last["open_time"].to_pydatetime()
                if s.scalar(select(MarketIndicator.id).where(MarketIndicator.symbol == symbol, MarketIndicator.timeframe == tf,
                                                             MarketIndicator.candle_time == ct)):
                    continue
                dv = lambda k: (Decimal(str(round(float(last[k]), 10))) if k in last and last[k] == last[k] else None)  # noqa: E731
                s.add(MarketIndicator(symbol=symbol, timeframe=tf, candle_time=ct, rsi=dv("rsi"), ema_9=dv("ema_9"),
                                      ema_20=dv("ema_20"), ema_50=dv("ema_50"), ema_200=dv("ema_200"), macd=dv("macd"),
                                      macd_signal=dv("macd_signal"), bollinger_upper=dv("bb_upper"),
                                      bollinger_lower=dv("bb_lower"), atr=dv("atr"), adx=dv("adx"), volume_ma=dv("volume_ma")))
            s.flush()
            if r.signal != "HOLD":
                log_event(s, "ai", f"{symbol}: {r.signal} (score {r.score:.1f}) - {r.reason[:300]}", category="ai_decision",
                          meta={"signal_id": sig.id})
            bus.publish("dashboard", "signal", {"id": sig.id, "symbol": symbol, "signal": r.signal, "score": r.score,
                                                "reason": r.reason, "created_at": sig.created_at})
            return sig.id

    def _set_signal_status(self, signal_id: int, status: str) -> None:
        with session_scope() as s:
            sig = s.get(AISignal, signal_id)
            if sig:
                sig.action_status = status[:48]

    # ================================================================== entries
    def account_snapshot(self, s) -> AccountSnapshot:
        acct = pf.get_account(s, self.mode)
        v = pf.mark_to_market(s, self.mode, lambda sym: price_cache.price(sym))
        assert self.om is not None
        pend_n, pend_notional = self.om.pending_entry_notional(s)
        return AccountSnapshot(equity=v.equity, cash=acct.cash_balance - pend_notional, invested=v.invested + pend_notional,
                               open_positions=len(pf.open_positions(s, self.mode)) + pend_n,
                               day_start_equity=acct.day_start_equity, peak_equity=acct.peak_equity,
                               consecutive_losses=acct.consecutive_losses, last_loss_at=acct.last_loss_at,
                               halted=acct.halted, halt_reason=acct.halt_reason)

    def connectivity_ok(self) -> bool:
        return self.stream.is_healthy() and self.public.last_error is None

    async def attempt_entry(self, symbol: str, result: SignalResult, signal_id: int) -> None:
        assert self.om is not None
        if not self.has_lease:
            self._set_signal_status(signal_id, "WORKER_NOT_LEASE_HOLDER")
            return
        f = self.filters.get(symbol)
        with session_scope() as s:
            rs, ex = get_risk(s), get_execution(s)
            pair = s.scalar(select(TradingPair).where(TradingPair.symbol == symbol))
            if pair is None or not pair.enabled:
                self._set_signal_status(signal_id, "SYMBOL_NOT_ENABLED")
                return
            acct = self.account_snapshot(s)
            last_loss = s.scalar(select(func.max(Position.closed_at)).where(
                Position.execution_mode == self.mode, Position.symbol == symbol, numeric(Position.realised_pnl) < 0))
            has_pos = s.scalar(select(Position.id).where(Position.execution_mode == self.mode, Position.symbol == symbol,
                                                         Position.status.in_(("OPEN", "CLOSING")))) is not None
            has_pending = self.om.has_active_entry(s, symbol)
        q = price_cache.get(symbol)
        stale = q is None or q.age() > rs.stale_data_seconds or not q.ask
        entry_price = q.ask if q and q.ask else Decimal(str(result.price or 0))
        if f is None:
            self._set_signal_status(signal_id, "NO_EXCHANGE_FILTERS")
            return
        market_order = ex.order_type == "MARKET"
        req = EntryRequest(symbol=symbol, entry_price=entry_price, atr=Decimal(str(result.atr)) if result.atr else None,
                           filters=f, now=datetime.now(timezone.utc), has_open_position=has_pos,
                           has_pending_order=has_pending, data_stale=stale, connectivity_ok=self.connectivity_ok(),
                           symbol_last_loss_at=last_loss, market_order=market_order)
        decision = evaluate_entry(req, acct, rs, ex)
        if decision.hard_breach:
            await self.halt(decision.hard_breach, "; ".join(decision.reasons))
        if not decision.approved:
            self._set_signal_status(signal_id, "REJECTED_BY_RISK")
            with session_scope() as s:
                risk_event(s, "TRADE_REJECTED", "INFO", f"{symbol} BUY rejected by risk engine: " + "; ".join(decision.reasons),
                           {"signal_id": signal_id, "decision": decision.to_dict()})
            return
        # Balance check against the venue itself in TESTNET mode
        if self.mode == "TESTNET":
            try:
                free = await self.om.adapter.free_balance(f.quote_asset)
            except BinanceError as exc:
                self._set_signal_status(signal_id, "BALANCE_CHECK_FAILED")
                with session_scope() as s:
                    log_event(s, "orders", f"Testnet balance check failed ({exc}); entry skipped", "WARNING", category="api_error")
                return
            needed = decision.notional * (1 + ex.estimated_fee_rate)
            if free < needed:
                self._set_signal_status(signal_id, "INSUFFICIENT_BALANCE")
                with session_scope() as s:
                    risk_event(s, "INSUFFICIENT_BALANCE", "WARNING", f"Testnet {f.quote_asset} balance {free} < required {needed:.2f}")
                return
        price = None
        if not market_order:
            price = f.round_price(entry_price * (1 - ex.limit_offset_bps / Decimal("10000")), "BUY")
        idem = f"ENTRY:{symbol}:{result.candle_time}"
        oid = await self.om.place(
            symbol=symbol, side="BUY", order_type=ex.order_type, quantity=decision.quantity, purpose="ENTRY",
            idempotency_key=idem, expected_price=entry_price, price=price, signal_id=signal_id,
            reason=f"strategy BUY score {result.score:.1f}", timeout_seconds=ex.limit_timeout_seconds,
            meta={"stop_loss": str(decision.stop_loss), "take_profit": str(decision.take_profit),
                  "trailing_stop_pct": str(rs.trailing_stop_pct) if rs.trailing_stop_enabled else None,
                  "strategy_name": result.strategy_name, "risk": decision.to_dict()},
        )
        self._set_signal_status(signal_id, "ORDER_PLACED" if oid else "DUPLICATE_BLOCKED")

    # ================================================================== exits
    async def exit_position_for(self, symbol: str, reason: str, signal_id: int | None = None) -> None:
        with session_scope() as s:
            pos = s.scalar(select(Position).where(Position.execution_mode == self.mode, Position.symbol == symbol,
                                                  Position.status == "OPEN"))
            pid = pos.id if pos else None
        if pid:
            await self.close_position(pid, reason)
            if signal_id:
                self._set_signal_status(signal_id, "EXIT_ORDER_PLACED")

    async def close_position(self, position_id: int, reason: str) -> int | None:
        assert self.om is not None
        if not self.has_lease:
            return None  # only the lease holder may place orders
        with session_scope() as s:
            pos = s.get(Position, position_id)
            if pos is None or pos.status != "OPEN" or pos.execution_mode != self.mode:
                return None
            symbol, qty = pos.symbol, pos.quantity
            attempts = s.scalar(select(func.count()).select_from(TradingOrder).where(
                TradingOrder.position_id == position_id, TradingOrder.purpose == "EXIT")) or 0
            ex = get_execution(s)
        f = self.filters.get(symbol)
        if f is None:
            return None
        q = price_cache.get(symbol)
        px = (q.bid if q and q.bid else None) or pos.current_price or pos.average_entry_price
        sell_qty = f.round_qty(qty, market=ex.allow_market_orders)
        if sell_qty <= 0 or f.validate(sell_qty, px, market=ex.allow_market_orders):
            with session_scope() as s:
                p = s.get(Position, position_id)
                errs = f.validate(sell_qty, px, market=ex.allow_market_orders)
                if sell_qty < max(f.min_qty, f.market_min_qty) or (sell_qty * px < f.min_notional):
                    pf.write_off_dust(s, p, reason)
                    pf.close_position(s, p, px, f"{reason}_DUST")
                    log_event(s, "portfolio", f"{symbol} position below exchange minimums; closed as dust", "WARNING")
                else:
                    log_event(s, "orders", f"Cannot exit {symbol}: {errs}", "ERROR", category="order_rejected")
            return None
        order_type = "MARKET" if ex.allow_market_orders else "LIMIT"
        price = None if order_type == "MARKET" else f.round_price(px * (1 - ex.limit_offset_bps / Decimal("10000")), "SELL")
        return await self.om.place(symbol=symbol, side="SELL", order_type=order_type, quantity=sell_qty, purpose="EXIT",
                                   idempotency_key=f"EXIT:{position_id}:{attempts}", expected_price=px, price=price,
                                   position_id=position_id, reason=reason, meta={"exit_reason": reason},
                                   timeout_seconds=ex.limit_timeout_seconds)

    async def _position_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.wait_for(self._position_check.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
            self._position_check.clear()
            try:
                await self.monitor_positions()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("position monitor failed")
            await asyncio.sleep(0.25)  # throttle

    async def monitor_positions(self) -> None:
        """Stop-loss / take-profit / trailing-stop management. Active in every control state."""
        with session_scope() as s:
            rs = get_risk(s)
            positions = [(p.id, p.symbol) for p in pf.open_positions(s, self.mode) if p.status == "OPEN"]
        for pid, symbol in positions:
            q = price_cache.get(symbol)
            if q is None or q.age() > rs.stale_data_seconds or not q.bid:
                if symbol not in self.stale_reported:
                    self.stale_reported.add(symbol)
                    with session_scope() as s:
                        risk_event(s, "STALE_POSITION_PRICE", "WARNING",
                                   f"No fresh price for open position {symbol}; stop-loss/take-profit cannot be evaluated")
                continue
            self.stale_reported.discard(symbol)
            trigger = None
            with session_scope() as s:
                p = s.get(Position, pid)
                if p is None or p.status != "OPEN":
                    continue
                p.current_price = q.bid
                p.unrealised_pnl = (p.quantity * q.bid - p.cost_basis - p.entry_fees_open).quantize(Decimal("0.00000001"))
                if p.highest_price is None or q.bid > p.highest_price:
                    p.highest_price = q.bid
                if p.trailing_stop_pct and p.stop_loss is not None:
                    f = self.filters.get(symbol)
                    new_stop = update_trailing_stop(p.stop_loss, p.highest_price, p.trailing_stop_pct,
                                                    f.tick_size if f else Decimal("0.00000001"))
                    if new_stop > p.stop_loss:
                        p.stop_loss = new_stop
                trigger = exit_trigger(q.bid, p.stop_loss, p.take_profit)
            if trigger:
                with session_scope() as s:
                    log_event(s, "trading", f"{symbol} {trigger} triggered at bid {q.bid}", "INFO", category="exit_trigger")
                await self.close_position(pid, trigger)

    # ================================================================== periodic tasks
    async def reconcile_tick(self) -> None:
        if self.om:
            await self.om.reconcile()

    async def risk_tick(self) -> None:
        with session_scope() as s:
            rs = get_risk(s)
            acct = self.account_snapshot(s)
        breach = check_hard_limits(acct, rs)
        if breach and not acct.halted:
            await self.halt(*breach)
        for sym in self.top_symbols:
            stale = price_cache.is_stale(sym, rs.stale_data_seconds)
            key = f"stale:{sym}"
            if stale and key not in self.stale_reported and self.stream_status == "connected":
                self.stale_reported.add(key)
                with session_scope() as s:
                    risk_event(s, "STALE_MARKET_DATA", "WARNING", f"{sym} market data is stale; new entries blocked")
            elif not stale:
                self.stale_reported.discard(key)

    async def halt(self, breach_type: str, description: str) -> None:
        with session_scope() as s:
            acct = pf.get_account(s, self.mode, lock=True)
            if acct.halted:
                return
            acct.halted, acct.halt_reason, acct.halted_at = True, f"{breach_type}: {description}", datetime.now(timezone.utc)
            risk_event(s, breach_type, "CRITICAL", f"HARD LIMIT: {description}. New positions blocked until explicit reset; "
                       "open positions remain under stop-loss/take-profit management.")
        bus.publish("dashboard", "halt", {"type": breach_type, "description": description})

    async def snapshot_tick(self) -> None:
        with session_scope() as s:
            snap = pf.record_snapshot(s, self.mode, lambda sym: price_cache.price(sym))
            bus.publish("dashboard", "portfolio", {"total_equity": snap.total_equity, "available_balance": snap.available_balance,
                                                   "invested_capital": snap.invested_capital, "realised_pnl": snap.realised_pnl,
                                                   "unrealised_pnl": snap.unrealised_pnl, "drawdown": snap.drawdown,
                                                   "recorded_at": snap.recorded_at})

    async def lease_tick(self) -> None:
        if not self.has_lease:
            return
        if not self._acquire_lease():
            # Another instance now owns trading. Stand down locally: do NOT touch the
            # shared control state (that would stop the legitimate lease holder too),
            # and stop every loop here so this instance can never place orders.
            log.error("worker lease lost to another instance; this instance stops trading")
            self.has_lease = False
            self._stopping = True
            with session_scope() as s:
                log_event(s, "worker", f"Worker {self.owner} lost its lease to another instance and stood down "
                          "(no orders will be placed by it)", "CRITICAL", category="system")
            self.lease_lost.set()
            return
        self.publish_status()

    async def control_tick(self) -> None:
        with session_scope() as s:
            ctl = get_control(s)
            self._apply_exec_settings(s)
            # Row lock: the API appends to this queue under the same lock, so a request
            # added while we drain it can never be overwritten by the clear below.
            row = s.scalar(select(SystemSetting).where(SystemSetting.setting_key == CLOSE_REQUESTS_KEY).with_for_update())
            requests = list(row.setting_value or []) if row else []
            if row is not None and requests:
                row.setting_value = []
        for req in requests:  # manual close requests queued by the API
            pid = int(req["position_id"])
            oid = await self.close_position(pid, "MANUAL_CLOSE")
            with session_scope() as s:
                log_event(s, "trading", f"Manual close of position {pid} requested by {req.get('user')}: "
                          + ("exit order placed" if oid else "no open position / exit already pending"), category="manual")
        if ctl.execution_mode != self.mode:
            await self.switch_mode(ctl.execution_mode)

    def publish_status(self) -> None:
        """Persist worker status + latest prices so a separate API process can serve them."""
        st = self.status()
        st["heartbeat"] = time.time()
        with session_scope() as s:
            row = s.scalar(select(SystemSetting).where(SystemSetting.setting_key == STATUS_KEY))
            if row is None:
                s.add(SystemSetting(setting_key=STATUS_KEY, setting_value=st))
            else:
                row.setting_value = st
            for pair in s.scalars(select(TradingPair).where(TradingPair.market_rank.is_not(None))):
                q = price_cache.get(pair.symbol)
                if q and q.last:
                    pair.last_price = q.last
                    if q.change_pct is not None:
                        pair.price_change_pct_24h = q.change_pct
                    if q.quote_volume is not None:
                        pair.quote_volume_24h = q.quote_volume

    async def switch_mode(self, mode: str) -> None:
        with session_scope() as s:
            if pf.open_positions(s, self.mode) or s.scalar(select(TradingOrder.id).where(
                    TradingOrder.execution_mode == self.mode, TradingOrder.status.in_(ACTIVE))):
                log_event(s, "worker", f"Mode switch to {mode} deferred: {self.mode} still has open positions/orders", "WARNING")
                return
        try:
            om = self._build_order_manager(mode)
        except RuntimeError as exc:
            with session_scope() as s:
                log_event(s, "worker", f"Mode switch refused: {exc}", "ERROR")
            return
        self.mode, self.om = mode, om
        await self.refresh_exchange_info()
        with session_scope() as s:
            pf.get_account(s, mode, self.settings.initial_balance_usdt)
            log_event(s, "worker", f"Execution mode switched to {mode}", category="system")
        await self.refresh_universe()

    async def discovery_tick(self) -> None:
        with session_scope() as s:
            mk = get_market(s)
        if self.last_discovery_at is None or time.time() - self.last_discovery_at >= mk.ranking_refresh_minutes * 60:
            await self.refresh_exchange_info()
            await self.refresh_universe()

    # ================================================================== status
    def status(self) -> dict:
        return {
            "worker_running": not self._stopping and self.started_at is not None,
            "owner": self.owner,
            "execution_mode": self.mode,
            "uptime_seconds": int(time.time() - self.started_at) if self.started_at else 0,
            "stream": {"status": self.stream_status, "connected": self.stream.connected,
                       "healthy": self.stream.is_healthy(), "reconnects": self.stream.reconnects,
                       "invalid_messages": self.stream.invalid_messages},
            "rest": {"last_error": self.public.last_error, "used_weight_1m": self.public.used_weight_1m},
            "testnet": {"configured": self.testnet is not None,
                        "last_error": self.testnet.last_error if self.testnet else None,
                        "symbols_available": len(self.testnet_symbols) if self.testnet_symbols is not None else None},
            "top_symbols": self.top_symbols,
            "last_discovery_at": self.last_discovery_at,
        }


class PublicTestnetProbe(PublicMarketClient):
    """Unauthenticated read of the testnet exchangeInfo (to flag testnet availability)."""

    def __init__(self, base_url: str) -> None:
        from app.config import TESTNET_REST_HOSTS
        from app.exchange.rest import _BaseClient

        _BaseClient.__init__(self, base_url, TESTNET_REST_HOSTS)

    async def exchange_info(self, symbols=None) -> dict:  # type: ignore[override]
        try:
            return await self._request("GET", "/api/v3/exchangeInfo")
        finally:
            await self.close()

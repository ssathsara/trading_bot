import { useEffect, useState } from "react";
import { useSaveSettings, useSettings, useStatus } from "@/lib/queries";
import { fmtDuration } from "@/lib/format";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle, ErrorBox, Input, PageHeader, Select, Switch } from "@/components/ui";
import { ControlButtons } from "./Dashboard";

// Must match LIVE_CONFIRMATION_PHRASE in backend/app/schemas.py
const LIVE_PHRASE = "I UNDERSTAND THIS USES REAL MONEY";

function Row({ label, ok, detail }: { label: string; ok: boolean | undefined; detail?: string }) {
  return (
    <div className="flex items-center justify-between border-b border-border/60 py-2 text-sm last:border-0">
      <span>{label}</span>
      <span className="flex items-center gap-2 text-xs text-muted">{detail}<Badge tone={ok ? "up" : ok === false ? "down" : "neutral"}>{ok ? "OK" : ok === false ? "DOWN" : "N/A"}</Badge></span>
    </div>
  );
}

export default function SettingsPage() {
  const { data: s, error } = useSettings();
  const { data: st } = useStatus();
  const save = useSaveSettings();
  const [strategy, setStrategy] = useState<any>(null);
  const [market, setMarket] = useState<any>(null);
  const [execution, setExecution] = useState<any>(null);
  // non-null while the LIVE confirmation box is open (holds the typed phrase)
  const [liveDraft, setLiveDraft] = useState<string | null>(null);
  useEffect(() => {
    if (s && !strategy) { setStrategy(s.strategy); setMarket(s.market); setExecution(s.execution); }
  }, [s, strategy]);

  const field = (obj: any, set: (v: any) => void, key: string, label: string) => (
    <label key={key} className="text-xs text-muted">{label}
      <Input className="num mt-1" value={obj?.[key] ?? ""} onChange={(e) => set({ ...obj, [key]: e.target.value })} />
    </label>
  );
  const w = strategy?.weights;

  return (
    <div>
      <PageHeader title="System Settings" description="Paper and testnet are simulations. LIVE mode trades real money and must be enabled in the server's .env."><ControlButtons /></PageHeader>
      <ErrorBox error={error ?? save.error} />
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader><CardTitle>Trading mode</CardTitle></CardHeader>
          <CardContent className="space-y-3 text-sm">
            <div className="flex items-center gap-3">
              <Select value={liveDraft !== null ? "LIVE" : s?.execution_mode ?? "PAPER"} aria-label="Execution mode"
                onChange={(e) => {
                  if (e.target.value === "LIVE") { setLiveDraft(""); return; }
                  setLiveDraft(null);
                  save.mutate({ path: "/api/settings", body: { execution_mode: e.target.value } });
                }}>
                <option value="PAPER">Local paper trading (simulated)</option>
                <option value="TESTNET" disabled={!s?.testnet_configured}>Binance Spot Testnet{!s?.testnet_configured ? " (credentials not configured)" : ""}</option>
                <option value="LIVE" disabled={!s?.live_trading_available}>LIVE — real money{!s?.live_trading_available ? " (not enabled on server)" : ""}</option>
              </Select>
              {s?.execution_mode === "LIVE"
                ? <Badge tone="down">REAL MONEY</Badge>
                : <Badge tone="neutral">Live: {s?.live_trading_available ? `available (cap ${s?.live_max_capital_usdt} USDT)` : "disabled"}</Badge>}
            </div>
            {liveDraft !== null && (
              <div className="space-y-2 rounded-md border border-down/60 bg-down/10 p-3 text-xs" data-testid="live-confirm">
                <p className="font-semibold text-down">LIVE mode places real orders with real money on your Binance account.</p>
                <p>The bot will trade at most {s?.live_max_capital_usdt} USDT (LIVE_MAX_CAPITAL_USDT). Losses are real and the strategy is not proven profitable.
                  To continue, type <span className="font-mono font-semibold">{LIVE_PHRASE}</span>:</p>
                <Input aria-label="Live confirmation" value={liveDraft} onChange={(e) => setLiveDraft(e.target.value)} />
                <div className="flex gap-2">
                  <Button variant="danger"disabled={liveDraft.trim() !== LIVE_PHRASE || save.isPending}
                    onClick={() => save.mutate({ path: "/api/settings", body: { execution_mode: "LIVE", confirm_live: liveDraft.trim() } },
                      { onSuccess: () => setLiveDraft(null) })}>Switch to LIVE</Button>
                  <Button variant="outline" onClick={() => setLiveDraft(null)}>Cancel</Button>
                </div>
              </div>
            )}
            <p className="text-xs text-muted">Changing mode requires the engine to be STOPPED and no open positions in the current mode. Each mode only ever uses its own endpoint and keys.</p>
            <div className="flex items-center gap-2">
              <Switch checked={!!s?.auto_trading} label="Auto-trading" onChange={(v) => save.mutate({ path: "/api/settings", body: { auto_trading: v } })} />
              Auto-trading {s?.auto_trading ? "enabled" : "disabled (signals only)"}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Connections</CardTitle></CardHeader>
          <CardContent>
            <Row label="Database" ok={st?.database.connected} detail={st?.database.dialect} />
            <Row label="Trading worker" ok={st?.worker.alive} detail={st?.worker.uptime_seconds ? `up ${fmtDuration(st.worker.uptime_seconds)}` : undefined} />
            <Row label="Binance market stream" ok={st?.worker.stream?.healthy} detail={`${st?.worker.stream?.status ?? "—"} · ${st?.worker.stream?.reconnects ?? 0} reconnects${st?.worker.stream?.lag_s != null ? ` · ${st.worker.stream.lag_s}s lag` : ""}`} />
            <Row label="Binance REST API" ok={st ? !st.worker.rest?.last_error : undefined} detail={st?.worker.rest?.last_error ?? `weight ${st?.worker.rest?.used_weight_1m ?? 0}/min`} />
            <Row label="Spot Testnet" ok={st?.testnet.configured ? !st.testnet.last_error : undefined}
              detail={st?.testnet.configured ? (st.testnet.last_error ?? "credentials configured") : `not configured · ${st?.testnet.symbols_available ?? "?"} symbols listed`} />
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Strategy configuration</CardTitle><span className="text-xs text-muted">{strategy?.name}</span></CardHeader>
          <CardContent>
            {strategy && (
              <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
                {(["trend_timeframe", "setup_timeframe", "entry_timeframe"] as const).map((k) => (
                  <label key={k} className="text-xs text-muted">{k.replace("_timeframe", "")} timeframe
                    <Select className="mt-1 w-full" value={strategy[k]} onChange={(e) => setStrategy({ ...strategy, [k]: e.target.value })}>
                      {({ trend_timeframe: ["15m", "1h", "4h", "1d"], setup_timeframe: ["5m", "15m", "1h"], entry_timeframe: ["1m", "5m", "15m", "1h"] })[k].map((t) => <option key={t}>{t}</option>)}
                    </Select></label>
                ))}
                {field(strategy, setStrategy, "rsi_entry_min", "RSI entry min")}
                {field(strategy, setStrategy, "rsi_entry_max", "RSI entry max")}
                {field(strategy, setStrategy, "min_adx", "Min ADX")}
                {field(strategy, setStrategy, "min_volume_ratio", "Min volume / MA20")}
                {field(strategy, setStrategy, "min_score", "Min score (0-100)")}
                {field(strategy, setStrategy, "ml_min_probability", "ML min probability")}
                {w && Object.keys(w).map((k) => (
                  <label key={k} className="text-xs text-muted">Weight: {k}
                    <Input className="num mt-1" value={w[k]} onChange={(e) => setStrategy({ ...strategy, weights: { ...w, [k]: e.target.value } })} /></label>
                ))}
                <label className="col-span-full flex items-center gap-2 text-sm"><Switch checked={strategy.exit_on_trend_reversal} label="Exit on reversal"
                  onChange={(v) => setStrategy({ ...strategy, exit_on_trend_reversal: v })} />Exit on trend reversal</label>
                <label className="col-span-full flex items-center gap-2 text-sm"><Switch checked={strategy.ml_filter_enabled} label="ML filter"
                  onChange={(v) => setStrategy({ ...strategy, ml_filter_enabled: v })} />ML filter (requires a validated model; fails closed)</label>
                <label className="col-span-full flex items-center gap-2 text-sm"><Switch checked={!!strategy.market_filter_enabled} label="Market filter"
                  onChange={(v) => setStrategy({ ...strategy, market_filter_enabled: v })} />Market filter: no new buys while the market leader is in a downtrend (trend timeframe; fails closed)</label>
                {strategy.market_filter_enabled && (
                  <label className="text-xs text-muted">Market leader symbol
                    <Input className="num mt-1" value={strategy.market_filter_symbol ?? "BTCUSDT"}
                      onChange={(e) => setStrategy({ ...strategy, market_filter_symbol: e.target.value.toUpperCase() })} /></label>
                )}
              </div>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Market data &amp; execution</CardTitle></CardHeader>
          <CardContent>
            {market && execution && (
              <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
                {field(market, setMarket, "top_n", "Max pairs (0 = all)")}
                {field(market, setMarket, "min_quote_volume_usdt", "Min 24h volume (USDT, 0 = any)")}
                {field(market, setMarket, "ranking_refresh_minutes", "Ranking refresh (min)")}
                {field(market, setMarket, "min_history_candles", "Min history candles")}
                <label className="col-span-full text-xs text-muted">Selected cryptocurrencies (comma-separated; empty = every pair in the universe)
                  <Input className="mt-1" value={(market.selected_symbols ?? []).join(",")}
                    onChange={(e) => setMarket({ ...market, selected_symbols: e.target.value.toUpperCase().split(",").map((x: string) => x.trim()).filter(Boolean) })} /></label>
                <label className="col-span-full text-xs text-muted">Excluded symbols
                  <Input className="mt-1" value={(market.excluded_symbols ?? []).join(",")}
                    onChange={(e) => setMarket({ ...market, excluded_symbols: e.target.value.toUpperCase().split(",").map((x: string) => x.trim()).filter(Boolean) })} /></label>
                <label className="text-xs text-muted">Order type<Select className="mt-1 w-full" value={execution.order_type} onChange={(e) => setExecution({ ...execution, order_type: e.target.value })}>
                  <option>MARKET</option><option>LIMIT</option></Select></label>
                {field(execution, setExecution, "limit_offset_bps", "Limit offset (bps)")}
                {field(execution, setExecution, "limit_timeout_seconds", "Limit timeout (s)")}
                {field(execution, setExecution, "paper_fee_rate", "Paper fee rate")}
                {field(execution, setExecution, "paper_slippage_bps", "Paper slippage (bps)")}
                {field(execution, setExecution, "estimated_fee_rate", "Risk est. fee rate")}
                {field(execution, setExecution, "exchange_stop_buffer_pct", "Exchange stop buffer (%)")}
                <label className="col-span-full flex items-center gap-2 text-sm"><Switch checked={execution.allow_market_orders} label="Allow market orders"
                  onChange={(v) => setExecution({ ...execution, allow_market_orders: v })} />Allow market orders</label>
                <label className="col-span-full flex items-center gap-2 text-sm"><Switch checked={execution.exchange_stop_enabled ?? true} label="Exchange-side stop-loss"
                  onChange={(v) => setExecution({ ...execution, exchange_stop_enabled: v })} />Exchange-side stop-loss (testnet: protects open positions while the bot is offline)</label>
              </div>
            )}
          </CardContent>
        </Card>
      </div>
      <div className="mt-4 flex items-center gap-3">
        <Button disabled={save.isPending} onClick={() => save.mutate({ path: "/api/settings", body: { strategy, market, execution } })}>Save strategy, market &amp; execution</Button>
        {save.isSuccess && <span className="text-xs text-up">Saved.</span>}
        {save.error && Array.isArray((save.error as any).detail) && (
          <span className="text-xs text-down">{(save.error as any).detail.map((d: any) => `${d.loc?.slice(-1)[0]}: ${d.msg}`).join("; ")}</span>
        )}
      </div>
    </div>
  );
}

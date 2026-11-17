import { useMemo } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useAnalysis, useCandles, useHistory, usePositions, useTopCoins } from "@/lib/queries";
import { useLive } from "@/store/live";
import { fmtPct, fmtPrice, fmtQty, fmtUsd, num, pnlClass } from "@/lib/format";
import { Badge, Card, CardContent, CardHeader, CardTitle, Empty, ErrorBox, PageHeader, Select, SignalBadge } from "@/components/ui";
import { CandleChart, LineChart, type PriceLine, type TradeMarker } from "@/components/Charts";
import { useState } from "react";

const TFS = ["1m", "5m", "15m", "1h", "4h", "1d"];

export default function Terminal() {
  const { symbol: param } = useParams();
  const nav = useNavigate();
  const { data: coins } = useTopCoins();
  const symbol = param ?? coins?.items[0]?.symbol ?? "BTCUSDT";
  const [tf, setTf] = useState("15m");
  const { data, error, isLoading } = useCandles(symbol, tf);
  const { data: positions } = usePositions();
  const { data: analysis } = useAnalysis(symbol);
  const { data: history } = useHistory(1, symbol);
  const ticker = useLive((s) => s.tickers[symbol]);
  const k = useLive((s) => s.lastKline[`${symbol}:${tf}`]);
  const pos = positions?.find((p) => p.symbol === symbol);

  const candles = data?.candles ?? [];
  const lines = useMemo<PriceLine[]>(() => pos ? [
    { price: num(pos.average_entry_price)!, label: "Entry", kind: "entry" as const },
    ...(pos.stop_loss ? [{ price: num(pos.stop_loss)!, label: "Stop-loss", kind: "sl" as const }] : []),
    ...(pos.take_profit ? [{ price: num(pos.take_profit)!, label: "Take-profit", kind: "tp" as const }] : []),
  ] : [], [pos]);
  const markers = useMemo<TradeMarker[]>(() => {
    if (!candles.length) return [];
    const first = candles[0].time;
    const out: TradeMarker[] = [];
    for (const t of history?.items ?? []) {
      const open = Math.floor(new Date(t.opened_at).getTime() / 1000);
      if (open >= first) out.push({ time: open, side: "BUY" });
      if (t.closed_at) {
        const close = Math.floor(new Date(t.closed_at).getTime() / 1000);
        if (close >= first) out.push({ time: close, side: "SELL", text: t.exit_reason ?? "SELL" });
      }
    }
    // markers must sit on candle times: snap to the candle containing the event
    return out.map((m) => ({ ...m, time: [...candles].reverse().find((c) => c.time <= m.time)?.time ?? m.time }));
  }, [history, candles]);
  const live = k ? { time: Math.floor(k.open_time / 1000), open: +k.open, high: +k.high, low: +k.low, close: +k.close, volume: +k.volume } : null;
  const rsi = candles.filter((c) => c.rsi != null).map((c) => ({ time: c.time, value: c.rsi as number }));
  const last = candles[candles.length - 1];

  return (
    <div>
      <PageHeader title="Trading Terminal" description="Closed-candle chart with indicators; live candle updates via WebSocket">
        <Select value={symbol} onChange={(e) => nav(`/terminal/${e.target.value}`)} aria-label="Trading pair">
          {(coins?.items ?? [{ symbol }]).map((c) => <option key={c.symbol}>{c.symbol}</option>)}
        </Select>
        <div className="flex rounded-md border border-border" role="group" aria-label="Timeframe">
          {TFS.map((t) => (
            <button key={t} onClick={() => setTf(t)} className={`px-2.5 py-1.5 text-xs ${t === tf ? "bg-primary text-primary-fg" : "text-muted hover:text-foreground"}`}>{t}</button>
          ))}
        </div>
      </PageHeader>
      <div className="mb-3 flex flex-wrap items-baseline gap-x-6 gap-y-1">
        <span className="text-xl font-semibold">{symbol}</span>
        <span className="num text-xl">{fmtPrice(ticker?.price ?? last?.close)}</span>
        <span className={`num text-sm ${pnlClass(ticker?.change_pct)}`}>{fmtPct(ticker?.change_pct)} 24h</span>
        <span className="num text-xs text-muted">bid {fmtPrice(ticker?.bid)} · ask {fmtPrice(ticker?.ask)}</span>
        {data?.quality_issues?.length ? <Badge tone="warn" title={data.quality_issues.join("; ")}>data quality warning</Badge> : null}
      </div>
      <ErrorBox error={error} />
      <div className="grid gap-4 xl:grid-cols-4">
        <Card className="xl:col-span-3">
          <CardContent className="pt-4">
            {isLoading ? <Empty>Loading candles…</Empty> : candles.length ? <CandleChart candles={candles} lines={lines} markers={markers} live={live} /> : <Empty>No candles</Empty>}
            <div className="mt-2 flex flex-wrap gap-4 text-xs text-muted">
              <span><i className="mr-1 inline-block h-0.5 w-3 bg-info align-middle" />EMA20</span>
              <span><i className="mr-1 inline-block h-0.5 w-3 bg-warn align-middle" />EMA50</span>
              <span><i className="mr-1 inline-block h-0.5 w-3 bg-muted align-middle" />EMA200</span>
              <span>dashed: Bollinger (20,2)</span><span>source: {data?.source}</span>
            </div>
            {rsi.length > 0 && (
              <div className="mt-3">
                <div className="text-xs text-muted">RSI (14)</div>
                <LineChart data={rsi} height={120} baseline={50} />
              </div>
            )}
          </CardContent>
        </Card>
        <div className="space-y-4">
          <Card>
            <CardHeader><CardTitle>AI signal</CardTitle><SignalBadge signal={analysis?.signal} /></CardHeader>
            <CardContent className="space-y-2 text-sm">
              {analysis ? (
                <>
                  <div className="flex justify-between"><span className="text-muted">Score</span><span className="num">{num(analysis.score)?.toFixed(1)} / 100</span></div>
                  <div className="flex justify-between"><span className="text-muted">Action</span><span className="text-xs">{analysis.action_status}</span></div>
                  <p className="text-xs text-muted">{analysis.reason}</p>
                </>
              ) : <p className="text-xs text-muted">No analysis for this symbol yet.</p>}
            </CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle>Open position</CardTitle>{pos && <Badge tone="primary">{pos.execution_mode}</Badge>}</CardHeader>
            <CardContent className="space-y-1.5 text-sm">
              {pos ? (
                <>
                  {[["Quantity", fmtQty(pos.quantity)], ["Entry", fmtPrice(pos.average_entry_price)], ["Current", fmtPrice(pos.current_price)],
                    ["Stop-loss", fmtPrice(pos.stop_loss)], ["Take-profit", fmtPrice(pos.take_profit)], ["Invested", fmtUsd(pos.invested_amount)]].map(([l, v]) => (
                    <div key={l} className="flex justify-between"><span className="text-muted">{l}</span><span className="num">{v}</span></div>
                  ))}
                  <div className="flex justify-between"><span className="text-muted">Unrealised</span><span className={`num ${pnlClass(pos.unrealised_pnl)}`}>{fmtUsd(pos.unrealised_pnl, true)}</span></div>
                </>
              ) : <p className="text-xs text-muted">No open position in {symbol}.</p>}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}

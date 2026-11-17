import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, post, type Page } from "@/lib/api";
import { useTopCoins } from "@/lib/queries";
import { fmtPct, fmtPrice, fmtQty, fmtTime, fmtUsd, pnlClass } from "@/lib/format";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle, Empty, ErrorBox, Input, PageHeader, Select, Stat, Table, Td, Th } from "@/components/ui";
import { LineChart } from "@/components/Charts";

interface Run { id: number; symbol: string; timeframe: string; status: string; metrics: Record<string, any> | null; error: string | null; created_at: string; params: any }
interface RunDetail extends Run { equity_curve: { time: string; equity: number }[] | null; trades: any[] | null }

const isoDay = (d: Date) => d.toISOString().slice(0, 10);

export default function Backtest() {
  const { data: coins } = useTopCoins();
  const qc = useQueryClient();
  const [form, setForm] = useState({
    symbol: "BTCUSDT", timeframe: "15m", start: isoDay(new Date(Date.now() - 60 * 86400e3)), end: isoDay(new Date(Date.now() - 86400e3)),
    initial_balance: "10000", fee_rate: "0.001", slippage_bps: "5", risk_pct: "0.5",
  });
  const [selected, setSelected] = useState<number | null>(null);
  const runs = useQuery({
    queryKey: ["backtests"], queryFn: () => api<Page<Run>>("/api/backtest/results"),
    refetchInterval: (q) => (q.state.data?.items.some((r) => r.status === "PENDING" || r.status === "RUNNING") ? 2000 : false),
  });
  const detail = useQuery({ queryKey: ["backtest", selected], enabled: selected !== null, queryFn: () => api<RunDetail>(`/api/backtest/results/${selected}`) });
  const run = useMutation({
    mutationFn: () => post<Run>("/api/backtest/run", { ...form, start: `${form.start}T00:00:00Z`, end: `${form.end}T00:00:00Z` }),
    onSuccess: (r) => { setSelected(r.id); qc.invalidateQueries({ queryKey: ["backtests"] }); },
  });
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm({ ...form, [k]: e.target.value });
  const d = detail.data;
  const m = d?.metrics;

  return (
    <div>
      <PageHeader title="Backtesting" description="Hypothetical historical simulation — next-bar-open fills, fees, slippage, stop-first intrabar rule. Not indicative of future results." />
      <Card className="mb-4">
        <CardContent className="grid grid-cols-2 gap-3 pt-4 md:grid-cols-4 lg:grid-cols-8">
          <label className="text-xs text-muted">Cryptocurrency<Select className="mt-1 w-full" value={form.symbol} onChange={set("symbol")}>
            {(coins?.items ?? [{ symbol: "BTCUSDT" }]).map((c) => <option key={c.symbol}>{c.symbol}</option>)}</Select></label>
          <label className="text-xs text-muted">Timeframe<Select className="mt-1 w-full" value={form.timeframe} onChange={set("timeframe")}>
            {["5m", "15m", "1h", "4h"].map((t) => <option key={t}>{t}</option>)}</Select></label>
          <label className="text-xs text-muted">Start<Input type="date" className="mt-1" value={form.start} onChange={set("start")} /></label>
          <label className="text-xs text-muted">End<Input type="date" className="mt-1" value={form.end} onChange={set("end")} /></label>
          <label className="text-xs text-muted">Initial balance<Input className="mt-1" value={form.initial_balance} onChange={set("initial_balance")} /></label>
          <label className="text-xs text-muted">Fee rate<Input className="mt-1" value={form.fee_rate} onChange={set("fee_rate")} /></label>
          <label className="text-xs text-muted">Slippage (bps)<Input className="mt-1" value={form.slippage_bps} onChange={set("slippage_bps")} /></label>
          <label className="text-xs text-muted">Risk %<Input className="mt-1" value={form.risk_pct} onChange={set("risk_pct")} /></label>
        </CardContent>
        <CardContent className="flex items-center gap-3">
          <Button onClick={() => run.mutate()} disabled={run.isPending}>{run.isPending ? "Submitting…" : "Run backtest"}</Button>
          <span className="text-xs text-muted">Strategy: current trend-following configuration (higher timeframe derived from the base timeframe).</span>
        </CardContent>
        <CardContent><ErrorBox error={run.error} /></CardContent>
      </Card>

      <div className="grid gap-4 xl:grid-cols-3">
        <Card>
          <CardHeader><CardTitle>Runs</CardTitle></CardHeader>
          <Table>
            <thead><tr><Th>#</Th><Th>Pair</Th><Th>Status</Th><Th className="text-right">Return</Th></tr></thead>
            <tbody>
              {runs.data?.items.map((r) => (
                <tr key={r.id} onClick={() => setSelected(r.id)} className={`cursor-pointer hover:bg-surface-2 ${selected === r.id ? "bg-surface-2" : ""}`}>
                  <Td>{r.id}</Td><Td>{r.symbol} {r.timeframe}</Td>
                  <Td><Badge tone={r.status === "DONE" ? "up" : r.status === "FAILED" ? "down" : "warn"}>{r.status}</Badge></Td>
                  <Td className={`num text-right ${pnlClass(r.metrics?.total_return_pct)}`}>{r.metrics ? fmtPct(r.metrics.total_return_pct) : "—"}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
          {!runs.data?.items.length && <Empty>No backtests yet</Empty>}
        </Card>
        <div className="space-y-4 xl:col-span-2">
          {!d && <Card><Empty>Select or run a backtest.</Empty></Card>}
          {d?.status === "FAILED" && <ErrorBox error={new Error(d.error ?? "failed")} />}
          {(d?.status === "PENDING" || d?.status === "RUNNING") && <Card><Empty>Running — fetching historical candles and simulating…</Empty></Card>}
          {m && (
            <>
              <div className="rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-xs text-warn">{m.disclaimer}</div>
              <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                <Stat label="Total return" value={fmtPct(m.total_return_pct)} valueClass={pnlClass(m.total_return_pct)} />
                <Stat label="Net profit" value={fmtUsd(m.net_profit, true)} valueClass={pnlClass(m.net_profit)} />
                <Stat label="Win rate" value={m.win_rate_pct != null ? fmtPct(m.win_rate_pct, false) : "—"} sub={`${m.number_of_trades} trades`} />
                <Stat label="Profit factor" value={m.profit_factor?.toFixed(2) ?? "—"} />
                <Stat label="Max drawdown" value={fmtPct(m.max_drawdown_pct, false)} valueClass="text-down" />
                <Stat label="Sharpe ratio" value={m.sharpe_ratio?.toFixed(2) ?? "—"} sub="annualised, per-bar" />
                <Stat label="Avg win / loss" value={<span className="text-base">{fmtUsd(m.average_win)} / {fmtUsd(m.average_loss)}</span>} />
                <Stat label="Fees · exposure" value={<span className="text-base">{fmtUsd(m.total_fees)} · {fmtPct(m.exposure_pct, false)}</span>} sub={m.halted_at ? `halted by risk limit at ${fmtTime(m.halted_at)}` : `${m.candles} candles`} />
              </div>
              <Card><CardHeader><CardTitle>Equity curve (backtest)</CardTitle></CardHeader>
                <CardContent><LineChart data={(d.equity_curve ?? []).map((e) => ({ time: Math.floor(new Date(e.time).getTime() / 1000), value: e.equity }))} baseline={m.initial_balance} /></CardContent></Card>
              <Card>
                <CardHeader><CardTitle>Simulated trades</CardTitle></CardHeader>
                <Table>
                  <thead><tr><Th>Entry</Th><Th>Exit</Th><Th className="text-right">Entry px</Th><Th className="text-right">Exit px</Th><Th className="text-right">Qty</Th><Th className="text-right">P&amp;L</Th><Th>Reason</Th></tr></thead>
                  <tbody>
                    {(d.trades ?? []).slice(-100).reverse().map((t, i) => (
                      <tr key={i}><Td className="text-muted">{fmtTime(t.entry_time)}</Td><Td className="text-muted">{fmtTime(t.exit_time)}</Td>
                        <Td className="num text-right">{fmtPrice(t.entry_price)}</Td><Td className="num text-right">{fmtPrice(t.exit_price)}</Td>
                        <Td className="num text-right">{fmtQty(t.quantity)}</Td><Td className={`num text-right ${pnlClass(t.pnl)}`}>{fmtUsd(t.pnl, true)}</Td>
                        <Td className="text-xs text-muted">{t.exit_reason}</Td></tr>
                    ))}
                  </tbody>
                </Table>
                {!d.trades?.length && <Empty>No trades were generated in this period.</Empty>}
              </Card>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

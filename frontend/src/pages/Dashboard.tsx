import { Link } from "react-router-dom";
import { Pause, Play, Square } from "lucide-react";
import { useControl, usePerformance, usePositions, useStatus, useSummary } from "@/lib/queries";
import { fmtPct, fmtTime, fmtUsd, num, pnlClass } from "@/lib/format";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle, Empty, ErrorBox, PageHeader, SignalBadge, Stat, Table, Td, Th } from "@/components/ui";
import { LineChart } from "@/components/Charts";

export function ControlButtons() {
  const { data } = useStatus();
  const ctl = useControl();
  const state = data?.control.state;
  return (
    <>
      <Badge tone={state === "RUNNING" ? "up" : state === "PAUSED" ? "warn" : "neutral"}>{state ?? "…"}</Badge>
      <Button size="sm" onClick={() => ctl.mutate(state === "PAUSED" ? "resume" : "start")} disabled={state === "RUNNING" || ctl.isPending}>
        <Play className="h-3.5 w-3.5" />{state === "PAUSED" ? "Resume" : "Start"}
      </Button>
      <Button size="sm" variant="warn" onClick={() => ctl.mutate("pause")} disabled={state !== "RUNNING" || ctl.isPending}>
        <Pause className="h-3.5 w-3.5" />Pause
      </Button>
      <Button size="sm" variant="outline" onClick={() => ctl.mutate("stop")} disabled={state === "STOPPED" || ctl.isPending}>
        <Square className="h-3.5 w-3.5" />Stop
      </Button>
      {ctl.error && <span className="text-xs text-down">{(ctl.error as Error).message}</span>}
    </>
  );
}

export default function Dashboard() {
  const { data, error } = useSummary();
  const { data: positions } = usePositions();
  const { data: perf } = usePerformance();
  const p = data?.portfolio;
  const equity = (perf?.equity_curve ?? []).map((e: any) => ({ time: Math.floor(new Date(e.time).getTime() / 1000), value: Number(e.equity) }));

  return (
    <div>
      <PageHeader title="Dashboard" description="Portfolio and AI trading status (simulated capital)"><ControlButtons /></PageHeader>
      <ErrorBox error={error} />
      {p?.halted && (
        <div className="mb-4 rounded-md border border-down/40 bg-down/10 p-3 text-sm text-down">
          Hard risk limit reached: {p.halt_reason}. New positions are blocked. <Link to="/risk" className="underline">Review and reset</Link>
        </div>
      )}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Total portfolio value" value={fmtUsd(p?.total_equity)} sub={<span className={pnlClass(p?.net_profit)}>{fmtUsd(p?.net_profit, true)} net</span>} />
        <Stat label="Available balance" value={fmtUsd(p?.available_balance)} sub={`${fmtUsd(p?.invested_capital)} invested`} />
        <Stat label="Daily P&L" value={fmtUsd(p?.daily_pnl, true)} valueClass={pnlClass(p?.daily_pnl)} />
        <Stat label="Realised P&L" value={fmtUsd(p?.realised_pnl, true)} valueClass={pnlClass(p?.realised_pnl)} sub={`${fmtUsd(p?.total_fees)} fees paid`} />
        <Stat label="Unrealised P&L" value={fmtUsd(p?.unrealised_pnl, true)} valueClass={pnlClass(p?.unrealised_pnl)} sub="not counted as realised" />
        <Stat label="Win rate" value={p?.win_rate != null ? fmtPct(p.win_rate, false) : "—"} sub={`${p?.winning_trades ?? 0}W / ${p?.losing_trades ?? 0}L`} />
        <Stat label="Active positions" value={p?.open_positions ?? "—"} sub={`max drawdown ${fmtPct(p?.max_drawdown_pct, false)}`} />
        <Stat label="Execution mode" value={data?.execution_mode ?? "—"} sub={`AI trading: ${data?.control.state ?? "—"}${data?.control.auto_trading === false ? " (auto off)" : ""}`} />
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader><CardTitle>Equity curve</CardTitle><span className="text-xs text-muted">snapshots every 60s</span></CardHeader>
          <CardContent>{equity.length > 1 ? <LineChart data={equity} baseline={num(p?.initial_balance) ?? undefined} /> : <Empty>Equity history appears after the worker records snapshots.</Empty>}</CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Open positions</CardTitle><Link to="/positions" className="text-xs text-primary">View all</Link></CardHeader>
          <CardContent className="space-y-2">
            {!positions?.length && <Empty>No open positions</Empty>}
            {positions?.map((pos) => (
              <div key={pos.id} className="flex items-center justify-between rounded-md bg-surface-2 px-3 py-2 text-sm">
                <span className="font-medium">{pos.symbol}</span>
                <span className={`num ${pnlClass(pos.unrealised_pnl)}`}>{fmtUsd(pos.unrealised_pnl, true)}</span>
              </div>
            ))}
          </CardContent>
        </Card>
      </div>

      <Card className="mt-4">
        <CardHeader><CardTitle>Latest AI decisions</CardTitle><Link to="/analysis" className="text-xs text-primary">AI analysis</Link></CardHeader>
        <Table>
          <thead><tr><Th>Time</Th><Th>Symbol</Th><Th>Signal</Th><Th>Score</Th><Th>Action</Th><Th>Reason</Th></tr></thead>
          <tbody>
            {data?.latest_signals.map((s) => (
              <tr key={s.id}>
                <Td className="text-muted">{fmtTime(s.created_at)}</Td>
                <Td><Link to={`/analysis/${s.symbol}`} className="font-medium hover:text-primary">{s.symbol}</Link></Td>
                <Td><SignalBadge signal={s.signal} /></Td>
                <Td className="num">{num(s.confidence_score)?.toFixed(1)}</Td>
                <Td className="text-xs text-muted">{s.action_status}</Td>
                <Td className="max-w-[28rem] truncate text-xs text-muted" title={s.reason}>{s.reason}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
        {!data?.latest_signals.length && <Empty>No signals yet — the worker analyses each symbol when a candle closes.</Empty>}
      </Card>
    </div>
  );
}

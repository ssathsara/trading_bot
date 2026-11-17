import { usePerformance } from "@/lib/queries";
import { fmtPct, fmtUsd, num, pnlClass } from "@/lib/format";
import { Card, CardContent, CardHeader, CardTitle, Empty, ErrorBox, PageHeader, Stat, Table, Td, Th } from "@/components/ui";
import { BarChart, LineChart } from "@/components/Charts";

const ts = (s: string) => Math.floor(new Date(s).getTime() / 1000);

export default function Analytics() {
  const { data, error } = usePerformance();
  const s = data?.summary;
  const equity = (data?.equity_curve ?? []).map((e: any) => ({ time: ts(e.time), value: Number(e.equity) }));
  const dd = (data?.equity_curve ?? []).map((e: any) => ({ time: ts(e.time), value: -Number(e.drawdown) }));
  const daily = (data?.daily_pnl ?? []).map((d: any) => ({ time: ts(`${d.date}T00:00:00Z`), value: Number(d.pnl) }));
  const monthly = (data?.monthly_pnl ?? []).map((d: any) => ({ time: ts(`${d.month}-01T00:00:00Z`), value: Number(d.pnl) }));
  const volume = (data?.trading_volume ?? []).map((d: any) => ({ time: ts(`${d.date}T00:00:00Z`), value: Number(d.volume) }));

  return (
    <div>
      <PageHeader title="Analytics" description={`Actual ${data?.execution_mode ?? ""} performance from recorded executions (backtests are reported separately)`} />
      <ErrorBox error={error} />
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        <Stat label="Net profit" value={fmtUsd(s?.net_profit, true)} valueClass={pnlClass(s?.net_profit)} />
        <Stat label="Win rate" value={s?.win_rate != null ? fmtPct(s.win_rate, false) : "—"} sub={`${s?.closed_trades ?? 0} closed trades`} />
        <Stat label="Profit factor" value={num(s?.profit_factor)?.toFixed(2) ?? "—"} sub="gross win / gross loss" />
        <Stat label="Max drawdown" value={fmtPct(s?.max_drawdown_pct, false)} valueClass="text-down" />
        <Stat label="Avg win / loss" value={<span className="text-base">{fmtUsd(s?.average_win)} / {fmtUsd(s?.average_loss)}</span>} />
      </div>
      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Card><CardHeader><CardTitle>Portfolio equity curve</CardTitle></CardHeader>
          <CardContent>{equity.length > 1 ? <LineChart data={equity} baseline={num(s?.initial_balance) ?? undefined} /> : <Empty>Not enough snapshots yet</Empty>}</CardContent></Card>
        <Card><CardHeader><CardTitle>Drawdown (%)</CardTitle></CardHeader>
          <CardContent>{dd.length > 1 ? <LineChart data={dd} color="#f43f5e" /> : <Empty>Not enough snapshots yet</Empty>}</CardContent></Card>
        <Card><CardHeader><CardTitle>Daily P&amp;L (realised)</CardTitle></CardHeader>
          <CardContent>{daily.length ? <BarChart data={daily} /> : <Empty>No closed trades yet</Empty>}</CardContent></Card>
        <Card><CardHeader><CardTitle>Monthly P&amp;L (realised)</CardTitle></CardHeader>
          <CardContent>{monthly.length ? <BarChart data={monthly} /> : <Empty>No closed trades yet</Empty>}</CardContent></Card>
        <Card><CardHeader><CardTitle>Trading volume (USDT)</CardTitle></CardHeader>
          <CardContent>{volume.length ? <BarChart data={volume} /> : <Empty>No executions yet</Empty>}</CardContent></Card>
        <Card>
          <CardHeader><CardTitle>Performance by cryptocurrency</CardTitle></CardHeader>
          <Table>
            <thead><tr><Th>Symbol</Th><Th className="text-right">Trades</Th><Th className="text-right">Win rate</Th><Th className="text-right">P&amp;L</Th></tr></thead>
            <tbody>
              {(data?.by_symbol ?? []).map((r: any) => (
                <tr key={r.symbol}><Td className="font-medium">{r.symbol}</Td><Td className="num text-right">{r.trades}</Td>
                  <Td className="num text-right">{fmtPct((r.wins / r.trades) * 100, false, 1)}</Td>
                  <Td className={`num text-right ${pnlClass(r.pnl)}`}>{fmtUsd(r.pnl, true)}</Td></tr>
              ))}
            </tbody>
          </Table>
          {!data?.by_symbol?.length && <Empty>No closed trades yet</Empty>}
        </Card>
        <Card className="lg:col-span-2">
          <CardHeader><CardTitle>Performance by strategy</CardTitle></CardHeader>
          <Table>
            <thead><tr><Th>Strategy</Th><Th className="text-right">Trades</Th><Th className="text-right">Win rate</Th><Th className="text-right">P&amp;L</Th></tr></thead>
            <tbody>
              {(data?.by_strategy ?? []).map((r: any) => (
                <tr key={r.strategy}><Td className="font-medium">{r.strategy}</Td><Td className="num text-right">{r.trades}</Td>
                  <Td className="num text-right">{fmtPct((r.wins / r.trades) * 100, false, 1)}</Td>
                  <Td className={`num text-right ${pnlClass(r.pnl)}`}>{fmtUsd(r.pnl, true)}</Td></tr>
              ))}
            </tbody>
          </Table>
          {!data?.by_strategy?.length && <Empty>No closed trades yet</Empty>}
        </Card>
      </div>
    </div>
  );
}

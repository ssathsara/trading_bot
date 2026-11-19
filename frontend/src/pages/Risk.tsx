import { useEffect, useState } from "react";
import { useControl, useRiskEvents, useRiskStatus, useSaveSettings, useSettings } from "@/lib/queries";
import { fmtTime, num } from "@/lib/format";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle, Empty, ErrorBox, Input, PageHeader, Select, Switch, Table, Td, Th } from "@/components/ui";

const FIELDS: [string, string, string?][] = [
  ["risk_per_trade_pct", "Risk per trade (% equity)"],
  ["max_daily_loss_pct", "Max daily loss (%) — hard halt"],
  ["max_drawdown_pct", "Max drawdown (%) — hard halt"],
  ["max_open_positions", "Max open positions"],
  ["max_position_alloc_pct", "Max allocation per position (%)"],
  ["max_total_alloc_pct", "Max total allocation (%)"],
  ["min_reward_risk", "Min reward:risk (after costs)"],
  ["max_order_notional_usdt", "Max order size (USDT)"],
  ["max_consecutive_losses", "Max consecutive losses"],
  ["cooldown_minutes", "Cooldown after losses (min)"],
  ["max_atr_pct", "Volatility limit (ATR % of price)"],
  ["stale_data_seconds", "Stale-data threshold (s)"],
  ["stop_loss_atr_mult", "Stop-loss ATR multiple", "ATR"],
  ["stop_loss_pct", "Stop-loss %", "PERCENT"],
  ["take_profit_rr", "Take-profit reward:risk", "RR"],
  ["take_profit_pct", "Take-profit %", "PERCENT_TP"],
  ["trailing_stop_pct", "Trailing stop %"],
];

const DYN_FIELDS: [string, string][] = [
  ["dyn_trail_atr_mult", "Trail stop: ATR multiple under high"],
  ["dyn_breakeven_atr_mult", "Break-even after +N × ATR"],
  ["dyn_tp_extend_adx", "Raise take-profit when ADX ≥"],
  ["dyn_tp_extend_atr_mult", "Raised take-profit: close + N × ATR"],
  ["dyn_tp_tighten_rsi", "Lower take-profit when RSI ≥"],
  ["dyn_tp_tighten_atr_mult", "Lowered take-profit: close + N × ATR"],
];

export default function Risk() {
  const settings = useSettings();
  const status = useRiskStatus();
  const events = useRiskEvents();
  const save = useSaveSettings();
  const ctl = useControl();
  const [form, setForm] = useState<Record<string, any> | null>(null);
  useEffect(() => { if (settings.data && !form) setForm(settings.data.risk); }, [settings.data, form]);

  const visible = (cond?: string) => !cond || (cond === "ATR" && form?.stop_loss_mode === "ATR") || (cond === "PERCENT" && form?.stop_loss_mode === "PERCENT")
    || (cond === "RR" && form?.take_profit_mode === "RR") || (cond === "PERCENT_TP" && form?.take_profit_mode === "PERCENT");

  return (
    <div>
      <PageHeader title="Risk Management" description="The risk engine can veto any AI signal. Hard limits halt new entries until explicitly reset." />
      {status.data?.halted && (
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-md border border-down/40 bg-down/10 p-3 text-sm text-down">
          <span><b>Trading halted:</b> {status.data.halt_reason}. Open positions remain under stop-loss/take-profit management.</span>
          <Button variant="danger" size="sm" disabled={ctl.isPending}
            onClick={() => { if (confirm("Reset the hard-limit halt? Daily and peak equity baselines restart from current equity.")) ctl.mutate("risk/reset"); }}>
            Acknowledge &amp; reset
          </Button>
        </div>
      )}
      <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-5">
        {status.data?.metrics.map((mt) => {
          const v = num(mt.value) ?? 0, lim = num(mt.limit) ?? 1;
          const ratio = Math.min(v / lim, 1);
          const tone = ratio >= 1 ? "bg-down" : ratio >= 0.75 ? "bg-warn" : "bg-up";
          return (
            <Card key={mt.name} className="p-4">
              <div className="flex items-center justify-between text-xs text-muted">{mt.name}{mt.hard && <Badge tone="down">hard</Badge>}</div>
              <div className="num mt-1 text-lg font-semibold">{mt.unit ? v.toFixed(2) : v}{mt.unit} <span className="text-xs text-muted">/ {mt.unit ? lim.toFixed(2) : lim}{mt.unit}</span></div>
              <div className="mt-2 h-1.5 rounded bg-surface-2"><div className={`h-1.5 rounded ${tone}`} style={{ width: `${ratio * 100}%` }} /></div>
              {ratio >= 1 && <div className="mt-1 text-xs text-down">limit reached</div>}
            </Card>
          );
        })}
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader><CardTitle>Limits</CardTitle></CardHeader>
          <CardContent>
            {form && (
              <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); save.mutate({ path: "/api/settings/risk", body: form }); }}>
                <div className="grid grid-cols-2 gap-3">
                  <label className="text-xs text-muted">Stop-loss mode<Select className="mt-1 w-full" value={form.stop_loss_mode} onChange={(e) => setForm({ ...form, stop_loss_mode: e.target.value })}>
                    <option value="ATR">ATR-based</option><option value="PERCENT">Percentage</option></Select></label>
                  <label className="text-xs text-muted">Take-profit mode<Select className="mt-1 w-full" value={form.take_profit_mode} onChange={(e) => setForm({ ...form, take_profit_mode: e.target.value })}>
                    <option value="RR">Reward:risk multiple</option><option value="PERCENT">Percentage</option></Select></label>
                  {FIELDS.filter(([, , c]) => visible(c)).map(([k, label]) => (
                    <label key={k} className="text-xs text-muted">{label}
                      <Input className="num mt-1" value={form[k] ?? ""} onChange={(e) => setForm({ ...form, [k]: e.target.value })} inputMode="decimal" />
                    </label>
                  ))}
                </div>
                <div className="flex items-center gap-2 text-sm">
                  <Switch checked={!!form.trailing_stop_enabled} onChange={(v) => setForm({ ...form, trailing_stop_enabled: v })} label="Trailing stop" />Trailing stop enabled
                </div>
                <div className="border-t border-border pt-3">
                  <div className="flex items-center gap-2 text-sm">
                    <Switch checked={!!form.dynamic_exits_enabled} onChange={(v) => setForm({ ...form, dynamic_exits_enabled: v })} label="Dynamic exits" />Dynamic stop-loss / take-profit
                  </div>
                  <p className="mt-1 text-xs text-muted">Each open position's exits are re-analysed on every closed candle of its own symbol. The stop-loss only moves up; the take-profit rises in strong trends and comes down when momentum fades.</p>
                  {form.dynamic_exits_enabled && (
                    <div className="mt-3 grid grid-cols-2 gap-3">
                      {DYN_FIELDS.map(([k, label]) => (
                        <label key={k} className="text-xs text-muted">{label}
                          <Input className="num mt-1" value={form[k] ?? ""} onChange={(e) => setForm({ ...form, [k]: e.target.value })} inputMode="decimal" />
                        </label>
                      ))}
                    </div>
                  )}
                </div>
                <ErrorBox error={save.error} />
                {save.error && (save.error as any).detail && Array.isArray((save.error as any).detail) && (
                  <ul className="text-xs text-down">{(save.error as any).detail.map((d: any, i: number) => <li key={i}>{d.loc?.slice(-1)[0]}: {d.msg}</li>)}</ul>
                )}
                <div className="flex items-center gap-3">
                  <Button type="submit" disabled={save.isPending}>Save risk settings</Button>
                  {save.isSuccess && <span className="text-xs text-up">Saved — applies to the next decision.</span>}
                </div>
                <p className="text-xs text-muted">Defaults are conservative test parameters, not guarantees of safety or profitability.</p>
              </form>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Risk events &amp; warnings</CardTitle></CardHeader>
          <Table>
            <thead><tr><Th>Time</Th><Th>Type</Th><Th>Severity</Th><Th>Description</Th></tr></thead>
            <tbody>
              {events.data?.items.map((e) => (
                <tr key={e.id}><Td className="text-muted">{fmtTime(e.created_at)}</Td><Td className="text-xs">{e.event_type}</Td>
                  <Td><Badge tone={e.severity === "CRITICAL" ? "down" : e.severity === "WARNING" ? "warn" : "neutral"}>{e.severity}</Badge></Td>
                  <Td className="max-w-[26rem] whitespace-normal text-xs text-muted">{e.description}</Td></tr>
              ))}
            </tbody>
          </Table>
          {!events.data?.items.length && <Empty>No risk events</Empty>}
        </Card>
      </div>
    </div>
  );
}

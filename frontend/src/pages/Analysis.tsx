import { useNavigate, useParams } from "react-router-dom";
import { CheckCircle2, XCircle } from "lucide-react";
import { useAnalysis, useTopCoins } from "@/lib/queries";
import { fmtPct, fmtPrice, fmtTime, num } from "@/lib/format";
import { Badge, Card, CardContent, CardHeader, CardTitle, Empty, ErrorBox, PageHeader, Select, SignalBadge } from "@/components/ui";
import type { Condition } from "@/lib/api";

const LABELS: Record<string, string> = {
  close: "Close", rsi: "RSI(14)", ema_9: "EMA 9", ema_20: "EMA 20", ema_50: "EMA 50", ema_200: "EMA 200", macd: "MACD",
  macd_signal: "MACD signal", macd_hist: "MACD hist", bb_upper: "BB upper", bb_lower: "BB lower", atr: "ATR(14)", adx: "ADX(14)",
  plus_di: "+DI", minus_di: "−DI", volume: "Volume", volume_ma: "Volume MA20",
};

function Conditions({ items }: { items: Condition[] }) {
  if (!items.length) return <Empty>No conditions evaluated</Empty>;
  return (
    <ul className="space-y-1.5">
      {items.map((c) => (
        <li key={c.name} className="flex items-start gap-2 text-sm">
          {c.passed ? <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-up" /> : <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-down" />}
          <div><span className="font-medium">{c.name.replaceAll("_", " ")}</span>{c.gate && <Badge className="ml-1.5">required</Badge>}
            <div className="text-xs text-muted">{c.detail}</div></div>
        </li>
      ))}
    </ul>
  );
}

export default function AnalysisPage() {
  const { symbol: param } = useParams();
  const nav = useNavigate();
  const { data: coins } = useTopCoins();
  const symbol = param ?? coins?.items[0]?.symbol;
  const { data: a, error, isLoading } = useAnalysis(symbol);

  return (
    <div>
      <PageHeader title="AI Analysis" description="Rule-based multi-timeframe evaluation. Explanations list only conditions that were actually computed.">
        <Select value={symbol ?? ""} onChange={(e) => nav(`/analysis/${e.target.value}`)} aria-label="Symbol">
          {(coins?.items ?? []).map((c) => <option key={c.symbol}>{c.symbol}</option>)}
        </Select>
      </PageHeader>
      {isLoading && <Empty>Loading…</Empty>}
      {error && <ErrorBox error={error} />}
      {a && (
        <div className="space-y-4">
          <Card>
            <CardContent className="flex flex-wrap items-center gap-x-8 gap-y-3 pt-4">
              <div><div className="text-xs text-muted">Signal</div><div className="mt-1"><SignalBadge signal={a.signal} /></div></div>
              <div><div className="text-xs text-muted">Strategy score</div><div className="num text-xl font-semibold">{num(a.score)?.toFixed(1)}<span className="text-sm text-muted"> / 100</span></div></div>
              <div><div className="text-xs text-muted">Overall trend ({a.timeframes.trend})</div><div className="font-medium">{a.market.trend?.trend ?? "—"}</div></div>
              <div><div className="text-xs text-muted">Market condition ({a.timeframes.setup})</div>
                <div className="text-sm">{a.market.setup ? `${a.market.setup.trend} · RSI ${a.market.setup.rsi_state.toLowerCase()} · volume ${a.market.setup.volume_state.toLowerCase()} · volatility ${a.market.setup.volatility_state.toLowerCase()}` : "—"}</div></div>
              <div><div className="text-xs text-muted">Action</div><div className="text-sm">{a.action_status}</div></div>
              <div><div className="text-xs text-muted">Evaluated</div><div className="text-sm">{fmtTime(a.created_at)}</div></div>
            </CardContent>
            <CardContent>
              <p className="rounded-md bg-surface-2 p-3 text-sm">{a.reason}</p>
              <p className="mt-2 text-xs text-muted">{a.score_note}</p>
            </CardContent>
          </Card>
          <div className="grid gap-4 lg:grid-cols-2">
            <Card><CardHeader><CardTitle>Entry conditions</CardTitle></CardHeader><CardContent><Conditions items={a.entry_conditions} /></CardContent></Card>
            <Card><CardHeader><CardTitle>Exit conditions</CardTitle><span className="text-xs text-muted">plus stop-loss / take-profit</span></CardHeader>
              <CardContent><Conditions items={a.exit_conditions} /></CardContent></Card>
          </div>
          <Card>
            <CardHeader><CardTitle>Technical indicators</CardTitle><span className="text-xs text-muted">latest closed candle per timeframe</span></CardHeader>
            <CardContent className="grid gap-4 md:grid-cols-3">
              {(["trend", "setup", "entry"] as const).map((role) => {
                const snap = a.indicators[role] ?? {};
                const m = a.market[role];
                return (
                  <div key={role}>
                    <div className="mb-2 text-xs font-semibold uppercase text-muted">{role} · {a.timeframes[role]}</div>
                    <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-sm">
                      {Object.entries(LABELS).filter(([k]) => k in snap).map(([k, label]) => (
                        <div key={k} className="contents"><dt className="text-muted">{label}</dt><dd className="num text-right">{k === "rsi" || k === "adx" ? num(snap[k])?.toFixed(2) ?? "—" : fmtPrice(snap[k] as number)}</dd></div>
                      ))}
                      {m && <>
                        <dt className="text-muted">Support</dt><dd className="num text-right">{fmtPrice(m.support)}</dd>
                        <dt className="text-muted">Resistance</dt><dd className="num text-right">{fmtPrice(m.resistance)}</dd>
                        <dt className="text-muted">ATR % price</dt><dd className="num text-right">{fmtPct(m.atr_pct, false)}</dd>
                      </>}
                    </dl>
                  </div>
                );
              })}
            </CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle>Machine-learning filter</CardTitle></CardHeader>
            <CardContent className="text-sm">
              {a.ml ? (
                <div className="space-y-1">
                  <Badge tone={a.ml.validated ? "up" : "warn"}>{a.ml.validated ? "validated (filter only)" : "not validated — unused"}</Badge>
                  <p className="text-muted">{a.ml.note}</p>
                  <p className="num text-xs text-muted">Out-of-sample: AUC {a.ml.test.auc?.toFixed(3) ?? "—"} · precision {a.ml.test.precision?.toFixed(3)} vs base rate {a.ml.test.base_rate?.toFixed(3)} (n={a.ml.test.n})</p>
                </div>
              ) : <p className="text-muted">No model trained for this symbol. The ML module is optional and can only filter (never create) BUY signals.</p>}
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}

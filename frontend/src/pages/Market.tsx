import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowDown, ArrowUp } from "lucide-react";
import { useTopCoins } from "@/lib/queries";
import { useLive } from "@/store/live";
import { fmtCompact, fmtPct, fmtPrice, num, pnlClass } from "@/lib/format";
import { Badge, Card, Empty, ErrorBox, Input, PageHeader, SignalBadge, Table, Td, Th } from "@/components/ui";
import type { Coin } from "@/lib/api";

type Key = "rank" | "symbol" | "price" | "change" | "volume" | "rsi" | "confidence";

export function mergeLive(c: Coin, t?: { price: string; change_pct: string; quote_volume: string }) {
  return { ...c, price: t?.price ?? c.price, change_pct_24h: t?.change_pct ?? c.change_pct_24h, quote_volume_24h: t?.quote_volume ?? c.quote_volume_24h };
}

export default function Market() {
  const { data, error, isLoading } = useTopCoins();
  const tickers = useLive((s) => s.tickers);
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<{ key: Key; dir: 1 | -1 }>({ key: "rank", dir: 1 });

  const rows = useMemo(() => {
    const value = (c: Coin): number | string => ({
      rank: c.rank, symbol: c.symbol, price: num(c.price) ?? 0, change: num(c.change_pct_24h) ?? 0,
      volume: num(c.quote_volume_24h) ?? 0, rsi: num(c.rsi) ?? -1, confidence: num(c.confidence) ?? -1,
    })[sort.key];
    return (data?.items ?? [])
      .map((c) => mergeLive(c, tickers[c.symbol]))
      .filter((c) => !q || c.symbol.includes(q.toUpperCase()) || c.name.toLowerCase().includes(q.toLowerCase()))
      .sort((a, b) => (value(a) > value(b) ? 1 : value(a) < value(b) ? -1 : 0) * sort.dir);
  }, [data, tickers, q, sort]);

  const H = ({ k, children, right }: { k: Key; children: React.ReactNode; right?: boolean }) => (
    <Th className={right ? "text-right" : ""}>
      <button className="inline-flex items-center gap-1 hover:text-foreground" onClick={() => setSort((s) => ({ key: k, dir: s.key === k ? (-s.dir as 1 | -1) : 1 }))}>
        {children}{sort.key === k && (sort.dir === 1 ? <ArrowUp className="h-3 w-3" /> : <ArrowDown className="h-3 w-3" />)}
      </button>
    </Th>
  );

  return (
    <div>
      <PageHeader title="Live Market" description="Top eligible USDT spot pairs by 24h quote volume (stablecoins, leveraged & tokenized-stock assets excluded)">
        <Input placeholder="Search coin or symbol" value={q} onChange={(e) => setQ(e.target.value)} className="w-56" aria-label="Search" />
      </PageHeader>
      <ErrorBox error={error} />
      <Card>
        <Table>
          <thead>
            <tr>
              <H k="rank">#</H><Th>Coin</Th><H k="symbol">Symbol</H><H k="price" right>Price</H><H k="change" right>24h</H>
              <H k="volume" right>Volume (USDT)</H><H k="rsi" right>RSI</H><Th className="text-right">MACD</Th>
              <Th>AI signal</Th><H k="confidence" right>Confidence</H><Th>Action status</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => (
              <tr key={c.symbol} className="hover:bg-surface-2/60">
                <Td className="text-muted">{c.rank}</Td>
                <Td className="font-medium">{c.name}</Td>
                <Td><Link to={`/terminal/${c.symbol}`} className="hover:text-primary">{c.symbol}</Link></Td>
                <Td className="num text-right">{fmtPrice(c.price)}</Td>
                <Td className={`num text-right ${pnlClass(c.change_pct_24h)}`}>{fmtPct(c.change_pct_24h)}</Td>
                <Td className="num text-right">{fmtCompact(c.quote_volume_24h)}</Td>
                <Td className="num text-right">{num(c.rsi)?.toFixed(1) ?? "—"}</Td>
                <Td className={`num text-right ${num(c.macd) !== null && num(c.macd_signal) !== null ? (num(c.macd)! > num(c.macd_signal)! ? "text-up" : "text-down") : ""}`}>
                  {num(c.macd)?.toPrecision(4) ?? "—"}
                </Td>
                <Td><SignalBadge signal={c.signal} /></Td>
                <Td className="num text-right">{num(c.confidence)?.toFixed(1) ?? "—"}</Td>
                <Td className="space-x-1 text-xs">
                  {!c.enabled ? <Badge tone="warn" title={c.note ?? ""}>{c.note ? "unavailable" : "disabled"}</Badge> : <Badge tone="up">enabled</Badge>}
                  {!c.testnet_supported && <Badge tone="neutral" title="Not listed on Binance Spot Testnet; paper trading only">no testnet</Badge>}
                  <span className="text-muted">{c.action_status && c.action_status !== "NONE" ? c.action_status : ""}</span>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
        {!isLoading && !rows.length && <Empty>No pairs yet — discovery runs when the worker starts.</Empty>}
      </Card>
      <p className="mt-2 text-xs text-muted">Confidence is a 0–100 strategy score, not a probability of profit.</p>
    </div>
  );
}

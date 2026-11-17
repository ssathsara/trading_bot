import { useState } from "react";
import { useHistory, useOrders } from "@/lib/queries";
import { fmtPrice, fmtQty, fmtTime, fmtUsd, num, pnlClass } from "@/lib/format";
import { Badge, Card, Empty, ErrorBox, Input, PageHeader, Pager, Table, Td, Th } from "@/components/ui";

export default function HistoryPage() {
  const [tab, setTab] = useState<"trades" | "orders">("trades");
  const [page, setPage] = useState(1);
  const [symbol, setSymbol] = useState("");
  const sym = /^[A-Z0-9]{2,20}$/.test(symbol) ? symbol : undefined;
  const trades = useHistory(page, sym);
  const orders = useOrders(page, sym);

  return (
    <div>
      <PageHeader title="Trading History" description="Round-trip trades and every order request (simulated / testnet executions only)">
        <Input placeholder="Filter symbol (e.g. BTCUSDT)" value={symbol} onChange={(e) => { setSymbol(e.target.value.toUpperCase()); setPage(1); }} className="w-52" />
        <div className="flex rounded-md border border-border">
          {(["trades", "orders"] as const).map((t) => (
            <button key={t} onClick={() => { setTab(t); setPage(1); }} className={`px-3 py-1.5 text-xs capitalize ${tab === t ? "bg-primary text-primary-fg" : "text-muted"}`}>{t}</button>
          ))}
        </div>
      </PageHeader>
      <ErrorBox error={tab === "trades" ? trades.error : orders.error} />
      <Card>
        {tab === "trades" ? (
          <>
            <Table>
              <thead><tr><Th>Opened</Th><Th>Closed</Th><Th>Symbol</Th><Th>Side</Th><Th className="text-right">Entry</Th><Th className="text-right">Exit</Th>
                <Th className="text-right">Quantity</Th><Th className="text-right">Fees</Th><Th className="text-right">Net P&amp;L</Th><Th>Exit reason</Th><Th>Mode</Th><Th>Status</Th></tr></thead>
              <tbody>
                {trades.data?.items.map((t) => {
                  const fees = (num(t.entry_fee) ?? 0) + (num(t.exit_fee) ?? 0);
                  return (
                    <tr key={t.id}>
                      <Td className="text-muted">{fmtTime(t.opened_at)}</Td>
                      <Td className="text-muted">{fmtTime(t.closed_at)}</Td>
                      <Td className="font-medium">{t.symbol}</Td>
                      <Td><Badge tone="up">BUY</Badge>{t.closed_at && <> → <Badge tone="down">SELL</Badge></>}</Td>
                      <Td className="num text-right">{fmtPrice(t.entry_price)}</Td>
                      <Td className="num text-right">{fmtPrice(t.exit_price)}</Td>
                      <Td className="num text-right">{fmtQty(t.quantity)}</Td>
                      <Td className="num text-right">{fmtUsd(fees)}</Td>
                      <Td className={`num text-right ${pnlClass(t.realised_pnl)}`}>{t.realised_pnl === null ? "open" : fmtUsd(t.realised_pnl, true)}</Td>
                      <Td className="text-xs text-muted">{t.exit_reason ?? "—"}</Td>
                      <Td><Badge tone={t.execution_mode === "TESTNET" ? "info" : "primary"}>{t.execution_mode === "PAPER" ? "PAPER (sim)" : t.execution_mode}</Badge></Td>
                      <Td><Badge tone={t.status === "CLOSED" ? "neutral" : "primary"}>{t.status}</Badge></Td>
                    </tr>
                  );
                })}
              </tbody>
            </Table>
            {!trades.data?.items.length && <Empty>No trades yet.</Empty>}
            {trades.data && <Pager page={page} total={trades.data.total} pageSize={trades.data.page_size} onPage={setPage} />}
          </>
        ) : (
          <>
            <Table>
              <thead><tr><Th>Time</Th><Th>Symbol</Th><Th>Purpose</Th><Th>Side</Th><Th>Type</Th><Th className="text-right">Qty</Th><Th className="text-right">Filled</Th>
                <Th className="text-right">Avg price</Th><Th className="text-right">Fee</Th><Th className="text-right">Slippage</Th><Th>Status</Th><Th>Detail</Th></tr></thead>
              <tbody>
                {orders.data?.items.map((o) => (
                  <tr key={o.id}>
                    <Td className="text-muted">{fmtTime(o.created_at)}</Td>
                    <Td className="font-medium">{o.symbol}</Td>
                    <Td className="text-xs">{o.purpose}</Td>
                    <Td><Badge tone={o.side === "BUY" ? "up" : "down"}>{o.side}</Badge></Td>
                    <Td className="text-xs">{o.order_type}</Td>
                    <Td className="num text-right">{fmtQty(o.quantity)}</Td>
                    <Td className="num text-right">{fmtQty(o.filled_quantity)}</Td>
                    <Td className="num text-right">{fmtPrice(o.average_fill_price)}</Td>
                    <Td className="num text-right">{fmtUsd(o.fee_quote)}</Td>
                    <Td className="num text-right">{o.slippage_bps != null ? `${num(o.slippage_bps)?.toFixed(1)} bps` : "—"}</Td>
                    <Td><Badge tone={o.status === "FILLED" ? "up" : ["REJECTED", "EXPIRED", "CANCELED"].includes(o.status) ? "down" : "warn"}>{o.status}</Badge></Td>
                    <Td className="max-w-[20rem] truncate text-xs text-muted" title={o.error_message ?? o.reason ?? ""}>{o.error_message ?? o.reason ?? ""}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
            {!orders.data?.items.length && <Empty>No orders yet.</Empty>}
            {orders.data && <Pager page={page} total={orders.data.total} pageSize={orders.data.page_size} onPage={setPage} />}
          </>
        )}
      </Card>
    </div>
  );
}

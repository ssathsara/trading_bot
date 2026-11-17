import { useMutation, useQueryClient } from "@tanstack/react-query";
import { post } from "@/lib/api";
import { usePositions } from "@/lib/queries";
import { useLive } from "@/store/live";
import { fmtDuration, fmtPrice, fmtQty, fmtUsd, num, pnlClass } from "@/lib/format";
import { Badge, Button, Card, Empty, ErrorBox, PageHeader, Table, Td, Th } from "@/components/ui";

export default function Positions() {
  const { data, error } = usePositions();
  const tickers = useLive((s) => s.tickers);
  const qc = useQueryClient();
  const close = useMutation({
    mutationFn: (id: number) => post(`/api/trading/close-position/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["positions"] }),
  });

  return (
    <div>
      <PageHeader title="Open Positions" description="Managed by stop-loss / take-profit monitoring in every engine state" />
      <ErrorBox error={error ?? close.error} />
      <Card>
        <Table>
          <thead><tr>
            <Th>Symbol</Th><Th className="text-right">Entry</Th><Th className="text-right">Current</Th><Th className="text-right">Quantity</Th>
            <Th className="text-right">Invested</Th><Th className="text-right">Stop-loss</Th><Th className="text-right">Take-profit</Th>
            <Th className="text-right">Unrealised P&amp;L</Th><Th>Duration</Th><Th>Status</Th><Th />
          </tr></thead>
          <tbody>
            {data?.map((p) => {
              const live = num(tickers[p.symbol]?.bid ?? null);
              const current = live ?? num(p.current_price);
              // Live estimate between server marks, same formula as the backend:
              // qty * bid - cost basis - entry fees still attached to the open quantity.
              const shown = live !== null
                ? num(p.quantity)! * live - num(p.cost_basis)! - (num(p.entry_fees_open) ?? 0)
                : num(p.unrealised_pnl);
              return (
                <tr key={p.id}>
                  <Td className="font-medium">{p.symbol}</Td>
                  <Td className="num text-right">{fmtPrice(p.average_entry_price)}</Td>
                  <Td className="num text-right">{fmtPrice(current)}</Td>
                  <Td className="num text-right">{fmtQty(p.quantity)}</Td>
                  <Td className="num text-right">{fmtUsd(p.invested_amount)}</Td>
                  <Td className="num text-right text-down">{fmtPrice(p.stop_loss)}</Td>
                  <Td className="num text-right text-up">{fmtPrice(p.take_profit)}</Td>
                  <Td className={`num text-right ${pnlClass(shown)}`}>{fmtUsd(shown, true)}</Td>
                  <Td className="text-muted">{fmtDuration(p.duration_seconds)}</Td>
                  <Td><Badge tone={p.status === "OPEN" ? "primary" : "warn"}>{p.status}</Badge></Td>
                  <Td>
                    <Button size="sm" variant="outline" disabled={p.status !== "OPEN" || close.isPending}
                      onClick={() => { if (confirm(`Close ${p.symbol} position with a market exit (${p.execution_mode})?`)) close.mutate(p.id); }}>
                      Close
                    </Button>
                  </Td>
                </tr>
              );
            })}
          </tbody>
        </Table>
        {!data?.length && <Empty>No open positions.</Empty>}
      </Card>
      {close.isSuccess && <p className="mt-2 text-xs text-muted">Close request queued — the worker submits the exit and reconciles the fills.</p>}
    </div>
  );
}

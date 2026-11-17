import { useState } from "react";
import { useLogs } from "@/lib/queries";
import { fmtTime } from "@/lib/format";
import { Badge, Card, Empty, ErrorBox, Input, PageHeader, Pager, Select, Table, Td, Th } from "@/components/ui";

const COMPONENTS = ["", "ai", "orders", "trading", "risk", "websocket", "worker", "system", "discovery", "settings", "portfolio", "backtest", "api"];
const CATEGORY_LABEL: Record<string, string> = {
  ai_decision: "AI decision", order_request: "Order request", order_execution: "Order execution", order_rejected: "Rejected order",
  order_unknown: "Order status unknown", risk: "Risk warning", api_error: "API error", websocket: "WebSocket", system: "System",
  position_closed: "Position closed", exit_trigger: "Exit trigger", manual: "Manual action", discovery: "Discovery", settings: "Settings",
};

export default function Logs() {
  const [page, setPage] = useState(1);
  const [level, setLevel] = useState("");
  const [component, setComponent] = useState("");
  const [q, setQ] = useState("");
  const { data, error } = useLogs(page, level || undefined, component || undefined, q.length >= 2 ? q : undefined);

  return (
    <div>
      <PageHeader title="Activity Logs" description="AI decisions, orders, executions, rejections, risk warnings, API and WebSocket events (secrets are redacted)">
        <Input placeholder="Search" value={q} onChange={(e) => { setQ(e.target.value); setPage(1); }} className="w-44" />
        <Select value={component} onChange={(e) => { setComponent(e.target.value); setPage(1); }} aria-label="Component">
          {COMPONENTS.map((c) => <option key={c} value={c}>{c || "All components"}</option>)}
        </Select>
        <Select value={level} onChange={(e) => { setLevel(e.target.value); setPage(1); }} aria-label="Level">
          {["", "INFO", "WARNING", "ERROR", "CRITICAL"].map((l) => <option key={l} value={l}>{l || "All levels"}</option>)}
        </Select>
      </PageHeader>
      <ErrorBox error={error} />
      <Card>
        <Table>
          <thead><tr><Th>Time</Th><Th>Level</Th><Th>Component</Th><Th>Category</Th><Th>Message</Th></tr></thead>
          <tbody>
            {data?.items.map((l) => {
              const cat = (l.metadata?.category as string | undefined) ?? "";
              return (
                <tr key={l.id}>
                  <Td className="text-muted">{fmtTime(l.created_at)}</Td>
                  <Td><Badge tone={l.level === "ERROR" || l.level === "CRITICAL" ? "down" : l.level === "WARNING" ? "warn" : "neutral"}>{l.level}</Badge></Td>
                  <Td className="text-xs">{l.component}</Td>
                  <Td className="text-xs text-muted">{CATEGORY_LABEL[cat] ?? cat}</Td>
                  <Td className="max-w-[48rem] whitespace-normal text-xs">
                    {l.message}
                    {l.metadata?.simulated === true && <Badge tone="primary" className="ml-2">simulated</Badge>}
                  </Td>
                </tr>
              );
            })}
          </tbody>
        </Table>
        {!data?.items.length && <Empty>No log entries</Empty>}
        {data && <Pager page={page} total={data.total} pageSize={data.page_size} onPage={setPage} />}
      </Card>
    </div>
  );
}

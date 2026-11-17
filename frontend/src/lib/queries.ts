import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, post, put, type Analysis, type CandleRow, type Coin, type Control, type LogRow, type Order, type Page,
  type Portfolio, type Position, type RiskEventRow, type Signal, type Trade } from "./api";

export interface SystemStatus {
  database: { connected: boolean; dialect: string };
  control: Control; execution_mode: string; environment_label: string; live_trading_enabled: boolean;
  worker: { alive: boolean; heartbeat?: number; uptime_seconds?: number; top_symbols?: string[];
    stream?: { status: string; connected: boolean; healthy: boolean; reconnects: number; invalid_messages: number };
    rest?: { last_error: string | null; used_weight_1m: number } };
  testnet: { configured: boolean; last_error?: string | null; symbols_available?: number | null };
  risk: { halted: boolean; halt_reason: string | null; halted_at: string | null };
}

export const useStatus = () => useQuery({ queryKey: ["status"], queryFn: () => api<SystemStatus>("/api/system/status"), refetchInterval: 10_000 });
export const useSummary = () => useQuery({
  queryKey: ["summary"], refetchInterval: 15_000,
  queryFn: () => api<{ portfolio: Portfolio; control: Control; execution_mode: string; worker: SystemStatus["worker"]; latest_signals: Signal[] }>("/api/dashboard/summary"),
});
export const useTopCoins = () => useQuery({ queryKey: ["top-coins"], queryFn: () => api<{ items: Coin[] }>("/api/market/top-coins"), refetchInterval: 30_000 });
export const usePositions = () => useQuery({ queryKey: ["positions"], queryFn: () => api<Position[]>("/api/trading/positions"), refetchInterval: 10_000 });
export const useHistory = (page: number, symbol?: string) => useQuery({
  queryKey: ["history", page, symbol], placeholderData: keepPreviousData,
  queryFn: () => api<Page<Trade>>(`/api/trading/history?page=${page}&page_size=25${symbol ? `&symbol=${symbol}` : ""}`),
});
export const useOrders = (page: number, symbol?: string) => useQuery({
  queryKey: ["orders", page, symbol], placeholderData: keepPreviousData,
  queryFn: () => api<Page<Order>>(`/api/trading/orders?page=${page}&page_size=25${symbol ? `&symbol=${symbol}` : ""}`),
});
export const useSignals = (page: number, symbol?: string, signal?: string) => useQuery({
  queryKey: ["signals", page, symbol, signal], placeholderData: keepPreviousData,
  queryFn: () => api<Page<Signal>>(`/api/ai/signals?page=${page}&page_size=25${symbol ? `&symbol=${symbol}` : ""}${signal ? `&signal=${signal}` : ""}`),
});
export const useAnalysis = (symbol: string | undefined) => useQuery({
  queryKey: ["analysis", symbol], enabled: !!symbol, retry: false,
  queryFn: () => api<Analysis>(`/api/ai/analysis/${symbol}`),
});
export const useCandles = (symbol: string, timeframe: string) => useQuery({
  queryKey: ["candles", symbol, timeframe], refetchInterval: 60_000,
  queryFn: () => api<{ candles: CandleRow[]; source: string; quality_issues: string[] }>(`/api/market/candles/${symbol}?timeframe=${timeframe}&limit=300`),
});
export const useLogs = (page: number, level?: string, component?: string, q?: string) => useQuery({
  queryKey: ["logs", page, level, component, q], placeholderData: keepPreviousData,
  queryFn: () => {
    const p = new URLSearchParams({ page: String(page), page_size: "50" });
    if (level) p.set("level", level);
    if (component) p.set("component", component);
    if (q) p.set("q", q);
    return api<Page<LogRow>>(`/api/system/logs?${p}`);
  },
});
export const useRiskEvents = () => useQuery({ queryKey: ["risk", "events"], queryFn: () => api<Page<RiskEventRow>>("/api/risk/events?page_size=50") });
export const useRiskStatus = () => useQuery({
  queryKey: ["risk", "status"], refetchInterval: 15_000,
  queryFn: () => api<{ halted: boolean; halt_reason: string | null; metrics: { name: string; value: string | number; limit: string | number; unit: string; hard: boolean }[] }>("/api/risk/status"),
});
export const usePerformance = () => useQuery({ queryKey: ["performance"], queryFn: () => api<any>("/api/portfolio/performance"), refetchInterval: 60_000 });
export const useSettings = () => useQuery({ queryKey: ["settings"], queryFn: () => api<any>("/api/settings") });

export function useControl() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (action: "start" | "stop" | "pause" | "resume" | "risk/reset") => post(`/api/system/${action}`),
    onSettled: () => qc.invalidateQueries(),
  });
}

export function useSaveSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ path, body }: { path: string; body: unknown }) => put(path, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }),
  });
}

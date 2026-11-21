import { useAuth } from "@/store/auth";

export class ApiError extends Error {
  constructor(public status: number, message: string, public detail?: unknown) {
    super(message);
  }
}

/** Fetch wrapper: attaches the JWT, parses structured errors, logs out on 401. */
export async function api<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const token = useAuth.getState().token;
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const res = await fetch(path, { ...init, headers });
  if (res.status === 401 && token) useAuth.getState().logout();
  const text = await res.text();
  let data: any = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    // Non-JSON body (e.g. a proxy's HTML 502 page while the backend restarts).
    if (!res.ok) throw new ApiError(res.status, `Request failed (${res.status}${res.statusText ? ` ${res.statusText}` : ""})`);
    throw new ApiError(res.status, "Unexpected non-JSON response from server");
  }
  if (!res.ok) {
    const msg = typeof data?.error === "string" ? data.error : `Request failed (${res.status})`;
    throw new ApiError(res.status, msg, data?.detail);
  }
  return data as T;
}

export const post = <T,>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
export const put = <T,>(path: string, body: unknown) => api<T>(path, { method: "PUT", body: JSON.stringify(body) });

// ---- shared types (decimals arrive as strings to preserve precision) ----
export type Dec = string | null;

export interface Page<T> { items: T[]; total: number; page: number; page_size: number }

export interface Portfolio {
  execution_mode: string; initial_balance: Dec; available_balance: Dec; invested_capital: Dec; total_equity: Dec;
  realised_pnl: Dec; unrealised_pnl: Dec; net_profit: Dec; daily_pnl: Dec; total_fees: Dec; closed_trades: number;
  winning_trades: number; losing_trades: number; win_rate: Dec; average_win: Dec; average_loss: Dec;
  profit_factor: Dec; max_drawdown_pct: Dec; current_drawdown_pct: Dec; open_positions: number; halted: boolean;
  halt_reason: string | null;
}

export interface Control { state: "STOPPED" | "RUNNING" | "PAUSED"; auto_trading: boolean; execution_mode: "PAPER" | "TESTNET" | "LIVE" }

export interface Signal {
  id: number; symbol: string; signal: "BUY" | "SELL" | "HOLD"; confidence_score: Dec; strategy_name: string;
  reason: string; action_status: string; candle_time: string | null; created_at: string;
}

export interface Coin {
  rank: number; symbol: string; name: string; base_asset: string; price: Dec; change_pct_24h: Dec;
  quote_volume_24h: Dec; rsi: Dec; macd: Dec; macd_signal: Dec; signal: Signal["signal"] | null; confidence: Dec;
  action_status: string | null; enabled: boolean; testnet_supported: boolean; note: string | null;
}

export interface Position {
  id: number; symbol: string; execution_mode: string; quantity: Dec; average_entry_price: Dec; current_price: Dec;
  stop_loss: Dec; take_profit: Dec; trailing_stop_pct: Dec; unrealised_pnl: Dec; realised_pnl: Dec; cost_basis: Dec; entry_fees_open: Dec;
  status: string; opened_at: string; invested_amount: Dec; duration_seconds: number | null; strategy_name: string | null;
}

export interface Trade {
  id: number; symbol: string; side: string; entry_price: Dec; exit_price: Dec; quantity: Dec; entry_fee: Dec;
  exit_fee: Dec; realised_pnl: Dec; status: string; exit_reason: string | null; strategy_name: string | null;
  execution_mode: string; opened_at: string; closed_at: string | null;
}

export interface Order {
  id: number; client_order_id: string; symbol: string; side: string; order_type: string; quantity: Dec; price: Dec;
  filled_quantity: Dec; average_fill_price: Dec; fee_quote: Dec; slippage_bps: Dec; status: string;
  execution_mode: string; purpose: string; reason: string | null; error_message: string | null; created_at: string;
}

export interface LogRow { id: number; level: string; component: string; message: string; metadata: Record<string, unknown> | null; created_at: string }
export interface RiskEventRow { id: number; event_type: string; severity: string; description: string; created_at: string }

export interface Condition { name: string; passed: boolean; detail: string; gate: boolean }
export interface Analysis {
  symbol: string; signal: Signal["signal"]; score: Dec; reason: string; action_status: string; strategy: string;
  created_at: string; candle_time: string | null; timeframes: { trend: string; setup: string; entry: string };
  market: Record<string, { trend: string; rsi_state: string; volume_state: string; volatility_state: string;
    support: number | null; resistance: number | null; atr_pct: number | null }>;
  indicators: Record<string, Record<string, number | string | null>>;
  entry_conditions: Condition[]; exit_conditions: Condition[]; score_note: string;
  ml: { validated: boolean; note: string; test: Record<string, number | null> } | null;
}

export interface CandleRow {
  time: number; open: number; high: number; low: number; close: number; volume: number;
  ema_20?: number | null; ema_50?: number | null; ema_200?: number | null; bb_upper?: number | null; bb_lower?: number | null;
  rsi?: number | null; macd?: number | null; macd_signal?: number | null;
}

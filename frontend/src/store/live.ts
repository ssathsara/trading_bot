import { create } from "zustand";

export interface LiveTicker { symbol: string; price: string; change_pct: string; quote_volume: string; bid: string | null; ask: string | null; at: number }
export interface LiveKline { symbol: string; interval: string; open_time: number; open: string; high: string; low: string; close: string; volume: string; closed: boolean }

interface LiveState {
  tickers: Record<string, LiveTicker>;
  lastKline: Record<string, LiveKline>; // key: SYMBOL:interval
  socket: Record<string, "connecting" | "open" | "closed">;
  stream: { status: string; at: number } | null;
  setTicker: (t: LiveTicker) => void;
  setKline: (k: LiveKline) => void;
  setSocket: (channel: string, s: "connecting" | "open" | "closed") => void;
  setStream: (status: string) => void;
}

export const useLive = create<LiveState>()((set) => ({
  tickers: {},
  lastKline: {},
  socket: {},
  stream: null,
  setTicker: (t) => set((s) => ({ tickers: { ...s.tickers, [t.symbol]: t } })),
  setKline: (k) => set((s) => ({ lastKline: { ...s.lastKline, [`${k.symbol}:${k.interval}`]: k } })),
  setSocket: (channel, st) => set((s) => ({ socket: { ...s.socket, [channel]: st } })),
  setStream: (status) => set({ stream: { status, at: Date.now() } }),
}));

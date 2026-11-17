import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/store/auth";
import { useLive } from "@/store/live";

type Handler = (msg: { type: string; data: any }) => void;

/** Authenticated WebSocket with exponential-backoff reconnect and heartbeat timeout. */
function connect(channel: string, onMessage: Handler, token: string): () => void {
  let ws: WebSocket | null = null;
  let closed = false;
  let backoff = 1000;
  let retry: ReturnType<typeof setTimeout> | undefined;
  let watchdog: ReturnType<typeof setTimeout> | undefined;
  const setSocket = useLive.getState().setSocket;

  const arm = () => {
    clearTimeout(watchdog);
    // Server pings every 20s; silence for 45s means the socket is dead.
    watchdog = setTimeout(() => ws?.close(), 45_000);
  };

  const open = () => {
    setSocket(channel, "connecting");
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws/${channel}?token=${encodeURIComponent(token)}`);
    ws.onopen = () => { backoff = 1000; setSocket(channel, "open"); arm(); };
    ws.onmessage = (ev) => {
      arm();
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type !== "ping" && msg.type !== "hello") onMessage(msg);
      } catch { /* ignore malformed frames */ }
    };
    ws.onclose = (ev) => {
      clearTimeout(watchdog);
      setSocket(channel, "closed");
      if (ev.code === 4401) {
        // Server rejected the token (expired/invalid): reconnecting cannot succeed.
        closed = true;
        useAuth.getState().logout();
        return;
      }
      if (!closed) {
        retry = setTimeout(open, backoff);
        backoff = Math.min(backoff * 2, 30_000);
      }
    };
  };
  open();
  return () => { closed = true; clearTimeout(retry); clearTimeout(watchdog); ws?.close(); };
}

/** Mount once (in the layout): routes live events into stores and invalidates queries. */
export function useLiveFeeds() {
  const token = useAuth((s) => s.token);
  const qc = useQueryClient();
  useEffect(() => {
    if (!token) return;
    const live = useLive.getState();
    let pending = new Set<string>();
    let flush: ReturnType<typeof setTimeout> | undefined;
    // Coalesce invalidations so bursts of events cause at most one refetch per second.
    const invalidate = (...keys: string[]) => {
      keys.forEach((k) => pending.add(k));
      if (!flush) flush = setTimeout(() => {
        pending.forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
        pending = new Set(); flush = undefined;
      }, 1000);
    };
    const stops = [
      connect("market", (m) => {
        if (m.type === "ticker") live.setTicker({ ...m.data, at: Date.now() });
        else if (m.type === "kline") live.setKline(m.data);
        else if (m.type === "universe") invalidate("top-coins");
      }, token),
      connect("dashboard", (m) => {
        if (m.type === "stream_status") live.setStream(m.data.status);
        if (m.type === "signal") invalidate("signals", "top-coins", "analysis");
        if (m.type === "log") invalidate("logs");
        if (m.type === "risk_event" || m.type === "halt") invalidate("risk", "logs", "status");
        if (m.type === "portfolio") invalidate("summary", "performance");
      }, token),
      connect("trading", () => invalidate("positions", "orders", "history", "summary"), token),
    ];
    return () => { stops.forEach((s) => s()); clearTimeout(flush); };
  }, [token, qc]);
}

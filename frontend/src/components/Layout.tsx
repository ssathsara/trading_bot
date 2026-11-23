import { NavLink, Outlet } from "react-router-dom";
import {
  Activity, BarChart3, Bot, CandlestickChart, FlaskConical, History, LayoutDashboard, LogOut, Moon, ScrollText,
  Settings, Shield, Sun, Wallet,
} from "lucide-react";
import { useAuth, useUi } from "@/store/auth";
import { useLive } from "@/store/live";
import { useStatus } from "@/lib/queries";
import { useLiveFeeds } from "@/lib/ws";
import { cn } from "@/lib/format";
import { Badge, Button } from "./ui";

const NAV = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard },
  { to: "/market", label: "Live Market", icon: Activity },
  { to: "/terminal", label: "Trading Terminal", icon: CandlestickChart },
  { to: "/analysis", label: "AI Analysis", icon: Bot },
  { to: "/positions", label: "Open Positions", icon: Wallet },
  { to: "/history", label: "Trading History", icon: History },
  { to: "/analytics", label: "Analytics", icon: BarChart3 },
  { to: "/backtest", label: "Backtesting", icon: FlaskConical },
  { to: "/risk", label: "Risk Management", icon: Shield },
  { to: "/settings", label: "System Settings", icon: Settings },
  { to: "/logs", label: "Activity Logs", icon: ScrollText },
];

/** Always-visible environment indicator. LIVE (real money) is shown as a solid red bar. */
export function EnvironmentBanner() {
  const { data } = useStatus();
  const mode = data?.execution_mode ?? "PAPER";
  const state = data?.control.state ?? "—";
  const halted = data?.risk.halted;
  return (
    <div data-testid="env-banner" className={cn("flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-1.5 text-xs font-medium",
      mode === "LIVE" ? "bg-down text-white" : mode === "TESTNET" ? "bg-info/15 text-info" : "bg-primary/15 text-primary")}>
      <span className="font-semibold tracking-wide">
        {mode === "LIVE" ? "LIVE TRADING — REAL MONEY, REAL ORDERS"
          : mode === "TESTNET" ? "BINANCE SPOT TESTNET — test funds only" : "PAPER TRADING — simulated funds, no real orders"}
      </span>
      {mode === "LIVE"
        ? <span>Capital cap: {data?.live?.max_capital_usdt ?? "—"} USDT</span>
        : <span>Live trading: {data?.live_trading_enabled ? "available" : "disabled"}</span>}
      <span>Engine: {state}</span>
      {halted && <span className="text-down">HARD RISK LIMIT ACTIVE — reset required</span>}
    </div>
  );
}

function ConnectionDots() {
  const sockets = useLive((s) => s.socket);
  const { data } = useStatus();
  const stream = data?.worker.stream;
  const marketDetail = stream ? `${stream.status}${stream.lag_s != null ? ` · data ${stream.lag_s}s behind` : ""}` : undefined;
  const items: [string, boolean, string?][] = [
    ["API", !!data],
    ["Worker", !!data?.worker.alive],
    ["Market", !!stream?.healthy, marketDetail],
    ["Live", sockets.dashboard === "open"],
  ];
  return (
    <div className="flex items-center gap-3 text-xs text-muted">
      {items.map(([label, ok, detail]) => (
        <span key={label} className="flex items-center gap-1.5" title={`${label}: ${detail ?? (ok ? "connected" : "disconnected")}`}>
          <span className={cn("h-2 w-2 rounded-full", ok ? "bg-up" : "bg-down")} />{label}
        </span>
      ))}
    </div>
  );
}

export default function Layout() {
  useLiveFeeds();
  const { theme, toggleTheme } = useUi();
  const { username, logout } = useAuth();
  return (
    <div className="flex h-full flex-col">
      <div className="flex h-full flex-col bg-background text-foreground">
        <EnvironmentBanner />
        <div className="flex min-h-0 flex-1">
          <aside className="hidden w-56 shrink-0 flex-col border-r border-border bg-surface md:flex">
            <div className="flex items-center gap-2 px-4 py-4">
              <img src="/favicon.svg" alt="" className="h-7 w-7" />
              <div className="leading-tight">
                <div className="text-sm font-semibold">Spot Agent</div>
                <Badge tone="primary">Simulation</Badge>
              </div>
            </div>
            <nav className="flex-1 space-y-0.5 overflow-y-auto px-2">
              {NAV.map(({ to, label, icon: Icon }) => (
                <NavLink key={to} to={to} end={to === "/"}
                  className={({ isActive }) => cn("flex items-center gap-2.5 rounded-md px-3 py-2 text-sm",
                    isActive ? "bg-primary/10 font-medium text-primary" : "text-muted hover:bg-surface-2 hover:text-foreground")}>
                  <Icon className="h-4 w-4" />{label}
                </NavLink>
              ))}
            </nav>
          </aside>
          <div className="flex min-w-0 flex-1 flex-col">
            <header className="flex items-center justify-between gap-3 border-b border-border bg-surface px-4 py-2">
              <nav className="flex gap-1 overflow-x-auto md:hidden">
                {NAV.map(({ to, icon: Icon, label }) => (
                  <NavLink key={to} to={to} end={to === "/"} aria-label={label}
                    className={({ isActive }) => cn("rounded p-2", isActive ? "text-primary" : "text-muted")}><Icon className="h-4 w-4" /></NavLink>
                ))}
              </nav>
              <ConnectionDots />
              <div className="flex items-center gap-2">
                <span className="hidden text-xs text-muted sm:inline">{username}</span>
                <Button variant="ghost" size="icon" onClick={toggleTheme} aria-label="Toggle theme">
                  {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
                </Button>
                <Button variant="ghost" size="icon" onClick={logout} aria-label="Log out"><LogOut className="h-4 w-4" /></Button>
              </div>
            </header>
            <main className="min-h-0 flex-1 overflow-y-auto p-4 md:p-6"><Outlet /></main>
          </div>
        </div>
      </div>
    </div>
  );
}

import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AppRoutes } from "@/App";
import { useAuth } from "@/store/auth";
import { fmtPct, fmtPrice, fmtUsd, pnlClass } from "@/lib/format";
import { mockApi, renderWithProviders } from "./utils";

const status = {
  database: { connected: true, dialect: "sqlite" }, control: { state: "RUNNING", auto_trading: true, execution_mode: "PAPER" },
  execution_mode: "PAPER", environment_label: "PAPER TRADING (SIMULATED)", live_trading_enabled: false,
  worker: { alive: true, stream: { status: "connected", connected: true, healthy: true, reconnects: 0, invalid_messages: 0 }, rest: { last_error: null, used_weight_1m: 10 } },
  testnet: { configured: false }, risk: { halted: false, halt_reason: null, halted_at: null },
};
const portfolio = {
  execution_mode: "PAPER", initial_balance: "10000", available_balance: "8000.5", invested_capital: "2000", total_equity: "10012.34",
  realised_pnl: "15.5", unrealised_pnl: "-3.16", net_profit: "12.34", daily_pnl: "12.34", total_fees: "4.2", closed_trades: 4,
  winning_trades: 3, losing_trades: 1, win_rate: "75", average_win: "8", average_loss: "-8.5", profit_factor: "2.82",
  max_drawdown_pct: "0.8", current_drawdown_pct: "0.1", open_positions: 1, halted: false, halt_reason: null,
};
const coin = (rank: number, symbol: string, change: string, signal: string) => ({
  rank, symbol, name: symbol.replace("USDT", ""), base_asset: symbol.replace("USDT", ""), price: "100.5", change_pct_24h: change,
  quote_volume_24h: String(1e9 / rank), rsi: "55.2", macd: "0.5", macd_signal: "0.4", signal, confidence: "70",
  action_status: "NONE", enabled: true, testnet_supported: rank === 1, note: null,
});

function login() { useAuth.setState({ token: "t", username: "admin" }); }

describe("formatting", () => {
  it("formats prices, money and percentages", () => {
    expect(fmtPrice("64717.99")).toBe("64,717.99");
    expect(fmtPrice("0.00001234")).toBe("0.00001234");
    expect(fmtUsd("-3.16")).toBe("-$3.16");
    expect(fmtUsd("12.3", true)).toBe("+$12.30");
    expect(fmtPct("2.5")).toBe("+2.50%");
    expect(fmtUsd(null)).toBe("—");
    expect(pnlClass("-1")).toBe("text-down");
  });
});

describe("authentication", () => {
  it("shows login and stores token on success", async () => {
    useAuth.setState({ token: null, username: null });
    const fetchMock = mockApi({ "/api/auth/login": () => [200, { access_token: "abc" }], "/api/system/status": status, "/api/dashboard/summary": { portfolio, control: status.control, execution_mode: "PAPER", worker: status.worker, latest_signals: [] } });
    renderWithProviders(<AppRoutes />);
    await userEvent.type(screen.getByLabelText(/password/i), "secret-pass");
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }));
    await waitFor(() => expect(useAuth.getState().token).toBe("abc"));
    expect(fetchMock).toHaveBeenCalledWith("/api/auth/login", expect.objectContaining({ method: "POST" }));
  });

  it("shows an error for bad credentials", async () => {
    useAuth.setState({ token: null, username: null });
    mockApi({ "/api/auth/login": () => [401, { error: "invalid username or password" }] });
    renderWithProviders(<AppRoutes />);
    await userEvent.type(screen.getByLabelText(/password/i), "wrong");
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("invalid username or password");
  });
});

describe("dashboard", () => {
  it("renders portfolio stats and a permanent simulation banner", async () => {
    login();
    mockApi({
      "/api/system/status": status,
      "/api/dashboard/summary": { portfolio, control: status.control, execution_mode: "PAPER", worker: status.worker,
        latest_signals: [{ id: 1, symbol: "BTCUSDT", signal: "BUY", confidence_score: "81.5", strategy_name: "t", reason: "BUY: all entry conditions met", action_status: "ORDER_PLACED", candle_time: null, created_at: new Date().toISOString() }] },
      "/api/trading/positions": [],
      "/api/portfolio/performance": { equity_curve: [], daily_pnl: [] },
    });
    renderWithProviders(<AppRoutes />);
    expect(await screen.findByText("$10,012.34")).toBeInTheDocument();
    expect(screen.getByText("+$12.34 net")).toBeInTheDocument();
    expect(screen.getByText("75.00%")).toBeInTheDocument();
    const banner = screen.getByTestId("env-banner");
    await waitFor(() => expect(banner).toHaveTextContent(/PAPER TRADING/));
    expect(banner).toHaveTextContent("Live trading: disabled");
    expect(await screen.findByText("ORDER_PLACED")).toBeInTheDocument();
  });

  it("warns when a hard risk limit is active", async () => {
    login();
    mockApi({
      "/api/system/status": { ...status, risk: { halted: true, halt_reason: "MAX_DRAWDOWN", halted_at: null } },
      "/api/dashboard/summary": { portfolio: { ...portfolio, halted: true, halt_reason: "MAX_DRAWDOWN: 10%" }, control: status.control, execution_mode: "PAPER", worker: status.worker, latest_signals: [] },
      "/api/trading/positions": [], "/api/portfolio/performance": { equity_curve: [] },
    });
    renderWithProviders(<AppRoutes />);
    expect(await screen.findByText(/Hard risk limit reached/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("env-banner")).toHaveTextContent(/reset required/));
  });
});

describe("live market", () => {
  it("lists coins, supports search and sorting", async () => {
    login();
    mockApi({
      "/api/system/status": status,
      "/api/market/top-coins": { items: [coin(1, "BTCUSDT", "1.2", "HOLD"), coin(2, "ETHUSDT", "-3.4", "BUY"), coin(3, "SOLUSDT", "5.6", "SELL")] },
    });
    renderWithProviders(<AppRoutes />, "/market");
    const table = await screen.findByRole("table");
    await within(table).findByText("ETHUSDT");
    const rows = () => within(table).getAllByRole("row").slice(1).map((r) => r.querySelectorAll("td")[2].textContent);
    expect(rows()).toEqual(["BTCUSDT", "ETHUSDT", "SOLUSDT"]);
    await userEvent.click(within(table).getByRole("button", { name: "24h" }));
    expect(rows()).toEqual(["ETHUSDT", "BTCUSDT", "SOLUSDT"]);
    await userEvent.type(screen.getByLabelText("Search"), "sol");
    expect(rows()).toEqual(["SOLUSDT"]);
    expect(within(table).getByText("SELL")).toBeInTheDocument();
    expect(within(table).getAllByText("no testnet").length).toBe(1);
  });
});

describe("settings", () => {
  it("never offers a live trading mode", async () => {
    login();
    const settings = {
      execution_mode: "PAPER", auto_trading: true, state: "STOPPED", testnet_configured: false, live_trading_available: false,
      risk: {}, strategy: { name: "trend_following_v1", trend_timeframe: "1h", setup_timeframe: "15m", entry_timeframe: "5m", weights: { trend: "30" }, exit_on_trend_reversal: true, ml_filter_enabled: false },
      market: { selected_symbols: [], excluded_symbols: [] }, execution: { order_type: "MARKET", allow_market_orders: true },
    };
    mockApi({ "/api/system/status": status, "/api/settings": settings });
    renderWithProviders(<AppRoutes />, "/settings");
    const select = await screen.findByLabelText("Execution mode");
    const options = within(select).getAllByRole("option").map((o) => o.textContent ?? "");
    expect(options.some((o) => /live/i.test(o))).toBe(false);
    expect(options).toHaveLength(2);
    expect(within(select).getByRole("option", { name: /testnet/i })).toBeDisabled();
  });
});

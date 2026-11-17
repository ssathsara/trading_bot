import { render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

export function mockApi(routes: Record<string, unknown | ((init?: RequestInit) => [number, unknown])>) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    const key = Object.keys(routes).sort((a, b) => b.length - a.length).find((k) => url.startsWith(k));
    if (!key) return new Response(JSON.stringify({ error: "not found" }), { status: 404 });
    const v = routes[key];
    const [status, body] = typeof v === "function" ? (v as (i?: RequestInit) => [number, unknown])(init) : [200, v];
    return new Response(JSON.stringify(body), { status });
  });
}

export function renderWithProviders(ui: React.ReactElement, route = "/") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter></QueryClientProvider>);
}

import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => { cleanup(); vi.restoreAllMocks(); sessionStorage.clear(); });

// lightweight-charts needs canvas/ResizeObserver; charts are not under test here.
vi.mock("@/components/Charts", () => ({
  CandleChart: () => null, LineChart: () => null, BarChart: () => null,
}));
class WS { onopen?: () => void; onclose?: () => void; onmessage?: () => void; close() {} }
// @ts-expect-error test stub
globalThis.WebSocket = WS;

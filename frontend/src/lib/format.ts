import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export const cn = (...inputs: ClassValue[]) => twMerge(clsx(inputs));

export const num = (v: string | number | null | undefined): number | null => {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
};

/** Price with precision adapted to magnitude (crypto prices span 1e-8 .. 1e5). */
export function fmtPrice(v: string | number | null | undefined): string {
  const n = num(v);
  if (n === null) return "—";
  const abs = Math.abs(n);
  const digits = abs >= 1000 ? 2 : abs >= 1 ? 4 : abs >= 0.01 ? 6 : 8;
  return n.toLocaleString("en-US", { minimumFractionDigits: Math.min(2, digits), maximumFractionDigits: digits });
}

export function fmtUsd(v: string | number | null | undefined, signed = false): string {
  const n = num(v);
  if (n === null) return "—";
  const s = Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const sign = n < 0 ? "-" : signed && n > 0 ? "+" : "";
  return `${sign}$${s}`;
}

export function fmtPct(v: string | number | null | undefined, signed = true, digits = 2): string {
  const n = num(v);
  if (n === null) return "—";
  return `${signed && n > 0 ? "+" : ""}${n.toFixed(digits)}%`;
}

export function fmtQty(v: string | number | null | undefined): string {
  const n = num(v);
  if (n === null) return "—";
  return n.toLocaleString("en-US", { maximumFractionDigits: 8 });
}

export function fmtCompact(v: string | number | null | undefined): string {
  const n = num(v);
  if (n === null) return "—";
  return n.toLocaleString("en-US", { notation: "compact", maximumFractionDigits: 2 });
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const d = Math.floor(seconds / 86400), h = Math.floor((seconds % 86400) / 3600), m = Math.floor((seconds % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

export const pnlClass = (v: string | number | null | undefined) => {
  const n = num(v);
  return n === null || n === 0 ? "text-muted" : n > 0 ? "text-up" : "text-down";
};

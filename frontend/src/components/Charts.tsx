import { useEffect, useRef } from "react";
import {
  CandlestickSeries, ColorType, createChart, createSeriesMarkers, HistogramSeries, LineSeries, LineStyle, TickMarkType,
  type IChartApi, type ISeriesApi, type SeriesMarker, type Time, type UTCTimestamp,
} from "lightweight-charts";
import { useUi } from "@/store/auth";
import type { CandleRow } from "@/lib/api";

function palette(theme: "dark" | "light") {
  const css = getComputedStyle(document.documentElement);
  const v = (n: string, f: string) => css.getPropertyValue(n).trim() || f;
  return {
    bg: v("--surface", theme === "dark" ? "#12161d" : "#fff"), text: v("--muted", "#8a93a3"), grid: v("--border", "#252b36"),
    up: v("--up", "#22c55e"), down: v("--down", "#f43f5e"), primary: v("--primary", "#14b8a6"), info: v("--info", "#60a5fa"),
    warn: v("--warn", "#f59e0b"),
  };
}

// lightweight-charts renders timestamps in UTC; these formatters show the browser's local time instead.
const toDate = (t: Time) => new Date((t as number) * 1000);
const pad = (n: number) => String(n).padStart(2, "0");

function localTick(t: Time, type: TickMarkType): string {
  const d = toDate(t);
  switch (type) {
    case TickMarkType.Year: return String(d.getFullYear());
    case TickMarkType.Month: return d.toLocaleString(undefined, { month: "short" });
    case TickMarkType.DayOfMonth: return String(d.getDate());
    case TickMarkType.TimeWithSeconds: return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    default: return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
}

function localCrosshair(t: Time): string {
  const d = toDate(t);
  return `${d.toLocaleDateString(undefined, { day: "2-digit", month: "short", year: "2-digit" })} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function useChart(height: number) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const theme = useUi((s) => s.theme);
  useEffect(() => {
    if (!ref.current) return;
    const p = palette(theme);
    const chart = createChart(ref.current, {
      height, autoSize: true,
      layout: { background: { type: ColorType.Solid, color: p.bg }, textColor: p.text, fontSize: 11 },
      grid: { vertLines: { color: p.grid }, horzLines: { color: p.grid } },
      rightPriceScale: { borderColor: p.grid },
      timeScale: { borderColor: p.grid, timeVisible: true, tickMarkFormatter: localTick },
      localization: { timeFormatter: localCrosshair },
    });
    chartRef.current = chart;
    return () => { chart.remove(); chartRef.current = null; };
  }, [theme, height]);
  return { ref, chartRef, theme };
}

export interface PriceLine { price: number; label: string; kind: "sl" | "tp" | "entry" }
export interface TradeMarker { time: number; side: "BUY" | "SELL"; text?: string }

/** Candlestick chart with EMA/Bollinger overlays, SL/TP lines and buy/sell markers. */
export function CandleChart({ candles, lines = [], markers = [], live, height = 420 }:
  { candles: CandleRow[]; lines?: PriceLine[]; markers?: TradeMarker[]; live?: CandleRow | null; height?: number }) {
  const { ref, chartRef, theme } = useChart(height);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !candles.length) return;
    const p = palette(theme);
    const t = (c: CandleRow) => c.time as UTCTimestamp;
    const cs = chart.addSeries(CandlestickSeries, { upColor: p.up, downColor: p.down, borderVisible: false, wickUpColor: p.up, wickDownColor: p.down });
    cs.setData(candles.map((c) => ({ time: t(c), open: c.open, high: c.high, low: c.low, close: c.close })));
    seriesRef.current = cs;
    const overlay = (key: keyof CandleRow, color: string, style = LineStyle.Solid) => {
      const pts = candles.filter((c) => c[key] != null).map((c) => ({ time: t(c), value: c[key] as number }));
      if (!pts.length) return;
      const s = chart.addSeries(LineSeries, { color, lineWidth: 1, lineStyle: style, priceLineVisible: false, lastValueVisible: false });
      s.setData(pts);
    };
    overlay("ema_20", p.info);
    overlay("ema_50", p.warn);
    overlay("ema_200", p.text);
    overlay("bb_upper", p.grid, LineStyle.Dashed);
    overlay("bb_lower", p.grid, LineStyle.Dashed);
    const vol = chart.addSeries(HistogramSeries, { priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    vol.setData(candles.map((c) => ({ time: t(c), value: c.volume, color: c.close >= c.open ? `${p.up}55` : `${p.down}55` })));
    for (const l of lines) {
      cs.createPriceLine({ price: l.price, title: l.label, lineWidth: 1, lineStyle: LineStyle.Dashed,
        color: l.kind === "sl" ? p.down : l.kind === "tp" ? p.up : p.primary });
    }
    if (markers.length) {
      const ms: SeriesMarker<Time>[] = markers.sort((a, b) => a.time - b.time).map((m) => ({
        time: m.time as UTCTimestamp, position: m.side === "BUY" ? "belowBar" : "aboveBar",
        color: m.side === "BUY" ? p.up : p.down, shape: m.side === "BUY" ? "arrowUp" : "arrowDown", text: m.text ?? m.side,
      }));
      createSeriesMarkers(cs, ms);
    }
    chart.timeScale().fitContent();
    return () => { try { chart.removeSeries(cs); } catch { /* chart already disposed */ } seriesRef.current = null; };
  }, [candles, lines, markers, theme, chartRef]);

  useEffect(() => {
    if (live && seriesRef.current && candles.length && live.time >= candles[candles.length - 1].time) {
      seriesRef.current.update({ time: live.time as UTCTimestamp, open: live.open, high: live.high, low: live.low, close: live.close });
    }
  }, [live, candles]);

  return <div ref={ref} className="w-full" style={{ height }} />;
}

/** Simple time-series line chart (equity curve, indicator panes). */
export function LineChart({ data, height = 240, color, baseline }: { data: { time: number; value: number }[]; height?: number; color?: string; baseline?: number }) {
  const { ref, chartRef, theme } = useChart(height);
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !data.length) return;
    const p = palette(theme);
    const s = chart.addSeries(LineSeries, { color: color ?? p.primary, lineWidth: 2, priceLineVisible: false });
    const seen = new Set<number>();
    s.setData(data.filter((d) => !seen.has(d.time) && seen.add(d.time)).map((d) => ({ time: d.time as UTCTimestamp, value: d.value })));
    if (baseline !== undefined) s.createPriceLine({ price: baseline, color: p.text, lineStyle: LineStyle.Dotted, lineWidth: 1, title: "" });
    chart.timeScale().fitContent();
    return () => { try { chart.removeSeries(s); } catch { /* disposed */ } };
  }, [data, theme, color, baseline, chartRef]);
  return <div ref={ref} className="w-full" style={{ height }} />;
}

/** Bar chart for daily/monthly P&L and volume (positive = up colour, negative = down colour). */
export function BarChart({ data, height = 220 }: { data: { time: number; value: number }[]; height?: number }) {
  const { ref, chartRef, theme } = useChart(height);
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !data.length) return;
    const p = palette(theme);
    const s = chart.addSeries(HistogramSeries, { priceLineVisible: false });
    s.setData(data.map((d) => ({ time: d.time as UTCTimestamp, value: d.value, color: d.value >= 0 ? p.up : p.down })));
    chart.timeScale().fitContent();
    return () => { try { chart.removeSeries(s); } catch { /* disposed */ } };
  }, [data, theme, chartRef]);
  return <div ref={ref} className="w-full" style={{ height }} />;
}

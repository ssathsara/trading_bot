import { useEffect } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { useAuth, useUi } from "@/store/auth";
import Layout from "@/components/Layout";
import Login from "@/pages/Login";
import Dashboard from "@/pages/Dashboard";
import Market from "@/pages/Market";
import Terminal from "@/pages/Terminal";
import AnalysisPage from "@/pages/Analysis";
import Positions from "@/pages/Positions";
import HistoryPage from "@/pages/History";
import Analytics from "@/pages/Analytics";
import Backtest from "@/pages/Backtest";
import Risk from "@/pages/Risk";
import SettingsPage from "@/pages/Settings";
import Logs from "@/pages/Logs";

export function AppRoutes() {
  const token = useAuth((s) => s.token);
  const theme = useUi((s) => s.theme);
  useEffect(() => {
    // Theme tokens live on <html> so charts (which read computed CSS vars) match the UI.
    document.documentElement.classList.toggle("dark", theme === "dark");
  }, [theme]);
  if (!token) return <Login />;
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="market" element={<Market />} />
        <Route path="terminal" element={<Terminal />} />
        <Route path="terminal/:symbol" element={<Terminal />} />
        <Route path="analysis" element={<AnalysisPage />} />
        <Route path="analysis/:symbol" element={<AnalysisPage />} />
        <Route path="positions" element={<Positions />} />
        <Route path="history" element={<HistoryPage />} />
        <Route path="analytics" element={<Analytics />} />
        <Route path="backtest" element={<Backtest />} />
        <Route path="risk" element={<Risk />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="logs" element={<Logs />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

export default function App() {
  return (
    <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <AppRoutes />
    </BrowserRouter>
  );
}

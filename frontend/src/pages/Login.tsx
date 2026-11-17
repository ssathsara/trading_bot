import { useState } from "react";
import { post } from "@/lib/api";
import { useAuth } from "@/store/auth";
import { Button, Card, ErrorBox, Input } from "@/components/ui";

export default function Login() {
  const login = useAuth((s) => s.login);
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await post<{ access_token: string }>("/api/auth/login", { username, password });
      login(r.access_token, username);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center bg-background p-4 text-foreground">
      <Card className="w-full max-w-sm p-6">
        <div className="mb-5 flex items-center gap-3">
          <img src="/favicon.svg" alt="" className="h-9 w-9" />
          <div>
            <h1 className="text-base font-semibold">Spot Trading Agent</h1>
            <p className="text-xs text-muted">Paper &amp; testnet simulation · live trading disabled</p>
          </div>
        </div>
        <form onSubmit={submit} className="space-y-3">
          <label className="block text-sm">
            <span className="mb-1 block text-xs text-muted">Username</span>
            <Input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required />
          </label>
          <label className="block text-sm">
            <span className="mb-1 block text-xs text-muted">Password</span>
            <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required />
          </label>
          <ErrorBox error={error} />
          <Button type="submit" className="w-full" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</Button>
        </form>
      </Card>
    </div>
  );
}

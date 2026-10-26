"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import BrandMark from "@/components/BrandMark";
import { ErrorNote } from "@/components/Status";
import { api, saveSession } from "@/lib/api";
import type { User } from "@/lib/types";

const DEMO = [
  { email: process.env.NEXT_PUBLIC_DEMO_ADMIN_EMAIL || "admin@paypap.demo", role: "Admin · uploads and reviews" },
  { email: process.env.NEXT_PUBLIC_DEMO_APPROVER_EMAIL || "approver@paypap.demo", role: "Approver · approves and releases" },
];

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState(DEMO[0].email);
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await api<{ token: string; user: User }>("/api/auth/login", "POST", { json: { email, password } });
      saveSession(res.token, res.user);
      router.push("/");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}>
      <form onSubmit={submit} className="glass-panel" style={{ width: "100%", maxWidth: 420, padding: 28, display: "flex", flexDirection: "column", gap: 18 }}>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 10, textAlign: "center" }}>
          <BrandMark size={72} radius={22} />
          <h1 style={{ margin: 0, fontSize: 28, fontWeight: 700, letterSpacing: "-0.03em" }}>PAY PAP</h1>
          <p className="muted" style={{ margin: 0, fontSize: 14 }}>
            Bulk contractor payouts, checked by rules and AI, released by a person.
          </p>
        </div>

        <label style={{ display: "flex", flexDirection: "column", gap: 6, fontSize: 13 }}>
          <span className="muted">Account</span>
          <select className="field well" value={email} onChange={(e) => setEmail(e.target.value)} style={{ paddingRight: 36 }}>
            {DEMO.map((d) => (
              <option key={d.email} value={d.email}>
                {d.email} — {d.role}
              </option>
            ))}
          </select>
        </label>
        <label style={{ display: "flex", flexDirection: "column", gap: 6, fontSize: 13 }}>
          <span className="muted">Password</span>
          <input className="field well" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </label>
        <ErrorNote error={error} />
        <button className="btn btn-solid" style={{ height: 48, fontSize: 15 }} disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <p className="muted" style={{ margin: 0, fontSize: 12, textAlign: "center" }}>
          Demo passwords are in the README · PayPal sandbox only, no real money moves.
        </p>
      </form>
    </main>
  );
}

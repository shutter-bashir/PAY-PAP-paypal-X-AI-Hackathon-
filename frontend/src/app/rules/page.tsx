"use client";

import { useEffect, useState } from "react";
import { ErrorNote, PageTitle } from "@/components/Status";
import { api, money } from "@/lib/api";
import type { Meta } from "@/lib/types";
import { useUser } from "@/lib/useSession";

const RULES: { code: string; severity: "blocking" | "warning"; what: string }[] = [
  { code: "MISSING_EMAIL / INVALID_EMAIL", severity: "blocking", what: "No usable PayPal email, so there is nowhere to send the money." },
  { code: "MISSING_AMOUNT / INVALID_AMOUNT / NON_POSITIVE_AMOUNT", severity: "blocking", what: "The amount is missing, unreadable, or zero or less." },
  { code: "CURRENCY_MISMATCH", severity: "blocking", what: "The row is in a different currency from the batch." },
  { code: "AMOUNT_OVER_CAP", severity: "blocking", what: "One payment above the per-person cap." },
  { code: "INVOICE_ALREADY_PAID", severity: "blocking", what: "This invoice was already paid to this person in an earlier batch." },
  { code: "RECIPIENT_BLOCKED", severity: "blocking", what: "The contractor is blocked in the directory." },
  { code: "PAYPAL_REJECTED_RECEIVER", severity: "blocking", what: "PayPal turned away the last payout to this email (for example INVALID_EMAIL), so it would fail again." },
  { code: "PAYPAL_REJECTED_DOMAIN", severity: "blocking", what: "PayPal has turned away payouts to 3 or more addresses on this email domain and paid none." },
  { code: "MALFORMED_ROW", severity: "blocking", what: "The row's values don't line up with the header (often an unquoted 1,200.00)." },
  { code: "DUPLICATE_INVOICE / INVOICE_SHARED", severity: "warning", what: "The same invoice appears twice, or under two different people." },
  { code: "DUPLICATE_RECIPIENT", severity: "warning", what: "The same person is paid more than once for different invoices." },
  { code: "RECIPIENT_TOTAL_OVER_CAP", severity: "warning", what: "One person's rows add up to more than the per-person cap." },
  { code: "OUTLIER_AMOUNT", severity: "warning", what: "An amount far above the rest of the batch." },
  { code: "NEW_RECIPIENT / RECIPIENT_NOT_VETTED / NAME_MISMATCH", severity: "warning", what: "Unknown or unvetted contractor, or the name doesn't match the directory." },
  { code: "MISSING_INVOICE", severity: "warning", what: "No invoice ID, so repeat payments can't be detected." },
];

export default function RulesPage() {
  const user = useUser();
  const [meta, setMeta] = useState<Meta | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!user) return;
    api<Meta>("/api/meta")
      .then(setMeta)
      .catch((e: Error) => setError(e.message));
  }, [user]);

  const cards = meta
    ? [
        { label: "Per-person cap", value: money(meta.limits.max_amount_per_recipient, meta.currency), sub: "per contractor, per batch" },
        { label: "Batch cap", value: money(meta.limits.max_batch_total, meta.currency), sub: "total of included rows" },
        { label: "Max rows", value: String(meta.limits.max_batch_items), sub: "included rows per batch" },
        { label: "Outlier threshold", value: `${meta.limits.suspicious_amount_zscore}σ`, sub: "above the other rows" },
        { label: "Approval valid for", value: `${meta.limits.approval_ttl_minutes} min`, sub: "then it expires unused" },
        { label: "Status checks", value: meta.webhook_configured ? "Webhook + poll" : "Polling only", sub: `poll every ${meta.poll_interval_seconds}s` },
      ]
    : [];

  return (
    <section className="glass-panel" style={{ padding: 18, display: "flex", flexDirection: "column", gap: 16 }}>
      <PageTitle
        title="Rules and caps"
        sub={meta ? `Rules version ${meta.rules_version} · PayPal ${meta.paypal_mode} · AI ${meta.ai.enabled ? `${meta.ai.provider} (${meta.ai.model})` : "off"}` : "What decides whether a row can be paid."}
      />
      <ErrorNote error={error} />
      <div style={{ display: "grid", gap: 14, gridTemplateColumns: "repeat(auto-fill, minmax(190px, 1fr))" }}>
        {cards.map((c) => (
          <div key={c.label} className="glass-card" style={{ padding: 16 }}>
            <div className="muted" style={{ fontSize: 12 }}>{c.label}</div>
            <div className="num" style={{ marginTop: 6, fontSize: 26, fontWeight: 700, letterSpacing: "-0.03em" }}>{c.value}</div>
            <div className="muted" style={{ marginTop: 4, fontSize: 11 }}>{c.sub}</div>
          </div>
        ))}
      </div>
      <div className="glass-card" style={{ padding: 16 }}>
        <h2 className="card-title">The rules</h2>
        <p className="muted" style={{ margin: "4px 0 12px", fontSize: 13 }}>
          Blocking rows can never be paid. Warning rows wait for a person to choose pay or leave out. The AI only explains these results.
        </p>
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 8 }}>
          {RULES.map((r) => (
            <li key={r.code} className="well" style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10, padding: "10px 12px", borderRadius: 14 }}>
              <span className={`pill ${r.severity === "blocking" ? "tone-bad" : "tone-warn"}`}>{r.severity}</span>
              <span className="mono" style={{ fontSize: 12, fontWeight: 600 }}>{r.code}</span>
              <span className="muted" style={{ fontSize: 13, flex: "1 1 260px" }}>{r.what}</span>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

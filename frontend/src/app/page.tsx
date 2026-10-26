"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { IconChevronRight, IconPlus } from "@/components/Icons";
import { ErrorNote, PageTitle, StatusBadge } from "@/components/Status";
import { api, money, when } from "@/lib/api";
import type { Overview } from "@/lib/types";
import { useUser } from "@/lib/useSession";

export default function BatchesPage() {
  const user = useUser();
  const [batches, setBatches] = useState<Overview[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!user) return;
    api<Overview[]>("/api/batches")
      .then(setBatches)
      .catch((e: Error) => setError(e.message));
  }, [user]);

  return (
    <section className="glass-panel" style={{ padding: 18, display: "flex", flexDirection: "column", gap: 16 }}>
      <PageTitle title="Payout batches" sub="Each batch is one uploaded CSV, from review to PayPal reconciliation.">
        <Link href="/upload" className="btn btn-solid" style={{ height: 40, textDecoration: "none" }}>
          <IconPlus size={16} /> Upload CSV
        </Link>
      </PageTitle>
      <ErrorNote error={error} />
      {batches === null && !error ? <p className="muted" style={{ padding: "0 4px" }}>Loading…</p> : null}
      {batches?.length === 0 ? (
        <div className="glass-card" style={{ padding: 32, textAlign: "center" }}>
          <p style={{ margin: 0, fontWeight: 600 }}>No batches yet</p>
          <p className="muted" style={{ margin: "6px 0 0", fontSize: 13 }}>
            Upload a contractor CSV to start. The demo files are <code>backend/seed/batch_1_5_people.csv</code> to <code>batch_5_5_people.csv</code>.
          </p>
        </div>
      ) : null}
      <div style={{ display: "grid", gap: 14, gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))" }}>
        {batches?.map((b) => {
          const paidShare = b.included_total ? Math.min(100, Math.round((Number(b.paid_total) / Number(b.included_total)) * 100)) : 0;
          return (
            <Link key={b.batch_id} href={`/batches/${b.batch_id}`} className="glass-card" style={{ padding: 16, display: "flex", flexDirection: "column", gap: 12, textDecoration: "none" }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                <span style={{ fontSize: 15, fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  #{b.batch_no} {b.name}
                </span>
                <IconChevronRight size={16} />
              </div>
              <StatusBadge status={b.status} />
              <div>
                <div className="num" style={{ fontSize: 30, fontWeight: 700, letterSpacing: "-0.03em", lineHeight: 1 }}>
                  {money(b.included_total, b.currency)}
                </div>
                <div className="muted" style={{ marginTop: 6, fontSize: 12 }}>
                  {b.included_count} of {b.row_count} rows · {b.undecided_count ? `${b.undecided_count} need a decision` : "all decided"}
                </div>
              </div>
              {b.paypal_payout_batch_id ? (
                <div>
                  <div style={{ height: 6, borderRadius: 9999, overflow: "hidden", background: "var(--track)" }}>
                    <div style={{ width: `${paidShare}%`, height: "100%", borderRadius: 9999, background: "var(--accent)" }} />
                  </div>
                  <div className="muted num" style={{ display: "flex", justifyContent: "space-between", marginTop: 6, fontSize: 11 }}>
                    <span>{money(b.paid_total, b.currency)} paid</span>
                    <span>
                      {b.payout_pending_count} pending · {b.failed_count} failed
                    </span>
                  </div>
                </div>
              ) : null}
              <span className="muted" style={{ fontSize: 11 }}>{when(b.created_at)}</span>
            </Link>
          );
        })}
      </div>
    </section>
  );
}

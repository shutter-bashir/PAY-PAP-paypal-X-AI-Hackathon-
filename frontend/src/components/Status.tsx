import type { BatchStatus } from "@/lib/types";

const STATUS: Record<BatchStatus, { label: string; tone: string }> = {
  draft: { label: "Draft", tone: "tone-muted" },
  in_review: { label: "In review", tone: "tone-warn" },
  approved: { label: "Approved", tone: "tone-info" },
  submitting: { label: "Sending", tone: "tone-info" },
  submitted: { label: "Sent to PayPal", tone: "tone-info" },
  processing: { label: "PayPal processing", tone: "tone-info" },
  completed: { label: "Completed", tone: "tone-ok" },
  rejected: { label: "Rejected", tone: "tone-bad" },
  cancelled: { label: "Cancelled", tone: "tone-muted" },
};

export function StatusBadge({ status }: { status: BatchStatus }) {
  const s = STATUS[status] ?? { label: status, tone: "tone-muted" };
  return (
    <span className={`pill ${s.tone}`}>
      <span className="pill-dot" aria-hidden="true" />
      {s.label}
    </span>
  );
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <div role="alert" className="alert tone-bad">
      {error}
    </div>
  );
}

export function PageTitle({ title, sub, children }: { title: string; sub?: React.ReactNode; children?: React.ReactNode }) {
  return (
    <div style={{ display: "flex", flexWrap: "wrap", alignItems: "flex-end", justifyContent: "space-between", gap: 12, padding: "2px 4px 0" }}>
      <div>
        <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, letterSpacing: "-0.02em" }}>{title}</h1>
        {sub ? (
          <p className="muted" style={{ margin: "4px 0 0", fontSize: 13 }}>
            {sub}
          </p>
        ) : null}
      </div>
      {children}
    </div>
  );
}

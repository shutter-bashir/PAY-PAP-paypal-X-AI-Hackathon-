"use client";

import { useCallback, useEffect, useState } from "react";
import { api, when } from "@/lib/api";
import type { AuditRow } from "@/lib/types";
import { IconShield } from "./Icons";
import { ErrorNote } from "./Status";

const ACTOR_TONE: Record<AuditRow["actor_type"], string> = {
  user: "tone-info",
  agent: "tone-warn",
  system: "tone-muted",
  paypal: "tone-ok",
};

/** One line in plain words for an audit event. */
export function describeAudit(row: AuditRow): string {
  const d = row.details as Record<string, unknown>;
  switch (row.action) {
    case "item.decision_changed":
      return `Row ${d.row}: ${d.from ?? "new"} → ${d.to}${d.reason ? ` (${d.reason})` : ""}`;
    case "item.payout_status_changed":
      return `Row ${d.row}: ${d.from ?? "—"} → ${d.to} via ${d.source}`;
    case "flag.raised":
      return `${d.severity} ${d.code}: ${d.message}`;
    case "batch.status_changed":
      return `${d.from} → ${d.to}`;
    case "batch.approved":
      return `${d.item_count} rows, ${d.total_amount} ${d.currency}`;
    case "agent.tool_called":
      return `${d.tool}`;
    case "agent.filter_applied":
      return `"${d.query}" matched ${d.matched} rows`;
    case "webhook.received":
      return `${d.event_type}`;
    default:
      return Object.entries(d)
        .filter(([, v]) => v !== null && v !== "" && typeof v !== "object")
        .map(([k, v]) => `${k}: ${v}`)
        .join(" · ");
  }
}

export default function AuditTable({ batchId }: { batchId?: string }) {
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [action, setAction] = useState("");
  const [chain, setChain] = useState<{ ok: boolean; text: string } | null>(null);

  const load = useCallback(
    (before?: number) => {
      const params = new URLSearchParams({ limit: "200" });
      if (batchId) params.set("batch_id", batchId);
      if (before) params.set("before_id", String(before));
      return api<AuditRow[]>(`/api/audit?${params}`)
        .then((page) => {
          setRows((prev) => (before ? [...prev, ...page] : page));
          setHasMore(page.length === 200);
        })
        .catch((e: Error) => setError(e.message));
    },
    [batchId],
  );

  useEffect(() => {
    load();
  }, [load]);

  async function verify() {
    try {
      const r = await api<{ rows_checked: number; first_bad_id: number | null }>("/api/audit/verify");
      setChain(
        r.first_bad_id === null
          ? { ok: true, text: `Hash chain intact: ${r.rows_checked} entries verified.` }
          : { ok: false, text: `Hash chain BROKEN at entry ${r.first_bad_id} (${r.rows_checked} verified before it).` },
      );
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const shown = action ? rows.filter((r) => r.action.startsWith(action)) : rows;
  const actions = [...new Set(rows.map((r) => r.action.split(".")[0]))].sort();

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
        <select className="field well" style={{ width: "auto", minWidth: 180, paddingRight: 36 }} value={action} onChange={(e) => setAction(e.target.value)} aria-label="Filter by event type">
          <option value="">All events</option>
          {actions.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
        <button className="btn" onClick={verify}>
          <IconShield size={16} /> Verify hash chain
        </button>
        {chain ? <span className={`pill ${chain.ok ? "tone-ok" : "tone-bad"}`}>{chain.text}</span> : null}
      </div>
      <ErrorNote error={error} />
      <div className="glass-card" style={{ overflowX: "auto", padding: 6 }}>
        <table style={{ width: "100%", borderCollapse: "separate", borderSpacing: 0, fontSize: 13 }}>
          <thead>
            <tr className="muted" style={{ textAlign: "left", fontSize: 11, textTransform: "uppercase", letterSpacing: ".04em" }}>
              <th style={{ padding: "10px 12px", fontWeight: 600 }}>#</th>
              <th style={{ padding: "10px 12px", fontWeight: 600 }}>When</th>
              <th style={{ padding: "10px 12px", fontWeight: 600 }}>Who</th>
              <th style={{ padding: "10px 12px", fontWeight: 600 }}>Event</th>
              <th style={{ padding: "10px 12px", fontWeight: 600 }}>Details</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => (
              <tr key={r.id} style={{ verticalAlign: "top" }}>
                <td className="muted num" style={{ padding: "8px 12px", borderTop: "1px solid var(--track)" }}>{r.id}</td>
                <td className="muted" style={{ padding: "8px 12px", borderTop: "1px solid var(--track)", whiteSpace: "nowrap" }}>{when(r.occurred_at)}</td>
                <td style={{ padding: "8px 12px", borderTop: "1px solid var(--track)", whiteSpace: "nowrap" }}>
                  <span className={`pill ${ACTOR_TONE[r.actor_type]}`} style={{ height: 22 }}>
                    {r.actor_type}
                  </span>
                  {r.actor_email ? <span className="muted" style={{ marginLeft: 6, fontSize: 11 }}>{r.actor_email}</span> : null}
                </td>
                <td className="mono" style={{ padding: "8px 12px", borderTop: "1px solid var(--track)", fontSize: 12, whiteSpace: "nowrap" }}>{r.action}</td>
                <td style={{ padding: "8px 12px", borderTop: "1px solid var(--track)", fontSize: 12 }}>{describeAudit(r)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {shown.length === 0 ? <p className="muted" style={{ padding: 12, fontSize: 13 }}>No events yet.</p> : null}
      </div>
      {hasMore ? (
        <button className="btn" style={{ alignSelf: "flex-start" }} onClick={() => load(rows[rows.length - 1]?.id)}>
          Load older events
        </button>
      ) : null}
    </div>
  );
}

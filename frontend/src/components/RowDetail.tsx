"use client";

import { useEffect } from "react";
import { money } from "@/lib/api";
import type { Decision, Item } from "@/lib/types";
import { IconClose } from "./Icons";

interface Props {
  item: Item;
  currency: string;
  editable: boolean;
  onDecide: (rows: number[], decision: Exclude<Decision, "pending">) => void;
  onClose: () => void;
}

export default function RowDetail({ item, currency, editable, onDecide, onClose }: Props) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const blocked = item.top_severity === "blocking";
  const facts: [string, React.ReactNode][] = [
    ["Email", item.receiver ?? "missing"],
    ["Invoice", item.invoice_id ?? "—"],
    ["Note", item.note ?? "—"],
    ["Vetting", item.recipient_vetting_status ?? "new contractor"],
    ["Decision", `${item.decision}${item.decision_reason ? ` (${item.decision_reason})` : ""}`],
  ];
  if (item.payout_status) facts.push(["PayPal", `${item.payout_status}${item.paypal_transaction_id ? ` · ${item.paypal_transaction_id}` : ""}`]);

  return (
    <>
      <div className="drawer-veil" onClick={onClose} aria-hidden="true" />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label={`Row ${item.row_number}`}>
        <div style={{ display: "flex", alignItems: "flex-start", gap: 12, padding: 18 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="muted" style={{ fontSize: 12 }}>Row {item.row_number}</div>
            <div style={{ fontSize: 20, fontWeight: 700, letterSpacing: "-0.02em" }}>{item.recipient_name ?? "Unnamed"}</div>
            <div className="num" style={{ fontSize: 34, fontWeight: 700, letterSpacing: "-0.03em", marginTop: 6 }}>
              {money(item.amount, currency)}
            </div>
          </div>
          <button type="button" className="icon-btn icon-btn-sm" aria-label="Close" onClick={onClose}>
            <IconClose size={18} />
          </button>
        </div>
        <div style={{ flex: 1, overflowY: "auto", padding: "0 18px 18px", display: "flex", flexDirection: "column", gap: 14 }}>
          {editable ? (
            <div style={{ display: "flex", gap: 8 }}>
              <button className="btn btn-solid" disabled={blocked || item.decision === "include"} title={blocked ? "Blocked by a rule; it cannot be paid" : undefined} onClick={() => onDecide([item.row_number], "include")}>
                Pay this row
              </button>
              <button className="btn" disabled={item.decision === "exclude"} onClick={() => onDecide([item.row_number], "exclude")}>
                Leave out
              </button>
            </div>
          ) : null}
          <div className="glass-card" style={{ padding: 14 }}>
            <h3 className="card-title" style={{ fontSize: 14 }}>Why it was flagged</h3>
            {item.flags.length === 0 ? <p className="muted" style={{ margin: "6px 0 0", fontSize: 13 }}>No flags. The rules found nothing to check.</p> : null}
            <ul style={{ listStyle: "none", margin: "8px 0 0", padding: 0, display: "flex", flexDirection: "column", gap: 12 }}>
              {item.flags.map((f) => (
                <li key={f.id} style={{ fontSize: 13 }}>
                  <span className={`pill ${f.severity === "blocking" ? "tone-bad" : f.severity === "warning" ? "tone-warn" : "tone-info"}`}>
                    {f.code.replaceAll("_", " ").toLowerCase()}
                  </span>
                  <p style={{ margin: "6px 0 0" }}>{f.message}</p>
                  {f.ai_explanation ? (
                    <p className="well" style={{ margin: "6px 0 0", padding: "8px 10px", borderRadius: 12, fontSize: 12 }}>
                      <strong>AI:</strong> {f.ai_explanation}
                    </p>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
          <dl className="glass-card" style={{ margin: 0, padding: 14, display: "flex", flexDirection: "column", gap: 8 }}>
            {facts.map(([k, v]) => (
              <div key={k} style={{ display: "flex", justifyContent: "space-between", gap: 12, fontSize: 13 }}>
                <dt className="muted">{k}</dt>
                <dd style={{ margin: 0, textAlign: "right", wordBreak: "break-word" }}>{v}</dd>
              </div>
            ))}
          </dl>
        </div>
      </aside>
    </>
  );
}

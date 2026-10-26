"use client";

import { useEffect, useRef, useState } from "react";
import { api, money } from "@/lib/api";
import { useSpeech } from "@/lib/useSpeech";
import type { AgentReply } from "@/lib/types";
import { IconClose, IconSparkle } from "./Icons";
import MicButton from "./MicButton";
import { ErrorNote } from "./Status";

const SUGGESTIONS = [
  "What are the biggest risks in this batch?",
  "Which rows still need a decision, and what would you do with each?",
  "Propose a batch that pays every clean row and stays within the caps.",
];

interface Props {
  open: boolean;
  onClose: () => void;
  batchId: string;
  currency: string;
  canApply: boolean;
  /** A question to send as soon as the drawer opens (from the header's agent bar). */
  initialQuestion: string | null;
  onApply: (include: number[], exclude: number[], reason: string) => Promise<string>;
}

export default function AgentDrawer({ open, onClose, batchId, currency, canApply, initialQuestion, onApply }: Props) {
  const [question, setQuestion] = useState("");
  const [reply, setReply] = useState<AgentReply | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [applying, setApplying] = useState(false);
  const [applied, setApplied] = useState<{ ok: boolean; text: string } | null>(null);
  const asked = useRef<string | null>(null);
  const speech = useSpeech((text) => void ask(text));

  async function ask(q: string) {
    if (!q.trim()) return;
    setQuestion(q);
    setBusy(true);
    setError(null);
    setApplied(null);
    try {
      setReply(await api<AgentReply>(`/api/batches/${batchId}/agent`, "POST", { json: { question: q } }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (open && initialQuestion && asked.current !== initialQuestion) {
      asked.current = initialQuestion;
      void ask(initialQuestion);
    }
    // ask() only reads its argument and stable setters
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initialQuestion]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  const proposal = reply?.proposal;

  async function apply() {
    if (!proposal) return;
    setApplying(true);
    setApplied(null);
    try {
      setApplied({ ok: true, text: await onApply(proposal.include_rows, proposal.exclude_rows, "applied agent proposal") });
    } catch (e) {
      setApplied({ ok: false, text: (e as Error).message });
    } finally {
      setApplying(false);
    }
  }

  return (
    <>
      <div className="drawer-veil" onClick={onClose} aria-hidden="true" />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label="Payout agent">
        <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "16px 16px 12px" }}>
          <span aria-hidden="true" style={{ width: 40, height: 40, borderRadius: 9999, background: "radial-gradient(circle at 32% 30%,#c4f4ff 0%,#6fa9ff 36%,#8b5cf6 70%,#2c2158 100%)", boxShadow: "0 0 14px rgba(120,140,255,.55)" }} />
          <div style={{ flex: 1 }}>
            <div style={{ fontWeight: 600 }}>Payout agent</div>
            <div className="muted" style={{ fontSize: 12 }}>Reads and proposes. Cannot approve or pay without an approval.</div>
          </div>
          <button type="button" className="icon-btn icon-btn-sm" aria-label="Close" onClick={onClose}>
            <IconClose size={18} />
          </button>
        </div>
        <div style={{ flex: 1, overflowY: "auto", padding: "0 16px 16px", display: "flex", flexDirection: "column", gap: 12 }}>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void ask(question);
            }}
            style={{ display: "flex", flexDirection: "column", gap: 8 }}
          >
            <textarea className="field field-box well" rows={3} value={question} onChange={(e) => setQuestion(e.target.value)} placeholder={speech.state === "listening" ? "Listening… ask your question, then pause" : speech.state === "transcribing" ? "Turning your voice into text…" : "Ask about this batch… or tap the mic and speak"} aria-label="Question for the agent" />
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <button className="btn btn-solid" disabled={busy || !question.trim() || speech.state !== "idle"}>
                <IconSparkle size={16} /> {busy ? "Thinking…" : "Ask"}
              </button>
              <MicButton supported={speech.supported} state={speech.state} level={speech.level} onStart={speech.start} onStop={speech.stop} disabled={busy} />
              {speech.state === "listening" ? <span className="muted" style={{ fontSize: 12 }}>Listening… it sends when you pause.</span> : null}
              {speech.state === "transcribing" ? <span className="muted" style={{ fontSize: 12 }}>Turning your voice into text…</span> : null}
            </div>
            {speech.error ? <p className="alert tone-warn" style={{ margin: 0 }}>{speech.error}</p> : null}
          </form>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {SUGGESTIONS.map((s) => (
              <button key={s} type="button" className="btn" style={{ height: "auto", minHeight: 36, padding: "8px 14px", whiteSpace: "normal", textAlign: "left", justifyContent: "flex-start", fontWeight: 500 }} disabled={busy} onClick={() => ask(s)}>
                {s}
              </button>
            ))}
          </div>
          <ErrorNote error={error} />
          {busy ? <p className="muted" style={{ margin: 0, fontSize: 13 }}>The agent is reading the batch… on the free Groq plan this can take up to a minute.</p> : null}
          {reply ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <div className="well" style={{ whiteSpace: "pre-wrap", padding: 14, borderRadius: 16, fontSize: 14, lineHeight: 1.5 }}>
                {reply.text}
              </div>
              {reply.tool_calls.length ? (
                <p className="muted" style={{ margin: 0, fontSize: 11 }}>
                  Tools used: {reply.tool_calls.map((t) => `${t.tool}${t.error ? " (refused)" : ""}`).join(", ")}
                </p>
              ) : null}
              {proposal ? (
                <div className="glass-card" style={{ padding: 14, display: "flex", flexDirection: "column", gap: 6 }}>
                  <div style={{ fontWeight: 600 }}>
                    Proposal: pay {proposal.row_count} rows · {money(proposal.total, currency)}
                  </div>
                  <p className="muted" style={{ margin: 0, fontSize: 12 }}>{proposal.rationale}</p>
                  <p className="muted" style={{ margin: 0, fontSize: 12 }}>
                    Pays rows {proposal.include_rows.join(", ") || "none"}; the other {proposal.exclude_rows.length} rows are left out.
                  </p>
                  {proposal.blocked_rows_dropped.length ? (
                    <p style={{ margin: 0, fontSize: 12, color: "var(--warn-ink)" }}>Blocked rows left out: {proposal.blocked_rows_dropped.join(", ")}</p>
                  ) : null}
                  {proposal.limit_problems.map((p) => (
                    <p key={p} style={{ margin: 0, fontSize: 12, color: "var(--bad)" }}>
                      {p}
                    </p>
                  ))}
                  <button className="btn btn-solid" style={{ alignSelf: "flex-start", marginTop: 4 }} disabled={!canApply || applying || proposal.limit_problems.length > 0} onClick={apply}>
                    {applying ? "Applying…" : "Apply as my decisions"}
                  </button>
                  {!canApply ? <p className="muted" style={{ margin: 0, fontSize: 12 }}>Decisions are locked once a batch is approved or sent.</p> : null}
                  {applied ? (
                    <p role="status" className={`alert ${applied.ok ? "tone-ok" : "tone-bad"}`} style={{ margin: 0 }}>
                      {applied.text}
                    </p>
                  ) : null}
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      </aside>
    </>
  );
}

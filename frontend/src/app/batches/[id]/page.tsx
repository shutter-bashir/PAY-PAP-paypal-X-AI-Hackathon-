"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import AgentDrawer from "@/components/AgentDrawer";
import AuditTable from "@/components/AuditTable";
import {
  IconBack,
  IconBan,
  IconCheck,
  IconChecklist,
  IconChevronRight,
  IconClipboard,
  IconClose,
  IconCopy,
  IconDownload,
  IconExternal,
  IconFilter,
  IconPen,
  IconPie,
  IconPlus,
  IconRefresh,
  IconRetry,
  IconSearch,
  IconSend,
  IconSparkle,
  IconWarning,
} from "@/components/Icons";
import ReviewGrid from "@/components/ReviewGrid";
import RowDetail from "@/components/RowDetail";
import { ErrorNote, StatusBadge } from "@/components/Status";
import { api, download, money, when } from "@/lib/api";
import { ShellAction, SHELL_EVENT, takeStashed } from "@/lib/bus";
import type { BatchDetail, Decision, FilterSpec, Item, Overview, Preflight } from "@/lib/types";
import { useUser } from "@/lib/useSession";

type Tab = "review" | "reconcile" | "audit" | "ai";
type Filter = { label: string; key: string; rows: Set<number> };
const LIVE = ["submitting", "submitted", "processing"];
const SENT = ["submitted", "processing", "completed", "rejected"];

const time = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "—";

export default function BatchPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const user = useUser();
  const [detail, setDetail] = useState<BatchDetail | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  const [tab, setTab] = useState<Tab>("review");
  const [openItem, setOpenItem] = useState<Item | null>(null);
  const [quick, setQuick] = useState("");
  const [nlQuery, setNlQuery] = useState("");
  const [filter, setFilter] = useState<Filter | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [agentOpen, setAgentOpen] = useState(false);
  const [agentQuestion, setAgentQuestion] = useState<string | null>(null);
  const [logOpen, setLogOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [comment, setComment] = useState("");
  const tabsRef = useRef<HTMLDivElement>(null);

  const load = useCallback(
    () =>
      Promise.all([api<BatchDetail>(`/api/batches/${id}`), api<Item[]>(`/api/batches/${id}/items`)])
        .then(([d, i]) => {
          setDetail(d);
          setItems(i);
          setOpenItem((cur) => (cur ? (i.find((x) => x.id === cur.id) ?? null) : null));
        })
        .catch((e: Error) => setError(e.message)),
    [id],
  );

  useEffect(() => {
    if (user) load();
  }, [user, load]);

  // Before sending: the PayPal balance, and included rows PayPal turned away before.
  const [preflight, setPreflight] = useState<Preflight | null>(null);
  const includedKey = `${detail?.overview.status}|${detail?.overview.included_count}|${detail?.overview.included_total}`;
  useEffect(() => {
    if (!detail || !["in_review", "approved", "submitting"].includes(detail.overview.status)) return;
    let live = true;
    api<Preflight>(`/api/batches/${id}/preflight`)
      .then((p) => live && setPreflight(p))
      .catch(() => live && setPreflight(null));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- re-check only when the included rows change
  }, [id, includedKey]);

  // While PayPal works on the batch, ask it for news on each refresh: on serverless hosting there is
  // no worker polling in the background, and a missed webhook would otherwise leave rows pending.
  const status = detail?.overview.status;
  useEffect(() => {
    if (!status || !LIVE.includes(status)) return;
    const timer = setInterval(() => {
      api(`/api/batches/${id}/sync`, "POST")
        .catch(() => undefined)
        .finally(() => load());
    }, 15000);
    return () => clearInterval(timer);
  }, [status, load, id]);

  const showTab = useCallback((t: Tab) => {
    setTab(t);
    setTimeout(() => tabsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
  }, []);

  // The header's agent bar and the dock talk to this page through shell actions.
  const handleShell = useCallback(
    (action: ShellAction) => {
      if (action.type === "ask") {
        setAgentQuestion(action.question);
        setAgentOpen(true);
      } else if (action.type === "open-agent") {
        setAgentOpen(true);
      } else {
        showTab(action.tab);
      }
    },
    [showTab],
  );

  useEffect(() => {
    const listener = (e: Event) => handleShell((e as CustomEvent<ShellAction>).detail);
    window.addEventListener(SHELL_EVENT, listener);
    // Read the stashed action inside the timer: in development React runs this effect
    // twice, and taking it eagerly would let the first (cancelled) run swallow it.
    const timer = setTimeout(() => {
      const pending = takeStashed();
      if (pending) handleShell(pending);
    }, 0);
    return () => {
      window.removeEventListener(SHELL_EVENT, listener);
      clearTimeout(timer);
    };
  }, [handleShell]);

  async function run(label: string, fn: () => Promise<string | void>) {
    setBusy(label);
    setError(null);
    setNote(null);
    try {
      const text = await fn();
      if (text) setNote({ ok: true, text });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const decide = useCallback(
    (rows: number[], decision: Exclude<Decision, "pending">, reason?: string) =>
      run("decide", async () => {
        await api(`/api/batches/${id}/decisions`, "POST", {
          json: { decisions: rows.map((row_number) => ({ row_number, decision, reason: reason ?? null })) },
        });
        await load();
      }),
    [id, load],
  );

  async function applyProposal(include: number[], exclude: number[], reason: string): Promise<string> {
    const res = await api<{ changed: number; overview: Overview }>(`/api/batches/${id}/decisions`, "POST", {
      json: {
        decisions: [
          ...include.map((row_number) => ({ row_number, decision: "include", reason })),
          ...exclude.map((row_number) => ({ row_number, decision: "exclude", reason })),
        ],
      },
    });
    await load();
    const ov = res.overview;
    return `Applied: ${res.changed} row(s) changed. The batch now pays ${ov.included_count} rows · ${money(ov.included_total, ov.currency)}.`;
  }

  const rowsWhere = useCallback((pred: (i: Item) => boolean) => new Set(items.filter(pred).map((i) => i.row_number)), [items]);

  function toggleFilter(key: string, label: string, pred: (i: Item) => boolean, target: Tab = "review") {
    if (filter?.key === key) {
      setFilter(null);
      return;
    }
    setFilter({ key, label, rows: rowsWhere(pred) });
    showTab(target);
  }

  const applyNl = () =>
    run("filter", async () => {
      const r = await api<{ filter: FilterSpec; row_numbers: number[]; total: string }>(`/api/batches/${id}/filter`, "POST", {
        json: { query: nlQuery },
      });
      setFilter({ key: "nl", label: `${r.filter.explanation} · ${money(r.total, detail?.overview.currency)}`, rows: new Set(r.row_numbers) });
      showTab("review");
    });

  function exportCsv() {
    const header = ["row", "name", "paypal_email", "amount", "currency", "invoice", "decision", "flags", "paypal_status", "transaction_id", "report_bucket"];
    const quote = (v: unknown) => {
      const s = v === null || v === undefined ? "" : String(v);
      return /[",\n]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s;
    };
    const lines = [header.join(",")].concat(
      items.map((i) =>
        [i.row_number, i.recipient_name, i.receiver, i.amount, i.currency, i.invoice_id, i.decision, i.flag_codes.join(" "), i.payout_status, i.paypal_transaction_id, i.reconciliation_bucket]
          .map(quote)
          .join(","),
      ),
    );
    const blob = new Blob([lines.join("\n") + "\n"], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `paypap-batch-${detail?.batch.batch_no ?? id}.csv`;
    a.click();
    URL.revokeObjectURL(url);
    setNote({ ok: true, text: `Exported ${items.length} rows.` });
  }

  const highlights = useMemo(() => {
    const paying = items.filter((i) => i.decision === "include").slice(0, 3);
    const flagged = items.filter((i) => i.top_severity === "blocking" || (i.top_severity === "warning" && i.decision === "pending")).slice(0, 3);
    return [...paying, ...flagged].sort((a, b) => a.row_number - b.row_number);
  }, [items]);

  if (!detail || !user) {
    return (
      <section className="glass-panel" style={{ padding: 18 }}>
        <ErrorNote error={error} />
        {!error ? <p className="muted" style={{ margin: 0 }}>Loading batch…</p> : null}
      </section>
    );
  }

  const { overview: ov, batch, approval } = detail;
  const editable = ov.status === "draft" || ov.status === "in_review";
  const approvedOrLater = !editable && ov.status !== "cancelled";
  const sent = SENT.includes(ov.status);
  const isUploader = batch.created_by === user.id;
  const blockers = [
    ov.undecided_count ? `${ov.undecided_count} row(s) still need pay or leave out.` : null,
    ov.included_count === 0 ? "No rows are included." : null,
    ...detail.batch_flags.filter((f) => f.severity === "blocking").map((f) => f.message),
  ].filter(Boolean) as string[];
  const canApprove = ov.status === "in_review" && user.role === "approver" && !isUploader && blockers.length === 0;
  const lastSubmission = detail.submissions[detail.submissions.length - 1];
  const approvedTotal = approval ? Number(approval.total_amount) : Number(ov.included_total);
  const paidShare = approvedTotal ? Math.min(100, (Number(ov.paid_total) / approvedTotal) * 100) : 0;
  const summary = batch.ai_summary;
  const warningRows = items.filter((i) => i.top_severity === "warning").length;
  const aiExplained = items.flatMap((i) => i.flags.filter((f) => f.ai_explanation).map((f) => ({ row: i.row_number, name: i.recipient_name, f })));

  const steps = [
    { label: "Every row decided", icon: <IconChecklist size={22} />, done: ov.undecided_count === 0 && ov.included_count > 0 },
    { label: "Total approved", icon: <IconPen size={22} />, done: approvedOrLater },
    { label: "Sent to PayPal", icon: <IconSend size={22} />, done: sent },
  ];

  const rings = [
    { label: "Paid", value: ov.paid_count, color: "var(--accent)", key: "paid", pred: (i: Item) => i.reconciliation_bucket === "paid", tab: "reconcile" as Tab },
    { label: "Failed", value: ov.failed_count, color: "#e2711d", key: "failed", pred: (i: Item) => i.reconciliation_bucket === "failed", tab: "reconcile" as Tab },
    { label: "Pending", value: ov.payout_pending_count, color: "var(--info)", key: "pending-pay", pred: (i: Item) => i.reconciliation_bucket === "pending", tab: "reconcile" as Tab },
    { label: "Blocked", value: ov.blocking_rows, color: "var(--ink-3)", key: "blocked", pred: (i: Item) => i.top_severity === "blocking", tab: "review" as Tab },
  ];
  const ringBase = Math.max(1, ov.included_count + ov.blocking_rows);

  const tile = (bg: string, shadow: string) => ({
    width: 42,
    height: 42,
    borderRadius: 13,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    background: bg,
    color: "#fff",
    boxShadow: `inset 0 1px 0 rgba(255,255,255,.35), 0 5px 12px -5px ${shadow}`,
  });
  const tileBtn = { display: "flex", flexDirection: "column" as const, alignItems: "center", gap: 6, padding: 0, border: 0, background: "transparent", font: "inherit", color: "var(--ink)", cursor: "pointer" };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <section aria-label={`Batch ${batch.batch_no}`} className="glass-panel" style={{ padding: 18, display: "flex", flexDirection: "column", gap: 16 }}>
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: "10px 16px", padding: "2px 4px 0" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12, minWidth: 0 }}>
            <Link href="/" aria-label="Back to batches" title="Back to batches" className="icon-btn" style={{ width: 40, height: 40, background: "var(--card-strong)", border: "1px solid var(--card-border)" }}>
              <IconBack size={18} />
            </Link>
            <h1 style={{ margin: 0, fontSize: 20, fontWeight: 600, letterSpacing: "-0.01em", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
              #{batch.batch_no} {batch.name}
            </h1>
            <StatusBadge status={ov.status} />
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10 }}>
            <p className="muted" style={{ margin: 0, fontSize: 12, lineHeight: 1.5 }}>
              {batch.source_filename} · uploaded {when(batch.created_at)} ·{" "}
              <code title="Sent as sender_batch_id; fixed for the life of the batch so a retry can never pay twice" className="mono well" style={{ fontSize: 11, padding: "2px 6px", borderRadius: 6 }}>
                {batch.sender_batch_id.slice(0, 12)}…{batch.sender_batch_id.slice(-4)}
              </code>
            </p>
            {editable && ov.status !== "draft" ? (
              <button
                className="btn btn-sm btn-danger"
                disabled={busy !== null}
                onClick={() => {
                  if (window.confirm("Cancel this batch? Nothing has been paid; it will be closed for good.")) {
                    void run("cancel", async () => {
                      await api(`/api/batches/${id}/cancel`, "POST");
                      await load();
                      return "Batch cancelled.";
                    });
                  }
                }}
              >
                Cancel batch
              </button>
            ) : null}
          </div>
        </div>

        <ErrorNote error={error} />
        {note ? (
          <div role="status" className={`alert ${note.ok ? "tone-ok" : "tone-bad"}`}>
            {note.text}
          </div>
        ) : null}

        <div className="bento">
          {/* Filter */}
          <div className="glass-card" style={{ gridArea: "filt", minWidth: 0, display: "flex", flexDirection: "column", gap: 12, padding: 16 }}>
            <form
              role="search"
              className="well"
              onSubmit={(e) => {
                e.preventDefault();
                if (nlQuery.trim()) void applyNl();
              }}
              style={{ display: "flex", alignItems: "center", gap: 8, height: 44, padding: "0 5px 0 14px", borderRadius: 9999 }}
            >
              <IconSearch size={16} />
              <label htmlFor="row-q" className="sr-only">
                Filter rows in plain language
              </label>
              <input
                id="row-q"
                value={nlQuery}
                onChange={(e) => setNlQuery(e.target.value)}
                placeholder="Ask for rows, e.g. “approved contractors under $500”"
                style={{ flex: "1 1 auto", minWidth: 0, height: "100%", border: 0, background: "transparent", font: "inherit", fontSize: 13, color: "var(--ink)", outline: "none" }}
              />
              <button type="submit" className="icon-btn icon-btn-sm btn-white" aria-label="Filter rows" title="Filter rows" disabled={busy !== null || !nlQuery.trim()}>
                <IconFilter size={16} />
              </button>
            </form>
            <div style={{ flex: "1 1 auto", display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 10 }}>
              {[
                { key: "included", label: "Included", value: ov.included_count, icon: <IconCheck size={18} />, pred: (i: Item) => i.decision === "include" },
                { key: "warnings", label: "Warnings", value: warningRows, icon: <IconWarning size={18} />, pred: (i: Item) => i.top_severity === "warning" },
                { key: "blocked", label: "Blocked", value: ov.blocking_rows, icon: <IconBan size={18} />, pred: (i: Item) => i.top_severity === "blocking" },
              ].map((s) => (
                <button
                  key={s.key}
                  type="button"
                  aria-pressed={filter?.key === s.key}
                  onClick={() => toggleFilter(s.key, `${s.label} rows`, s.pred)}
                  className="well"
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    alignItems: "center",
                    justifyContent: "center",
                    gap: 3,
                    minHeight: 92,
                    padding: "10px 6px",
                    borderRadius: 18,
                    font: "inherit",
                    color: "var(--ink)",
                    cursor: "pointer",
                    outline: filter?.key === s.key ? "2px solid var(--accent)" : undefined,
                  }}
                >
                  {s.icon}
                  <span className="num" style={{ fontSize: 20, fontWeight: 700 }}>{s.value}</span>
                  <span className="muted" style={{ fontSize: 11 }}>{s.label}</span>
                </button>
              ))}
            </div>
          </div>

          {/* Approval */}
          <div className="glass-card" style={{ gridArea: "appr", minWidth: 0, display: "flex", flexDirection: "column", justifyContent: "space-between", gap: 14, padding: 16 }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
              <h2 className="card-title">Approval</h2>
              <StatusBadge status={ov.status} />
            </div>
            <ol style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 8 }}>
              {steps.map((s) => (
                <li key={s.label} style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 7 }}>
                  <span style={{ position: "relative", width: 50, height: 50, borderRadius: 15, display: "flex", alignItems: "center", justifyContent: "center", background: "var(--chip-white)", color: "var(--ink)", boxShadow: "0 3px 8px rgba(15,23,42,.16)", opacity: s.done ? 1 : 0.65 }}>
                    {s.icon}
                    {s.done ? (
                      <span aria-hidden="true" style={{ position: "absolute", right: -5, bottom: -5, width: 19, height: 19, borderRadius: 9999, display: "flex", alignItems: "center", justifyContent: "center", background: "var(--accent)", color: "#fff", boxShadow: "0 0 0 2px var(--chip-white)" }}>
                        <IconCheck size={11} strokeWidth={3.2} />
                      </span>
                    ) : null}
                  </span>
                  <span className="muted" style={{ fontSize: 11, lineHeight: 1.25, textAlign: "center" }}>
                    {s.label}
                    <span className="sr-only">{s.done ? " (done)" : " (not yet)"}</span>
                  </span>
                </li>
              ))}
              <li style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 7 }}>
                <button type="button" aria-label={`PayPal submission attempts: ${detail.submissions.length}`} title="Open the PayPal submission log" className="well" onClick={() => setLogOpen(true)} style={{ width: 50, height: 50, borderRadius: 15, color: "var(--ink)", font: "inherit", fontSize: 17, fontWeight: 700, cursor: "pointer" }}>
                  {detail.submissions.length}
                </button>
                <span className="muted" style={{ fontSize: 11 }}>Attempt{detail.submissions.length === 1 ? "" : "s"}</span>
              </li>
            </ol>

            {preflight ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12 }}>
                <span className="muted">
                  PayPal balance:{" "}
                  {preflight.balance !== null ? <strong style={{ color: "var(--ink)" }}>{money(preflight.balance, preflight.currency)}</strong> : "could not be read"}
                  <span title="Read with the APIMatic PayPal SDK. PayPal can take up to 3 hours to update a balance."> · may lag up to 3 h</span>
                </span>
                {preflight.short_by !== null ? (
                  <span style={{ color: "var(--warn-ink)" }}>
                    {money(preflight.short_by, preflight.currency)} short of this batch.{" "}
                    {preflight.balance_blocks ? "Sending will be refused until funds are added." : "The sandbox lets the balance go negative, so it can still be sent."}
                  </span>
                ) : null}
                {preflight.turned_away.length ? (
                  <span style={{ color: "var(--bad)" }}>
                    PayPal turned away earlier payouts to row{preflight.turned_away.length === 1 ? "" : "s"}{" "}
                    {preflight.turned_away.slice(0, 8).map((t) => t.row).join(", ")}
                    {preflight.turned_away.length > 8 ? "…" : ""}. Leave {preflight.turned_away.length === 1 ? "it" : "them"} out, or sending will be refused.
                  </span>
                ) : null}
              </div>
            ) : null}

            {ov.status === "in_review" ? (
              user.role !== "approver" ? (
                <p className="muted" style={{ margin: 0, fontSize: 12 }}>
                  {blockers.length ? blockers[0] : "Ready. Sign in as the approver to approve it."}
                </p>
              ) : isUploader ? (
                <p className="muted" style={{ margin: 0, fontSize: 12 }}>You uploaded this batch, so the other account must approve it.</p>
              ) : blockers.length ? (
                <p style={{ margin: 0, fontSize: 12, color: "var(--warn-ink)" }}>{blockers.join(" ")}</p>
              ) : confirming ? (
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  <input className="field well" placeholder="Note for the record (optional)" value={comment} onChange={(e) => setComment(e.target.value)} maxLength={500} style={{ height: 38 }} />
                  <div style={{ display: "flex", gap: 8 }}>
                    <button
                      className="btn btn-solid"
                      disabled={busy !== null}
                      onClick={() =>
                        run("approve", async () => {
                          await api(`/api/batches/${id}/approve`, "POST", {
                            json: { item_count: ov.included_count, total_amount: String(ov.included_total), comment: comment || null },
                          });
                          setConfirming(false);
                          await load();
                          return "Approved. You can now send it to PayPal.";
                        })
                      }
                    >
                      Confirm {money(ov.included_total, ov.currency)}
                    </button>
                    <button className="btn" onClick={() => setConfirming(false)}>
                      Back
                    </button>
                  </div>
                </div>
              ) : (
                <button className="btn btn-solid" disabled={!canApprove} onClick={() => setConfirming(true)}>
                  <IconPen size={16} /> Approve {money(ov.included_total, ov.currency)}
                </button>
              )
            ) : null}
            {ov.status === "approved" || ov.status === "submitting" ? (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                <button
                  className="btn btn-solid"
                  disabled={busy !== null}
                  onClick={() =>
                    run("send", async () => {
                      const r = await api<{ outcome: string; error: string | null }>(`/api/batches/${id}/submit`, "POST");
                      await load();
                      if (r.outcome !== "accepted") throw new Error(r.error ?? `PayPal outcome: ${r.outcome}. It is safe to retry.`);
                      return "PayPal accepted the batch. Statuses arrive by webhook in a minute or two.";
                    })
                  }
                >
                  <IconSend size={16} /> {busy === "send" ? "Sending…" : ov.status === "submitting" ? "Retry send" : "Send to PayPal"}
                </button>
                {ov.status === "approved" ? (
                  <button
                    className="btn btn-danger"
                    disabled={busy !== null}
                    onClick={() =>
                      run("revoke", async () => {
                        await api(`/api/batches/${id}/revoke`, "POST", { json: { reason: "revoked in review" } });
                        await load();
                        return "Approval revoked; the batch is back in review.";
                      })
                    }
                  >
                    Revoke
                  </button>
                ) : null}
              </div>
            ) : null}
          </div>

          {/* PayPal batch */}
          <div className="glass-card" style={{ gridArea: "play", minWidth: 0, display: "flex", flexDirection: "column", gap: 12, padding: 16 }}>
            <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
              <div aria-hidden="true" style={{ flex: "0 0 auto", width: 58, height: 58, borderRadius: 15, display: "flex", alignItems: "center", justifyContent: "center", background: "linear-gradient(150deg,#1e3a8a,#0b1230)", color: "#f6f1ea", boxShadow: "0 8px 16px -8px rgba(10,20,60,.6)" }}>
                <IconSend size={24} />
              </div>
              <div style={{ flex: "1 1 auto", minWidth: 0, display: "flex", flexDirection: "column", gap: 2 }}>
                <span className="muted" style={{ fontSize: 11 }}>PayPal batch</span>
                <span className="mono" style={{ fontSize: 14, fontWeight: 600, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                  {batch.paypal_payout_batch_id ?? "Not sent yet"}
                </span>
                <span className="muted" style={{ fontSize: 12 }}>
                  {batch.paypal_payout_batch_id
                    ? `${batch.paypal_batch_status ?? "—"}${lastSubmission?.http_status ? ` · HTTP ${lastSubmission.http_status}` : ""}`
                    : "Sent once approved"}
                </span>
              </div>
              <button
                type="button"
                className="icon-btn icon-btn-sm"
                aria-label="Copy PayPal batch ID"
                title="Copy batch ID"
                disabled={!batch.paypal_payout_batch_id}
                onClick={() =>
                  navigator.clipboard
                    .writeText(batch.paypal_payout_batch_id ?? "")
                    .then(() => setNote({ ok: true, text: `Copied ${batch.paypal_payout_batch_id}` }))
                    .catch(() => setNote({ ok: false, text: "Could not copy; select the ID instead." }))
                }
              >
                <IconCopy size={16} />
              </button>
            </div>
            <div>
              <div role="progressbar" aria-label="Paid of approved total" aria-valuemin={0} aria-valuemax={approvedTotal} aria-valuenow={Number(ov.paid_total)} style={{ height: 6, borderRadius: 9999, overflow: "hidden", background: "var(--track)" }}>
                <div style={{ width: `${paidShare}%`, height: "100%", borderRadius: 9999, background: "var(--accent)", transition: "width .4s ease" }} />
              </div>
              <div className="muted num" style={{ display: "flex", justifyContent: "space-between", marginTop: 6, fontSize: 11 }}>
                <span>{money(ov.paid_total, ov.currency)} paid</span>
                <span>{money(approvedTotal, ov.currency)}</span>
              </div>
            </div>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 14 }}>
              <button
                type="button"
                className="icon-btn"
                style={{ width: 40, height: 40 }}
                aria-label="Retry failed payouts in a new batch"
                title={ov.failed_count ? `Start a new batch with the ${ov.failed_count} failed row(s)` : "No failed payouts to retry"}
                disabled={!ov.failed_count || busy !== null}
                onClick={() =>
                  run("retry", async () => {
                    const r = await api<{ batch_id: string }>(`/api/batches/${id}/retry-failed`, "POST");
                    router.push(`/batches/${r.batch_id}`);
                  })
                }
              >
                <IconRetry size={18} />
              </button>
              <button
                type="button"
                className="icon-btn btn-white"
                style={{ width: 46, height: 46 }}
                aria-label="Refresh PayPal statuses"
                title="Check PayPal now"
                disabled={!batch.paypal_payout_batch_id || busy !== null}
                onClick={() =>
                  run("sync", async () => {
                    const r = await api<{ items_changed: number; paypal_batch_status: string }>(`/api/batches/${id}/sync`, "POST");
                    await load();
                    return `PayPal says ${r.paypal_batch_status}; ${r.items_changed} row(s) updated.`;
                  })
                }
              >
                <IconRefresh size={19} />
              </button>
              <button type="button" className="icon-btn" style={{ width: 40, height: 40 }} aria-label="Open submission log" title="Open submission log" onClick={() => setLogOpen(true)}>
                <IconExternal size={18} />
              </button>
            </div>
          </div>

          {/* Need a decision */}
          <button
            type="button"
            className="glass-card"
            aria-pressed={filter?.key === "undecided"}
            onClick={() => toggleFilter("undecided", "Rows that need a decision", (i) => i.decision === "pending")}
            style={{ gridArea: "need", minWidth: 0, display: "flex", flexDirection: "column", justifyContent: "space-between", gap: 12, minHeight: 176, padding: 16, font: "inherit", color: "var(--ink)", textAlign: "left", cursor: "pointer", outline: filter?.key === "undecided" ? "2px solid var(--accent)" : undefined }}
          >
            <div>
              <h2 className="card-title">Need a decision</h2>
              <ul className="muted" style={{ listStyle: "none", margin: "8px 0 0", padding: 0, display: "flex", flexDirection: "column", gap: 5, fontSize: 12 }}>
                <li>○ {warningRows} rows with warnings</li>
                <li>○ {ov.excluded_count} rows left out</li>
              </ul>
            </div>
            <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
              <span className="num" style={{ fontSize: 44, fontWeight: 700, lineHeight: 1, letterSpacing: "-0.03em" }}>{ov.undecided_count}</span>
              <span className="muted" style={{ fontSize: 12 }}>left</span>
            </div>
          </button>

          {/* PayPal items */}
          <div className="glass-card" style={{ gridArea: "ring", minWidth: 0, display: "flex", flexDirection: "column", gap: 10, padding: 14 }}>
            <h2 className="card-title" style={{ fontSize: 13 }}>PayPal items</h2>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: "10px 6px", justifyItems: "center" }}>
              {rings.map((r) => {
                const dash = Math.round((Math.min(r.value, ringBase) / ringBase) * 132);
                return (
                  <button
                    key={r.key}
                    type="button"
                    aria-pressed={filter?.key === r.key}
                    title={`Show ${r.label.toLowerCase()} rows`}
                    onClick={() => toggleFilter(r.key, `${r.label} rows`, r.pred, r.tab)}
                    style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 4, padding: 4, border: 0, borderRadius: 16, background: filter?.key === r.key ? "var(--well)" : "transparent", font: "inherit", color: "var(--ink)", cursor: "pointer" }}
                  >
                    <span style={{ position: "relative", width: 52, height: 52 }}>
                      <svg width="52" height="52" viewBox="0 0 52 52" aria-hidden="true">
                        <circle cx="26" cy="26" r="21" fill="var(--card)" stroke="var(--track)" strokeWidth="5" />
                        {r.value ? <circle cx="26" cy="26" r="21" fill="none" stroke={r.color} strokeWidth="5" strokeLinecap="round" strokeDasharray={`${Math.max(dash, 5)} 132`} transform="rotate(-90 26 26)" /> : null}
                      </svg>
                      <span className="num" style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 15, fontWeight: 700 }}>
                        {r.value}
                      </span>
                    </span>
                    <span className="muted" style={{ fontSize: 11 }}>{r.label}</span>
                  </button>
                );
              })}
            </div>
          </div>

          {/* Decisions lock */}
          <div className="glass-card" style={{ gridArea: "lock", minWidth: 0, display: "flex", flexDirection: "column", justifyContent: "space-between", gap: 14, minHeight: 160, padding: 16 }}>
            <div>
              <h2 className="card-title" style={{ lineHeight: 1.25 }}>
                Decisions
                <br />
                {editable ? "open" : "locked"}
              </h2>
              <p className="muted" style={{ margin: "4px 0 0", fontSize: 11, lineHeight: 1.4 }}>
                {editable ? "They lock when the batch is approved" : ov.status === "approved" ? "Tap to unlock (revokes the approval)" : "Locked for good: sent to PayPal"}
              </p>
            </div>
            <button
              type="button"
              className="toggle"
              aria-pressed={!editable}
              aria-label={editable ? "Decisions open" : "Decisions locked"}
              title={editable ? "Decisions lock automatically on approval" : ov.status === "approved" ? "Unlock by revoking the approval" : "Cannot unlock after sending"}
              disabled={ov.status !== "approved" || busy !== null}
              onClick={() => {
                if (window.confirm("Unlock decisions? This revokes the approval and sends the batch back to review.")) {
                  void run("revoke", async () => {
                    await api(`/api/batches/${id}/revoke`, "POST", { json: { reason: "unlocked decisions" } });
                    await load();
                    return "Approval revoked; decisions are open again.";
                  });
                }
              }}
            />
          </div>

          {/* AI review */}
          <div className="glass-card" style={{ gridArea: "ai", minWidth: 0, display: "flex", flexDirection: "column", justifyContent: "space-between", gap: 14, minHeight: 160, padding: 16 }}>
            <div>
              <h2 className="card-title" style={{ lineHeight: 1.25 }}>
                AI
                <br />
                review
              </h2>
              <p className="muted" style={{ margin: "4px 0 0", fontSize: 11, lineHeight: 1.4 }}>
                {busy === "explain" ? "Explaining… about a minute" : summary ? `${summary.risk_level} risk · ${time(batch.ai_summarized_at)}` : "Not run · explains flags only"}
              </p>
            </div>
            <button
              type="button"
              className="toggle"
              aria-pressed={Boolean(summary)}
              aria-label={summary ? "Open the AI review" : "Explain flags with AI"}
              title={summary ? "Open the AI review" : "Explain flags with AI"}
              disabled={busy !== null}
              onClick={() =>
                summary
                  ? showTab("ai")
                  : run("explain", async () => {
                      await api(`/api/batches/${id}/explain`, "POST");
                      await load();
                      showTab("ai");
                      return "AI review ready.";
                    })
              }
            />
          </div>

          {/* Rows */}
          <div className="glass-card" style={{ gridArea: "rows", minWidth: 0, display: "flex", flexDirection: "column", gap: 14, padding: 16 }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
              <h2 className="card-title">
                Rows <span className="muted" style={{ fontWeight: 500 }}>· {ov.row_count}</span>
              </h2>
              <div style={{ display: "flex", gap: 6 }}>
                <button
                  type="button"
                  className="btn btn-sm tone-white"
                  title="Download PayPal bulk-create files (40 accounts each) so every contractor here gets a sandbox account"
                  disabled={busy !== null}
                  onClick={() =>
                    run("sandbox", async () => {
                      await download(`/api/batches/${id}/sandbox-accounts`, `batch-${ov.batch_no}-sandbox-accounts.zip`);
                      return "Sandbox account files downloaded. Upload each one at developer.paypal.com > Sandbox accounts > Bulk create (see README.txt in the zip).";
                    })
                  }
                >
                  <IconDownload size={13} /> {busy === "sandbox" ? "Preparing…" : "Sandbox accounts"}
                </button>
                <button type="button" className="btn btn-sm tone-white" onClick={() => { setFilter(null); showTab("review"); }}>
                  View all <IconChevronRight size={13} strokeWidth={2.2} />
                </button>
              </div>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 8 }}>
              <button
                type="button"
                style={tileBtn}
                disabled={busy !== null}
                onClick={() =>
                  run("explain", async () => {
                    await api(`/api/batches/${id}/explain`, "POST");
                    await load();
                    showTab("ai");
                    return "AI review ready.";
                  })
                }
              >
                <span aria-hidden="true" style={tile("#7457e8", "rgba(80,50,180,.6)")}><IconSparkle size={20} /></span>
                <span style={{ fontSize: 11 }}>{busy === "explain" ? "Explaining…" : "Explain"}</span>
              </button>
              <button type="button" style={tileBtn} onClick={exportCsv}>
                <span aria-hidden="true" style={tile("#2d74e6", "rgba(30,90,200,.6)")}><IconDownload size={20} /></span>
                <span style={{ fontSize: 11 }}>Export</span>
              </button>
              <button type="button" style={tileBtn} onClick={() => showTab("audit")}>
                <span aria-hidden="true" style={tile("#e8902a", "rgba(200,110,20,.6)")}><IconClipboard size={20} /></span>
                <span style={{ fontSize: 11 }}>Audit</span>
              </button>
              <button type="button" style={tileBtn} onClick={() => showTab("reconcile")}>
                <span aria-hidden="true" style={tile("#1f9d74", "rgba(20,130,90,.6)")}><IconPie size={20} /></span>
                <span style={{ fontSize: 11 }}>Reconcile</span>
              </button>
            </div>
            <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 6 }}>
              {highlights.map((i) => {
                const top = i.flags.find((f) => f.severity === "blocking") ?? i.flags.find((f) => f.severity === "warning");
                const [label, tone] =
                  i.top_severity === "blocking"
                    ? [top?.code.replaceAll("_", " ").toLowerCase() ?? "blocked", "tone-bad"]
                    : i.decision === "pending"
                      ? [top?.code.replaceAll("_", " ").toLowerCase() ?? "decide", "tone-warn"]
                      : i.decision === "include"
                        ? [i.payout_status ? i.payout_status.toLowerCase() : "pay", "tone-ok"]
                        : ["left out", "tone-muted"];
                return (
                  <li key={i.id}>
                    <button
                      type="button"
                      className="well"
                      onClick={() => setOpenItem(i)}
                      style={{ width: "100%", display: "grid", gridTemplateColumns: "minmax(0,1fr) auto auto", alignItems: "center", gap: 10, padding: "8px 10px 8px 12px", borderRadius: 14, font: "inherit", color: "var(--ink)", textAlign: "left", cursor: "pointer" }}
                    >
                      <span style={{ minWidth: 0, display: "flex", flexDirection: "column", gap: 1 }}>
                        <span style={{ fontSize: 13, fontWeight: 600, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{i.recipient_name ?? "Unnamed"}</span>
                        <span className="muted" style={{ fontSize: 11, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                          Row {i.row_number} · {top && i.decision !== "include" ? top.message : (i.receiver ?? "no PayPal email")}
                        </span>
                      </span>
                      <span className="num" style={{ fontSize: 13, fontWeight: 600 }}>{money(i.amount, ov.currency)}</span>
                      <span className={`pill ${tone}`} style={{ height: 24, minWidth: 72, justifyContent: "center", fontSize: 11 }}>
                        {label}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>

          {/* Total */}
          <div className="glass-card" style={{ gridArea: "amt", minWidth: 0, display: "flex", flexDirection: "column", gap: 14, padding: 16 }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
              <h2 className="card-title">{approval ? "Approved total" : "To pay"}</h2>
              <button
                type="button"
                className="toggle"
                aria-pressed={Boolean(approval?.signature_valid)}
                aria-label={approval ? (approval.signature_valid ? "Signature verified" : "Signature invalid") : "Not approved yet"}
                title={approval ? (approval.signature_valid ? "The approval's HMAC signature checks out" : "Signature invalid: this approval cannot release money") : "No approval yet"}
                onClick={() =>
                  setNote(
                    approval
                      ? approval.signature_valid
                        ? { ok: true, text: "Signature verified: this approval was made by PAY PAP and has not been changed." }
                        : { ok: false, text: "Signature invalid: this approval cannot release money. Revoke it and approve again." }
                      : { ok: true, text: "Not approved yet. The approver's approval is signed so it cannot be forged." },
                  )
                }
              />
            </div>
            <div style={{ padding: "10px 0 4px" }}>
              <div className="num" style={{ fontSize: 46, fontWeight: 700, lineHeight: 1, letterSpacing: "-0.035em" }}>{money(approvedTotal, ov.currency)}</div>
              <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>
                {approval ? approval.item_count : ov.included_count} of {ov.row_count} rows · {ov.currency}
              </div>
            </div>
            <dl style={{ margin: 0, display: "flex", flexDirection: "column", gap: 8 }}>
              {(approval
                ? [
                    ["Approved by", approval.approved_by_name ?? "Approver"],
                    ["Approved at", time(approval.created_at)],
                    ["Expires", approval.consumed_at ? "Used" : time(approval.expires_at)],
                    ["Excluded", `${approval.excluded_count} rows`],
                  ]
                : [
                    ["Status", "Not approved yet"],
                    ["Undecided", `${ov.undecided_count} rows`],
                    ["Excluded", `${ov.excluded_count} rows`],
                  ]
              ).map(([k, v]) => (
                <div key={k} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
                  <dt className="muted" style={{ fontSize: 12 }}>{k}</dt>
                  <dd className="well num" style={{ margin: 0, padding: "5px 10px", borderRadius: 10, fontSize: 12, fontWeight: 600 }}>{v}</dd>
                </div>
              ))}
            </dl>
            {approval?.comment ? (
              <figure style={{ margin: "auto 0 0", padding: "10px 12px", borderRadius: 14, background: "var(--card-strong)", border: "1px solid var(--card-border)" }}>
                <blockquote style={{ margin: 0, fontSize: 13, fontWeight: 500 }}>“{approval.comment}”</blockquote>
                <figcaption className="muted" style={{ marginTop: 3, fontSize: 11 }}>
                  Approval note · {approval.signature_valid ? "signature verified" : "signature invalid"}
                </figcaption>
              </figure>
            ) : null}
          </div>
        </div>
      </section>

      <div ref={tabsRef} className="under-panel" style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 12, scrollMarginTop: 16 }}>
        <nav aria-label="Batch views" className="glass-bar" style={{ maxWidth: "100%", overflowX: "auto", display: "flex", alignItems: "center", gap: 4, padding: 6, borderRadius: 9999 }}>
          {(
            [
              ["review", "Review"],
              ["reconcile", "Reconciliation"],
              ["audit", "Audit trail"],
              ["ai", "AI review"],
            ] as [Tab, string][]
          ).map(([t, label]) => (
            <button
              key={t}
              type="button"
              aria-current={tab === t ? "page" : undefined}
              onClick={() => setTab(t)}
              className="btn"
              style={{ height: 44, padding: "0 20px", fontSize: 14, border: 0, fontWeight: tab === t ? 600 : 500, background: tab === t ? "var(--chip-white)" : "transparent", boxShadow: tab === t ? "0 3px 10px rgba(15,23,42,.22)" : "none" }}
            >
              {label}
            </button>
          ))}
          <Link href="/upload" aria-label="Upload a new CSV" title="Upload a new CSV" className="icon-btn is-on" style={{ marginLeft: 6 }}>
            <IconPlus size={20} strokeWidth={2} />
          </Link>
        </nav>
      </div>

      <section className="glass-panel" style={{ padding: 18, display: "flex", flexDirection: "column", gap: 12 }}>
        {tab === "review" || tab === "reconcile" ? (
          <>
            <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
              <input className="field well" style={{ width: 220, height: 38 }} value={quick} onChange={(e) => setQuick(e.target.value)} placeholder="Quick search" aria-label="Quick search" />
              {filter && tab === "review" ? (
                <span className="pill tone-info" style={{ height: 32, maxWidth: "100%" }}>
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{filter.label} · {filter.rows.size} rows</span>
                  <button type="button" className="icon-btn" style={{ width: 22, height: 22 }} aria-label="Clear filter" onClick={() => setFilter(null)}>
                    <IconClose size={13} />
                  </button>
                </span>
              ) : null}
              {filter && tab === "review" && editable && filter.rows.size ? (
                <>
                  <button className="btn btn-sm" onClick={() => decide([...filter.rows], "include", `filter: ${filter.label}`)}>Pay these</button>
                  <button className="btn btn-sm" onClick={() => decide([...filter.rows], "exclude", `filter: ${filter.label}`)}>Leave these out</button>
                </>
              ) : null}
              {editable && ov.undecided_count ? (
                <button className="btn btn-sm" onClick={() => decide(items.filter((i) => i.decision === "pending").map((i) => i.row_number), "include", "included after review")}>
                  Pay all {ov.undecided_count} undecided
                </button>
              ) : null}
              {tab === "reconcile" && !batch.paypal_payout_batch_id ? <span className="muted" style={{ fontSize: 13 }}>Reconciliation starts once PayPal accepts the batch.</span> : null}
            </div>
            <ReviewGrid
              items={tab === "reconcile" ? items.filter((i) => i.decision === "include" || i.reconciliation_bucket) : items}
              currency={ov.currency}
              editable={editable}
              onlyRows={filter ? filter.rows : null}
              quickText={quick}
              onDecide={(rows, d) => decide(rows, d)}
              onOpen={setOpenItem}
              mode={tab === "reconcile" ? "reconcile" : "review"}
            />
          </>
        ) : null}
        {tab === "audit" ? <AuditTable batchId={id} /> : null}
        {tab === "ai" ? (
          summary ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              <div className="glass-card" style={{ padding: 16 }}>
                <span className={`pill ${summary.risk_level === "high" ? "tone-bad" : summary.risk_level === "medium" ? "tone-warn" : "tone-ok"}`}>{summary.risk_level} risk</span>
                <h2 style={{ margin: "10px 0 0", fontSize: 18, fontWeight: 600 }}>{summary.headline}</h2>
                <div style={{ display: "grid", gap: 16, gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", marginTop: 12, fontSize: 13 }}>
                  <ul style={{ margin: 0, paddingLeft: 18, display: "flex", flexDirection: "column", gap: 4 }}>
                    {summary.key_points.map((p) => <li key={p}>{p}</li>)}
                  </ul>
                  <div>
                    <div className="muted" style={{ fontSize: 12, fontWeight: 600 }}>Before approving</div>
                    <ul style={{ margin: "4px 0 0", paddingLeft: 18, display: "flex", flexDirection: "column", gap: 4 }}>
                      {summary.recommended_actions.map((p) => <li key={p}>{p}</li>)}
                    </ul>
                  </div>
                </div>
                <p className="muted" style={{ margin: "12px 0 0", fontSize: 11 }}>{batch.ai_model} · {when(batch.ai_summarized_at)}</p>
              </div>
              <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 8 }}>
                {aiExplained.map(({ row, name, f }) => (
                  <li key={f.id} className="well" style={{ padding: "10px 12px", borderRadius: 14, fontSize: 13 }}>
                    <span className={`pill ${f.severity === "blocking" ? "tone-bad" : "tone-warn"}`} style={{ height: 22 }}>{f.code.replaceAll("_", " ").toLowerCase()}</span>{" "}
                    <strong>Row {row}</strong> <span className="muted">{name}</span>
                    <p style={{ margin: "6px 0 0" }}>{f.ai_explanation}</p>
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <div className="glass-card" style={{ padding: 24, textAlign: "center" }}>
              <p style={{ margin: 0, fontWeight: 600 }}>No AI review yet</p>
              <p className="muted" style={{ margin: "6px 0 14px", fontSize: 13 }}>The rules have already run. The AI explains each flag in plain words and summarises the batch.</p>
              <button
                className="btn btn-solid"
                disabled={busy !== null}
                onClick={() =>
                  run("explain", async () => {
                    await api(`/api/batches/${id}/explain`, "POST");
                    await load();
                    return "AI review ready.";
                  })
                }
              >
                <IconSparkle size={16} /> {busy === "explain" ? "Explaining… about a minute" : "Explain flags with AI"}
              </button>
            </div>
          )
        ) : null}
      </section>

      {openItem ? <RowDetail item={openItem} currency={ov.currency} editable={editable} onDecide={(rows, d) => decide(rows, d)} onClose={() => setOpenItem(null)} /> : null}
      <AgentDrawer
        open={agentOpen}
        onClose={() => setAgentOpen(false)}
        batchId={id}
        currency={ov.currency}
        canApply={editable}
        initialQuestion={agentQuestion}
        onApply={applyProposal}
      />
      {logOpen ? <SubmissionLog detail={detail} onClose={() => setLogOpen(false)} /> : null}
    </div>
  );
}

function SubmissionLog({ detail, onClose }: { detail: BatchDetail; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <>
      <div className="drawer-veil" onClick={onClose} aria-hidden="true" />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label="PayPal submission log">
        <div style={{ display: "flex", alignItems: "center", gap: 12, padding: 18 }}>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 18, fontWeight: 700 }}>PayPal submission log</div>
            <div className="muted" style={{ fontSize: 12 }}>
              Every attempt sends the same ID <span className="mono">{detail.batch.sender_batch_id}</span>, so a retry can never pay twice.
            </div>
          </div>
          <button type="button" className="icon-btn icon-btn-sm" aria-label="Close" onClick={onClose}>
            <IconClose size={18} />
          </button>
        </div>
        <div style={{ flex: 1, overflowY: "auto", padding: "0 18px 18px", display: "flex", flexDirection: "column", gap: 10 }}>
          {detail.submissions.length === 0 ? <p className="muted" style={{ margin: 0 }}>Not sent to PayPal yet.</p> : null}
          {detail.submissions.map((s) => (
            <div key={s.id} className="glass-card" style={{ padding: 14, display: "flex", flexDirection: "column", gap: 6, fontSize: 13 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <strong>Attempt {s.attempt_no}</strong>
                <span className={`pill ${s.outcome === "accepted" ? "tone-ok" : s.outcome === "rejected" ? "tone-bad" : "tone-warn"}`}>{s.outcome}</span>
              </div>
              <div className="muted">
                {when(s.started_at)}
                {s.http_status ? ` · HTTP ${s.http_status}` : ""}
              </div>
              {s.paypal_payout_batch_id ? <div>PayPal batch <span className="mono">{s.paypal_payout_batch_id}</span></div> : null}
              {s.paypal_debug_id ? <div className="muted">Debug ID <span className="mono">{s.paypal_debug_id}</span></div> : null}
              {s.error ? <div style={{ color: "var(--bad-ink)" }}>{s.error}</div> : null}
            </div>
          ))}
        </div>
      </aside>
    </>
  );
}

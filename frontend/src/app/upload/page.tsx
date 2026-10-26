"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { IconUpload } from "@/components/Icons";
import { ErrorNote, PageTitle } from "@/components/Status";
import { api, money } from "@/lib/api";
import { useUser } from "@/lib/useSession";

const FIELDS: { key: string; label: string; required?: boolean }[] = [
  { key: "receiver", label: "PayPal email", required: true },
  { key: "amount", label: "Amount", required: true },
  { key: "recipient_name", label: "Name" },
  { key: "invoice_id", label: "Invoice ID" },
  { key: "currency", label: "Currency" },
  { key: "note", label: "Note" },
  { key: "external_ref", label: "Contractor ID" },
  { key: "country_code", label: "Country" },
];

interface Preview {
  headers: string[];
  mapping: Record<string, string>;
  unmapped_columns: string[];
  notices: string[];
  row_count: number;
  problems: { row: number; code: string; message: string }[];
  sample: { row: number; receiver: string | null; name: string | null; amount: string | null; invoice: string | null }[];
}

const cell = { padding: "8px 12px", borderTop: "1px solid var(--track)" } as const;

export default function UploadPage() {
  useUser();
  const router = useRouter();
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"preview" | "import" | null>(null);
  const [dragging, setDragging] = useState(false);

  async function runPreview(chosen: File, withMapping = false) {
    setBusy("preview");
    setError(null);
    try {
      const f = new FormData();
      f.append("file", chosen);
      if (withMapping) f.append("mapping", JSON.stringify(mapping));
      const p = await api<Preview>("/api/batches/preview", "POST", { form: f });
      setPreview(p);
      setMapping(p.mapping);
    } catch (e) {
      setPreview(null);
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function runImport() {
    if (!file) return;
    setBusy("import");
    setError(null);
    try {
      const f = new FormData();
      f.append("file", file);
      f.append("mapping", JSON.stringify(mapping));
      if (name) f.append("name", name);
      const summary = await api<{ batch_id: string }>("/api/batches", "POST", { form: f });
      router.push(`/batches/${summary.batch_id}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(null);
    }
  }

  function choose(f: File | null) {
    setFile(f);
    setPreview(null);
    if (f) {
      setName(f.name.replace(/\.csv$/i, ""));
      void runPreview(f);
    }
  }

  return (
    <section className="glass-panel" style={{ padding: 18, display: "flex", flexDirection: "column", gap: 16 }}>
      <PageTitle
        title="Upload a payout CSV"
        sub="One row per payment: the contractor's PayPal email and amount, plus an invoice ID so repeats can be caught. Nothing is paid at this step."
      />

      <label
        className="glass-card"
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          choose(e.dataTransfer.files?.[0] ?? null);
        }}
        style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 10, padding: 36, cursor: "pointer", textAlign: "center", borderStyle: "dashed", outline: dragging ? "2px solid var(--accent)" : undefined }}
      >
        <input type="file" accept=".csv,text/csv" className="sr-only" onChange={(e) => choose(e.target.files?.[0] ?? null)} />
        <span style={{ width: 56, height: 56, borderRadius: 18, display: "flex", alignItems: "center", justifyContent: "center", background: "#2d74e6", color: "#fff", boxShadow: "0 8px 18px -8px rgba(30,90,200,.6)" }}>
          <IconUpload size={24} />
        </span>
        <span style={{ fontWeight: 600 }}>{file ? file.name : "Choose a CSV file or drop it here"}</span>
        <span className="muted" style={{ fontSize: 12 }}>Comma, semicolon or tab separated · up to 5 MB</span>
      </label>

      <ErrorNote error={error} />
      {busy === "preview" ? <p className="muted" style={{ margin: 0 }}>Reading the file…</p> : null}

      {preview ? (
        <>
          <div className="glass-card" style={{ padding: 16, display: "flex", flexDirection: "column", gap: 12 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
              <h2 className="card-title">Column mapping</h2>
              <span className="muted" style={{ fontSize: 12 }}>{preview.row_count} rows found</span>
            </div>
            <div style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))" }}>
              {FIELDS.map((f) => (
                <label key={f.key} style={{ display: "flex", flexDirection: "column", gap: 6, fontSize: 13 }}>
                  <span className="muted">
                    {f.label}
                    {f.required ? " *" : ""}
                  </span>
                  <select className="field well" style={{ paddingRight: 36 }} value={mapping[f.key] ?? ""} onChange={(e) => setMapping({ ...mapping, [f.key]: e.target.value })}>
                    <option value="">(not in this file)</option>
                    {preview.headers.map((h) => (
                      <option key={h} value={h}>
                        {h}
                      </option>
                    ))}
                  </select>
                </label>
              ))}
            </div>
            {preview.unmapped_columns.length ? <p className="muted" style={{ margin: 0, fontSize: 12 }}>Ignored columns: {preview.unmapped_columns.join(", ")}</p> : null}
            {preview.notices.map((n) => (
              <p key={n} className="alert tone-warn" style={{ margin: 0 }}>
                {n}
              </p>
            ))}
            <button className="btn" style={{ alignSelf: "flex-start" }} disabled={!file || busy !== null} onClick={() => file && runPreview(file, true)}>
              Re-check with this mapping
            </button>
          </div>

          <div className="glass-card" style={{ padding: 6, overflowX: "auto" }}>
            <h2 className="card-title" style={{ padding: "10px 12px 4px" }}>First rows</h2>
            <table style={{ width: "100%", borderCollapse: "separate", borderSpacing: 0, fontSize: 13 }}>
              <thead>
                <tr className="muted" style={{ textAlign: "left", fontSize: 11, textTransform: "uppercase", letterSpacing: ".04em" }}>
                  <th style={{ padding: "8px 12px" }}>Row</th>
                  <th style={{ padding: "8px 12px" }}>Email</th>
                  <th style={{ padding: "8px 12px" }}>Name</th>
                  <th style={{ padding: "8px 12px", textAlign: "right" }}>Amount</th>
                  <th style={{ padding: "8px 12px" }}>Invoice</th>
                </tr>
              </thead>
              <tbody>
                {preview.sample.map((r) => (
                  <tr key={r.row}>
                    <td className="muted" style={cell}>{r.row}</td>
                    <td style={cell}>{r.receiver ?? <span style={{ color: "var(--bad)" }}>missing</span>}</td>
                    <td style={cell}>{r.name ?? "—"}</td>
                    <td className="num" style={{ ...cell, textAlign: "right" }}>{r.amount ? money(r.amount) : <span style={{ color: "var(--bad)" }}>—</span>}</td>
                    <td style={cell}>{r.invoice ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {preview.problems.length ? (
            <div className="glass-card" style={{ padding: 16 }}>
              <h2 className="card-title">Problems found while reading</h2>
              <p className="muted" style={{ margin: "4px 0 10px", fontSize: 12 }}>These rows are kept and flagged; you decide on them in review.</p>
              <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, display: "flex", flexDirection: "column", gap: 4 }}>
                {preview.problems.map((p, i) => (
                  <li key={i}>
                    <span className="muted">Row {p.row}:</span> {p.message}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          <div className="glass-card" style={{ padding: 16, display: "flex", flexWrap: "wrap", alignItems: "flex-end", gap: 12 }}>
            <label style={{ flex: "1 1 260px", display: "flex", flexDirection: "column", gap: 6, fontSize: 13 }}>
              <span className="muted">Batch name</span>
              <input className="field well" value={name} onChange={(e) => setName(e.target.value)} maxLength={120} />
            </label>
            <button className="btn btn-solid" style={{ height: 44 }} disabled={!mapping.receiver || !mapping.amount || busy !== null} onClick={runImport}>
              {busy === "import" ? "Checking every row…" : `Import ${preview.row_count} rows for review`}
            </button>
          </div>
        </>
      ) : null}
    </section>
  );
}

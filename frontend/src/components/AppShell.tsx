"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { api, clearSession, getSession, SESSION_EVENT } from "@/lib/api";
import { dispatch, onBatchPage, ShellAction, stash } from "@/lib/bus";
import type { AuditRow, Overview } from "@/lib/types";
import { useSpeech } from "@/lib/useSpeech";
import { useTheme } from "@/lib/useTheme";
import Backdrop from "./Backdrop";
import BrandMark from "./BrandMark";
import {
  IconBell,
  IconChevronDown,
  IconClipboard,
  IconExternal,
  IconHome,
  IconMoon,
  IconPie,
  IconShield,
  IconSliders,
  IconSparkle,
  IconSun,
  IconUpload,
} from "./Icons";
import { describeAudit } from "./AuditTable";
import MicButton from "./MicButton";

function subscribe(callback: () => void) {
  window.addEventListener("storage", callback);
  window.addEventListener(SESSION_EVENT, callback);
  return () => {
    window.removeEventListener("storage", callback);
    window.removeEventListener(SESSION_EVENT, callback);
  };
}

const sessionSnapshot = () => JSON.stringify(getSession()?.user ?? null);
const SEEN_KEY = "paypap.seenAudit";

/** Run a shell action on the open batch, or open the most recent batch and run it there. */
function useBatchAction() {
  const pathname = usePathname();
  const router = useRouter();
  return useCallback(
    async (action: ShellAction): Promise<string | null> => {
      if (onBatchPage(pathname)) {
        dispatch(action);
        return null;
      }
      const batches = await api<Overview[]>("/api/batches");
      if (!batches.length) return "Upload a CSV first; the agent works on a batch.";
      stash(action);
      router.push(`/batches/${batches[0].batch_id}`);
      return null;
    },
    [pathname, router],
  );
}

export default function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const raw = useSyncExternalStore(subscribe, sessionSnapshot, () => "null");
  const user = JSON.parse(raw) as { display_name: string; role: string } | null;
  const [theme, setTheme] = useTheme();
  const runAction = useBatchAction();
  const [question, setQuestion] = useState("");
  const [shellNote, setShellNote] = useState<string | null>(null);

  const go = async (action: ShellAction) => {
    setShellNote(null);
    try {
      const note = await runAction(action);
      if (note) setShellNote(note);
    } catch (e) {
      setShellNote((e as Error).message);
    }
  };

  // Voice: words appear in the bar as they are heard, and the question is sent when the speaker stops.
  const speech = useSpeech((text) => {
    setQuestion("");
    void go({ type: "ask", question: text });
  });

  if (pathname.startsWith("/login")) {
    return (
      <>
        <Backdrop />
        <div style={{ position: "relative", zIndex: 1 }}>{children}</div>
      </>
    );
  }

  const nav = [
    { href: "/", label: "Batches", icon: <IconHome />, active: pathname === "/" || pathname.startsWith("/batches") },
    { href: "/upload", label: "Upload CSV", icon: <IconUpload />, active: pathname.startsWith("/upload") },
    { href: "/audit", label: "Audit log", icon: <IconClipboard />, active: pathname.startsWith("/audit") },
  ];

  return (
    <>
      <Backdrop />
      <div className="shell">
        <header
          className="glass-bar"
          style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 12, padding: "8px 10px 8px 8px", borderRadius: 32 }}
        >
          <Link href="/" style={{ display: "flex", alignItems: "center", gap: 10, flex: "0 0 auto", textDecoration: "none" }}>
            <BrandMark size={40} />
            <span style={{ display: "flex", flexDirection: "column", lineHeight: 1.25 }}>
              <span style={{ fontSize: 15, fontWeight: 600 }}>{user ? `Welcome, ${user.display_name}` : "PAY PAP"}</span>
              <span className="muted" style={{ fontSize: 12 }}>
                PAY PAP · {user?.role ?? "guest"} · sandbox
              </span>
            </span>
          </Link>

          <form
            className="well"
            style={{ flex: "1 1 300px", minWidth: 0, display: "flex", alignItems: "center", gap: 10, height: 48, padding: "0 6px", borderRadius: 9999 }}
            onSubmit={(e) => {
              e.preventDefault();
              if (!question.trim()) return;
              void go({ type: "ask", question: question.trim() });
              setQuestion("");
            }}
          >
            <span
              aria-hidden="true"
              style={{
                flex: "0 0 auto",
                width: 34,
                height: 34,
                borderRadius: 9999,
                background: "radial-gradient(circle at 32% 30%,#c4f4ff 0%,#6fa9ff 36%,#8b5cf6 70%,#2c2158 100%)",
                boxShadow: "0 0 14px rgba(120,140,255,.55), inset 0 0 0 1px rgba(255,255,255,.55)",
              }}
            />
            <label htmlFor="agent-q" className="sr-only">
              Ask the payout agent
            </label>
            <input
              id="agent-q"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder={
                speech.state === "listening"
                  ? "Listening… ask your question, then pause"
                  : speech.state === "transcribing"
                    ? "Turning your voice into text…"
                    : "Ask the payout agent — “What are the biggest risks in this batch?”"
              }
              style={{ flex: "1 1 auto", minWidth: 0, height: "100%", border: 0, background: "transparent", font: "inherit", fontSize: 13, color: "var(--ink)", outline: "none" }}
            />
            <MicButton supported={speech.supported} state={speech.state} level={speech.level} onStart={speech.start} onStop={speech.stop} />
            <button type="submit" className="btn btn-solid" disabled={!question.trim() || speech.state !== "idle"}>
              Ask
            </button>
          </form>

          <div role="group" aria-label="Theme" className="well" style={{ flex: "0 0 auto", display: "flex", gap: 2, padding: 3, borderRadius: 9999 }}>
            {(["light", "dark"] as const).map((t) => (
              <button
                key={t}
                type="button"
                aria-pressed={theme === t}
                onClick={() => setTheme(t)}
                className="btn"
                style={{
                  height: 38,
                  border: 0,
                  fontSize: 12,
                  fontWeight: theme === t ? 600 : 500,
                  background: theme === t ? "var(--chip-white)" : "transparent",
                  boxShadow: theme === t ? "0 1px 4px rgba(15,23,42,.2)" : "none",
                }}
              >
                {t === "light" ? <IconSun size={15} /> : <IconMoon size={15} />}
                {t === "light" ? "Light" : "Dark"}
              </button>
            ))}
          </div>

          <Notifications />

          <button
            type="button"
            className="btn"
            style={{ height: 44 }}
            onClick={() => {
              clearSession();
              router.push("/login");
            }}
          >
            Sign out
          </button>
        </header>

        {shellNote || speech.error ? (
          <div role="status" className="alert glass-bar" style={{ alignSelf: "center" }}>
            {speech.error ?? shellNote}
          </div>
        ) : null}

        <div className="main-row">
          <nav className="dock glass-bar" aria-label="Main">
            {nav.map((n) => (
              <Link key={n.href} href={n.href} aria-label={n.label} title={n.label} aria-current={n.active ? "page" : undefined} className="icon-btn">
                {n.icon}
              </Link>
            ))}
            <button type="button" className="icon-btn" aria-label="Ask the payout agent" title="Ask the payout agent" onClick={() => go({ type: "open-agent" })}>
              <IconSparkle />
            </button>
            <span className="dock-sep" aria-hidden="true" />
            <button type="button" className="icon-btn" aria-label="Reconciliation" title="Reconciliation" onClick={() => go({ type: "tab", tab: "reconcile" })}>
              <IconPie />
            </button>
            <Link href="/rules" aria-label="Rules and caps" title="Rules and caps" aria-current={pathname.startsWith("/rules") ? "page" : undefined} className="icon-btn">
              <IconSliders />
            </Link>
            <MoreMenu />
          </nav>
          <div style={{ flex: "1 1 auto", minWidth: 0 }}>{children}</div>
        </div>

        <p style={{ margin: "auto 0 0", textAlign: "center", fontSize: 12, lineHeight: 1.5 }} className="on-scene">
          The AI prepares and explains. Fixed rules and a human approval decide whether money moves. PayPal sandbox only.
        </p>
      </div>
    </>
  );
}

function usePopover() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent ? e.key === "Escape" : !ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [open]);
  return { open, setOpen, ref };
}

function Notifications() {
  const { open, setOpen, ref } = usePopover();
  const [events, setEvents] = useState<AuditRow[]>([]);
  const [seen, setSeen] = useState(() => {
    try {
      return Number(localStorage.getItem(SEEN_KEY) ?? 0);
    } catch {
      return 0;
    }
  });

  const load = useCallback(() => {
    if (!getSession()) return;
    api<AuditRow[]>("/api/audit?limit=12")
      .then(setEvents)
      .catch(() => setEvents([]));
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 60000);
    return () => clearInterval(timer);
  }, [load]);

  const unread = events.filter((e) => e.id > seen).length;

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next) {
      load();
      const top = events[0]?.id ?? seen;
      setSeen(top);
      try {
        localStorage.setItem(SEEN_KEY, String(top));
      } catch {
        // Unread count just resets on reload.
      }
    }
  }

  return (
    <div ref={ref} style={{ position: "relative", flex: "0 0 auto" }}>
      <button
        type="button"
        aria-label={`Notifications${unread ? `, ${unread} new` : ""}`}
        title="Notifications"
        className="icon-btn"
        style={{ border: "1px solid var(--card-border)", background: "var(--card-strong)" }}
        onClick={toggle}
        aria-expanded={open}
      >
        <IconBell size={19} />
        {unread ? (
          <span
            style={{ position: "absolute", top: 4, right: 4, minWidth: 16, height: 16, padding: "0 4px", borderRadius: 9999, background: "var(--bad)", color: "#fff", fontSize: 10, fontWeight: 700, display: "flex", alignItems: "center", justifyContent: "center" }}
          >
            {unread > 9 ? "9+" : unread}
          </span>
        ) : null}
      </button>
      {open ? (
        <div className="popover" style={{ right: 0, top: 52, width: 340 }}>
          <div style={{ padding: "6px 8px 8px", fontSize: 13, fontWeight: 600 }}>Latest activity</div>
          <ul style={{ listStyle: "none", margin: 0, padding: 0, maxHeight: 360, overflowY: "auto" }}>
            {events.map((e) => (
              <li key={e.id} style={{ padding: "8px", borderRadius: 12, fontSize: 12 }}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                  <span className="mono" style={{ fontSize: 11 }}>
                    {e.action}
                  </span>
                  <span className="muted" style={{ fontSize: 11, whiteSpace: "nowrap" }}>
                    {new Date(e.occurred_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                  </span>
                </div>
                <div className="muted" style={{ marginTop: 2 }}>
                  {describeAudit(e)}
                </div>
              </li>
            ))}
            {events.length === 0 ? <li className="muted" style={{ padding: 8, fontSize: 12 }}>Nothing yet.</li> : null}
          </ul>
          <Link href="/audit" className="btn btn-sm" style={{ width: "100%", marginTop: 6, textDecoration: "none" }} onClick={() => setOpen(false)}>
            Open the audit log
          </Link>
        </div>
      ) : null}
    </div>
  );
}

function MoreMenu() {
  const { open, setOpen, ref } = usePopover();
  const [note, setNote] = useState<string | null>(null);

  async function health() {
    try {
      const h = await api<{ ok: boolean; ai_provider: string; ai_enabled: boolean; missing_keys: string[] }>("/health");
      setNote(
        `API OK · AI ${h.ai_enabled ? `on (${h.ai_provider})` : "off"}` +
          (h.missing_keys.length ? ` · missing: ${h.missing_keys.join(", ")}` : " · all keys set"),
      );
    } catch (e) {
      setNote((e as Error).message);
    }
  }

  async function verify() {
    try {
      const r = await api<{ rows_checked: number; first_bad_id: number | null }>("/api/audit/verify");
      setNote(r.first_bad_id === null ? `Audit chain intact (${r.rows_checked} entries)` : `Audit chain BROKEN at entry ${r.first_bad_id}`);
    } catch (e) {
      setNote((e as Error).message);
    }
  }

  const item = { display: "flex", alignItems: "center", gap: 10, width: "100%", padding: "10px 12px", border: 0, borderRadius: 12, background: "transparent", color: "var(--ink)", font: "inherit", fontSize: 13, textAlign: "left" as const, cursor: "pointer", textDecoration: "none" };

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button type="button" className="icon-btn" aria-label="More" title="More" aria-expanded={open} onClick={() => { setOpen(!open); setNote(null); }}>
        <IconChevronDown size={18} />
      </button>
      {open ? (
        <div className="popover" style={{ left: 54, bottom: 0 }}>
          <button type="button" style={item} onClick={health}>
            <IconSparkle size={16} /> Check API and AI status
          </button>
          <button type="button" style={item} onClick={verify}>
            <IconShield size={16} /> Verify the audit hash chain
          </button>
          <a style={item} href="https://www.sandbox.paypal.com" target="_blank" rel="noreferrer">
            <IconExternal size={16} /> Open PayPal sandbox
          </a>
          <a style={item} href="https://developer.paypal.com/dashboard/" target="_blank" rel="noreferrer">
            <IconExternal size={16} /> PayPal developer dashboard
          </a>
          {note ? (
            <p role="status" className="muted" style={{ margin: "6px 12px 4px", fontSize: 12 }}>
              {note}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

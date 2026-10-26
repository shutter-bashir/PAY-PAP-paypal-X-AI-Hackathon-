import type { User } from "./types";

// Render's service linking supplies a bare host name, so add the scheme when it is missing.
const RAW_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";
const BASE = (RAW_BASE.includes("://") ? RAW_BASE : `https://${RAW_BASE}`).replace(/\/$/, "");
const TOKEN_KEY = "paypap.token";
const USER_KEY = "paypap.user";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export function getSession(): { token: string; user: User } | null {
  try {
    const token = localStorage.getItem(TOKEN_KEY);
    const user = localStorage.getItem(USER_KEY);
    return token && user ? { token, user: JSON.parse(user) as User } : null;
  } catch {
    return null;
  }
}

export const SESSION_EVENT = "paypap-session";

export function saveSession(token: string, user: User) {
  try {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(USER_KEY, JSON.stringify(user));
  } catch {
    // Storage blocked: the user will be asked to sign in again on reload.
  }
  window.dispatchEvent(new Event(SESSION_EVENT));
}

export function clearSession() {
  try {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
  } catch {
    // nothing stored
  }
  if (typeof window !== "undefined") window.dispatchEvent(new Event(SESSION_EVENT));
}

type Body = { json?: unknown; form?: FormData };

export async function api<T>(path: string, method = "GET", body: Body = {}): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getSession()?.token;
  if (token) headers.Authorization = `Bearer ${token}`;
  let payload: BodyInit | undefined;
  if (body.json !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body.json);
  } else if (body.form) {
    payload = body.form;
  }
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, { method, headers, body: payload });
  } catch {
    throw new ApiError(0, `Cannot reach the API at ${BASE}. Is the backend running?`);
  }
  const text = await response.text();
  const data = text ? safeJson(text) : null;
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/auth/login") {
      clearSession(); // useUser() sees the session end and sends the user to /login
    }
    throw new ApiError(response.status, errorText(data) ?? `Request failed (${response.status})`);
  }
  return data as T;
}

/** Save a file the API returns (it needs the sign-in token, so a plain link won't do). */
export async function download(path: string, filename: string): Promise<void> {
  const token = getSession()?.token;
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
  } catch {
    throw new ApiError(0, `Cannot reach the API at ${BASE}. Is the backend running?`);
  }
  if (!response.ok) {
    const text = await response.text();
    throw new ApiError(response.status, errorText(text ? safeJson(text) : null) ?? `Download failed (${response.status})`);
  }
  const url = URL.createObjectURL(await response.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function errorText(data: unknown): string | null {
  if (data && typeof data === "object" && "detail" in data) {
    const detail = (data as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map((d) => d?.msg ?? String(d)).join("; ");
  }
  return typeof data === "string" ? data : null;
}

export function money(value: number | string | null | undefined, currency = "USD"): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = typeof value === "string" ? Number(value) : value;
  return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(n);
}

export function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

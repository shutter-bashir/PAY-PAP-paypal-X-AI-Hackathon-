"use client";

// Lets the shell (header agent bar, dock) drive the batch page. If no batch page is
// open, the action is stashed and replayed when the most recent batch opens.

export type ShellAction =
  | { type: "ask"; question: string }
  | { type: "open-agent" }
  | { type: "tab"; tab: "review" | "reconcile" | "audit" | "ai" };

export const SHELL_EVENT = "paypap-shell";
const PENDING = "paypap.pending";

export function onBatchPage(pathname: string): boolean {
  return /^\/batches\/[^/]+$/.test(pathname);
}

export function dispatch(action: ShellAction) {
  window.dispatchEvent(new CustomEvent<ShellAction>(SHELL_EVENT, { detail: action }));
}

export function stash(action: ShellAction) {
  try {
    sessionStorage.setItem(PENDING, JSON.stringify(action));
  } catch {
    // Without storage the action is simply not replayed.
  }
}

export function takeStashed(): ShellAction | null {
  try {
    const raw = sessionStorage.getItem(PENDING);
    sessionStorage.removeItem(PENDING);
    return raw ? (JSON.parse(raw) as ShellAction) : null;
  } catch {
    return null;
  }
}

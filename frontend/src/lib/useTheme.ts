"use client";

import { useCallback, useSyncExternalStore } from "react";

export type Theme = "light" | "dark";
const KEY = "paypap.theme";
const EVENT = "paypap-theme";

function stored(): Theme | null {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : null;
  } catch {
    return null;
  }
}

function systemTheme(): Theme {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function currentTheme(): Theme {
  return stored() ?? systemTheme();
}

export function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
}

function subscribe(callback: () => void) {
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  window.addEventListener(EVENT, callback);
  media.addEventListener("change", callback);
  return () => {
    window.removeEventListener(EVENT, callback);
    media.removeEventListener("change", callback);
  };
}

/** Light/dark theme: the saved choice, else the device setting. */
export function useTheme(): [Theme, (t: Theme) => void] {
  const theme = useSyncExternalStore(subscribe, currentTheme, () => "light" as Theme);
  const setTheme = useCallback((t: Theme) => {
    try {
      localStorage.setItem(KEY, t);
    } catch {
      // Not saved; it still applies for this visit.
    }
    applyTheme(t);
    window.dispatchEvent(new Event(EVENT));
  }, []);
  return [theme, setTheme];
}

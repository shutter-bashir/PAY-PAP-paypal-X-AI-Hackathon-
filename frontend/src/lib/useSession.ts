"use client";

import { useRouter } from "next/navigation";
import { useEffect, useMemo, useSyncExternalStore } from "react";
import { getSession, SESSION_EVENT } from "./api";
import type { User } from "./types";

function subscribe(callback: () => void) {
  window.addEventListener(SESSION_EVENT, callback);
  window.addEventListener("storage", callback);
  return () => {
    window.removeEventListener(SESSION_EVENT, callback);
    window.removeEventListener("storage", callback);
  };
}

const snapshot = () => JSON.stringify(getSession()?.user ?? null);

/** The signed-in user, or null while signed out (redirects to /login). */
export function useUser(): User | null {
  const router = useRouter();
  const raw = useSyncExternalStore(subscribe, snapshot, () => "null");
  const user = useMemo(() => JSON.parse(raw) as User | null, [raw]);
  useEffect(() => {
    if (!getSession()) router.replace("/login");
  }, [raw, router]);
  return user;
}

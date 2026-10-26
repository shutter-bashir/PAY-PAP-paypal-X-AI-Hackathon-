"use client";

import { themeQuartz } from "ag-grid-community";
import { useTheme } from "./useTheme";

const shared = {
  fontFamily: "var(--font-ios)",
  fontSize: 13,
  headerFontWeight: 600,
  spacing: 7,
  wrapperBorderRadius: 18,
  borderRadius: 10,
  backgroundColor: "transparent",
  headerBackgroundColor: "transparent",
  oddRowBackgroundColor: "transparent",
};

const light = themeQuartz.withParams({
  ...shared,
  foregroundColor: "#0f172a",
  headerTextColor: "#334155",
  borderColor: "rgba(15, 23, 42, 0.1)",
  rowHoverColor: "rgba(45, 116, 230, 0.07)",
  selectedRowBackgroundColor: "rgba(16, 185, 129, 0.14)",
  accentColor: "#10b981",
  chromeBackgroundColor: "rgba(255, 255, 255, 0.6)",
});

const dark = themeQuartz.withParams({
  ...shared,
  foregroundColor: "#eef2ff",
  headerTextColor: "#cbd5e1",
  borderColor: "rgba(147, 175, 255, 0.12)",
  rowHoverColor: "rgba(255, 255, 255, 0.06)",
  selectedRowBackgroundColor: "rgba(16, 185, 129, 0.2)",
  accentColor: "#10b981",
  chromeBackgroundColor: "rgba(14, 24, 62, 0.6)",
  browserColorScheme: "dark",
});

/** Glass AG Grid theme that follows PAY PAP's light/dark switch. */
export function useGridTheme() {
  const [theme] = useTheme();
  return theme === "dark" ? dark : light;
}

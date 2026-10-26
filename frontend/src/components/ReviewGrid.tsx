"use client";

import type { ColDef, GridApi, ICellRendererParams, IRowNode, RowClassRules } from "ag-grid-community";
import { AgGridReact } from "ag-grid-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { money } from "@/lib/api";
import { registerGrid } from "@/lib/grid";
import type { Decision, Item } from "@/lib/types";
import { useGridTheme } from "@/lib/useGridTheme";
import { IconCheck, IconClose } from "./Icons";

registerGrid();

interface Props {
  items: Item[];
  currency: string;
  editable: boolean;
  onlyRows: Set<number> | null; // the active quick or natural-language filter
  quickText: string;
  onDecide: (rows: number[], decision: Exclude<Decision, "pending">) => void;
  onOpen: (item: Item) => void;
  mode: "review" | "reconcile";
}

const DECISION_TONE: Record<Decision, string> = { include: "tone-ok", exclude: "tone-muted", pending: "tone-warn" };
const DECISION_LABEL: Record<Decision, string> = { include: "Pay", exclude: "Left out", pending: "Decide" };
const PAYOUT_TONE: Record<string, string> = {
  SUCCESS: "tone-ok",
  PENDING: "tone-info",
  UNCLAIMED: "tone-warn",
  ONHOLD: "tone-warn",
  FAILED: "tone-bad",
  RETURNED: "tone-bad",
  BLOCKED: "tone-bad",
  REFUNDED: "tone-bad",
  REVERSED: "tone-bad",
};
const SEVERITY_TONE = { blocking: "tone-bad", warning: "tone-warn", info: "tone-info" } as const;

export default function ReviewGrid({ items, currency, editable, onlyRows, quickText, onDecide, onOpen, mode }: Props) {
  const theme = useGridTheme();
  const apiRef = useRef<GridApi<Item> | null>(null);
  const [selected, setSelected] = useState<number[]>([]);

  const columns = useMemo<ColDef<Item>[]>(() => {
    const decisionCell = (p: ICellRendererParams<Item>) => {
      const item = p.data;
      if (!item) return null;
      const blocked = item.top_severity === "blocking";
      return (
        <div style={{ display: "flex", height: "100%", alignItems: "center", gap: 4 }}>
          <span className={`pill ${DECISION_TONE[item.decision]}`} style={{ height: 22, minWidth: 64, justifyContent: "center" }}>
            {DECISION_LABEL[item.decision]}
          </span>
          {editable ? (
            <>
              <button
                className="icon-btn icon-btn-sm"
                style={{ width: 28, height: 28 }}
                disabled={blocked || item.decision === "include"}
                title={blocked ? "Blocked by a rule; it cannot be paid" : "Pay this row"}
                aria-label={`Pay row ${item.row_number}`}
                onClick={(e) => {
                  e.stopPropagation();
                  onDecide([item.row_number], "include");
                }}
              >
                <IconCheck size={15} />
              </button>
              <button
                className="icon-btn icon-btn-sm"
                style={{ width: 28, height: 28 }}
                disabled={item.decision === "exclude"}
                title="Leave this row out"
                aria-label={`Leave out row ${item.row_number}`}
                onClick={(e) => {
                  e.stopPropagation();
                  onDecide([item.row_number], "exclude");
                }}
              >
                <IconClose size={15} />
              </button>
            </>
          ) : null}
        </div>
      );
    };
    const flagsCell = (p: ICellRendererParams<Item>) => (
      <div style={{ display: "flex", height: "100%", alignItems: "center", gap: 4, overflow: "hidden" }}>
        {(p.data?.flags ?? []).map((f) => (
          <span key={f.id} className={`pill ${SEVERITY_TONE[f.severity]}`} style={{ height: 22 }} title={f.ai_explanation ?? f.message}>
            {f.code.replaceAll("_", " ").toLowerCase()}
          </span>
        ))}
      </div>
    );
    const payoutCell = (p: ICellRendererParams<Item>) =>
      p.value ? (
        <span className={`pill ${PAYOUT_TONE[p.value as string] ?? "tone-muted"}`} style={{ height: 22 }}>
          {String(p.value)}
        </span>
      ) : null;

    const base: ColDef<Item>[] = [
      { field: "row_number", headerName: "Row", width: 80, pinned: "left", sort: "asc" },
      { headerName: "Decision", width: 170, cellRenderer: decisionCell, valueGetter: (p) => p.data?.decision, pinned: "left" },
      { field: "recipient_name", headerName: "Name", flex: 1, minWidth: 150 },
      { field: "receiver", headerName: "PayPal email", flex: 2, minWidth: 220, valueFormatter: (p) => p.value ?? "(missing)" },
      {
        field: "amount",
        headerName: "Amount",
        width: 120,
        type: "rightAligned",
        valueFormatter: (p) => (p.value === null || p.value === undefined ? "—" : money(p.value, currency)),
      },
      { field: "invoice_id", headerName: "Invoice", width: 150 },
    ];
    if (mode === "review") {
      base.push(
        { field: "recipient_vetting_status", headerName: "Vetting", width: 110, valueFormatter: (p) => p.value ?? "new" },
        { headerName: "Flags", flex: 2, minWidth: 220, cellRenderer: flagsCell, valueGetter: (p) => p.data?.flag_codes.join(" ") },
      );
    } else {
      base.push(
        { field: "payout_status", headerName: "PayPal status", width: 140, cellRenderer: payoutCell },
        { field: "reconciliation_bucket", headerName: "Report", width: 120 },
        { field: "paypal_transaction_id", headerName: "Transaction", width: 170 },
      );
    }
    return base;
  }, [currency, editable, mode, onDecide]);

  const rowClassRules = useMemo<RowClassRules<Item>>(
    () => ({
      "row-blocking": (p) => p.data?.top_severity === "blocking",
      "row-warning": (p) => p.data?.top_severity === "warning" && p.data.decision === "pending",
    }),
    [],
  );

  const isExternalFilterPresent = useCallback(() => onlyRows !== null, [onlyRows]);
  const doesExternalFilterPass = useCallback(
    (node: IRowNode<Item>) => (onlyRows ? onlyRows.has(node.data?.row_number ?? -1) : true),
    [onlyRows],
  );

  useEffect(() => {
    apiRef.current?.onFilterChanged();
  }, [onlyRows]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {editable && mode === "review" ? (
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8, fontSize: 13 }}>
          <span className="muted">{selected.length ? `${selected.length} selected` : "Tick rows to decide in bulk"}</span>
          <button className="btn btn-sm" disabled={!selected.length} onClick={() => onDecide(selected, "include")}>
            Pay selected
          </button>
          <button className="btn btn-sm" disabled={!selected.length} onClick={() => onDecide(selected, "exclude")}>
            Leave out selected
          </button>
        </div>
      ) : null}
      <div style={{ height: 600 }}>
        <AgGridReact<Item>
          theme={theme}
          rowData={items}
          columnDefs={columns}
          getRowId={(p) => p.data.id}
          rowClassRules={rowClassRules}
          quickFilterText={quickText}
          isExternalFilterPresent={isExternalFilterPresent}
          doesExternalFilterPass={doesExternalFilterPass}
          defaultColDef={{ sortable: true, filter: true, resizable: true }}
          rowSelection={
            editable && mode === "review"
              ? { mode: "multiRow", checkboxes: true, headerCheckbox: true, enableClickSelection: false, isRowSelectable: (n) => n.data?.top_severity !== "blocking" }
              : undefined
          }
          onGridReady={(e) => {
            apiRef.current = e.api;
          }}
          onSelectionChanged={(e) => setSelected(e.api.getSelectedRows().map((r) => r.row_number))}
          onRowClicked={(e) => e.data && onOpen(e.data)}
        />
      </div>
    </div>
  );
}

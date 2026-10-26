export type Severity = "blocking" | "warning" | "info";
export type Decision = "pending" | "include" | "exclude";
export type BatchStatus =
  | "draft"
  | "in_review"
  | "approved"
  | "submitting"
  | "submitted"
  | "processing"
  | "completed"
  | "rejected"
  | "cancelled";

export interface User {
  id: string;
  email: string;
  role: "admin" | "approver";
  display_name: string;
}

export interface Flag {
  id: string;
  code: string;
  severity: Severity;
  message: string;
  evidence: Record<string, unknown>;
  ai_explanation: string | null;
}

export interface Item {
  id: string;
  batch_id: string;
  row_number: number;
  receiver: string | null;
  recipient_name: string | null;
  amount: number | null;
  currency: string | null;
  invoice_id: string | null;
  note: string | null;
  decision: Decision;
  decision_reason: string | null;
  decided_by: string | null;
  committed_at: string | null;
  payout_status: string | null;
  paypal_transaction_id: string | null;
  payout_error: unknown;
  batch_status: BatchStatus;
  recipient_vetting_status: "approved" | "pending" | "blocked" | null;
  flag_codes: string[];
  top_severity: Severity | null;
  reconciliation_bucket: "paid" | "pending" | "failed" | "flagged" | "excluded" | null;
  flags: Flag[];
}

export interface Overview {
  batch_id: string;
  batch_no: number;
  name: string;
  status: BatchStatus;
  currency: string;
  sender_batch_id: string;
  paypal_payout_batch_id: string | null;
  paypal_batch_status: string | null;
  created_at: string;
  submitted_at: string | null;
  completed_at: string | null;
  row_count: number;
  included_count: number;
  included_total: number;
  excluded_count: number;
  undecided_count: number;
  blocking_rows: number;
  warning_rows: number;
  paid_count: number;
  paid_total: number;
  payout_pending_count: number;
  payout_pending_total: number;
  failed_count: number;
  failed_total: number;
  flagged_count: number;
}

export interface AiSummary {
  headline: string;
  risk_level: "low" | "medium" | "high";
  key_points: string[];
  recommended_actions: string[];
}

export interface Approval {
  id: string;
  approved_by: string;
  item_count: number;
  total_amount: number;
  currency: string;
  excluded_count: number;
  warned_count: number;
  comment: string | null;
  expires_at: string;
  created_at: string;
  consumed_at: string | null;
  approved_by_name: string | null;
  signature_valid: boolean;
}

export interface Submission {
  id: string;
  attempt_no: number;
  outcome: "in_flight" | "accepted" | "rejected" | "unknown";
  http_status: number | null;
  paypal_debug_id: string | null;
  paypal_payout_batch_id: string | null;
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface BatchDetail {
  batch: {
    id: string;
    batch_no: number;
    name: string;
    status: BatchStatus;
    currency: string;
    sender_batch_id: string;
    source_filename: string | null;
    column_mapping: Record<string, string> | null;
    import_errors: { row: number | null; column: string | null; code: string; message: string }[];
    rules_version: string | null;
    ai_summary: AiSummary | null;
    ai_model: string | null;
    ai_summarized_at: string | null;
    paypal_payout_batch_id: string | null;
    paypal_batch_status: string | null;
    created_at: string;
    created_by: string;
  };
  overview: Overview;
  approval: Approval | null;
  submissions: Submission[];
  batch_flags: { id: string; code: string; severity: Severity; message: string; ai_explanation: string | null }[];
}

export interface FilterSpec {
  decisions: Decision[];
  recipient_vetting: string[];
  min_amount: number | null;
  max_amount: number | null;
  flag_codes: string[];
  severities: string[];
  receiver_contains: string | null;
  name_contains: string | null;
  invoice_contains: string | null;
  payout_buckets: string[];
  row_numbers: number[];
  explanation: string;
}

export interface Proposal {
  include_rows: number[];
  exclude_rows: number[];
  rationale: string;
  total: string;
  row_count: number;
  blocked_rows_dropped: number[];
  limit_problems: string[];
  still_undecided: number[];
}

export interface AgentReply {
  text: string;
  tool_calls: { tool: string; input: Record<string, unknown>; error: boolean }[];
  proposal: Proposal | null;
}

export interface AuditRow {
  id: number;
  occurred_at: string;
  actor_type: "user" | "system" | "agent" | "paypal";
  actor_id: string | null;
  actor_email: string | null;
  action: string;
  batch_id: string | null;
  entity_type: string;
  entity_id: string | null;
  details: Record<string, unknown>;
}

export interface Meta {
  rules_version: string;
  limits: {
    max_amount_per_recipient: string;
    max_batch_total: string;
    max_batch_items: number;
    suspicious_amount_zscore: number;
    min_amount: string;
    approval_ttl_minutes: number;
  };
  currency: string;
  paypal_mode: string;
  webhook_configured: boolean;
  ai: { provider: string; model: string; enabled: boolean };
  poll_interval_seconds: number;
}

export interface Preflight {
  total: string;
  currency: string;
  turned_away: { row: number; receiver: string; code: string; reason: string }[];
  balance: string | null;
  balance_error: string | null;
  short_by: string | null;
  balance_blocks: boolean;
}

"""Database access through Supabase (PostgREST). Every write that moves a batch
forward goes through a Postgres function, so it runs as one transaction; see
backend/db/migrations/0001_initial_schema.sql.
"""
from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime

import httpx
from postgrest.exceptions import APIError

PAGE = 1000  # PostgREST's default max rows per request on Supabase


class StoreError(Exception):
    """The database refused an operation. The message is the database's reason."""


class PerThreadClient:
    """One Supabase client per thread. The client's HTTP/2 connection cannot be shared by
    concurrent requests (it fails with httpx.LocalProtocolError), and FastAPI serves
    requests from a thread pool."""

    def __init__(self, factory):
        self._factory = factory
        self._local = threading.local()

    def _client(self):
        client = getattr(self._local, "client", None)
        if client is None:
            client = self._local.client = self._factory()
        return client

    def table(self, name: str):
        return self._client().table(name)

    def rpc(self, fn: str, params: Mapping):
        return self._client().rpc(fn, params)


class Store:
    def __init__(self, client, url: str | None = None):
        """`client` is a supabase Client or a postgrest SyncPostgrestClient (both have .table and .rpc)."""
        self._db = client
        self._url = url

    @classmethod
    def from_settings(cls, settings) -> Store:
        from supabase import create_client

        key = settings.supabase_service_role_key.get_secret_value()
        if not settings.supabase_url or not key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in backend/.env.")
        return cls(PerThreadClient(lambda: create_client(settings.supabase_url, key)), settings.supabase_url.rstrip("/"))

    def server_now(self) -> datetime:
        """The database host's clock, from its HTTP Date header, for times the database itself checks
        (approval expiry). This machine's clock can be wrong; it is the fallback."""
        if self._url:
            try:
                date = httpx.head(f"{self._url}/rest/v1/", timeout=5).headers.get("date")
                if date:
                    return parsedate_to_datetime(date).astimezone(UTC)
            except (httpx.HTTPError, TypeError, ValueError):
                pass
        return datetime.now(UTC)

    # --- Reads -------------------------------------------------------------

    def user_by_email(self, email: str) -> dict | None:
        return self._one(self._db.table("app_users").select("*").eq("email", email.lower()))

    def user(self, user_id: str) -> dict | None:
        return self._one(self._db.table("app_users").select("*").eq("id", user_id))

    def get_batch(self, batch_id: str) -> dict:
        batch = self._one(self._db.table("batches").select("*").eq("id", batch_id))
        if batch is None:
            raise StoreError(f"batch {batch_id} not found")
        return batch

    def find_batch(self, ref: str) -> dict:
        """By batch number ('12' or '#12') or by id."""
        number = ref.lstrip("#")
        query = self._db.table("batches").select("*")
        query = query.eq("batch_no", int(number)) if number.isdigit() else query.eq("id", ref)
        batch = self._one(query)
        if batch is None:
            raise StoreError(f"batch {ref} not found")
        return batch

    def list_batches(self, limit: int = 50) -> list[dict]:
        return self._run(self._db.table("batch_overview").select("*").order("batch_no", desc=True).limit(limit))

    def batch_overview(self, batch_id: str) -> dict:
        overview = self._one(self._db.table("batch_overview").select("*").eq("batch_id", batch_id))
        if overview is None:
            raise StoreError(f"batch {batch_id} not found")
        return overview

    def items(self, batch_id: str) -> list[dict]:
        return self._all(lambda: self._db.table("batch_item_overview").select("*").eq("batch_id", batch_id)
                         .order("row_number"))

    def flags(self, batch_id: str) -> list[dict]:
        return self._all(lambda: self._db.table("flags").select("*").eq("batch_id", batch_id).order("created_at"))

    def included_items(self, batch_id: str) -> list[dict]:
        return self._all(lambda: self._db.table("batch_items").select("id,row_number,receiver,amount,currency")
                         .eq("batch_id", batch_id).eq("decision", "include").order("row_number"))

    def raw_rows(self, batch_id: str, item_ids: Sequence[str]) -> list[dict]:
        """The original CSV rows, as uploaded, for the given items."""
        rows: list[dict] = []
        for start in range(0, len(item_ids), 100):
            rows += self._run(self._db.table("batch_items").select("id,row_number,raw").eq("batch_id", batch_id)
                              .in_("id", list(item_ids[start:start + 100])).order("row_number"))
        return rows

    def payout_history(self) -> list[dict]:
        """Every row PayPal has reported on, across all batches: what it did with each receiver."""
        return self._all(lambda: self._db.table("batch_items")
                         .select("receiver,payout_status,payout_error,payout_status_at")
                         .not_.is_("payout_status", "null").order("payout_status_at"))

    def committed_items(self, batch_id: str) -> list[dict]:
        return self._all(lambda: self._db.table("batch_items")
                         .select("id,row_number,receiver_type,receiver,amount,currency,invoice_id,note")
                         .eq("batch_id", batch_id).not_.is_("committed_at", "null").order("row_number"))

    def live_approval(self, batch_id: str) -> dict | None:
        return self._one(self._db.table("approvals").select("*").eq("batch_id", batch_id).is_("revoked_at", "null"))

    def submissions(self, batch_id: str) -> list[dict]:
        return self._run(self._db.table("payout_submissions")
                         .select("id,attempt_no,outcome,http_status,paypal_debug_id,paypal_payout_batch_id,error,started_at,finished_at")
                         .eq("batch_id", batch_id).order("attempt_no"))

    def audit(self, *, batch_id: str | None = None, limit: int = 200, before_id: int | None = None) -> list[dict]:
        query = self._db.table("audit_log").select("id,occurred_at,actor_type,actor_id,action,batch_id,entity_type,entity_id,details")
        if batch_id:
            query = query.eq("batch_id", batch_id)
        if before_id:
            query = query.lt("id", before_id)
        return self._run(query.order("id", desc=True).limit(limit))

    def verify_audit_chain(self) -> dict:
        data = self._rpc("verify_audit_chain", {})
        return data[0] if isinstance(data, list) else data

    def batches_to_poll(self) -> list[dict]:
        return self._run(self._db.table("batches").select("id,batch_no,paypal_payout_batch_id,submitted_at,last_polled_at")
                         .in_("status", ["submitted", "processing"]).not_.is_("paypal_payout_batch_id", "null"))

    def approved_batches(self) -> list[dict]:
        return self._run(self._db.table("batches").select("id,batch_no").eq("status", "approved"))

    def batch_by_payout_id(self, payout_batch_id: str) -> dict | None:
        return self._one(self._db.table("batches").select("*").eq("paypal_payout_batch_id", payout_batch_id))

    def batch_by_sender_id(self, sender_batch_id: str) -> dict | None:
        return self._one(self._db.table("batches").select("*").eq("sender_batch_id", sender_batch_id))

    def import_context(self, receivers: Sequence[str], receiver_type: str = "EMAIL") -> dict:
        return self._rpc("import_context", {"p_receiver_type": receiver_type, "p_receivers": list(receivers)})

    # --- Writes ------------------------------------------------------------

    def import_batch(self, *, created_by: str, batch: Mapping, recipients: list, items: list, flags: list) -> str:
        return self._rpc("import_batch", {"p_created_by": created_by, "p_batch": batch, "p_recipients": recipients,
                                          "p_items": items, "p_flags": flags})

    def set_item_decisions(self, batch_id: str, decided_by: str, decisions: list[Mapping]) -> int:
        return self._rpc("set_item_decisions", {"p_batch_id": batch_id, "p_decided_by": decided_by,
                                                "p_decisions": decisions})

    def update_batch(self, batch_id: str, fields: Mapping) -> None:
        self._run(self._db.table("batches").update(_jsonable(fields)).eq("id", batch_id))

    def set_flag_explanations(self, explanations: Sequence[Mapping]) -> None:
        for e in explanations:
            self._run(self._db.table("flags").update(_jsonable({
                "ai_explanation": e["explanation"], "ai_model": e["model"], "ai_explained_at": e["at"],
            })).eq("id", e["flag_id"]))

    def approve_batch(self, *, approval_id: str, batch_id: str, approver_id: str, item_count: int,
                      total_amount: Decimal, expires_at: datetime, signature: str, comment: str | None) -> dict:
        return self._rpc("approve_batch", {
            "p_approval_id": approval_id, "p_batch_id": batch_id, "p_approver_id": approver_id,
            "p_item_count": item_count, "p_total_amount": str(total_amount), "p_expires_at": expires_at.isoformat(),
            "p_signature": signature, "p_comment": comment})

    def revoke_approval(self, approval_id: str, reason: str, revoked_by: str | None = None) -> dict:
        return self._rpc("revoke_approval", {"p_approval_id": approval_id, "p_reason": reason, "p_revoked_by": revoked_by})

    def claim_for_submit(self, batch_id: str, approval_id: str, submitted_by: str | None) -> dict:
        return self._rpc("claim_batch_for_submit", {"p_batch_id": batch_id, "p_approval_id": approval_id,
                                                    "p_submitted_by": submitted_by})

    def record_submission_result(self, submission_id: str, *, outcome: str, http_status: int | None = None,
                                 paypal_debug_id: str | None = None, paypal_payout_batch_id: str | None = None,
                                 paypal_batch_status: str | None = None, request_body: object = None,
                                 response_body: object = None, error: str | None = None) -> dict:
        return self._rpc("record_submission_result", {
            "p_submission_id": submission_id, "p_outcome": outcome, "p_http_status": http_status,
            "p_paypal_debug_id": paypal_debug_id, "p_paypal_payout_batch_id": paypal_payout_batch_id,
            "p_paypal_batch_status": paypal_batch_status, "p_request_body": request_body,
            "p_response_body": response_body, "p_error": error})

    def apply_item_statuses(self, updates: Sequence[Mapping], source: str) -> int:
        return self._rpc("apply_item_statuses", {"p_updates": _jsonable(updates), "p_source": source})

    def apply_batch_status(self, payout_batch_id: str, status: str) -> dict | None:
        data = self._rpc("apply_batch_status", {"p_paypal_payout_batch_id": payout_batch_id, "p_paypal_status": status})
        return data if isinstance(data, dict) and data.get("id") else None

    def record_webhook_event(self, event: Mapping, *, payout_batch_id: str | None, sender_item_id: str | None) -> bool:
        """Stores a verified event. Returns False if this event id was already stored (a redelivery)."""
        rows = self._run(self._db.table("webhook_events").upsert({
            "paypal_event_id": event["id"],
            "event_type": event.get("event_type", ""),
            "resource_type": event.get("resource_type"),
            "payout_batch_id": payout_batch_id,
            "sender_item_id": sender_item_id,
            "event_created_at": event.get("create_time"),
            "payload": event,
        }, on_conflict="paypal_event_id", ignore_duplicates=True))
        return bool(rows)

    def finish_webhook_event(self, paypal_event_id: str, error: str | None = None) -> None:
        self._run(self._db.table("webhook_events")
                  .update({"processed_at": datetime.now(UTC).isoformat(), "processing_error": error})
                  .eq("paypal_event_id", paypal_event_id))

    def write_audit(self, *, action: str, entity_type: str, entity_id: str | None, actor_type: str = "system",
                    actor_id: str | None = None, batch_id: str | None = None, details: Mapping | None = None) -> int:
        return self._rpc("write_audit", {"p_actor_type": actor_type, "p_actor_id": actor_id, "p_action": action,
                                         "p_entity_type": entity_type, "p_entity_id": entity_id,
                                         "p_batch_id": batch_id, "p_details": _jsonable(details or {})})

    def upsert_recipients(self, recipients: list[Mapping]) -> None:
        """Adds or updates directory entries. A known contractor ID whose email changed is
        updated in place (same person, new PayPal email) instead of clashing on the ID."""
        refs = [r["external_ref"] for r in recipients if r.get("external_ref")]
        known = {}
        for start in range(0, len(refs), 200):
            for row in self._run(self._db.table("recipients").select("external_ref,receiver_type,receiver")
                                 .in_("external_ref", refs[start:start + 200])):
                known[row["external_ref"]] = (row["receiver_type"], row["receiver"])
        moved = [r for r in recipients if r.get("external_ref") in known
                 and known[r["external_ref"]] != (r.get("receiver_type", "EMAIL"), r["receiver"])]
        for r in moved:
            self._run(self._db.table("recipients").update(dict(r)).eq("external_ref", r["external_ref"]))
        rest = [r for r in recipients if r not in moved]
        for start in range(0, len(rest), 500):
            self._run(self._db.table("recipients").upsert(rest[start:start + 500], on_conflict="receiver_type,receiver"))

    # --- Plumbing ----------------------------------------------------------

    def _rpc(self, fn: str, params: Mapping):
        return self._run(self._db.rpc(fn, _jsonable(params)))

    @staticmethod
    def _run(query):
        try:
            return query.execute().data
        except APIError as exc:
            raise StoreError(exc.message or str(exc)) from exc

    def _one(self, query) -> dict | None:
        rows = self._run(query.limit(1))
        return rows[0] if rows else None

    def _all(self, make_query) -> list[dict]:
        rows: list[dict] = []
        while True:
            page = self._run(make_query().range(len(rows), len(rows) + PAGE - 1))
            rows.extend(page)
            if len(page) < PAGE:
                return rows


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value

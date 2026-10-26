"""Submitting approved batches to PayPal Payouts and tracking what happens to them.

Why a retry can never pay twice:
1. batches.sender_batch_id is fixed when the batch is created; every attempt sends
   the same value, and PayPal refuses a second batch with the same id (checked in
   the sandbox: 400 USER_BUSINESS_ERROR, "Batch with given sender_batch_id already
   exists", with a link to the existing batch).
2. That refusal is read as "an earlier attempt got through": the existing
   payout_batch_id is taken from the link and the attempt is recorded as accepted.
3. claim_batch_for_submit() allows one attempt in flight at a time, and only
   for the rows locked by a valid approval.
4. If an attempt's result is unclear (timeout, 5xx, or any other error on a
   retry), it is recorded as 'unknown', the batch stays locked, and the only way
   forward is another attempt with the same sender_batch_id.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from app import approvals, rules
from app.paypal_balances import BalanceReader
from app.paypal_client import PayPalClient, PayPalError, debug_id, safe_json
from app.store import Store, StoreError

log = logging.getLogger(__name__)

PAGE_SIZE = 1000
DEFAULT_EMAIL_SUBJECT = "You have a payment"


class SubmitRefused(Exception):
    """The batch was not sent to PayPal. The message says why."""


@dataclass(frozen=True)
class CreateOutcome:
    outcome: str  # accepted | rejected | unknown
    http_status: int | None = None
    payout_batch_id: str | None = None
    batch_status: str | None = None
    debug_id: str | None = None
    error: str | None = None
    response_body: object = None


@dataclass(frozen=True)
class SubmitResult:
    batch_id: str
    attempt_no: int
    outcome: str
    batch_status: str
    payout_batch_id: str | None
    http_status: int | None
    error: str | None


@dataclass(frozen=True)
class SyncResult:
    batch_id: str
    paypal_batch_status: str | None
    batch_status: str
    items_seen: int
    items_changed: int


# --- Request and response mapping --------------------------------------------


def build_payout_request(batch: Mapping, items: Sequence[Mapping]) -> dict:
    header = {
        "sender_batch_id": batch["sender_batch_id"],
        "recipient_type": "EMAIL",
        "email_subject": batch.get("email_subject") or DEFAULT_EMAIL_SUBJECT,
    }
    if batch.get("email_message"):
        header["email_message"] = batch["email_message"]
    body_items = []
    for it in items:
        item = {
            "recipient_type": it.get("receiver_type") or "EMAIL",
            "receiver": it["receiver"],
            "amount": {"value": f"{Decimal(str(it['amount'])):.2f}", "currency": it["currency"]},
            "sender_item_id": str(it["id"]),
        }
        note = it.get("note") or (f"Payment for invoice {it['invoice_id']}" if it.get("invoice_id") else None)
        if note:
            item["note"] = note[:4000]
        body_items.append(item)
    return {"sender_batch_header": header, "items": body_items}


def classify_create_response(status: int, body: object, *, is_retry: bool) -> CreateOutcome:
    """What a POST /v1/payments/payouts response means for the batch."""
    if status in (200, 201) and isinstance(body, dict) and body.get("batch_header", {}).get("payout_batch_id"):
        header = body["batch_header"]
        return CreateOutcome("accepted", status, header["payout_batch_id"], header.get("batch_status"),
                             response_body=body)
    error = _error_text(status, body)
    existing = existing_batch_id(body)
    if existing:
        return CreateOutcome("accepted", status, existing, None,
                             error=f"an earlier attempt already created PayPal batch {existing}", response_body=body)
    if status in (400, 422) and not is_retry:
        # First attempt and PayPal refused the request as invalid: nothing was created.
        return CreateOutcome("rejected", status, error=error, response_body=body)
    # Anything else may or may not have created the batch, so keep it locked and retryable.
    return CreateOutcome("unknown", status, error=error, response_body=body)


def existing_batch_id(body: object) -> str | None:
    """The payout_batch_id PayPal links to when it refuses a sender_batch_id it already has."""
    if not isinstance(body, dict):
        return None
    for detail in body.get("details") or []:
        if not isinstance(detail, dict) or str(detail.get("field", "")).upper() != "SENDER_BATCH_ID":
            continue
        if "already exists" not in str(detail.get("issue", "")).lower():
            continue
        for link in detail.get("link") or detail.get("links") or []:
            href = link.get("href", "") if isinstance(link, dict) else ""
            if "/v1/payments/payouts/" in href:
                return href.rstrip("/").rsplit("/", 1)[-1].split("?")[0] or None
    return None


def item_update_from_paypal(item: Mapping, *, event_at: str | None) -> dict | None:
    """Turns a PayPal payout item (from a poll or a webhook resource) into an apply_item_statuses() row."""
    sender_item_id = (item.get("payout_item") or {}).get("sender_item_id")
    status = item.get("transaction_status")
    if not sender_item_id or not status or not _is_uuid(sender_item_id):
        return None
    fee = (item.get("payout_item_fee") or {}).get("value")
    return {
        "item_id": sender_item_id,
        "status": status,
        "event_at": item.get("time_processed") or event_at,
        "paypal_payout_item_id": item.get("payout_item_id"),
        "paypal_transaction_id": item.get("transaction_id"),
        "error": item.get("errors"),
        "fee": fee,
    }


# --- Operations ----------------------------------------------------------------


@dataclass
class Preflight:
    """What is known before sending: rows PayPal is expected to turn away, and the balance."""
    total: Decimal
    currency: str
    turned_away: list[dict]  # {"row", "receiver", "code", "reason"}
    balance: Decimal | None = None
    balance_error: str | None = None

    @property
    def short_by(self) -> Decimal | None:
        return self.total - self.balance if self.balance is not None and self.balance < self.total else None


def preflight(store: Store, balances: BalanceReader | None, *, batch_id: str, currency: str) -> Preflight:
    """balances: None skips the balance (it is then reported as not checked)."""
    included = store.included_items(batch_id)
    history = rules.PayPalHistory.from_payouts(store.payout_history())
    turned_away = []
    for item in included:
        problem = history.problem(item["receiver"]) if item.get("receiver") else None
        if problem:
            turned_away.append({"row": item["row_number"], "receiver": item["receiver"], "code": problem[0],
                                "reason": problem[1]})
    check = Preflight(total=sum((Decimal(str(i["amount"])) for i in included), Decimal(0)), currency=currency,
                      turned_away=turned_away)
    if balances is None:
        check.balance_error = "not checked"
        return check
    try:
        check.balance = balances.available(currency)
    except PayPalError as exc:  # an unreadable balance never blocks a payout
        check.balance_error = str(exc)
    return check


def submit_batch(store: Store, paypal: PayPalClient, *, batch_id: str, signing_secret: str,
                 submitted_by: str | None = None, balances: BalanceReader | None = None,
                 require_funds: bool = False) -> SubmitResult:
    """require_funds: refuse when the PayPal balance cannot cover the batch (live mode). In the sandbox the
    reported balance is not what payouts draw on, so it is not checked against the batch."""
    approval = store.live_approval(batch_id)
    if approval is None:
        raise SubmitRefused("This batch has no live approval. An approver must approve it first.")
    if not approvals.verify(signing_secret, approval):
        store.write_audit(action="payout.refused", entity_type="approval", entity_id=approval["id"],
                          batch_id=batch_id, details={"reason": "approval signature does not match"})
        raise SubmitRefused("The approval record's signature does not match, so it was not made by this API "
                            "or was changed afterwards. Revoke it and approve again.")

    # Checked before an attempt is recorded, so a refusal leaves the batch approved and untouched.
    check = preflight(store, balances, batch_id=batch_id, currency=approval["currency"])
    if check.turned_away:
        rows = ", ".join(f"{t['row']} ({t['receiver']})" for t in check.turned_away[:10])
        store.write_audit(action="payout.refused", entity_type="batch", entity_id=batch_id, batch_id=batch_id,
                          details={"reason": "receivers PayPal turned away before", "rows": check.turned_away[:50]})
        raise SubmitRefused(f"Not sent: PayPal turned away earlier payouts to row(s) {rows}"
                            f"{' and more' if len(check.turned_away) > 10 else ''}, so they would fail again. "
                            "Revoke the approval, leave those rows out, and approve again.")
    # Only with real money: in the sandbox PayPal's reported balance lags and leaves out balances set
    # in the developer dashboard, which is what payouts there actually draw on.
    if check.short_by is not None and require_funds:
        store.write_audit(action="payout.refused", entity_type="batch", entity_id=batch_id, batch_id=batch_id,
                          details={"reason": "PayPal balance too low", "balance": str(check.balance),
                                   "total": str(check.total), "currency": check.currency})
        raise SubmitRefused(f"Not sent: the PayPal balance ({check.balance} {check.currency}) is "
                            f"{check.short_by} short of this batch ({check.total}). Add funds and send again.")

    try:
        submission = store.claim_for_submit(batch_id, approval["id"], submitted_by)
    except StoreError as exc:
        raise SubmitRefused(str(exc)) from exc

    try:
        batch = store.get_batch(batch_id)
        items = store.committed_items(batch_id)
        body = build_payout_request(batch, items)
        if len(items) != approval["item_count"]:
            raise RuntimeError("locked rows changed between claim and send")  # the database makes this unreachable
        try:
            response = paypal.create_payout(body, request_id=batch["sender_batch_id"])
            outcome = classify_create_response(response.status_code, safe_json(response),
                                               is_retry=submission["attempt_no"] > 1)
            outcome = CreateOutcome(**{**outcome.__dict__, "debug_id": debug_id(response)})
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            outcome = CreateOutcome("unknown", error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        # Never leave an attempt in flight: record it as unknown, then surface the bug.
        store.record_submission_result(submission["id"], outcome="unknown", error=f"internal error: {exc}")
        raise

    updated = store.record_submission_result(
        submission["id"], outcome=outcome.outcome, http_status=outcome.http_status,
        paypal_debug_id=outcome.debug_id, paypal_payout_batch_id=outcome.payout_batch_id,
        paypal_batch_status=outcome.batch_status, request_body=body, response_body=outcome.response_body,
        error=outcome.error)
    log.info("batch %s attempt %s: %s", batch_id, submission["attempt_no"], outcome.outcome)
    return SubmitResult(batch_id=batch_id, attempt_no=submission["attempt_no"], outcome=outcome.outcome,
                        batch_status=updated["status"], payout_batch_id=outcome.payout_batch_id,
                        http_status=outcome.http_status, error=outcome.error)


def sync_batch_status(store: Store, paypal: PayPalClient, *, batch_id: str) -> SyncResult:
    """Polling fallback: reads the batch and every item from PayPal and applies what changed."""
    batch = store.get_batch(batch_id)
    payout_batch_id = batch.get("paypal_payout_batch_id")
    if not payout_batch_id:
        raise ValueError("PayPal has not accepted this batch yet, so there is no status to read.")
    # Items PayPal has not processed carry no timestamp; date them at submission so
    # any webhook (always later) wins over them.
    fallback_at = batch.get("submitted_at") or datetime.now(UTC).isoformat()

    updates: list[dict] = []
    header: dict = {}
    page = 1
    while True:
        data = paypal.get_payout_batch(payout_batch_id, page=page, page_size=PAGE_SIZE)
        header = data.get("batch_header", {})
        items = data.get("items", [])
        updates.extend(u for u in (item_update_from_paypal(i, event_at=fallback_at) for i in items) if u)
        total_pages = data.get("total_page")
        if not items or len(items) < PAGE_SIZE or (total_pages and page >= int(total_pages)):
            break
        page += 1

    changed = store.apply_item_statuses(updates, "poll") if updates else 0
    status = header.get("batch_status")
    updated = store.apply_batch_status(payout_batch_id, status) if status else None
    return SyncResult(batch_id=batch_id, paypal_batch_status=status,
                      batch_status=(updated or batch)["status"], items_seen=len(updates), items_changed=changed)


def process_webhook_event(store: Store, event: Mapping) -> str:
    """Applies one signature-verified webhook event. Safe to call twice with the same event.
    Returns a short description of what happened."""
    resource = event.get("resource") or {}
    event_type = event.get("event_type", "")
    batch_header = resource.get("batch_header") or {}
    payout_batch_id = resource.get("payout_batch_id") or batch_header.get("payout_batch_id")
    sender_item_id = (resource.get("payout_item") or {}).get("sender_item_id")

    if not store.record_webhook_event(event, payout_batch_id=payout_batch_id, sender_item_id=sender_item_id):
        return "duplicate delivery ignored"

    error = None
    try:
        if event_type.startswith("PAYMENT.PAYOUTS-ITEM."):
            update = item_update_from_paypal(resource, event_at=event.get("create_time"))
            if update:
                update["event_at"] = event.get("create_time") or update["event_at"]
            changed = store.apply_item_statuses([update], "webhook") if update else 0
            result = f"item {sender_item_id}: {'updated' if changed else 'no change (older or unknown item)'}"
        elif event_type.startswith("PAYMENT.PAYOUTSBATCH."):
            status = batch_header.get("batch_status")
            updated = store.apply_batch_status(payout_batch_id, status) if payout_batch_id and status else None
            result = f"batch {payout_batch_id}: {updated['status'] if updated else 'unknown batch, polling will catch up'}"
        else:
            result = f"ignored event type {event_type}"
    except StoreError as exc:
        error = str(exc)
        result = f"failed: {error}"
    store.finish_webhook_event(event["id"], error)
    return result


# ---------------------------------------------------------------------------


def _error_text(status: int, body: object) -> str:
    if isinstance(body, dict):
        parts = [body.get("name"), body.get("message")]
        details = body.get("details") or []
        parts += [f"{d.get('field', '')} {d.get('issue', '')}".strip() for d in details if isinstance(d, dict)]
        text = "; ".join(p for p in parts if p)
        if text:
            return f"HTTP {status}: {text}"
    return f"HTTP {status}"


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True

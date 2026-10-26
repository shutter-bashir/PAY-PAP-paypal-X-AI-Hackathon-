"""Submit and status logic against a fake store and a fake PayPal (httpx.MockTransport).
The database side of the same guarantees is tested in backend/db/tests/test_schema.sql."""
import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from app import approvals, payouts
from app.paypal_client import PayPalClient
from app.store import StoreError

SECRET = "test-secret"
BATCH_ID = "22222222-2222-4222-8222-222222222222"
ITEM_IDS = ["aaaaaaaa-0000-4000-8000-000000000001", "aaaaaaaa-0000-4000-8000-000000000002"]


class FakeStore:
    def __init__(self, *, signed=True):
        self.batch = {"id": BATCH_ID, "sender_batch_id": "paypap_fixed", "currency": "USD", "status": "approved",
                      "email_subject": None, "email_message": None, "submitted_at": "2026-10-24T10:00:00+00:00",
                      "paypal_payout_batch_id": None}
        self.items = [
            {"id": ITEM_IDS[0], "receiver_type": "EMAIL", "receiver": "ann@example.com", "amount": 100,
             "currency": "USD", "invoice_id": "INV-1", "note": None},
            {"id": ITEM_IDS[1], "receiver_type": "EMAIL", "receiver": "bob@example.com", "amount": 50.5,
             "currency": "USD", "invoice_id": None, "note": "Thanks"},
        ]
        fields = dict(approval_id="11111111-1111-4111-8111-111111111111", batch_id=BATCH_ID,
                      sender_batch_id="paypap_fixed", approver_id="33333333-3333-4333-8333-333333333333",
                      item_count=2, total_amount=Decimal("150.50"), currency="USD",
                      expires_at=datetime(2030, 1, 1, tzinfo=UTC))
        self.approval = {"id": fields["approval_id"], "batch_id": BATCH_ID, "sender_batch_id": "paypap_fixed",
                         "approved_by": fields["approver_id"], "item_count": 2, "total_amount": 150.5,
                         "currency": "USD", "expires_at": "2030-01-01T00:00:00+00:00",
                         "signature": approvals.sign(SECRET, **fields) if signed else "0" * 64}
        self.attempts = []
        self.results = []
        self.audits = []
        self.item_updates = []
        self.batch_statuses = []
        self.events = set()
        self.claim_error = None

    def live_approval(self, batch_id):
        return self.approval

    def write_audit(self, **kwargs):
        self.audits.append(kwargs)

    def claim_for_submit(self, batch_id, approval_id, submitted_by):
        if self.claim_error:
            raise StoreError(self.claim_error)
        self.attempts.append(len(self.attempts) + 1)
        return {"id": f"sub-{len(self.attempts)}", "attempt_no": len(self.attempts)}

    def get_batch(self, batch_id):
        return self.batch

    def committed_items(self, batch_id):
        return self.items

    def record_submission_result(self, submission_id, **kwargs):
        self.results.append({"submission_id": submission_id, **kwargs})
        status = {"accepted": "submitted", "rejected": "rejected"}.get(kwargs["outcome"], "submitting")
        self.batch["status"] = status
        if kwargs["outcome"] == "accepted":
            self.batch["paypal_payout_batch_id"] = kwargs["paypal_payout_batch_id"]
        return self.batch

    def apply_item_statuses(self, updates, source):
        self.item_updates.append((source, updates))
        return len(updates)

    def apply_batch_status(self, payout_batch_id, status):
        self.batch_statuses.append((payout_batch_id, status))
        return {**self.batch, "status": "completed" if status == "SUCCESS" else self.batch["status"]}

    def record_webhook_event(self, event, **kwargs):
        if event["id"] in self.events:
            return False
        self.events.add(event["id"])
        return True

    def finish_webhook_event(self, event_id, error=None):
        pass


def paypal_with(handler):
    def route(request: httpx.Request):
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return handler(request)
    return PayPalClient("https://api-m.sandbox.paypal.com", "id", "secret", transport=httpx.MockTransport(route))


def created(request):
    return httpx.Response(201, json={"batch_header": {"payout_batch_id": "PB-1", "batch_status": "PENDING"}},
                          headers={"PayPal-Debug-Id": "dbg-1"})


def submit(store, paypal):
    return payouts.submit_batch(store, paypal, batch_id=BATCH_ID, signing_secret=SECRET, submitted_by="u1")


def test_build_payout_request_maps_rows_to_items():
    store = FakeStore()
    body = payouts.build_payout_request(store.batch, store.items)
    assert body["sender_batch_header"]["sender_batch_id"] == "paypap_fixed"
    assert body["items"][0] == {"recipient_type": "EMAIL", "receiver": "ann@example.com",
                                "amount": {"value": "100.00", "currency": "USD"}, "sender_item_id": ITEM_IDS[0],
                                "note": "Payment for invoice INV-1"}
    assert body["items"][1]["amount"]["value"] == "50.50" and body["items"][1]["note"] == "Thanks"


def test_accepted_submission_sends_sender_batch_id_and_request_id():
    seen = []

    def handler(request):
        seen.append(request)
        return created(request)

    store = FakeStore()
    result = submit(store, paypal_with(handler))
    assert result.outcome == "accepted" and result.payout_batch_id == "PB-1" and result.batch_status == "submitted"
    sent = json.loads(seen[0].content)
    assert sent["sender_batch_header"]["sender_batch_id"] == "paypap_fixed"
    assert seen[0].headers["PayPal-Request-Id"] == "paypap_fixed"
    assert store.results[0]["paypal_debug_id"] == "dbg-1"


def test_unsigned_approval_is_refused_before_any_call():
    calls = []
    store = FakeStore(signed=False)
    with pytest.raises(payouts.SubmitRefused, match="signature does not match"):
        submit(store, paypal_with(lambda r: calls.append(r) or created(r)))
    assert calls == [] and store.attempts == []
    assert store.audits[0]["action"] == "payout.refused"


def test_no_approval_is_refused():
    store = FakeStore()
    store.approval = None
    with pytest.raises(payouts.SubmitRefused, match="no live approval"):
        submit(store, paypal_with(created))


def test_database_refusal_is_surfaced():
    store = FakeStore()
    store.claim_error = "a submission for this batch is already in progress"
    with pytest.raises(payouts.SubmitRefused, match="already in progress"):
        submit(store, paypal_with(created))


def test_timeout_is_unknown_and_the_retry_reuses_the_same_ids():
    sent = []

    def flaky(request):
        sent.append(json.loads(request.content)["sender_batch_header"]["sender_batch_id"])
        if len(sent) == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return created(request)

    store = FakeStore()
    paypal = paypal_with(flaky)
    first = submit(store, paypal)
    assert first.outcome == "unknown" and first.batch_status == "submitting" and "ReadTimeout" in first.error
    second = submit(store, paypal)
    assert second.outcome == "accepted" and second.attempt_no == 2
    assert sent == ["paypap_fixed", "paypap_fixed"]


def test_validation_error_on_first_attempt_is_rejected():
    store = FakeStore()
    result = submit(store, paypal_with(lambda r: httpx.Response(422, json={
        "name": "VALIDATION_ERROR", "message": "Invalid request",
        "details": [{"field": "items[0].receiver", "issue": "invalid email"}]})))
    assert result.outcome == "rejected"
    assert result.error == "HTTP 422: VALIDATION_ERROR; Invalid request; items[0].receiver invalid email"


def test_any_other_error_on_a_retry_stays_unknown():
    store = FakeStore()
    store.attempts = [1]
    result = submit(store, paypal_with(lambda r: httpx.Response(400, json={"name": "SOMETHING_ELSE"})))
    assert result.outcome == "unknown"


# The body PayPal's sandbox returned when the same sender_batch_id was sent twice (2026-10-24).
ALREADY_EXISTS = {
    "name": "USER_BUSINESS_ERROR", "message": "User business error.", "debug_id": "ca4f69029c8c2",
    "details": [{"field": "SENDER_BATCH_ID", "location": "body", "issue": "Batch with given sender_batch_id already exists",
                 "link": [{"href": "https://api.sandbox.paypal.com/v1/payments/payouts/EBEARSN2ZJVK6", "rel": "self",
                           "method": "GET", "encType": "application/json"}]}],
    "links": [],
}


def test_retry_after_lost_response_recovers_the_existing_batch():
    sent = []

    def handler(request):
        sent.append(request)
        if len(sent) == 1:
            raise httpx.ReadTimeout("response lost", request=request)  # PayPal created it, we never heard
        return httpx.Response(400, json=ALREADY_EXISTS)

    store = FakeStore()
    paypal = paypal_with(handler)
    assert submit(store, paypal).outcome == "unknown"
    second = submit(store, paypal)
    assert second.outcome == "accepted" and second.payout_batch_id == "EBEARSN2ZJVK6"
    assert store.batch["paypal_payout_batch_id"] == "EBEARSN2ZJVK6"


def test_existing_batch_id_ignores_other_errors():
    assert payouts.existing_batch_id(ALREADY_EXISTS) == "EBEARSN2ZJVK6"
    assert payouts.existing_batch_id({"details": [{"field": "items[0].receiver", "issue": "already exists"}]}) is None
    assert payouts.existing_batch_id(None) is None


@pytest.mark.parametrize("status", [500, 503, 429, 403])
def test_server_and_auth_errors_are_unknown(status):
    assert submit(FakeStore(), paypal_with(lambda r: httpx.Response(status))).outcome == "unknown"


def test_unexpected_bug_still_records_the_attempt():
    store = FakeStore()
    store.items = store.items[:1]  # pretend rows changed after the claim
    with pytest.raises(RuntimeError):
        submit(store, paypal_with(created))
    assert store.results[0]["outcome"] == "unknown"


def test_expired_token_is_refreshed_once():
    tokens = iter(["old", "new"])
    calls = []

    def route(request):
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": next(tokens), "expires_in": 3600})
        calls.append(request.headers["Authorization"])
        return httpx.Response(401) if len(calls) == 1 else created(request)

    paypal = PayPalClient("https://x", "id", "secret", transport=httpx.MockTransport(route))
    assert submit(FakeStore(), paypal).outcome == "accepted"
    assert calls == ["Bearer old", "Bearer new"]


def payout_item(item_id, status, processed=None):
    item = {"payout_item_id": f"PI-{item_id[-1]}", "transaction_id": f"TX-{item_id[-1]}", "transaction_status": status,
            "payout_item_fee": {"value": "0.25", "currency": "USD"}, "payout_item": {"sender_item_id": item_id}}
    if processed:
        item["time_processed"] = processed
    return item


def test_sync_applies_items_then_batch_status():
    store = FakeStore()
    store.batch["paypal_payout_batch_id"] = "PB-1"
    paypal = paypal_with(lambda r: httpx.Response(200, json={
        "batch_header": {"batch_status": "SUCCESS"}, "total_page": 1,
        "items": [payout_item(ITEM_IDS[0], "SUCCESS", "2026-10-24T10:05:00Z"), payout_item(ITEM_IDS[1], "UNCLAIMED"),
                  {"transaction_status": "SUCCESS", "payout_item": {"sender_item_id": "not-ours"}}]}))
    result = payouts.sync_batch_status(store, paypal, batch_id=BATCH_ID)
    source, updates = store.item_updates[0]
    assert source == "poll" and len(updates) == 2
    assert updates[0]["event_at"] == "2026-10-24T10:05:00Z" and updates[0]["fee"] == "0.25"
    assert updates[1]["event_at"] == store.batch["submitted_at"]  # unprocessed items date from submission
    assert store.batch_statuses == [("PB-1", "SUCCESS")] and result.batch_status == "completed"


def test_sync_reads_every_page():
    pages = []

    def handler(request):
        page = int(request.url.params["page"])
        pages.append(page)
        items = [payout_item(ITEM_IDS[0], "SUCCESS")] * (payouts.PAGE_SIZE if page == 1 else 1)
        return httpx.Response(200, json={"batch_header": {"batch_status": "PROCESSING"}, "total_page": 2, "items": items})

    store = FakeStore()
    store.batch["paypal_payout_batch_id"] = "PB-1"
    payouts.sync_batch_status(store, paypal_with(handler), batch_id=BATCH_ID)
    assert pages == [1, 2]


def test_sync_before_acceptance_is_an_error():
    with pytest.raises(ValueError, match="not accepted"):
        payouts.sync_batch_status(FakeStore(), paypal_with(created), batch_id=BATCH_ID)


def item_event(event_id="WH-1"):
    return {"id": event_id, "event_type": "PAYMENT.PAYOUTS-ITEM.SUCCEEDED", "create_time": "2026-10-24T10:06:00Z",
            "resource": {**payout_item(ITEM_IDS[0], "SUCCESS", "2026-10-24T10:05:00Z"), "payout_batch_id": "PB-1"}}


def test_webhook_item_event_uses_the_event_time():
    store = FakeStore()
    assert "updated" in payouts.process_webhook_event(store, item_event())
    source, updates = store.item_updates[0]
    assert source == "webhook" and updates[0]["event_at"] == "2026-10-24T10:06:00Z"


def test_webhook_redelivery_is_ignored():
    store = FakeStore()
    payouts.process_webhook_event(store, item_event())
    assert payouts.process_webhook_event(store, item_event()) == "duplicate delivery ignored"
    assert len(store.item_updates) == 1


def test_webhook_batch_event_moves_the_batch():
    store = FakeStore()
    event = {"id": "WH-2", "event_type": "PAYMENT.PAYOUTSBATCH.SUCCESS",
             "resource": {"batch_header": {"payout_batch_id": "PB-1", "batch_status": "SUCCESS"}}}
    assert payouts.process_webhook_event(store, event) == "batch PB-1: completed"


def test_webhook_signature_check_sends_the_headers():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"verification_status": "SUCCESS"})

    headers = {"PAYPAL-AUTH-ALGO": "SHA256withRSA", "PAYPAL-CERT-URL": "https://cert", "PAYPAL-TRANSMISSION-ID": "t1",
               "PAYPAL-TRANSMISSION-SIG": "sig", "PAYPAL-TRANSMISSION-TIME": "2026-10-24T10:06:00Z"}
    paypal = paypal_with(handler)
    assert paypal.verify_webhook_signature(headers=headers, event=item_event(), webhook_id="WHID")
    assert seen[0]["webhook_id"] == "WHID" and seen[0]["transmission_sig"] == "sig"
    assert not paypal.verify_webhook_signature(headers={}, event=item_event(), webhook_id="WHID")
    assert len(seen) == 1  # missing headers never reach PayPal

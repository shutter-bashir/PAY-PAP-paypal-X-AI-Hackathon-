from datetime import UTC, datetime
from decimal import Decimal

from app import approvals

SECRET = "test-secret"


def stored_row(**overrides):
    """An approval as PostgREST returns it: numbers as JSON numbers, timestamps as ISO text."""
    fields = dict(approval_id="11111111-1111-4111-8111-111111111111",
                  batch_id="22222222-2222-4222-8222-222222222222", sender_batch_id="paypap_abc",
                  approver_id="33333333-3333-4333-8333-333333333333", item_count=3,
                  total_amount=Decimal("180.00"), currency="USD",
                  expires_at=datetime(2026, 10, 24, 12, 0, tzinfo=UTC))
    signature = approvals.sign(SECRET, **fields)
    row = {"id": fields["approval_id"], "batch_id": fields["batch_id"], "sender_batch_id": fields["sender_batch_id"],
           "approved_by": fields["approver_id"], "item_count": 3, "total_amount": 180,
           "currency": "USD", "expires_at": "2026-10-24T15:00:00+03:00", "signature": signature}
    row.update(overrides)
    return row


def test_signature_survives_the_database_round_trip():
    assert approvals.verify(SECRET, stored_row())


def test_any_changed_field_breaks_the_signature():
    for field, value in [("total_amount", 1800), ("item_count", 4), ("sender_batch_id", "paypap_other"),
                         ("approved_by", "44444444-4444-4444-8444-444444444444"),
                         ("expires_at", "2026-10-25T12:00:00+00:00"), ("currency", "EUR")]:
        assert not approvals.verify(SECRET, stored_row(**{field: value})), field


def test_wrong_secret_or_missing_signature_fails():
    assert not approvals.verify("other-secret", stored_row())
    assert not approvals.verify(SECRET, stored_row(signature=None))
    assert not approvals.verify("", stored_row())

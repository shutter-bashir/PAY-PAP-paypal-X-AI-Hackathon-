"""The approval gate on the API side.

An approval is an HMAC over the exact batch a person approved, signed with
APPROVAL_SIGNING_SECRET. The database stores it; submit re-checks it. A row
inserted by hand, or edited after the fact, fails the check and cannot release
money, even though the database itself would accept it.
"""
from __future__ import annotations

import hashlib
import hmac
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app import rules

_CENT = Decimal("0.01")
_VERSION = "paypap.approval.v1"


class ApprovalRefused(Exception):
    """The batch cannot be approved as it stands. The message says why."""


def approval_message(
    *,
    approval_id: str,
    batch_id: str,
    sender_batch_id: str,
    approver_id: str,
    item_count: int,
    total_amount: Decimal | str | float,
    currency: str,
    expires_at: datetime | str,
) -> bytes:
    total = Decimal(str(total_amount)).quantize(_CENT)
    return "|".join([
        _VERSION, str(approval_id), str(batch_id), sender_batch_id, str(approver_id),
        str(int(item_count)), f"{total:.2f}", currency, _utc_text(expires_at),
    ]).encode()


def sign(secret: str, **fields) -> str:
    if not secret:
        raise RuntimeError("APPROVAL_SIGNING_SECRET is empty; refusing to sign approvals.")
    return hmac.new(secret.encode(), approval_message(**fields), hashlib.sha256).hexdigest()


def verify(secret: str, approval: Mapping) -> bool:
    """True if a stored approval row carries a valid signature from this API."""
    if not secret or not approval.get("signature"):
        return False
    expected = sign(
        secret,
        approval_id=approval["id"],
        batch_id=approval["batch_id"],
        sender_batch_id=approval["sender_batch_id"],
        approver_id=approval["approved_by"],
        item_count=approval["item_count"],
        total_amount=approval["total_amount"],
        currency=approval["currency"],
        expires_at=approval["expires_at"],
    )
    return hmac.compare_digest(expected, approval["signature"])


def approve(store, settings, *, batch_id: str, approver_id: str, seen_item_count: int,
            seen_total: Decimal, comment: str | None = None) -> dict:
    """Approve what the approver saw on screen: seen_item_count rows totalling seen_total.

    Caps are re-checked here on the rows actually included; the database then
    re-checks counts, totals, blocking flags and repeat invoices in one transaction.
    """
    batch = store.get_batch(batch_id)
    included = store.included_items(batch_id)
    problems = rules.check_approval_limits(included, rules.Limits.from_settings(settings))
    if problems:
        raise ApprovalRefused(" ".join(problems))

    approval_id = str(uuid.uuid4())
    # The database checks the expiry against its own clock, so count from that, not this machine's.
    expires_at = (store.server_now() + timedelta(minutes=settings.approval_ttl_minutes)).replace(microsecond=0)
    total = Decimal(str(seen_total)).quantize(_CENT)
    signature = sign(
        settings.approval_signing_secret.get_secret_value(),
        approval_id=approval_id,
        batch_id=batch_id,
        sender_batch_id=batch["sender_batch_id"],
        approver_id=approver_id,
        item_count=seen_item_count,
        total_amount=total,
        currency=batch["currency"],
        expires_at=expires_at,
    )
    return store.approve_batch(
        approval_id=approval_id, batch_id=batch_id, approver_id=approver_id, item_count=seen_item_count,
        total_amount=total, expires_at=expires_at, signature=signature, comment=comment)


def _utc_text(value: datetime | str) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        raise ValueError("expires_at must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

"""Background worker: the polling fallback for webhooks, plus expiry of approvals
that were never submitted. On serverless hosting (Vercel) there is no worker; the
daily /api/cron/tick call runs the same two steps.

    python -m app.worker
"""
from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta

from app import payouts
from app.config import settings
from app.paypal_client import PayPalClient, PayPalError
from app.store import Store, StoreError

log = logging.getLogger("paypap.worker")


def poll_once(store: Store, paypal: PayPalClient) -> int:
    """Syncs every batch PayPal is still working on. Returns how many were polled."""
    cutoff = datetime.now(UTC) - timedelta(minutes=settings.status_poll_max_minutes)
    polled = 0
    for batch in store.batches_to_poll():
        submitted = datetime.fromisoformat(batch["submitted_at"]) if batch.get("submitted_at") else None
        if submitted and submitted < cutoff and batch.get("last_polled_at"):
            # Past the polling window, rely on webhooks and poll only once per window length.
            last = datetime.fromisoformat(batch["last_polled_at"])
            if datetime.now(UTC) - last < timedelta(minutes=settings.status_poll_max_minutes):
                continue
        try:
            result = payouts.sync_batch_status(store, paypal, batch_id=batch["id"])
            log.info("batch #%s: PayPal %s, %s item(s) changed", batch["batch_no"], result.paypal_batch_status,
                     result.items_changed)
            polled += 1
        except (PayPalError, StoreError) as exc:
            log.warning("batch #%s: poll failed: %s", batch["batch_no"], exc)
    return polled


def expire_approvals(store: Store) -> int:
    expired = 0
    now = datetime.now(UTC)
    for batch in store.approved_batches():
        approval = store.live_approval(batch["id"])
        if approval and datetime.fromisoformat(approval["expires_at"]) <= now:
            try:
                store.revoke_approval(approval["id"], "expired before submission")
                expired += 1
                log.info("batch #%s: approval expired, back to review", batch["batch_no"])
            except StoreError as exc:
                log.warning("batch #%s: could not expire approval: %s", batch["batch_no"], exc)
    return expired


def run(stop: threading.Event | None = None) -> None:
    """Polls until `stop` is set; the standalone worker never sets it."""
    stop = stop or threading.Event()
    store = Store.from_settings(settings)
    with PayPalClient.from_settings(settings) as paypal:
        log.info("worker started; polling every %ss", settings.status_poll_interval_seconds)
        while not stop.is_set():
            try:
                poll_once(store, paypal)
                expire_approvals(store)
            except Exception:  # keep the worker alive; the next tick retries
                log.exception("worker tick failed")
            stop.wait(settings.status_poll_interval_seconds)


def main() -> None:
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run()


if __name__ == "__main__":
    main()

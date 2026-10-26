"""Command-line run of the whole flow, from backend/:

    python -m app.cli seed --load-directory          # demo directory + seed/demo_payouts.csv
    python -m app.cli import seed/demo_payouts.csv   # parse, run rules, store as a batch in review
    python -m app.cli show 1                         # totals, flags, rows waiting for a decision
    python -m app.cli decide 1 --exclude 14,251 --pending include
    python -m app.cli approve 1                      # as the approver
    python -m app.cli submit 1                       # sends to the PayPal sandbox
    python -m app.cli status 1                       # polls PayPal and prints reconciliation
    python -m app.cli audit --verify
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from app import approvals, demo_data, payouts
from app.config import settings
from app.csv_import import CsvImportError
from app.importer import import_csv
from app.paypal_balances import BalanceReader
from app.paypal_client import PayPalClient, PayPalError
from app.store import Store, StoreError

BACKEND = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Pay pap command line")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("seed", help="write the demo CSV (and optionally load the 250-recipient directory)")
    p.add_argument("--out", default=str(BACKEND / "seed" / "demo_payouts.csv"))
    p.add_argument("--load-directory", action="store_true", help="upsert the demo recipients into Supabase")
    p.add_argument("--sandbox-receivers", default="", help="comma-separated sandbox Personal account emails")
    p.add_argument("--email-domain", help="put every contractor on this domain, e.g. paypap.com")
    p.add_argument("--sandbox-accounts", type=int, default=0, metavar="N",
                   help="list the first N clean contractors to create as sandbox Personal accounts")
    p.add_argument("--invoice-prefix", default="INV-202610",
                   help="change it for a fresh file once the previous one has been paid, e.g. INV-202611")

    p = sub.add_parser("import", help="import a CSV as a new batch")
    p.add_argument("path")
    p.add_argument("--name")
    p.add_argument("--map", action="append", default=[], metavar="FIELD=HEADER",
                   help="column mapping override, e.g. --map receiver='Work Email'")
    p.add_argument("--as", dest="as_email", default=settings.demo_admin_email)

    p = sub.add_parser("show", help="show a batch")
    p.add_argument("batch")
    p.add_argument("--all", action="store_true", help="list every row, not just flagged ones")

    p = sub.add_parser("decide", help="include or exclude rows")
    p.add_argument("batch")
    p.add_argument("--include", default="", help="rows, e.g. 5,9-12")
    p.add_argument("--exclude", default="", help="rows, e.g. 14,251")
    p.add_argument("--pending", choices=["include", "exclude"], help="decide every row still pending")
    p.add_argument("--reason")
    p.add_argument("--as", dest="as_email", default=settings.demo_admin_email)

    p = sub.add_parser("approve", help="approve a batch (as the approver)")
    p.add_argument("batch")
    p.add_argument("--comment")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("--as", dest="as_email", default=settings.demo_approver_email)

    p = sub.add_parser("revoke", help="revoke a batch's approval before submission")
    p.add_argument("batch")
    p.add_argument("--reason", default="revoked from the command line")
    p.add_argument("--as", dest="as_email", default=settings.demo_approver_email)

    p = sub.add_parser("submit", help="send an approved batch to PayPal (retry-safe)")
    p.add_argument("batch")
    p.add_argument("--as", dest="as_email", default=settings.demo_admin_email)

    p = sub.add_parser("status", help="poll PayPal for a submitted batch and print reconciliation")
    p.add_argument("batch")

    p = sub.add_parser("audit", help="print the audit log")
    p.add_argument("--batch")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--verify", action="store_true", help="check the hash chain")

    args = parser.parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except (CsvImportError, StoreError, approvals.ApprovalRefused, payouts.SubmitRefused, PayPalError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def cmd_seed(args) -> int:
    receivers = [e for e in args.sandbox_receivers.split(",") if e.strip()]
    data = demo_data.generate(sandbox_receivers=receivers, invoice_prefix=args.invoice_prefix,
                              email_domain=args.email_domain)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data.csv_bytes())
    print(f"wrote {out} ({len(data.rows)} rows)")
    for problem, rows in data.planted.items():
        print(f"  planted {problem:<20} rows {', '.join(map(str, rows))}")
    if args.sandbox_accounts:
        print(f"create these as sandbox Personal accounts so their payouts succeed:")
        for r in demo_data.clean_recipients(data, args.sandbox_accounts):
            row = next(row for row in data.rows if row["PayPal Email"] == r.email)
            print(f"  {r.email:<40} {r.full_name:<22} {r.country_code}  ${row['Amount (USD)']}")
    if args.load_directory:
        Store.from_settings(settings).upsert_recipients(data.directory_rows())
        print(f"loaded {len(data.recipients)} recipients into the directory")
    return 0


def cmd_import(args) -> int:
    store = Store.from_settings(settings)
    user = _user(store, args.as_email)
    mapping = dict(m.split("=", 1) for m in args.map) or None
    path = Path(args.path)
    summary = import_csv(store, settings, data=path.read_bytes(), filename=path.name, created_by=user["id"],
                         name=args.name, mapping=mapping)
    batch = store.get_batch(summary.batch_id)
    print(f"batch #{batch['batch_no']} ({summary.batch_id}) created from {path.name}")
    print(f"  {summary.rows} rows: {summary.included} included, {summary.pending} need a decision, {summary.excluded} excluded")
    print(f"  columns: " + ", ".join(f"{f}<-'{h}'" for f, h in summary.mapping.items()))
    if summary.unmapped_columns:
        print(f"  ignored columns: {', '.join(summary.unmapped_columns)}")
    for notice in summary.import_notices:
        print(f"  note: {notice}")
    for code, n in summary.flag_counts.items():
        print(f"  {n:>4} x {code}")
    return 0


def cmd_show(args) -> int:
    store = Store.from_settings(settings)
    batch = store.find_batch(args.batch)
    ov = store.batch_overview(batch["id"])
    print(f"batch #{ov['batch_no']} '{ov['name']}'  status={ov['status']}  sender_batch_id={ov['sender_batch_id']}")
    print(f"  included {ov['included_count']} rows = ${Decimal(str(ov['included_total'])):,.2f} {ov['currency']}; "
          f"excluded {ov['excluded_count']}; undecided {ov['undecided_count']}")
    if ov["paypal_payout_batch_id"]:
        print(f"  PayPal {ov['paypal_payout_batch_id']} {ov['paypal_batch_status']}: paid {ov['paid_count']} "
              f"(${Decimal(str(ov['paid_total'])):,.2f}), pending {ov['payout_pending_count']}, "
              f"failed {ov['failed_count']}, flagged {ov['flagged_count']}")
    for f in (f for f in store.flags(batch["id"]) if f["batch_item_id"] is None):
        print(f"  BATCH {f['severity'].upper()} {f['code']}: {f['message']}")
    flags_by_item: dict[str, list[dict]] = {}
    for f in store.flags(batch["id"]):
        if f["batch_item_id"]:
            flags_by_item.setdefault(f["batch_item_id"], []).append(f)
    for item in store.items(batch["id"]):
        item_flags = flags_by_item.get(item["id"], [])
        if not args.all and not item_flags:
            continue
        amount = f"{Decimal(str(item['amount'])):>9,.2f}" if item["amount"] is not None else "        -"
        status = f" {item['payout_status']}" if item["payout_status"] else ""
        print(f"  row {item['row_number']:>3} {item['decision']:<8} {amount} {item['receiver'] or '(no email)':<34}"
              f" {item['invoice_id'] or '':<16}{status}")
        for f in item_flags:
            print(f"          {f['severity']:<8} {f['code']}: {f['message']}")
    return 0


def cmd_decide(args) -> int:
    store = Store.from_settings(settings)
    user = _user(store, args.as_email)
    batch = store.find_batch(args.batch)
    decisions = {n: "include" for n in _rows(args.include)} | {n: "exclude" for n in _rows(args.exclude)}
    if args.pending:
        for item in store.items(batch["id"]):
            if item["decision"] == "pending" and item["row_number"] not in decisions:
                decisions[item["row_number"]] = args.pending
    if not decisions:
        raise ValueError("nothing to decide: pass --include, --exclude or --pending")
    changed = store.set_item_decisions(batch["id"], user["id"], [
        {"row_number": n, "decision": d, "reason": args.reason} for n, d in sorted(decisions.items())])
    print(f"{changed} row(s) changed")
    return 0


def cmd_approve(args) -> int:
    store = Store.from_settings(settings)
    user = _user(store, args.as_email)
    batch = store.find_batch(args.batch)
    ov = store.batch_overview(batch["id"])
    count, total = ov["included_count"], Decimal(str(ov["included_total"]))
    print(f"batch #{ov['batch_no']}: pay {count} recipients ${total:,.2f} {ov['currency']} "
          f"({ov['excluded_count']} excluded, {ov['undecided_count']} undecided)")
    if not args.yes and input("approve? [y/N] ").strip().lower() != "y":
        print("not approved")
        return 1
    approval = approvals.approve(store, settings, batch_id=batch["id"], approver_id=user["id"],
                                 seen_item_count=count, seen_total=total, comment=args.comment)
    print(f"approved by {user['email']}; approval {approval['id']} expires {approval['expires_at']}")
    return 0


def cmd_revoke(args) -> int:
    store = Store.from_settings(settings)
    user = _user(store, args.as_email)
    batch = store.find_batch(args.batch)
    approval = store.live_approval(batch["id"])
    if approval is None:
        raise ValueError("this batch has no live approval")
    store.revoke_approval(approval["id"], args.reason, user["id"])
    print("approval revoked; batch is back in review")
    return 0


def cmd_submit(args) -> int:
    store = Store.from_settings(settings)
    user = _user(store, args.as_email)
    batch = store.find_batch(args.batch)
    with PayPalClient.from_settings(settings) as paypal, BalanceReader.from_settings(settings) as balances:
        result = payouts.submit_batch(store, paypal, batch_id=batch["id"],
                                      signing_secret=settings.approval_signing_secret.get_secret_value(),
                                      submitted_by=user["id"], balances=balances,
                                      require_funds=settings.paypal_mode == "live")
    print(f"attempt {result.attempt_no}: {result.outcome} (HTTP {result.http_status}) -> batch {result.batch_status}")
    if result.payout_batch_id:
        print(f"  PayPal payout_batch_id {result.payout_batch_id}")
    if result.error:
        print(f"  {result.error}")
    if result.outcome == "unknown":
        print("  The result is unclear. Run submit again: it reuses the same sender_batch_id, so it cannot pay twice.")
    return 0 if result.outcome == "accepted" else 1


def cmd_status(args) -> int:
    store = Store.from_settings(settings)
    batch = store.find_batch(args.batch)
    with PayPalClient.from_settings(settings) as paypal:
        result = payouts.sync_batch_status(store, paypal, batch_id=batch["id"])
    print(f"PayPal batch {result.paypal_batch_status}; {result.items_changed} of {result.items_seen} items changed; "
          f"batch is {result.batch_status}")
    return cmd_show(argparse.Namespace(batch=args.batch, all=False))


def cmd_audit(args) -> int:
    store = Store.from_settings(settings)
    batch_id = store.find_batch(args.batch)["id"] if args.batch else None
    for row in reversed(store.audit(batch_id=batch_id, limit=args.limit)):
        print(f"{row['id']:>6} {row['occurred_at'][:19]} {row['actor_type']:<6} {row['action']:<28} "
              f"{json.dumps(row['details'])[:110]}")
    if args.verify:
        check = store.verify_audit_chain()
        state = "intact" if check["first_bad_id"] is None else f"BROKEN at id {check['first_bad_id']}"
        print(f"audit chain {state} ({check['rows_checked']} rows checked)")
    return 0


COMMANDS = {"seed": cmd_seed, "import": cmd_import, "show": cmd_show, "decide": cmd_decide, "approve": cmd_approve,
            "revoke": cmd_revoke, "submit": cmd_submit, "status": cmd_status, "audit": cmd_audit}


def _user(store: Store, email: str) -> dict:
    user = store.user_by_email(email)
    if user is None:
        raise ValueError(f"no user with email {email}; the migration seeds the two demo users")
    return user


def _rows(spec: str) -> list[int]:
    numbers: list[int] = []
    for part in filter(None, (p.strip() for p in spec.split(","))):
        if "-" in part:
            start, end = (int(x) for x in part.split("-", 1))
            numbers.extend(range(start, end + 1))
        else:
            numbers.append(int(part))
    return numbers


if __name__ == "__main__":
    sys.exit(main())

"""CSV file -> batch in review: parse, look up the directory, run the rules, store atomically."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from app import rules
from app.csv_import import ImportResult, parse_csv
from app.store import Store


@dataclass(frozen=True)
class ImportSummary:
    batch_id: str
    rows: int
    included: int
    pending: int
    excluded: int
    flag_counts: dict[str, int]
    import_notices: list[str]
    unmapped_columns: list[str]
    mapping: dict[str, str]


def run_rules(parsed: ImportResult, store: Store, settings) -> rules.RuleResult:
    receivers = sorted({r.receiver for r in parsed.rows if r.receiver})
    context = store.import_context(receivers)
    directory = {
        e["receiver"]: rules.DirectoryEntry(e["receiver"], e.get("full_name"), e["vetting_status"])
        for e in context.get("recipients", [])
    }
    paid = {(p["receiver"], p["invoice_id"].lower()): p["batch_no"] for p in context.get("paid_invoices", [])}
    ctx = rules.RuleContext(currency=settings.paypal_currency, limits=rules.Limits.from_settings(settings),
                            directory=directory, paid_invoices=paid,
                            paypal_history=rules.PayPalHistory.from_payouts(store.payout_history()))
    return rules.evaluate(parsed.rows, ctx)


def import_csv(store: Store, settings, *, data: bytes, filename: str, created_by: str, name: str | None = None,
               mapping: dict[str, str] | None = None, email_subject: str | None = None) -> ImportSummary:
    """Raises csv_import.CsvImportError if the file cannot be read; no batch is created then."""
    parsed = parse_csv(data, mapping=mapping, default_currency=settings.paypal_currency)
    result = run_rules(parsed, store, settings)

    # Only well-formed emails join the directory; they join as pending vetting.
    new_recipients = {}
    for r in parsed.rows:
        if r.receiver and not rules.email_problem(r.receiver) and r.receiver not in new_recipients:
            new_recipients[r.receiver] = {"receiver_type": "EMAIL", "receiver": r.receiver, "full_name": r.recipient_name,
                                          "country_code": r.country_code, "external_ref": r.external_ref}

    items = []
    for r in parsed.rows:
        decision, reason = result.decisions[r.row_number]
        items.append({
            "row_number": r.row_number, "raw": r.raw, "receiver_type": "EMAIL", "receiver": r.receiver,
            "recipient_name": r.recipient_name, "amount": str(r.amount) if r.amount is not None else None,
            "currency": r.currency, "invoice_id": r.invoice_id, "note": r.note,
            "decision": decision, "decision_reason": reason,
        })
    flags = [{"row_number": f.row_number, "code": f.code, "severity": f.severity, "message": f.message,
              "evidence": f.evidence} for f in result.flags]
    import_errors = [{"row": r.row_number, "column": p.column, "code": p.code, "message": p.message}
                     for r in parsed.rows for p in r.problems]
    import_errors += [{"row": None, "column": None, "code": "NOTICE", "message": n} for n in parsed.notices]

    batch_id = store.import_batch(
        created_by=created_by,
        batch={
            "name": name or filename,
            "currency": settings.paypal_currency,
            "source_filename": filename,
            "source_sha256": parsed.sha256,
            "column_mapping": parsed.mapping,
            "import_errors": import_errors,
            "email_subject": email_subject,
            "rules_version": rules.RULES_VERSION,
        },
        recipients=list(new_recipients.values()),
        items=items,
        flags=flags,
    )
    decisions = Counter(d for d, _ in result.decisions.values())
    return ImportSummary(
        batch_id=batch_id, rows=len(parsed.rows), included=decisions["include"], pending=decisions["pending"],
        excluded=decisions["exclude"], flag_counts=dict(Counter(f.code for f in result.flags).most_common()),
        import_notices=parsed.notices, unmapped_columns=parsed.unmapped_columns, mapping=parsed.mapping)

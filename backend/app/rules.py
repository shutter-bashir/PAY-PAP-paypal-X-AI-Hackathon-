"""Validation and risk rules. Pure functions: no database, no network, no AI.

These rules, not the language model, decide what may be paid. Each rule looks at
the parsed rows and returns flags:

* blocking - the row cannot be paid (the database refuses to include it).
* warning  - a person must look and choose include or exclude.
* info     - shown in the grid, no action needed.

evaluate() runs every rule and proposes a default decision per row: blocked rows
are excluded, warned rows wait for a person, clean rows are included.
check_approval_limits() re-applies the caps to the rows a person finally chose.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from email_validator import EmailNotValidError, validate_email

from app.csv_import import ParsedRow

RULES_VERSION = "2026-10-26.1"

BLOCKING = "blocking"
WARNING = "warning"
INFO = "info"
_RANK = {INFO: 1, WARNING: 2, BLOCKING: 3}

OUTLIER_MIN_ROWS = 10  # below this a z-score says nothing useful


@dataclass(frozen=True)
class Flag:
    code: str
    severity: str
    message: str
    row_number: int | None = None  # None = about the whole batch
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Limits:
    max_amount_per_recipient: Decimal
    max_batch_total: Decimal
    max_batch_items: int
    suspicious_amount_zscore: float
    min_amount: Decimal

    @classmethod
    def from_settings(cls, settings) -> Limits:
        return cls(
            max_amount_per_recipient=settings.max_amount_per_recipient,
            max_batch_total=settings.max_batch_total,
            max_batch_items=settings.max_batch_items,
            suspicious_amount_zscore=settings.suspicious_amount_zscore,
            min_amount=settings.min_amount,
        )


@dataclass(frozen=True)
class DirectoryEntry:
    receiver: str
    full_name: str | None
    vetting_status: str  # approved | pending | blocked


@dataclass(frozen=True)
class RuleContext:
    currency: str
    limits: Limits
    directory: Mapping[str, DirectoryEntry] = field(default_factory=dict)  # receiver -> entry
    paid_invoices: Mapping[tuple[str, str], int] = field(default_factory=dict)  # (receiver, invoice lower) -> batch_no
    paypal_history: PayPalHistory = field(default_factory=lambda: PayPalHistory())


DOMAIN_REJECTIONS_TO_BLOCK = 3  # receivers PayPal turned away on one domain, with none paid, before the domain is blocked


@dataclass(frozen=True)
class PayPalHistory:
    """What PayPal did with earlier payouts, so a row that would fail the same way is caught before sending."""
    rejected: Mapping[str, str] = field(default_factory=dict)  # receiver -> PayPal's reason, if the last payout was turned away
    domain_rejected: Mapping[str, int] = field(default_factory=dict)  # email domain -> receivers turned away
    domain_paid: Mapping[str, int] = field(default_factory=dict)  # email domain -> receivers paid

    @classmethod
    def from_payouts(cls, payouts: Sequence[Mapping]) -> PayPalHistory:
        """`payouts`: earlier batch items with receiver, payout_status, payout_error and payout_status_at."""
        latest: dict[str, Mapping] = {}
        for p in sorted((p for p in payouts if p.get("receiver") and p.get("payout_status")),
                        key=lambda p: p.get("payout_status_at") or ""):
            latest[p["receiver"].lower()] = p
        rejected = {r: reason for r, p in latest.items() if (reason := receiver_rejection(p)) is not None}
        paid = {r for r, p in latest.items() if p["payout_status"] == "SUCCESS"}
        domains_rejected: dict[str, int] = defaultdict(int)
        domains_paid: dict[str, int] = defaultdict(int)
        for r in rejected:
            domains_rejected[_domain(r)] += 1
        for r in paid:
            domains_paid[_domain(r)] += 1
        return cls(rejected, dict(domains_rejected), dict(domains_paid))

    def problem(self, receiver: str) -> tuple[str, str, dict] | None:
        """(code, message, evidence) when PayPal is expected to turn this receiver away."""
        receiver = receiver.lower()
        if receiver in self.rejected:
            return ("PAYPAL_REJECTED_RECEIVER",
                    f"PayPal turned away the last payout to {receiver} ({self.rejected[receiver]}). Sending again "
                    "would fail the same way: check the email and that it is a PayPal account.",
                    {"receiver": receiver, "paypal_reason": self.rejected[receiver]})
        domain = _domain(receiver)
        turned_away = self.domain_rejected.get(domain, 0)
        if turned_away >= DOMAIN_REJECTIONS_TO_BLOCK and not self.domain_paid.get(domain):
            return ("PAYPAL_REJECTED_DOMAIN",
                    f"PayPal has turned away payouts to {turned_away} @{domain} addresses and paid none, "
                    "so this one would very likely fail too.",
                    {"domain": domain, "receivers_turned_away": turned_away})
        return None


def receiver_rejection(payout: Mapping) -> str | None:
    """PayPal's reason when it failed a payout because of the receiver (e.g. INVALID_EMAIL), else None.
    Failures for other reasons (funds, limits) say nothing about the receiver."""
    if payout.get("payout_status") != "FAILED":
        return None
    error = payout.get("payout_error") or {}
    name = str(error.get("name") or "") if isinstance(error, Mapping) else ""
    if "EMAIL" in name or name.startswith("RECEIVER"):
        return f"{name}: {error.get('message')}" if error.get("message") else name
    return None


def _domain(receiver: str) -> str:
    return receiver.rsplit("@", 1)[-1].lower()


@dataclass
class RuleResult:
    flags: list[Flag]
    decisions: dict[int, tuple[str, str | None]]  # row_number -> (decision, reason)

    def flags_for(self, row_number: int) -> list[Flag]:
        return [f for f in self.flags if f.row_number == row_number]

    def batch_flags(self) -> list[Flag]:
        return [f for f in self.flags if f.row_number is None]


Rule = Callable[[Sequence[ParsedRow], RuleContext], Iterable[Flag]]


def money(value: Decimal) -> str:
    return f"${value:,.2f}"


def email_problem(receiver: str) -> str | None:
    """Why `receiver` is not a usable email address, or None if it is fine."""
    try:
        validate_email(receiver, check_deliverability=False)
    except EmailNotValidError as exc:
        return str(exc)
    return None


# --- Row rules ---------------------------------------------------------------


def rule_malformed_row(rows, ctx):
    for r in rows:
        for p in r.problems:
            if p.code == "MALFORMED_ROW":
                yield Flag("MALFORMED_ROW", BLOCKING, p.message, r.row_number, {"raw": r.raw})


def rule_receiver(rows, ctx):
    for r in rows:
        if r.receiver is None:
            yield Flag("MISSING_EMAIL", BLOCKING, "Row has no PayPal email, so there is nowhere to send the money.", r.row_number)
            continue
        problem = email_problem(r.receiver)
        if problem:
            yield Flag("INVALID_EMAIL", BLOCKING, f"'{r.receiver}' is not a valid email address: {problem}", r.row_number,
                       {"receiver": r.receiver})


def rule_amount(rows, ctx):
    for r in rows:
        unreadable = [p for p in r.problems if p.code in ("AMOUNT_UNREADABLE", "AMOUNT_TOO_PRECISE")]
        if unreadable:
            yield Flag("INVALID_AMOUNT", BLOCKING, unreadable[0].message, r.row_number, {"raw": r.raw})
        elif r.amount is None:
            yield Flag("MISSING_AMOUNT", BLOCKING, "Row has no amount.", r.row_number)
        elif r.amount <= 0:
            yield Flag("NON_POSITIVE_AMOUNT", BLOCKING, f"Amount {r.amount} is not positive; a payout must be more than zero.",
                       r.row_number, {"amount": str(r.amount)})
        elif r.amount < ctx.limits.min_amount:
            yield Flag("BELOW_MINIMUM", BLOCKING, f"Amount {money(r.amount)} is below the {money(ctx.limits.min_amount)} minimum.",
                       r.row_number, {"amount": str(r.amount)})


def rule_currency(rows, ctx):
    for r in rows:
        bad = [p for p in r.problems if p.code == "CURRENCY_UNREADABLE"]
        if bad:
            yield Flag("INVALID_CURRENCY", BLOCKING, bad[0].message, r.row_number)
        elif r.currency != ctx.currency:
            yield Flag("CURRENCY_MISMATCH", BLOCKING, f"Row is in {r.currency} but this batch pays in {ctx.currency}.",
                       r.row_number, {"currency": r.currency})


def rule_amount_over_cap(rows, ctx):
    cap = ctx.limits.max_amount_per_recipient
    for r in rows:
        if r.amount is not None and r.amount > cap:
            yield Flag("AMOUNT_OVER_CAP", BLOCKING, f"Amount {money(r.amount)} is above the {money(cap)} per-person cap.",
                       r.row_number, {"amount": str(r.amount), "cap": str(cap)})


def rule_recipient_total_over_cap(rows, ctx):
    cap = ctx.limits.max_amount_per_recipient
    for receiver, group in _by_receiver(rows).items():
        payable = [r for r in group if r.amount is not None and r.amount > 0]
        total = sum((r.amount for r in payable), Decimal(0))
        if len(payable) > 1 and total > cap:
            row_numbers = [r.row_number for r in payable]
            for r in payable:
                yield Flag("RECIPIENT_TOTAL_OVER_CAP", WARNING,
                           f"{receiver} is owed {money(total)} across rows {_rows(row_numbers)}, above the {money(cap)} per-person cap. "
                           "Exclude rows until the total is within the cap.",
                           r.row_number, {"receiver": receiver, "total": str(total), "rows": row_numbers, "cap": str(cap)})


def rule_duplicate_invoice(rows, ctx):
    groups: dict[str, list[ParsedRow]] = defaultdict(list)
    for r in rows:
        if r.invoice_id:
            groups[r.invoice_id.lower()].append(r)
    for group in groups.values():
        if len(group) < 2:
            continue
        row_numbers = [r.row_number for r in group]
        receivers = sorted({r.receiver or "(no email)" for r in group})
        for r in group:
            others = [n for n in row_numbers if n != r.row_number]
            if len(receivers) == 1:
                yield Flag("DUPLICATE_INVOICE", WARNING,
                           f"Invoice {r.invoice_id} is claimed again in row(s) {_rows(others)} by the same recipient. Pay it once.",
                           r.row_number, {"invoice_id": r.invoice_id, "rows": row_numbers})
            else:
                yield Flag("INVOICE_SHARED", WARNING,
                           f"Invoice {r.invoice_id} also appears in row(s) {_rows(others)} under a different email "
                           f"({', '.join(receivers)}). One invoice should have one payee.",
                           r.row_number, {"invoice_id": r.invoice_id, "rows": row_numbers, "receivers": receivers})


def rule_duplicate_recipient(rows, ctx):
    for receiver, group in _by_receiver(rows).items():
        invoices = {(r.invoice_id or "").lower() for r in group}
        if len(group) < 2 or len(invoices) < 2:
            continue  # identical invoices are DUPLICATE_INVOICE's job
        row_numbers = [r.row_number for r in group]
        for r in group:
            others = [n for n in row_numbers if n != r.row_number]
            yield Flag("DUPLICATE_RECIPIENT", WARNING,
                       f"{receiver} is also paid in row(s) {_rows(others)} for a different invoice. Check that both are owed.",
                       r.row_number, {"receiver": receiver, "rows": row_numbers})


def rule_missing_invoice(rows, ctx):
    for r in rows:
        if not r.invoice_id:
            yield Flag("MISSING_INVOICE", WARNING, "Row has no invoice ID, so a repeat payment for the same work cannot be detected.",
                       r.row_number)


def rule_invoice_already_paid(rows, ctx):
    for r in rows:
        if r.receiver and r.invoice_id:
            batch_no = ctx.paid_invoices.get((r.receiver, r.invoice_id.lower()))
            if batch_no is not None:
                yield Flag("INVOICE_ALREADY_PAID", BLOCKING,
                           f"Invoice {r.invoice_id} was already paid to {r.receiver} in batch #{batch_no}.",
                           r.row_number, {"invoice_id": r.invoice_id, "batch_no": batch_no})


def rule_recipient_vetting(rows, ctx):
    for r in rows:
        if r.receiver is None or email_problem(r.receiver):
            continue
        entry = ctx.directory.get(r.receiver)
        if entry is None:
            yield Flag("NEW_RECIPIENT", WARNING,
                       f"{r.receiver} is not in the contractor directory. They will be added as pending vetting.",
                       r.row_number, {"receiver": r.receiver})
        elif entry.vetting_status == "blocked":
            yield Flag("RECIPIENT_BLOCKED", BLOCKING, f"{r.receiver} is blocked in the contractor directory and must not be paid.",
                       r.row_number, {"receiver": r.receiver})
        elif entry.vetting_status == "pending":
            yield Flag("RECIPIENT_NOT_VETTED", WARNING, f"{r.receiver} has not been vetted yet.", r.row_number,
                       {"receiver": r.receiver})


def rule_paypal_rejected(rows, ctx):
    for r in rows:
        if r.receiver is None or email_problem(r.receiver):
            continue
        problem = ctx.paypal_history.problem(r.receiver)
        if problem:
            code, message, evidence = problem
            yield Flag(code, BLOCKING, message, r.row_number, evidence)


def rule_name_mismatch(rows, ctx):
    for r in rows:
        entry = ctx.directory.get(r.receiver) if r.receiver else None
        if entry and entry.full_name and r.recipient_name and _name_key(entry.full_name) != _name_key(r.recipient_name):
            yield Flag("NAME_MISMATCH", WARNING,
                       f"The file names this payee '{r.recipient_name}', but {r.receiver} belongs to '{entry.full_name}' "
                       "in the directory.",
                       r.row_number, {"file_name": r.recipient_name, "directory_name": entry.full_name})


def rule_outlier_amount(rows, ctx):
    """A positive amount far above the others. Each row is compared with the rest, so one
    huge amount cannot hide itself by inflating the spread."""
    payable = [r for r in rows if r.amount is not None and r.amount > 0]
    if len(payable) < OUTLIER_MIN_ROWS:
        return
    amounts = [float(r.amount) for r in payable]
    total = sum(amounts)
    total_sq = sum(a * a for a in amounts)
    n = len(amounts)
    median = statistics.median(amounts)
    for r, a in zip(payable, amounts):
        rest_mean = (total - a) / (n - 1)
        rest_var = (total_sq - a * a - (n - 1) * rest_mean ** 2) / (n - 2)
        if rest_var <= 0:
            continue
        z = (a - rest_mean) / rest_var ** 0.5
        if z > ctx.limits.suspicious_amount_zscore:
            yield Flag("OUTLIER_AMOUNT", WARNING,
                       f"Amount {money(r.amount)} is {a / median:.1f} times the median payout ({money(Decimal(str(median)))}) "
                       f"and {z:.1f} standard deviations above the other rows.",
                       r.row_number, {"amount": str(r.amount), "z_score": round(z, 2), "median": round(median, 2),
                                      "mean_of_others": round(rest_mean, 2)})


ROW_RULES: list[Rule] = [
    rule_malformed_row,
    rule_receiver,
    rule_amount,
    rule_currency,
    rule_amount_over_cap,
    rule_recipient_total_over_cap,
    rule_duplicate_invoice,
    rule_duplicate_recipient,
    rule_missing_invoice,
    rule_invoice_already_paid,
    rule_recipient_vetting,
    rule_paypal_rejected,
    rule_name_mismatch,
    rule_outlier_amount,
]


# --- Batch rules (run on the rows that survive the row rules) ---------------


def batch_flags(payable: Sequence[ParsedRow], ctx: RuleContext) -> list[Flag]:
    flags = []
    total = sum((r.amount for r in payable), Decimal(0))
    if total > ctx.limits.max_batch_total:
        flags.append(Flag("BATCH_OVER_TOTAL_CAP", WARNING,
                          f"Payable rows total {money(total)}, above the {money(ctx.limits.max_batch_total)} batch cap. "
                          "Exclude rows to approve.",
                          None, {"total": str(total), "cap": str(ctx.limits.max_batch_total)}))
    if len(payable) > ctx.limits.max_batch_items:
        flags.append(Flag("BATCH_TOO_MANY_ITEMS", WARNING,
                          f"{len(payable)} payable rows, above the {ctx.limits.max_batch_items}-row batch limit. "
                          "Exclude rows or split the file.",
                          None, {"count": len(payable), "limit": ctx.limits.max_batch_items}))
    return flags


def evaluate(rows: Sequence[ParsedRow], ctx: RuleContext) -> RuleResult:
    flags: list[Flag] = []
    for rule in ROW_RULES:
        flags.extend(rule(rows, ctx))

    worst: dict[int, str] = {}
    codes: dict[int, list[str]] = defaultdict(list)
    for f in flags:
        if f.row_number is not None:
            codes[f.row_number].append(f.code)
            if _RANK[f.severity] > _RANK.get(worst.get(f.row_number, INFO), 0):
                worst[f.row_number] = f.severity

    decisions: dict[int, tuple[str, str | None]] = {}
    for r in rows:
        severity = worst.get(r.row_number)
        if severity == BLOCKING:
            blocking = sorted({f.code for f in flags if f.row_number == r.row_number and f.severity == BLOCKING})
            decisions[r.row_number] = ("exclude", "Blocked by rules: " + ", ".join(blocking))
        elif severity == WARNING:
            decisions[r.row_number] = ("pending", None)
        else:
            decisions[r.row_number] = ("include", None)

    payable = [r for r in rows if decisions[r.row_number][0] != "exclude"]
    flags.extend(batch_flags(payable, ctx))
    return RuleResult(flags=flags, decisions=decisions)


def check_approval_limits(included: Sequence[Mapping], limits: Limits) -> list[str]:
    """Caps re-checked on the rows a person chose to include. Each item needs 'receiver',
    'amount' and 'row_number'. Returns reasons to refuse approval (empty list = within limits)."""
    problems = []
    amounts = [Decimal(str(i["amount"])) for i in included]
    total = sum(amounts, Decimal(0))
    if total > limits.max_batch_total:
        problems.append(f"Included rows total {money(total)}, above the {money(limits.max_batch_total)} batch cap.")
    if len(included) > limits.max_batch_items:
        problems.append(f"{len(included)} rows are included, above the {limits.max_batch_items}-row batch limit.")
    per_receiver: dict[str, list[Mapping]] = defaultdict(list)
    for i in included:
        per_receiver[i["receiver"]].append(i)
    for receiver, items in sorted(per_receiver.items()):
        subtotal = sum((Decimal(str(i["amount"])) for i in items), Decimal(0))
        if subtotal > limits.max_amount_per_recipient:
            problems.append(f"{receiver} would receive {money(subtotal)} (rows {_rows([i['row_number'] for i in items])}), "
                            f"above the {money(limits.max_amount_per_recipient)} per-person cap.")
    return problems


# ---------------------------------------------------------------------------


def _by_receiver(rows: Iterable[ParsedRow]) -> dict[str, list[ParsedRow]]:
    groups: dict[str, list[ParsedRow]] = defaultdict(list)
    for r in rows:
        if r.receiver:
            groups[r.receiver].append(r)
    return groups


def _rows(numbers: Iterable[int]) -> str:
    return ", ".join(str(n) for n in sorted(numbers))


def _name_key(name: str) -> str:
    return " ".join(name.lower().split())

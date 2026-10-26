from decimal import Decimal

import pytest

from app import rules
from app.csv_import import ParsedRow, RowProblem

LIMITS = rules.Limits(
    max_amount_per_recipient=Decimal("1000.00"),
    max_batch_total=Decimal("25000.00"),
    max_batch_items=500,
    suspicious_amount_zscore=3.0,
    min_amount=Decimal("0.01"),
)
_APPROVED = "approved"


def row(n, receiver="ann@example.com", amount="50.00", invoice=None, name=None, currency="USD", problems=()):
    return ParsedRow(
        row_number=n, raw={}, receiver=receiver, recipient_name=name,
        amount=Decimal(amount) if amount is not None else None, currency=currency,
        invoice_id=invoice if invoice is not None else f"INV-{n}", problems=list(problems))


def ctx(rows=(), **kwargs):
    """Every receiver in `rows` is in the directory as approved unless the test says otherwise."""
    directory = {r.receiver: rules.DirectoryEntry(r.receiver, None, _APPROVED) for r in rows if r.receiver}
    directory.update(kwargs.pop("directory", {}))
    return rules.RuleContext(currency="USD", limits=kwargs.pop("limits", LIMITS), directory=directory, **kwargs)


def codes(result, row_number=None):
    return sorted(f.code for f in result.flags if f.row_number == row_number)


def run(rows, **kwargs):
    return rules.evaluate(rows, ctx(rows, **kwargs))


def distinct(n):
    """n clean rows for n different people with similar amounts."""
    return [row(i + 2, receiver=f"p{i}@example.com", amount=str(60 + i % 7)) for i in range(n)]


def test_clean_row_has_no_flags_and_is_included():
    result = run([row(2)])
    assert result.flags == []
    assert result.decisions[2] == ("include", None)


def test_malformed_row_is_blocking():
    r = row(2, problems=[RowProblem("MALFORMED_ROW", None, "Row has 5 values but the header has 4 columns")])
    assert codes(run([r]), 2) == ["MALFORMED_ROW"]


def test_missing_email_is_blocking():
    result = run([row(2, receiver=None)])
    assert codes(result, 2) == ["MISSING_EMAIL"]
    assert result.decisions[2][0] == "exclude"


@pytest.mark.parametrize("email", ["jane@gmail", "bob@@example.com", "no-at-sign.example.com", "x@host.test"])
def test_invalid_email_is_blocking(email):
    result = run([row(2, receiver=email)])
    flag = next(f for f in result.flags if f.code == "INVALID_EMAIL")
    assert flag.severity == rules.BLOCKING
    assert email in flag.message


def test_unreadable_amount_reuses_the_parser_message():
    r = row(2, amount=None, problems=[RowProblem("AMOUNT_UNREADABLE", "amount", "'12,50' is not a number.")])
    flag = next(f for f in run([r]).flags if f.row_number == 2)
    assert (flag.code, flag.message) == ("INVALID_AMOUNT", "'12,50' is not a number.")


def test_missing_amount_is_blocking():
    assert codes(run([row(2, amount=None)]), 2) == ["MISSING_AMOUNT"]


@pytest.mark.parametrize("amount", ["0.00", "-45.00"])
def test_non_positive_amount_is_blocking(amount):
    assert codes(run([row(2, amount=amount)]), 2) == ["NON_POSITIVE_AMOUNT"]


def test_amount_below_minimum_is_blocking():
    limits = rules.Limits(**{**LIMITS.__dict__, "min_amount": Decimal("1.00")})
    assert codes(run([row(2, amount="0.50")], limits=limits), 2) == ["BELOW_MINIMUM"]


def test_currency_mismatch_is_blocking():
    assert codes(run([row(2, currency="EUR")]), 2) == ["CURRENCY_MISMATCH"]


def test_unreadable_currency_is_blocking():
    r = row(2, currency=None, problems=[RowProblem("CURRENCY_UNREADABLE", "currency", "'US$' is not a code")])
    assert codes(run([r]), 2) == ["INVALID_CURRENCY"]


def test_amount_over_per_person_cap_is_blocking():
    result = run([row(2, amount="1000.01")])
    assert "AMOUNT_OVER_CAP" in codes(result, 2)
    assert row(3, amount="1000.00").amount <= LIMITS.max_amount_per_recipient  # the cap itself is allowed
    assert "AMOUNT_OVER_CAP" not in codes(run([row(3, amount="1000.00")]), 3)


def test_recipient_total_over_cap_warns_every_row_of_that_person():
    rows = [row(2, amount="600.00"), row(3, amount="500.00"), row(4, receiver="bob@example.com", amount="900.00")]
    result = run(rows)
    assert "RECIPIENT_TOTAL_OVER_CAP" in codes(result, 2)
    assert "RECIPIENT_TOTAL_OVER_CAP" in codes(result, 3)
    assert "RECIPIENT_TOTAL_OVER_CAP" not in codes(result, 4)
    assert result.decisions[2] == ("pending", None)


def test_same_invoice_same_person_is_duplicate_invoice():
    rows = [row(2, invoice="INV-7"), row(3, invoice="inv-7")]
    result = run(rows)
    assert codes(result, 2) == ["DUPLICATE_INVOICE"]
    assert codes(result, 3) == ["DUPLICATE_INVOICE"]
    assert "row(s) 3" in next(f for f in result.flags if f.row_number == 2).message


def test_same_invoice_different_people_is_invoice_shared():
    rows = [row(2, invoice="INV-7"), row(3, receiver="bob@example.com", invoice="INV-7")]
    result = run(rows)
    assert codes(result, 2) == ["INVOICE_SHARED"]
    assert codes(result, 3) == ["INVOICE_SHARED"]


def test_same_person_different_invoices_is_duplicate_recipient():
    rows = [row(2, invoice="INV-1"), row(3, invoice="INV-2")]
    assert codes(run(rows), 2) == ["DUPLICATE_RECIPIENT"]


def test_identical_claims_are_not_also_duplicate_recipient():
    rows = [row(2, invoice="INV-1"), row(3, invoice="INV-1")]
    assert "DUPLICATE_RECIPIENT" not in codes(run(rows), 2)


def test_missing_invoice_warns():
    result = run([row(2, invoice="")])
    assert codes(result, 2) == ["MISSING_INVOICE"]
    assert result.decisions[2] == ("pending", None)


def test_invoice_already_paid_is_blocking():
    result = run([row(2, invoice="INV-1")], paid_invoices={("ann@example.com", "inv-1"): 4})
    flag = next(f for f in result.flags if f.code == "INVOICE_ALREADY_PAID")
    assert flag.severity == rules.BLOCKING and "batch #4" in flag.message


def test_recipient_not_in_directory_warns():
    r = row(2)
    result = rules.evaluate([r], rules.RuleContext("USD", LIMITS, directory={}))
    assert codes(result, 2) == ["NEW_RECIPIENT"]


def test_blocked_recipient_is_blocking():
    blocked = {"ann@example.com": rules.DirectoryEntry("ann@example.com", None, "blocked")}
    assert codes(run([row(2)], directory=blocked), 2) == ["RECIPIENT_BLOCKED"]


def test_unvetted_recipient_warns():
    pending = {"ann@example.com": rules.DirectoryEntry("ann@example.com", None, "pending")}
    assert codes(run([row(2)], directory=pending), 2) == ["RECIPIENT_NOT_VETTED"]


def test_name_mismatch_warns_but_ignores_case_and_spacing():
    entry = {"ann@example.com": rules.DirectoryEntry("ann@example.com", "Ann Lee", _APPROVED)}
    assert codes(run([row(2, name="Kevin Odhiambo")], directory=entry), 2) == ["NAME_MISMATCH"]
    assert codes(run([row(2, name="  ann   LEE ")], directory=entry), 2) == []


def test_one_outsized_amount_is_the_only_outlier():
    rows = distinct(30) + [row(40, receiver="big@example.com", amount="985.00")]
    result = run(rows)
    outliers = [f for f in result.flags if f.code == "OUTLIER_AMOUNT"]
    assert [f.row_number for f in outliers] == [40]
    assert outliers[0].evidence["z_score"] > 3


def test_outlier_rule_needs_enough_rows():
    rows = distinct(rules.OUTLIER_MIN_ROWS - 2) + [row(40, receiver="big@example.com", amount="985.00")]
    assert not [f for f in run(rows).flags if f.code == "OUTLIER_AMOUNT"]


def test_batch_total_over_cap_is_a_batch_flag():
    limits = rules.Limits(**{**LIMITS.__dict__, "max_batch_total": Decimal("100.00")})
    rows = distinct(3)
    result = run(rows, limits=limits)
    assert [f.code for f in result.batch_flags()] == ["BATCH_OVER_TOTAL_CAP"]


def test_batch_too_many_items_is_a_batch_flag():
    limits = rules.Limits(**{**LIMITS.__dict__, "max_batch_items": 2})
    result = run(distinct(3), limits=limits)
    assert [f.code for f in result.batch_flags()] == ["BATCH_TOO_MANY_ITEMS"]


def test_blocked_rows_do_not_count_toward_batch_caps():
    limits = rules.Limits(**{**LIMITS.__dict__, "max_batch_items": 2})
    rows = distinct(2) + [row(9, receiver=None)]
    assert run(rows, limits=limits).batch_flags() == []


def test_blocking_beats_warning_in_the_default_decision():
    result = run([row(2, receiver=None, invoice="")])
    assert set(codes(result, 2)) == {"MISSING_EMAIL", "MISSING_INVOICE"}
    decision, reason = result.decisions[2]
    assert decision == "exclude" and "MISSING_EMAIL" in reason


def test_approval_limits_recheck_the_included_rows():
    included = [{"row_number": 2, "receiver": "a@example.com", "amount": "600.00"},
                {"row_number": 3, "receiver": "a@example.com", "amount": "500.00"}]
    problems = rules.check_approval_limits(included, LIMITS)
    assert len(problems) == 1 and "a@example.com" in problems[0] and "rows 2, 3" in problems[0]
    assert rules.check_approval_limits(included[:1], LIMITS) == []


def test_approval_limits_batch_total_and_count():
    limits = rules.Limits(**{**LIMITS.__dict__, "max_batch_total": Decimal("100"), "max_batch_items": 1})
    included = [{"row_number": 2, "receiver": "a@example.com", "amount": 60},
                {"row_number": 3, "receiver": "b@example.com", "amount": 60}]
    problems = rules.check_approval_limits(included, limits)
    assert any("batch cap" in p for p in problems) and any("row batch limit" in p for p in problems)


def _payout(receiver, status, at, error=None):
    return {"receiver": receiver, "payout_status": status, "payout_status_at": at, "payout_error": error}


INVALID = {"name": "INVALID_EMAIL", "message": "Email Address doesn't exist."}


def test_paypal_history_blocks_receivers_and_domains_paypal_turned_away():
    history = rules.PayPalHistory.from_payouts(
        [_payout(f"p{i}@dead.example", "FAILED", "2026-10-26", INVALID) for i in range(3)]
        + [_payout("ok@live.example", "SUCCESS", "2026-10-25"), _payout("x@live.example", "FAILED", "2026-10-26", INVALID)])
    assert history.problem("P0@dead.example")[0] == "PAYPAL_REJECTED_RECEIVER"
    assert history.problem("new@dead.example")[0] == "PAYPAL_REJECTED_DOMAIN"  # 3 turned away, none paid
    assert history.problem("new@live.example") is None  # the domain has been paid before
    assert history.problem("x@live.example")[0] == "PAYPAL_REJECTED_RECEIVER"


def test_a_later_success_clears_a_receiver_and_other_failures_say_nothing():
    history = rules.PayPalHistory.from_payouts([
        _payout("a@x.example", "FAILED", "2026-10-25", INVALID), _payout("a@x.example", "SUCCESS", "2026-10-26"),
        _payout("b@x.example", "FAILED", "2026-10-26", {"name": "INSUFFICIENT_FUNDS", "message": "low balance"}),
    ])
    assert history.problem("a@x.example") is None and history.problem("b@x.example") is None

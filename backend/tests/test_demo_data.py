from collections import Counter
from decimal import Decimal

from app import demo_data, rules
from app.csv_import import parse_csv
from tests.test_rules import LIMITS


def evaluate_demo():
    data = demo_data.generate()
    parsed = parse_csv(data.csv_bytes())
    directory = {r.email: rules.DirectoryEntry(r.email, r.full_name, r.vetting_status) for r in data.recipients}
    result = rules.evaluate(parsed.rows, rules.RuleContext("USD", LIMITS, directory))
    return data, parsed, result


def rows_with(result, code):
    return sorted({f.row_number for f in result.flags if f.code == code})


def test_shape_and_total():
    data, parsed, _ = evaluate_demo()
    assert len(data.recipients) == 250 and len(parsed.rows) == 250
    assert sum(r.amount for r in parsed.rows if r.amount and r.amount > 0) == Decimal("18500.00")
    assert demo_data.generate().csv_bytes() == data.csv_bytes()  # deterministic


def test_each_planted_problem_is_caught_by_its_rule():
    data, _, result = evaluate_demo()
    expected = {
        "duplicate_invoice": "DUPLICATE_INVOICE",
        "shared_invoice": "INVOICE_SHARED",
        "missing_email": "MISSING_EMAIL",
        "invalid_email": "INVALID_EMAIL",
        "negative_amount": "NON_POSITIVE_AMOUNT",
        "unreadable_amount": "INVALID_AMOUNT",
        "missing_invoice": "MISSING_INVOICE",
        "name_mismatch": "NAME_MISMATCH",
        "blocked_recipient": "RECIPIENT_BLOCKED",
        "unvetted_recipient": "RECIPIENT_NOT_VETTED",
        "new_recipient": "NEW_RECIPIENT",
        "outsized_amount": "OUTLIER_AMOUNT",
    }
    for problem, code in expected.items():
        assert rows_with(result, code) == sorted(data.planted[problem]), problem
    assert set(data.planted["duplicate_recipient"]) <= set(rows_with(result, "DUPLICATE_RECIPIENT"))


def test_outsized_amount_is_under_the_cap_so_only_the_outlier_rule_sees_it():
    data, parsed, result = evaluate_demo()
    (row_number,) = data.planted["outsized_amount"]
    assert next(r for r in parsed.rows if r.row_number == row_number).amount < LIMITS.max_amount_per_recipient
    assert [f.code for f in result.flags if f.row_number == row_number] == ["OUTLIER_AMOUNT"]


def test_most_rows_are_clean():
    _, _, result = evaluate_demo()
    decisions = Counter(d for d, _ in result.decisions.values())
    assert decisions["include"] > 200 and decisions["exclude"] == 9


def test_sandbox_receivers_go_on_clean_rows():
    data = demo_data.generate(sandbox_receivers=["buyer-one@personal.example.com"])
    parsed = parse_csv(data.csv_bytes())
    assert parsed.rows[0].receiver == "buyer-one@personal.example.com"


def test_email_domain_puts_everyone_on_one_domain():
    data = demo_data.generate(email_domain="paypap.com", invoice_prefix="INV-202611")
    emails = {r["PayPal Email"] for r in data.rows if "@" in r["PayPal Email"] and "@@" not in r["PayPal Email"]}
    assert all(e.endswith("@paypap.com") for e in emails if not e.endswith("@gmail"))
    clean = demo_data.clean_recipients(data, 5)
    assert len(clean) == 5 and all(r.vetting_status == "approved" for r in clean)
    parsed = parse_csv(data.csv_bytes())
    directory = {r.email: rules.DirectoryEntry(r.email, r.full_name, r.vetting_status) for r in data.recipients}
    result = rules.evaluate(parsed.rows, rules.RuleContext("USD", LIMITS, directory))
    clean_rows = [r.row_number for r in parsed.rows if r.receiver in {c.email for c in clean}]
    assert len(clean_rows) == 5 and all(result.decisions[n] == ("include", None) for n in clean_rows)

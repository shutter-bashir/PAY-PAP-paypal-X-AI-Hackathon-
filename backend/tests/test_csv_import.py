from decimal import Decimal

import pytest

from app.csv_import import CsvImportError, parse_amount, parse_csv


def csv(text: str) -> bytes:
    return text.strip().encode("utf-8") + b"\n"


def test_maps_common_header_names():
    result = parse_csv(csv("""
Contractor ID,Full Name,PayPal Email,Amount (USD),Invoice Number,Country,Memo
C-1,Ann Lee,Ann@Example.com,"1,234.50",INV-1,ke,Design
"""))
    assert result.mapping == {"receiver": "PayPal Email", "recipient_name": "Full Name", "amount": "Amount (USD)",
                              "invoice_id": "Invoice Number", "note": "Memo", "external_ref": "Contractor ID",
                              "country_code": "Country"}
    r = result.rows[0]
    assert (r.receiver, r.amount, r.currency, r.country_code, r.row_number) == (
        "ann@example.com", Decimal("1234.50"), "USD", "KE", 2)


def test_explicit_mapping_overrides_guessing():
    result = parse_csv(csv("work mail,pay,email\nann@example.com,10,ignored\n"),
                       mapping={"receiver": "work mail", "amount": "pay"})
    assert result.rows[0].receiver == "ann@example.com"
    assert result.unmapped_columns == ["email"]


def test_explicit_mapping_to_a_missing_column_is_explained():
    with pytest.raises(CsvImportError, match="no such column.*Columns found: 'email', 'amount'"):
        parse_csv(csv("email,amount\na@example.com,1\n"), mapping={"invoice_id": "Invoice"})


def test_missing_required_column_lists_what_was_found():
    with pytest.raises(CsvImportError) as exc:
        parse_csv(csv("name,amount\nAnn,10\n"))
    message = str(exc.value)
    assert "recipient PayPal email" in message and "'name', 'amount'" in message


def test_two_columns_for_one_field_must_be_resolved():
    with pytest.raises(CsvImportError, match="all look like the recipient PayPal email"):
        parse_csv(csv("email,paypal email,amount\na@example.com,b@example.com,1\n"))


def test_duplicate_header_is_an_error():
    with pytest.raises(CsvImportError, match="'Amount' and 'amount' twice"):
        parse_csv(csv("email,Amount,amount\na@example.com,1,2\n"))


@pytest.mark.parametrize("data, message", [
    (b"", "empty"),
    (b"email,amount\n", "no data rows"),
    (b"\n\n", "empty"),
])
def test_empty_files_are_explained(data, message):
    with pytest.raises(CsvImportError, match=message):
        parse_csv(data)


def test_bom_semicolons_and_blank_rows():
    data = "﻿email;amount\n\na@example.com;5\n;\nb@example.com;6\n".encode("utf-8")
    result = parse_csv(data)
    assert result.delimiter == ";"
    assert [(r.receiver, r.row_number) for r in result.rows] == [("a@example.com", 3), ("b@example.com", 5)]


def test_non_utf8_file_is_read_with_a_notice():
    result = parse_csv("email,name,amount\na@example.com,Jos\xe9,5\n".encode("cp1252"))
    assert result.rows[0].recipient_name == "José"
    assert "Windows-1252" in result.notices[0]


def test_unquoted_thousands_separator_is_caught_as_malformed():
    result = parse_csv(csv("email,amount,invoice\na@example.com,1,200.00,INV-1\n"))
    row = result.rows[0]
    assert row.has_problem("MALFORMED_ROW")
    assert "1,200.00" in row.problems[0].message


def test_row_keeps_raw_values_and_problems():
    result = parse_csv(csv("email,amount,currency,country\na@example.com,abc,usd,Kenya\n"))
    row = result.rows[0]
    assert row.raw == {"email": "a@example.com", "amount": "abc", "currency": "usd", "country": "Kenya"}
    assert row.amount is None and row.currency == "USD" and row.country_code is None
    assert [p.code for p in row.problems] == ["AMOUNT_UNREADABLE", "COUNTRY_UNREADABLE"]


def test_bad_currency_is_a_row_problem():
    row = parse_csv(csv("email,amount,currency\na@example.com,5,US$\n")).rows[0]
    assert row.has_problem("CURRENCY_UNREADABLE") and row.currency is None


def test_sha256_identifies_the_file():
    data = csv("email,amount\na@example.com,5\n")
    assert parse_csv(data).sha256 == parse_csv(data).sha256 != parse_csv(data + b"b@example.com,6\n").sha256


@pytest.mark.parametrize("text, expected", [
    ("75", Decimal("75.00")),
    ("$1,234.50", Decimal("1234.50")),
    ("75 USD", Decimal("75.00")),
    ("(20.00)", Decimal("-20.00")),
    ("-45", Decimal("-45.00")),
    ("0.10", Decimal("0.10")),
])
def test_parse_amount_accepts(text, expected):
    assert parse_amount(text) == (expected, None)


@pytest.mark.parametrize("text, code, hint", [
    ("12,50", "AMOUNT_UNREADABLE", "Use a dot for cents"),
    ("abc", "AMOUNT_UNREADABLE", "not a number"),
    ("10.005", "AMOUNT_TOO_PRECISE", "two decimal places"),
    ("99999999999", "AMOUNT_UNREADABLE", "too large"),
])
def test_parse_amount_explains_rejections(text, code, hint):
    value, problem = parse_amount(text)
    assert value is None and problem.code == code and hint in problem.message


def test_blank_amount_is_missing_not_a_problem():
    assert parse_amount("  ") == (None, None)

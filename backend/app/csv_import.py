"""Parse a contractor payout CSV into normalized rows.

Columns are matched to our fields by header name (see FIELD_ALIASES), or by an
explicit mapping the user picked in the UI. This module never decides whether a
row is payable; that is the rules engine's job. Problems are reported two ways:

* CsvImportError for anything that stops the whole file (unreadable, no header,
  a required column missing). No batch is created.
* RowProblem on the row itself (an amount that is not a number, values that do
  not line up with the header). The row is still kept so it can be flagged.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

MAX_FILE_BYTES = 4 * 1024 * 1024  # Vercel refuses request bodies over 4.5 MB
MAX_ROWS = 15_000  # PayPal's item limit for one payout batch

# Field -> header names that map to it, compared after normalize_header().
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "receiver": ("email", "e mail", "email address", "paypal email", "paypal email address", "paypal",
                 "recipient email", "receiver", "receiver email", "payee email"),
    "recipient_name": ("name", "full name", "recipient name", "contractor", "contractor name", "payee",
                       "payee name", "freelancer", "freelancer name"),
    "amount": ("amount", "amount usd", "payment", "payment amount", "pay", "payout", "payout amount", "total",
               "value"),
    "currency": ("currency", "currency code", "ccy"),
    "invoice_id": ("invoice", "invoice id", "invoice number", "invoice no", "invoice ref", "inv", "inv no",
                   "reference"),
    "note": ("note", "notes", "memo", "description", "message"),
    "external_ref": ("contractor id", "recipient id", "payee id", "external id", "vendor id", "freelancer id"),
    "country_code": ("country", "country code"),
}
REQUIRED_FIELDS = ("receiver", "amount")
FIELD_LABELS = {
    "receiver": "recipient PayPal email",
    "recipient_name": "recipient name",
    "amount": "amount",
    "currency": "currency",
    "invoice_id": "invoice ID",
    "note": "note",
    "external_ref": "contractor ID",
    "country_code": "country",
}

_AMOUNT_DECORATION = re.compile(r"[\s$€£]|\b(?:USD|EUR|GBP|KES)\b", re.IGNORECASE)
_THOUSANDS = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?")
_PLAIN_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_CENT = Decimal("0.01")
_AMOUNT_LIMIT = Decimal("10000000000")  # batch_items.amount is numeric(12, 2)


class CsvImportError(Exception):
    """The file cannot be imported at all. The message is written for the person who uploaded it."""


@dataclass(frozen=True)
class RowProblem:
    code: str
    column: str | None
    message: str


@dataclass
class ParsedRow:
    row_number: int  # spreadsheet row number: the header is row 1
    raw: dict[str, str]
    receiver: str | None = None
    recipient_name: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    invoice_id: str | None = None
    note: str | None = None
    external_ref: str | None = None
    country_code: str | None = None
    problems: list[RowProblem] = field(default_factory=list)

    def has_problem(self, code: str) -> bool:
        return any(p.code == code for p in self.problems)


@dataclass
class ImportResult:
    rows: list[ParsedRow]
    mapping: dict[str, str]  # field -> header as it appears in the file
    unmapped_columns: list[str]
    notices: list[str]
    sha256: str
    delimiter: str


def normalize_header(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", header.lower()).strip()


def parse_csv(
    data: bytes,
    *,
    mapping: dict[str, str] | None = None,
    default_currency: str = "USD",
) -> ImportResult:
    """Parse CSV bytes. `mapping` (field -> header) overrides automatic column matching."""
    if not data.strip():
        raise CsvImportError("The file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise CsvImportError(f"The file is {len(data) / 1_048_576:.1f} MB; the limit is {MAX_FILE_BYTES // 1_048_576} MB.")

    notices: list[str] = []
    text = _decode(data, notices)
    delimiter = _sniff_delimiter(text)
    records = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))

    header_index = next((i for i, rec in enumerate(records) if any(cell.strip() for cell in rec)), None)
    if header_index is None:
        raise CsvImportError("The file has no header row.")
    headers = [h.strip() for h in records[header_index]]
    _check_headers(headers)
    resolved = _resolve_mapping(headers, mapping)
    column_of = {f: headers.index(h) for f, h in resolved.items()}
    mapped_headers = set(resolved.values())
    unmapped = [h for h in headers if h and h not in mapped_headers]

    rows: list[ParsedRow] = []
    for offset, record in enumerate(records[header_index + 1:], start=header_index + 2):
        if not any(cell.strip() for cell in record):
            continue
        rows.append(_parse_row(offset, record, headers, column_of, default_currency))
        if len(rows) > MAX_ROWS:
            raise CsvImportError(f"The file has more than {MAX_ROWS:,} rows, which is PayPal's limit for one batch. Split it into smaller files.")
    if not rows:
        raise CsvImportError("The file has a header row but no data rows.")

    return ImportResult(
        rows=rows,
        mapping=resolved,
        unmapped_columns=unmapped,
        notices=notices,
        sha256=hashlib.sha256(data).hexdigest(),
        delimiter=delimiter,
    )


def parse_amount(text: str) -> tuple[Decimal | None, RowProblem | None]:
    """Read an amount such as '1,234.50', '$75', '(20.00)' or '75 USD'. Blank means missing (no problem)."""
    original = text.strip()
    if not original:
        return None, None
    s = original
    negative = s.startswith("(") and s.endswith(")")
    if negative:
        s = s[1:-1]
    s = _AMOUNT_DECORATION.sub("", s)
    if _THOUSANDS.fullmatch(s):
        s = s.replace(",", "")
    if not _PLAIN_NUMBER.fullmatch(s):
        hint = " Use a dot for cents, for example 1234.50." if "," in s else ""
        return None, RowProblem("AMOUNT_UNREADABLE", "amount", f"'{original}' is not a number.{hint}")
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None, RowProblem("AMOUNT_UNREADABLE", "amount", f"'{original}' is not a number.")
    if negative:
        value = -value
    if value != value.quantize(_CENT):
        return None, RowProblem("AMOUNT_TOO_PRECISE", "amount", f"'{original}' has more than two decimal places.")
    if abs(value) >= _AMOUNT_LIMIT:
        return None, RowProblem("AMOUNT_UNREADABLE", "amount", f"'{original}' is too large to be a payout amount.")
    return value.quantize(_CENT), None


# ---------------------------------------------------------------------------


def _decode(data: bytes, notices: list[str]) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        notices.append("The file is not UTF-8, so it was read as Windows-1252 (Excel's default). Check names with accents.")
        return data.decode("cp1252", errors="replace")


def _sniff_delimiter(text: str) -> str:
    sample = text[:8192]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        return ","


def _check_headers(headers: list[str]) -> None:
    seen: dict[str, str] = {}
    for h in headers:
        if not h:
            continue
        key = h.lower()
        if key in seen:
            name = f"'{h}'" if seen[key] == h else f"'{seen[key]}' and '{h}'"
            raise CsvImportError(f"The header row has the column {name} twice. Rename one of them.")
        seen[key] = h


def _resolve_mapping(headers: list[str], explicit: dict[str, str] | None) -> dict[str, str]:
    resolved: dict[str, str] = {}
    if explicit:
        by_lower = {h.lower(): h for h in headers if h}
        for field_name, header in explicit.items():
            if field_name not in FIELD_ALIASES:
                raise CsvImportError(f"'{field_name}' is not a field that can be mapped. Known fields: {', '.join(FIELD_ALIASES)}.")
            actual = by_lower.get(header.strip().lower())
            if actual is None:
                raise CsvImportError(f"The mapping points {FIELD_LABELS[field_name]} at column '{header}', but the file has no such column. Columns found: {_list(headers)}.")
            resolved[field_name] = actual

    taken = set(resolved.values())
    for field_name, aliases in FIELD_ALIASES.items():
        if field_name in resolved:
            continue
        matches = [h for h in headers if h and h not in taken and normalize_header(h) in aliases]
        if len(matches) > 1:
            raise CsvImportError(f"Columns {_list(matches)} all look like the {FIELD_LABELS[field_name]}. Choose one in the column mapping.")
        if matches:
            resolved[field_name] = matches[0]
            taken.add(matches[0])

    missing = [f for f in REQUIRED_FIELDS if f not in resolved]
    if missing:
        wanted = "; ".join(f"{FIELD_LABELS[f]} (for example '{FIELD_ALIASES[f][0]}')" for f in missing)
        raise CsvImportError(f"No column was found for: {wanted}. Columns found: {_list(headers)}. Rename the column or choose it in the column mapping.")
    return resolved


def _parse_row(
    row_number: int,
    record: list[str],
    headers: list[str],
    column_of: dict[str, int],
    default_currency: str,
) -> ParsedRow:
    raw = {h or f"column_{i + 1}": (record[i] if i < len(record) else "") for i, h in enumerate(headers)}
    row = ParsedRow(row_number=row_number, raw=raw)
    if len(record) > len(headers):
        raw["_extra"] = " | ".join(record[len(headers):])
        row.problems.append(RowProblem(
            "MALFORMED_ROW", None,
            f"Row has {len(record)} values but the header has {len(headers)} columns, so its values may be in the "
            f"wrong columns. A common cause is an amount like 1,200.00 that is not in quotes."))

    def cell(field_name: str) -> str:
        i = column_of.get(field_name)
        return record[i].strip() if i is not None and i < len(record) else ""

    receiver = cell("receiver")
    if receiver.lower().startswith("mailto:"):
        receiver = receiver[7:]
    row.receiver = receiver.lower() or None
    row.recipient_name = " ".join(cell("recipient_name").split()) or None
    row.invoice_id = cell("invoice_id") or None
    row.note = cell("note") or None
    row.external_ref = cell("external_ref") or None

    row.amount, problem = parse_amount(cell("amount"))
    if problem:
        row.problems.append(problem)

    currency = cell("currency").upper()
    if not currency:
        row.currency = default_currency
    elif re.fullmatch(r"[A-Z]{3}", currency):
        row.currency = currency
    else:
        row.problems.append(RowProblem("CURRENCY_UNREADABLE", "currency", f"'{cell('currency')}' is not a three-letter currency code such as USD."))

    country = cell("country_code").upper()
    if re.fullmatch(r"[A-Z]{2}", country):
        row.country_code = country
    elif country:
        row.problems.append(RowProblem("COUNTRY_UNREADABLE", "country_code", f"'{cell('country_code')}' is not a two-letter country code such as KE; it was ignored."))
    return row


def _list(names: list[str]) -> str:
    return ", ".join(f"'{n}'" for n in names if n) or "(none)"

"""PayPal sandbox bulk-account files for the contractors in a batch.

PayPal has no API for creating sandbox accounts, but the developer dashboard
(developer.paypal.com > Sandbox accounts > Bulk create) takes a file of up to 40.
Once each contractor has a sandbox Personal account, payouts to them end SUCCESS
instead of UNCLAIMED. File rules: developer.paypal.com/tools/sandbox/bulk-accounts/
The files copy PayPal's own sample (developer.paypal.com/sandbox-bulk-headers.csv):
plain UTF-8, LF line endings, a positive balance. The page asks for UTF-16, but the
dashboard rejected UTF-16 files.
"""
from __future__ import annotations

import csv
import io
import re
import secrets
import string
import zipfile

HEADER = ["country_code", "account_type", "primary_email_alias", "password", "first_name", "last_name",
          "ppBalance", "addBank", "ccType", "payment_card"]
MAX_PER_FILE = 40  # PayPal's limit per upload
# Rows the rules already rejected for their email: PayPal would refuse them too.
BAD_EMAIL_FLAGS = {"MISSING_EMAIL", "INVALID_EMAIL"}
_NAME_PART = re.compile(r"[^A-Za-z' -]")
_SYMBOLS = "!@#"
BALANCE = "100"


def password() -> str:
    """10 characters with upper, lower, a digit and a symbol, and no run of 4 repeated
    or consecutive characters, as the bulk-upload page asks."""
    alphabet = string.ascii_letters + string.digits + _SYMBOLS
    while True:
        p = "".join(secrets.choice(alphabet) for _ in range(10))
        if (any(c.isupper() for c in p) and any(c.islower() for c in p) and any(c.isdigit() for c in p)
                and any(c in _SYMBOLS for c in p) and not _has_run(p)):
            return p


def _has_run(p: str) -> bool:
    for i in range(len(p) - 3):
        steps = {ord(p[j + 1]) - ord(p[j]) for j in range(i, i + 3)}
        if steps in ({0}, {1}, {-1}):
            return True
    return False


def _names(full_name: str | None) -> tuple[str, str]:
    parts = _NAME_PART.sub("", full_name or "").split()
    if not parts:
        return "Pay", "Contractor"
    return parts[0], " ".join(parts[1:]) or "Contractor"


def accounts(items: list[dict]) -> list[dict]:
    """One sandbox Personal account per distinct, usable email in the batch, in row order.
    An email PayPal already paid with SUCCESS has an account, and PayPal refuses duplicates."""
    seen = {(it.get("receiver") or "").strip().lower() for it in items if it.get("payout_status") == "SUCCESS"}
    out = []
    for it in items:
        email = (it.get("receiver") or "").strip()
        if (it.get("receiver_type") or "EMAIL") != "EMAIL" or not email or email.lower() in seen:
            continue
        if BAD_EMAIL_FLAGS & set(it.get("flag_codes") or []):
            continue
        seen.add(email.lower())
        first, last = _names(it.get("recipient_name"))
        out.append({"country_code": "US", "account_type": "PERSONAL", "primary_email_alias": email,
                    "password": password(), "first_name": first, "last_name": last, "ppBalance": BALANCE,
                    "addBank": "N", "ccType": "VISA", "payment_card": "PAYPAL"})
    return out


def file_bytes(rows: list[dict]) -> bytes:
    """Comma-separated UTF-8 with LF line endings and no byte-order mark, like PayPal's sample."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=HEADER, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def zip_bytes(batch_no: int, rows: list[dict]) -> bytes:
    chunks = [rows[i:i + MAX_PER_FILE] for i in range(0, len(rows), MAX_PER_FILE)]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for n, chunk in enumerate(chunks, 1):
            z.writestr(f"batch-{batch_no}-sandbox-accounts-{n}-of-{len(chunks)}.csv", file_bytes(chunk))
        z.writestr("README.txt", _readme(batch_no, len(rows), len(chunks)))
    return out.getvalue()


def _readme(batch_no: int, count: int, files: int) -> str:
    return f"""Sandbox accounts for batch #{batch_no}: {count} contractors in {files} file(s) of up to {MAX_PER_FILE}.

1. Open https://developer.paypal.com/dashboard/accounts (Sandbox accounts).
2. Choose Bulk create (upload file) and upload one file. Repeat for each file.
3. Send the batch again (or retry its failed rows): payouts to these emails now end SUCCESS
   instead of UNCLAIMED.

Every account is a US Personal account with a ${BALANCE} balance. Its password is in the file.
Upload the files as they are: opening and saving them in Excel can change the format
and PayPal will reject them.
If PayPal says an email is already taken (for example an account you created earlier),
delete that row in a text editor and upload the file again.
"""

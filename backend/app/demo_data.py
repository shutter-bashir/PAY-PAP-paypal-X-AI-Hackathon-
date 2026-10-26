"""Deterministic demo data: a 250-recipient directory and a 250-row payout CSV
worth about $18,500, with known problems planted in it.

The same seed always produces the same file, so the demo video, the tests and
the AI explanations all see identical data.
"""
from __future__ import annotations

import csv
import io
import random
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

SEED = 2026
RECIPIENT_COUNT = 250
ROW_COUNT = 250
TARGET_TOTAL = Decimal("18500.00")
OUTSIZED_AMOUNT = Decimal("985.00")  # just under the $1,000 per-person cap: only the outlier rule catches it
CSV_HEADER = ["Contractor ID", "Name", "PayPal Email", "Amount (USD)", "Invoice Number", "Country", "Note"]

FIRST_NAMES = [
    "Amina", "Brian", "Chen", "Daniela", "Emeka", "Fatima", "Gabriel", "Hana", "Ivan", "Jabari", "Kavya", "Liam",
    "Mariam", "Njeri", "Omar", "Priya", "Quinn", "Rosa", "Samuel", "Thandiwe", "Umar", "Valentina", "Wanjiru",
    "Xavier", "Yusuf", "Zainab", "Akira", "Bianca", "Chidi", "Dmitri", "Esther", "Farid", "Grace", "Hassan",
    "Imani", "Joao", "Kofi", "Leila", "Mateo", "Nadia",
]
LAST_NAMES = [
    "Achieng", "Banda", "Costa", "Diallo", "Eze", "Fernandes", "Gupta", "Hassan", "Ibrahim", "Juma", "Kamau",
    "Lopez", "Mwangi", "Nakamura", "Okafor", "Patel", "Quispe", "Rossi", "Silva", "Tanaka", "Usman", "Varga",
    "Wekesa", "Xu", "Yilmaz", "Zulu", "Otieno", "Mensah", "Novak", "Kariuki",
]
COUNTRIES = ["KE", "NG", "GH", "ZA", "IN", "PH", "BR", "MX", "PK", "EG", "UG", "TZ"]
WORK = ["Logo design", "Copywriting", "Data entry", "Translation", "Video editing", "Bug fixes", "Social media",
        "Illustration", "Transcription", "QA testing", "Web content", "Research"]
DOMAINS = ["example.com", "example.org", "example.net"]


@dataclass
class DemoRecipient:
    external_ref: str
    full_name: str
    email: str
    country_code: str
    vetting_status: str = "approved"


@dataclass
class DemoData:
    recipients: list[DemoRecipient]
    rows: list[dict]  # CSV rows keyed by CSV_HEADER
    planted: dict[str, list[int]] = field(default_factory=dict)  # problem -> spreadsheet row numbers

    def csv_bytes(self) -> bytes:
        buf = io.StringIO(newline="")
        writer = csv.DictWriter(buf, fieldnames=CSV_HEADER, lineterminator="\n")
        writer.writeheader()
        writer.writerows(self.rows)
        return buf.getvalue().encode("utf-8")

    def directory_rows(self) -> list[dict]:
        return [{"receiver_type": "EMAIL", "receiver": r.email, "full_name": r.full_name, "country_code": r.country_code,
                 "external_ref": r.external_ref, "vetting_status": r.vetting_status} for r in self.recipients]


def generate(seed: int = SEED, sandbox_receivers: list[str] | None = None, invoice_prefix: str = "INV-202610",
             email_domain: str | None = None) -> DemoData:
    """sandbox_receivers replace the emails of the first clean recipients, so those
    payouts land in real sandbox accounts (and show SUCCESS instead of UNCLAIMED).
    Once a file has been paid, the same invoices are blocked as already paid; pass a
    new invoice_prefix to make a fresh file for another run. email_domain puts every
    contractor on one domain (e.g. paypap.com) so matching sandbox accounts can be made."""
    rng = random.Random(seed)
    domains = [email_domain] if email_domain else DOMAINS
    # A different contractor-ID series per domain, so two demo directories can sit side by side.
    id_prefix = "P" if email_domain else "C"
    recipients = _recipients(rng, sandbox_receivers or [], domains, id_prefix)

    # One row per recipient for most of the directory; the planted extras fill the rest.
    extras = 6  # 3 repeated claims, 1 shared invoice, 2 second invoices
    newcomer = DemoRecipient(f"{id_prefix}-0999", "Lerato Mokoena", f"lerato.mokoena@{domains[-1]}", "ZA", "pending")
    billed = recipients[:ROW_COUNT - extras - 1] + [newcomer]
    rows = [_row(r, f"{invoice_prefix}-{i + 1:04d}", _base_amount(rng), rng) for i, r in enumerate(billed)]

    planted: dict[str, list[int]] = {}

    def row_no(index: int) -> int:
        return index + 2  # header is spreadsheet row 1

    def plant(problem: str, index: int) -> None:
        planted.setdefault(problem, []).append(row_no(index))

    # Index ranges are chosen so no row carries two planted problems.
    for i in (12, 57, 140):  # the same invoice claimed twice by the same person
        rows.append(dict(rows[i]))
        plant("duplicate_invoice", i)
        plant("duplicate_invoice", len(rows) - 1)
    rows.append({**_row(recipients[90], rows[33]["Invoice Number"], _base_amount(rng), rng)})
    plant("shared_invoice", 33)
    plant("shared_invoice", len(rows) - 1)
    for i in (75, 160):  # same person, a second, different invoice
        rows.append(_row(recipients[i], f"{invoice_prefix}-{9000 + i}", _base_amount(rng), rng))
        plant("duplicate_recipient", i)
        plant("duplicate_recipient", len(rows) - 1)

    for i in (20, 98, 175, 230):
        rows[i]["PayPal Email"] = ""
        plant("missing_email", i)
    rows[45]["PayPal Email"] = rows[45]["PayPal Email"].split(".")[0] + "@gmail"
    plant("invalid_email", 45)
    rows[121]["PayPal Email"] = rows[121]["PayPal Email"].replace("@", "@@")
    plant("invalid_email", 121)
    rows[199]["Amount (USD)"] = "-45.00"
    plant("negative_amount", 199)
    rows[210]["Amount (USD)"] = "12,50"
    plant("unreadable_amount", 210)
    for i in (66, 188):
        rows[i]["Invoice Number"] = ""
        plant("missing_invoice", i)
    rows[150]["Name"] = "Kevin Odhiambo"
    plant("name_mismatch", 150)
    plant("blocked_recipient", 8)
    plant("unvetted_recipient", 29)
    plant("unvetted_recipient", 112)
    plant("new_recipient", len(billed) - 1)

    _scale_to_target(rows, skip={199, 210})
    rows[183]["Amount (USD)"] = f"{OUTSIZED_AMOUNT:.2f}"
    plant("outsized_amount", 183)
    _scale_to_target(rows, skip={199, 210, 183})

    assert len(rows) == ROW_COUNT and len(recipients) == RECIPIENT_COUNT
    return DemoData(recipients=recipients, rows=rows, planted=planted)


# ---------------------------------------------------------------------------


def _recipients(rng: random.Random, sandbox_receivers: list[str], domains: list[str],
                id_prefix: str = "C") -> list[DemoRecipient]:
    names = [(f, l) for f in FIRST_NAMES for l in LAST_NAMES]
    rng.shuffle(names)
    people = []
    for i, (first, last) in enumerate(names[:RECIPIENT_COUNT]):
        email = f"{first.lower()}.{last.lower()}@{domains[i % len(domains)]}"
        people.append(DemoRecipient(f"{id_prefix}-{i + 1:04d}", f"{first} {last}", email, rng.choice(COUNTRIES)))
    people[8].vetting_status = "blocked"
    people[29].vetting_status = "pending"
    people[112].vetting_status = "pending"
    clean = [i for i in range(RECIPIENT_COUNT) if i not in _PLANTED_INDEXES]
    for index, email in zip(clean, sandbox_receivers):
        people[index].email = email.strip().lower()
        people[index].full_name = name_from_email(people[index].email)
    return people


def name_from_email(email: str) -> str:
    """'Vic-don-doom22@gmail.com' -> 'Vic Don Doom', so a sandbox account gets a matching name."""
    words = re.split(r"[._\-+\d]+", email.split("@")[0])
    return " ".join(w.capitalize() for w in words if w) or email


# Recipients whose rows carry a planted problem; sandbox emails are never put on these.
_PLANTED_INDEXES = {8, 12, 20, 29, 33, 45, 57, 66, 75, 90, 98, 112, 121, 140, 150, 160, 175, 183, 188, 199, 210, 230}


def _row(r: DemoRecipient, invoice: str, amount: Decimal, rng: random.Random) -> dict:
    return {"Contractor ID": r.external_ref, "Name": r.full_name, "PayPal Email": r.email,
            "Amount (USD)": f"{amount:.2f}", "Invoice Number": invoice, "Country": r.country_code,
            "Note": f"{rng.choice(WORK)}, October 2026"}


def _base_amount(rng: random.Random) -> Decimal:
    return Decimal(str(rng.triangular(30, 120, 70))).quantize(Decimal("0.50"), ROUND_HALF_UP)


def _scale_to_target(rows: list[dict], skip: set[int]) -> None:
    """Scale the ordinary amounts so every positive amount adds up to TARGET_TOTAL exactly."""
    fixed = sum((Decimal(rows[i]["Amount (USD)"]) for i in skip if _is_positive(rows[i]["Amount (USD)"])), Decimal(0))
    adjustable = [i for i in range(len(rows)) if i not in skip]
    current = sum(Decimal(rows[i]["Amount (USD)"]) for i in adjustable)
    factor = (TARGET_TOTAL - fixed) / current
    running = Decimal(0)
    for i in adjustable[:-1]:
        value = (Decimal(rows[i]["Amount (USD)"]) * factor).quantize(Decimal("0.01"), ROUND_HALF_UP)
        rows[i]["Amount (USD)"] = f"{value:.2f}"
        running += value
    rows[adjustable[-1]]["Amount (USD)"] = f"{TARGET_TOTAL - fixed - running:.2f}"


def _is_positive(text: str) -> bool:
    try:
        return Decimal(text) > 0
    except Exception:
        return False


def clean_recipients(data: DemoData, count: int) -> list[DemoRecipient]:
    """The first `count` recipients whose rows carry no planted problem: the ones to create as sandbox accounts."""
    return [r for i, r in enumerate(data.recipients) if i not in _PLANTED_INDEXES][:count]

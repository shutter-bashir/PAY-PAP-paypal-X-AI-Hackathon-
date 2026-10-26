"""PayPal sandbox bulk-account files built from a batch."""
import csv
import io
import zipfile

from app import sandbox_accounts as sa
from tests.test_api import bearer, client  # noqa: F401  (fixture)


def item(row, email, name="Ada Lovelace", flags=()):
    return {"row_number": row, "receiver_type": "EMAIL", "receiver": email, "recipient_name": name,
            "flag_codes": list(flags)}


def read_file(data: bytes) -> list[dict]:
    assert not data.startswith((b"\xef\xbb\xbf", b"\xff\xfe")) and b"\r" not in data
    return list(csv.DictReader(io.StringIO(data.decode("utf-8"))))


def test_one_account_per_usable_email():
    rows = sa.accounts([
        item(2, "ada@paypap.com"),
        item(3, "ADA@paypap.com"),  # same person again
        item(4, "bad-email", flags=["INVALID_EMAIL"]),
        item(5, "", flags=["MISSING_EMAIL"]),
        item(6, "grace@paypap.com", name="Grace Brewster Hopper"),
        item(7, "x@paypap.com", name=None),
    ])
    assert [r["primary_email_alias"] for r in rows] == ["ada@paypap.com", "grace@paypap.com", "x@paypap.com"]
    assert (rows[1]["first_name"], rows[1]["last_name"]) == ("Grace", "Brewster Hopper")
    assert (rows[2]["first_name"], rows[2]["last_name"]) == ("Pay", "Contractor")
    assert all(r["account_type"] == "PERSONAL" for r in rows)


def test_emails_already_paid_have_an_account_and_are_skipped():
    paid = {**item(2, "joe-doe@gmail.com"), "payout_status": "SUCCESS"}
    rows = sa.accounts([paid, item(3, "Joe-Doe@gmail.com"), {**item(4, "new@paypap.com"), "payout_status": "UNCLAIMED"}])
    assert [r["primary_email_alias"] for r in rows] == ["new@paypap.com"]


def test_passwords_follow_paypal_rules():
    for _ in range(200):
        p = sa.password()
        assert len(p) == 10 and not sa._has_run(p)
        assert any(c.isupper() for c in p) and any(c.islower() for c in p)
        assert any(c.isdigit() for c in p) and any(c in "!@#" for c in p)
    assert sa._has_run("ab1111cd") and sa._has_run("xx1234yy") and sa._has_run("zdcba")


def test_file_matches_paypals_sample_layout():
    rows = sa.accounts([item(2, "a@paypap.com", name="Lee, Jr")])
    data = sa.file_bytes(rows)
    assert data.decode("utf-8").splitlines()[0] == ",".join(sa.HEADER)
    row = read_file(data)[0]
    assert row["primary_email_alias"] == "a@paypap.com" and int(row["ppBalance"]) > 0


def test_zip_splits_into_files_of_40():
    rows = sa.accounts([item(i, f"c{i}@paypap.com") for i in range(2, 102)])  # 100 contractors
    with zipfile.ZipFile(io.BytesIO(sa.zip_bytes(7, rows))) as z:
        names = sorted(n for n in z.namelist() if n.endswith(".csv"))
        assert names == [f"batch-7-sandbox-accounts-{n}-of-3.csv" for n in (1, 2, 3)]
        assert [len(read_file(z.read(n))) for n in names] == [40, 40, 20]
        assert "README.txt" in z.namelist()


def test_endpoint_returns_the_zip(client):  # noqa: F811
    client.store.get_batch = lambda batch_id: {"id": batch_id, "batch_no": 5}
    client.store.items = lambda batch_id: [item(2, "ada@paypap.com")]
    response = client.get("/api/batches/b1/sandbox-accounts", headers=bearer(client, "admin@paypap.demo", "admin-pass"))
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    assert "batch-5-sandbox-accounts.zip" in response.headers["content-disposition"]


def test_endpoint_refuses_a_batch_without_emails(client):  # noqa: F811
    client.store.get_batch = lambda batch_id: {"id": batch_id, "batch_no": 5}
    client.store.items = lambda batch_id: [item(2, "", flags=["MISSING_EMAIL"])]
    response = client.get("/api/batches/b1/sandbox-accounts", headers=bearer(client, "admin@paypap.demo", "admin-pass"))
    assert response.status_code == 422

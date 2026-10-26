"""HTTP layer: sign-in, roles, and the webhook signature gate."""
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import main
from app.config import settings

USERS = {
    "admin@paypap.demo": {"id": "u-admin", "email": "admin@paypap.demo", "role": "admin", "display_name": "Demo Admin"},
    "approver@paypap.demo": {"id": "u-appr", "email": "approver@paypap.demo", "role": "approver",
                             "display_name": "Demo Approver"},
}


class FakeStore:
    def __init__(self):
        self.audits = []

    def user_by_email(self, email):
        return USERS.get(email.lower())

    def write_audit(self, **kwargs):
        self.audits.append(kwargs)


class FakePayPal:
    def __init__(self, genuine):
        self.genuine = genuine

    def verify_webhook_signature(self, **kwargs):
        return self.genuine


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "demo_admin_email", "admin@paypap.demo")
    monkeypatch.setattr(settings, "demo_approver_email", "approver@paypap.demo")
    monkeypatch.setattr(settings, "demo_admin_password", SecretStr("admin-pass"))
    monkeypatch.setattr(settings, "demo_approver_password", SecretStr("approver-pass"))
    monkeypatch.setattr(settings, "jwt_secret", SecretStr("jwt-test-secret-that-is-long-enough-for-hs256"))
    store = FakeStore()
    main.app.dependency_overrides[main.get_store] = lambda: store
    with TestClient(main.app) as c:
        c.store = store
        yield c
    main.app.dependency_overrides.clear()


def login(client, email, password):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def bearer(client, email, password):
    return {"Authorization": f"Bearer {login(client, email, password).json()['token']}"}


def test_login_and_me(client):
    response = login(client, "Admin@PayPap.demo", "admin-pass")
    assert response.status_code == 200 and response.json()["user"]["role"] == "admin"
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {response.json()['token']}"})
    assert me.json()["email"] == "admin@paypap.demo"


def test_wrong_password_and_unknown_user(client):
    assert login(client, "admin@paypap.demo", "nope").status_code == 401
    assert login(client, "someone@example.com", "admin-pass").status_code == 401


def test_requests_without_a_token_are_refused(client):
    assert client.get("/api/batches").status_code == 401
    assert client.get("/api/batches", headers={"Authorization": "Bearer junk"}).status_code == 401


def test_only_the_approver_can_approve(client):
    headers = bearer(client, "admin@paypap.demo", "admin-pass")
    response = client.post("/api/batches/b1/approve", json={"item_count": 1, "total_amount": "10.00"}, headers=headers)
    assert response.status_code == 403


def test_webhook_with_a_bad_signature_is_rejected_and_audited(client, monkeypatch):
    monkeypatch.setattr(main, "get_paypal", lambda: FakePayPal(genuine=False))
    response = client.post("/webhooks/paypal", json={"id": "WH-1", "event_type": "PAYMENT.PAYOUTS-ITEM.SUCCEEDED"})
    assert response.status_code == 400
    assert client.store.audits[0]["action"] == "webhook.rejected"


def test_webhook_needs_a_json_event(client):
    assert client.post("/webhooks/paypal", content=b"not json").status_code == 400
    assert client.post("/webhooks/paypal", json={"no": "id"}).status_code == 400


def test_genuine_webhook_is_processed(client, monkeypatch):
    monkeypatch.setattr(main, "get_paypal", lambda: FakePayPal(genuine=True))
    monkeypatch.setattr(main.payouts, "process_webhook_event", lambda store, event: "item x: updated")
    response = client.post("/webhooks/paypal", json={"id": "WH-2", "event_type": "PAYMENT.PAYOUTS-ITEM.SUCCEEDED"})
    assert response.status_code == 200 and response.json()["result"] == "item x: updated"


def test_webhooks_are_handled_in_parallel(client, monkeypatch):
    """PayPal sends a batch's events at once. Each one makes slow calls (signature check,
    database), so they must not queue behind each other or PayPal times out."""
    import asyncio
    import threading
    import time

    import httpx

    class SlowPayPal:
        def verify_webhook_signature(self, **kwargs):
            time.sleep(0.5)  # a real round trip to PayPal
            return True

    seen_threads = set()

    def process(store, event):
        seen_threads.add(threading.get_ident())
        return "updated"

    monkeypatch.setattr(main, "get_paypal", lambda: SlowPayPal())
    monkeypatch.setattr(main.payouts, "process_webhook_event", process)

    async def burst():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            return await asyncio.gather(*[
                http.post("/webhooks/paypal", json={"id": f"WH-{i}", "event_type": "PAYMENT.PAYOUTS-ITEM.UNCLAIMED"})
                for i in range(10)])

    started = time.perf_counter()
    responses = asyncio.run(burst())
    elapsed = time.perf_counter() - started
    assert all(r.status_code == 200 for r in responses)
    assert elapsed < 2.5, f"10 webhooks took {elapsed:.1f}s; they are being handled one at a time"
    assert len(seen_threads) > 1


def test_store_gives_each_thread_its_own_client():
    import threading

    from app.store import PerThreadClient

    class Client:
        def table(self, name):
            return self

    made: list[Client] = []  # holding references keeps each client alive for the identity checks

    def factory():
        made.append(Client())
        return made[-1]

    per_thread = PerThreadClient(factory)
    used = []
    threads = [threading.Thread(target=lambda: used.append(per_thread.table("x"))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(made) == 4 and all(any(u is m for m in made) for u in used) and len({id(u) for u in used}) == 4
    assert per_thread.table("x") is per_thread.table("x")  # reused within one thread


def test_retry_failed_rebuilds_a_csv_from_only_the_failed_rows(client, monkeypatch):
    from types import SimpleNamespace

    store = client.store
    store.get_batch = lambda batch_id: {"id": batch_id, "batch_no": 7, "column_mapping": {"receiver": "PayPal Email"}}
    store.items = lambda batch_id: [
        {"id": "a", "row_number": 2, "reconciliation_bucket": "paid"},
        {"id": "b", "row_number": 7, "reconciliation_bucket": "failed"},
    ]
    store.raw_rows = lambda batch_id, ids: [
        {"id": i, "raw": {"PayPal Email": "jabari@paypap.com", "Amount (USD)": "106.39", "_extra": ""}} for i in ids]
    captured = {}

    def fake_import(store_, settings_, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(batch_id="new-batch", rows=1)

    monkeypatch.setattr(main, "import_csv", fake_import)
    headers = bearer(client, "admin@paypap.demo", "admin-pass")
    response = client.post("/api/batches/b7/retry-failed", headers=headers)
    assert response.status_code == 200 and response.json()["batch_id"] == "new-batch"
    assert captured["data"].decode() == "PayPal Email,Amount (USD)\njabari@paypap.com,106.39\n"
    assert captured["name"] == "Retry of #7 (1 failed)" and captured["mapping"] == {"receiver": "PayPal Email"}

    store.items = lambda batch_id: [{"id": "a", "row_number": 2, "reconciliation_bucket": "paid"}]
    assert client.post("/api/batches/b7/retry-failed", headers=headers).status_code == 409


def test_meta_shows_limits_but_no_secrets(client):
    body = client.get("/api/meta", headers=bearer(client, "admin@paypap.demo", "admin-pass")).json()
    assert body["limits"]["max_amount_per_recipient"] and "ai" in body
    text = str(body).lower()
    assert "secret" not in text and "gsk_" not in text and "password" not in text


def test_voice_endpoint_checks_size_and_returns_text(client, monkeypatch):
    headers = bearer(client, "admin@paypap.demo", "admin-pass")
    monkeypatch.setattr(main.ai, "transcribe", lambda settings_, data, content_type: f"heard {len(data)} bytes of {content_type}")
    ok = client.post("/api/voice/transcribe", headers=headers, files={"audio": ("q.webm", b"a" * 4000, "audio/webm")})
    assert ok.status_code == 200 and ok.json()["text"] == "heard 4000 bytes of audio/webm"
    empty = client.post("/api/voice/transcribe", headers=headers, files={"audio": ("q.webm", b"a" * 10, "audio/webm")})
    assert empty.status_code == 422 and "empty" in empty.json()["detail"]
    assert client.post("/api/voice/transcribe", files={"audio": ("q.webm", b"a" * 4000, "audio/webm")}).status_code == 401


def test_cors_allows_only_the_listed_frontends(client):
    for origin, allowed in [(main._origins[0], True), ("https://evil.example", False)]:
        r = client.options("/health", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})
        assert (r.headers.get("access-control-allow-origin") == origin) is allowed


def test_cron_tick_needs_the_cron_secret(client, monkeypatch):
    monkeypatch.setattr(settings, "cron_secret", SecretStr("cron-test-secret"))
    monkeypatch.setattr(main, "get_paypal", lambda: object())
    monkeypatch.setattr(main.worker, "poll_once", lambda store, paypal: 2)
    monkeypatch.setattr(main.worker, "expire_approvals", lambda store: 1)
    assert client.get("/api/cron/tick").status_code == 401
    assert client.get("/api/cron/tick", headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = client.get("/api/cron/tick", headers={"Authorization": "Bearer cron-test-secret"})
    assert response.json() == {"polled": 2, "approvals_expired": 1}


def test_cron_tick_is_off_without_a_secret(client, monkeypatch):
    monkeypatch.setattr(settings, "cron_secret", SecretStr(""))
    assert client.get("/api/cron/tick", headers={"Authorization": "Bearer "}).status_code == 401


def test_the_bare_address_points_to_health_and_docs(client):
    body = client.get("/").json()
    assert body["status"] == "running" and body["health"] == "/health" and body["docs"] == "/docs"

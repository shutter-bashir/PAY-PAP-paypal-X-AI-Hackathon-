"""HTTP API. Run from backend/:  uvicorn app.main:app --reload"""
from __future__ import annotations

import atexit
import csv
import hmac
import io
import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Literal

import httpx
import jwt
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app import ai, approvals, payouts, sandbox_accounts, worker
from app.config import settings
from app.csv_import import CsvImportError, MAX_FILE_BYTES, parse_csv
from app.importer import import_csv
from app.paypal_balances import BalanceReader
from app.paypal_client import PayPalClient, PayPalError
from app.store import Store, StoreError

log = logging.getLogger("paypap.api")
logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="Pay pap API", version="0.1.0")
# FRONTEND_URL may list several sites, comma-separated (e.g. the Vercel address and localhost).
# A bare host name gets https://; browsers send a full origin.
_origins = [(o if "://" in o else f"https://{o}").rstrip("/") for o in map(str.strip, settings.frontend_url.split(",")) if o]
app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


# --- Dependencies --------------------------------------------------------------


@lru_cache
def get_store() -> Store:
    return Store.from_settings(settings)


@lru_cache
def get_paypal() -> PayPalClient:
    return PayPalClient.from_settings(settings)


@lru_cache
def get_balances() -> BalanceReader:
    """One APIMatic SDK client per process: it keeps the connection pool and the OAuth token."""
    reader = BalanceReader.from_settings(settings)
    atexit.register(reader.close)
    return reader


def get_assistant() -> ai.Assistant:
    return ai.Assistant.from_settings(settings)


class SharedPayPal:
    """Lets the agent use the shared client inside a `with` block without closing it."""

    def __enter__(self) -> PayPalClient:
        return get_paypal()

    def __exit__(self, *exc) -> None:
        pass


class User(BaseModel):
    id: str
    email: str
    role: Literal["admin", "approver"]
    display_name: str


def current_user(request: Request) -> User:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(401, "Sign in first.")
    try:
        claims = jwt.decode(header[7:], settings.jwt_secret.get_secret_value(), algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Your session has expired. Sign in again.")
    return User(id=claims["sub"], email=claims["email"], role=claims["role"], display_name=claims["name"])


def approver_only(user: Annotated[User, Depends(current_user)]) -> User:
    if user.role != "approver":
        raise HTTPException(403, "Only the approver can do this.")
    return user


StoreDep = Annotated[Store, Depends(get_store)]
UserDep = Annotated[User, Depends(current_user)]


@app.exception_handler(StoreError)
def _store_error(_: Request, exc: StoreError):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(ai.AIUnavailable)
def _ai_error(_: Request, exc: ai.AIUnavailable):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


# --- Auth ----------------------------------------------------------------------


class LoginIn(BaseModel):
    email: str
    password: str


@app.post("/api/auth/login")
def login(body: LoginIn, store: StoreDep):
    passwords = {settings.demo_admin_email.lower(): settings.demo_admin_password.get_secret_value(),
                 settings.demo_approver_email.lower(): settings.demo_approver_password.get_secret_value()}
    expected = passwords.get(body.email.strip().lower())
    if not expected or not hmac.compare_digest(expected.encode(), body.password.encode()):
        raise HTTPException(401, "Wrong email or password.")
    row = store.user_by_email(body.email.strip())
    if row is None:
        raise HTTPException(500, "Demo user is missing from the database; run the migration.")
    exp = datetime.now(UTC) + timedelta(minutes=settings.jwt_expire_minutes)
    token = jwt.encode({"sub": row["id"], "email": row["email"], "role": row["role"], "name": row["display_name"],
                        "exp": exp}, settings.jwt_secret.get_secret_value(), algorithm="HS256")
    return {"token": token, "user": {k: row[k] for k in ("id", "email", "role", "display_name")}}


@app.get("/api/auth/me")
def me(user: UserDep):
    return user


@app.get("/health")
def health():
    return {"ok": True, "paypal_mode": settings.paypal_mode, "ai_provider": settings.ai_provider,
            "ai_model": settings.ai_model_name, "ai_enabled": settings.ai_enabled,
            "missing_keys": settings.missing_required()}


# --- Batches -------------------------------------------------------------------


@app.get("/api/batches")
def list_batches(store: StoreDep, _: UserDep):
    return store.list_batches()


@app.post("/api/batches/preview")
async def preview_csv(_: UserDep, file: UploadFile = File(...), mapping: str | None = Form(None)):
    """Reads a CSV without saving anything: detected columns, the mapping, and the first rows."""
    data = await _read_upload(file)
    try:
        parsed = parse_csv(data, mapping=_mapping(mapping), default_currency=settings.paypal_currency)
    except CsvImportError as exc:
        raise HTTPException(422, str(exc))
    headers = list(parsed.rows[0].raw.keys()) if parsed.rows else []
    return {
        "headers": [h for h in headers if h != "_extra"], "mapping": parsed.mapping,
        "unmapped_columns": parsed.unmapped_columns, "notices": parsed.notices, "row_count": len(parsed.rows),
        "problems": [{"row": r.row_number, "code": p.code, "message": p.message} for r in parsed.rows for p in r.problems][:50],
        "sample": [{"row": r.row_number, "receiver": r.receiver, "name": r.recipient_name,
                    "amount": str(r.amount) if r.amount is not None else None, "invoice": r.invoice_id}
                   for r in parsed.rows[:8]],
    }


@app.post("/api/batches")
async def create_batch(store: StoreDep, user: UserDep, background: BackgroundTasks, file: UploadFile = File(...),
                       name: str | None = Form(None), mapping: str | None = Form(None)):
    data = await _read_upload(file)
    try:
        summary = await run_in_threadpool(  # the import talks to Supabase; keep the event loop free
            import_csv, store, settings, data=data, filename=file.filename or "upload.csv", created_by=user.id,
            name=name or None, mapping=_mapping(mapping))
    except CsvImportError as exc:
        raise HTTPException(422, str(exc))
    if settings.ai_enabled:
        background.add_task(_explain_quietly, summary.batch_id)
    return summary.__dict__


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str, store: StoreDep, _: UserDep):
    batch = store.get_batch(batch_id)
    approval = store.live_approval(batch_id)
    return {
        "batch": {k: batch[k] for k in ("id", "batch_no", "name", "status", "currency", "sender_batch_id",
                                        "source_filename", "column_mapping", "import_errors", "rules_version",
                                        "ai_summary", "ai_model", "ai_summarized_at", "paypal_payout_batch_id",
                                        "paypal_batch_status", "created_at", "submitted_at", "completed_at",
                                        "last_polled_at", "created_by")},
        "overview": store.batch_overview(batch_id),
        "approval": approval and {**{k: approval[k] for k in ("id", "approved_by", "item_count", "total_amount",
                                                              "currency", "excluded_count", "warned_count", "comment",
                                                              "expires_at", "created_at", "consumed_at")},
                                  "approved_by_name": (store.user(approval["approved_by"]) or {}).get("display_name"),
                                  "signature_valid": approvals.verify(settings.approval_signing_secret.get_secret_value(), approval)},
        "submissions": store.submissions(batch_id),
        "batch_flags": [f for f in store.flags(batch_id) if not f["batch_item_id"]],
    }


@app.get("/api/batches/{batch_id}/items")
def get_items(batch_id: str, store: StoreDep, _: UserDep):
    flags_by_item: dict[str, list] = {}
    for f in store.flags(batch_id):
        if f["batch_item_id"]:
            flags_by_item.setdefault(f["batch_item_id"], []).append(
                {k: f[k] for k in ("id", "code", "severity", "message", "evidence", "ai_explanation")})
    return [{**i, "flags": flags_by_item.get(i["id"], [])} for i in store.items(batch_id)]


@app.get("/api/batches/{batch_id}/sandbox-accounts")
def get_sandbox_accounts(batch_id: str, store: StoreDep, _: UserDep):
    """A zip of PayPal bulk-create files, so every contractor in the batch can get a sandbox account."""
    batch_no = store.get_batch(batch_id)["batch_no"]
    rows = sandbox_accounts.accounts(store.items(batch_id))
    if not rows:
        raise HTTPException(422, "This batch has no usable PayPal emails.")
    return Response(sandbox_accounts.zip_bytes(batch_no, rows), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="batch-{batch_no}-sandbox-accounts.zip"'})


class Decision(BaseModel):
    row_number: int
    decision: Literal["include", "exclude"]
    reason: str | None = None


class DecisionsIn(BaseModel):
    decisions: list[Decision] = Field(min_length=1, max_length=15000)


@app.post("/api/batches/{batch_id}/decisions")
def set_decisions(batch_id: str, body: DecisionsIn, store: StoreDep, user: UserDep):
    changed = store.set_item_decisions(batch_id, user.id, [d.model_dump() for d in body.decisions])
    return {"changed": changed, "overview": store.batch_overview(batch_id)}


@app.post("/api/batches/{batch_id}/explain")
def explain(batch_id: str, store: StoreDep, _: UserDep):
    output = ai.explain_batch(store, get_assistant(), batch_id)
    return {"summary": output.summary.model_dump(), "explained": len(output.explanations)}


class FilterIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)


@app.post("/api/batches/{batch_id}/filter")
def nl_filter(batch_id: str, body: FilterIn, store: StoreDep, user: UserDep):
    spec = get_assistant().translate_filter(body.query)
    rows = ai.apply_filter(store.items(batch_id), spec)
    store.write_audit(actor_type="agent", action="agent.filter_applied", entity_type="batch", entity_id=batch_id,
                      batch_id=batch_id, details={"query": body.query, "filter": spec.model_dump(),
                                                  "matched": len(rows), "asked_by": user.email})
    return {"filter": spec.model_dump(), "row_numbers": [r["row_number"] for r in rows],
            "total": str(sum((Decimal(str(r["amount"])) for r in rows if r.get("amount") is not None), Decimal(0)))}


class AgentIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


@app.post("/api/batches/{batch_id}/agent")
def ask_agent(batch_id: str, body: AgentIn, store: StoreDep, user: UserDep):
    store.get_batch(batch_id)
    tools = ai.AgentTools(store, settings, batch_id=batch_id, user_id=user.id, paypal_factory=SharedPayPal,
                          balances=get_balances())
    reply = get_assistant().run_agent(tools, body.question)
    return {"text": reply.text, "tool_calls": reply.tool_calls, "proposal": reply.proposal}


MAX_AUDIO_BYTES = 4 * 1024 * 1024  # minutes of compressed speech; Vercel refuses request bodies over 4.5 MB


@app.post("/api/voice/transcribe")
async def voice_transcribe(_: UserDep, audio: UploadFile = File(...)):
    """A spoken question, recorded in the browser, turned into text for the Ask box."""
    data = await audio.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "That recording is too long. Keep voice questions under a minute.")
    if len(data) < 500:
        raise HTTPException(422, "The recording was empty. Check that the right microphone is selected.")
    text = await run_in_threadpool(ai.transcribe, settings, data, audio.content_type or "audio/webm")
    return {"text": text}


class ApproveIn(BaseModel):
    item_count: int = Field(gt=0)
    total_amount: Decimal = Field(gt=0)
    comment: str | None = Field(None, max_length=500)


@app.post("/api/batches/{batch_id}/approve")
def approve(batch_id: str, body: ApproveIn, store: StoreDep, user: Annotated[User, Depends(approver_only)]):
    try:
        approval = approvals.approve(store, settings, batch_id=batch_id, approver_id=user.id,
                                     seen_item_count=body.item_count, seen_total=body.total_amount, comment=body.comment)
    except approvals.ApprovalRefused as exc:
        raise HTTPException(409, str(exc))
    return {"approval_id": approval["id"], "expires_at": approval["expires_at"]}


class RevokeIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


@app.post("/api/batches/{batch_id}/revoke")
def revoke(batch_id: str, body: RevokeIn, store: StoreDep, user: UserDep):
    approval = store.live_approval(batch_id)
    if approval is None:
        raise HTTPException(409, "This batch has no live approval.")
    store.revoke_approval(approval["id"], body.reason, user.id)
    return {"status": "in_review"}


@app.post("/api/batches/{batch_id}/cancel")
def cancel(batch_id: str, store: StoreDep, user: UserDep):
    store.update_batch(batch_id, {"status": "cancelled"})
    store.write_audit(actor_type="user", actor_id=user.id, action="batch.cancelled", entity_type="batch",
                      entity_id=batch_id, batch_id=batch_id)
    return {"status": "cancelled"}


@app.post("/api/batches/{batch_id}/submit")
def submit(batch_id: str, store: StoreDep, user: UserDep):
    try:
        result = payouts.submit_batch(store, get_paypal(), batch_id=batch_id,
                                      signing_secret=settings.approval_signing_secret.get_secret_value(),
                                      submitted_by=user.id, balances=get_balances(),
                                      require_funds=settings.paypal_mode == "live")
    except payouts.SubmitRefused as exc:
        raise HTTPException(409, str(exc))
    return result.__dict__


@app.post("/api/batches/{batch_id}/retry-failed")
def retry_failed(batch_id: str, store: StoreDep, user: UserDep):
    """Starts a new batch from the rows PayPal failed or returned, so they can be reviewed and paid again.
    The rules run afresh; a failed payout does not count as paid, so its invoice is not blocked."""
    batch = store.get_batch(batch_id)
    failed = [i for i in store.items(batch_id) if i["reconciliation_bucket"] == "failed"]
    if not failed:
        raise HTTPException(409, "No payouts in this batch failed, so there is nothing to retry.")
    raws = [r["raw"] for r in store.raw_rows(batch_id, [i["id"] for i in failed])]
    headers = [h for h in raws[0] if h != "_extra"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=headers, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(raws)
    try:
        summary = import_csv(store, settings, data=buffer.getvalue().encode("utf-8"),
                             filename=f"retry-of-batch-{batch['batch_no']}.csv", created_by=user.id,
                             name=f"Retry of #{batch['batch_no']} ({len(failed)} failed)",
                             mapping=batch.get("column_mapping"))
    except CsvImportError as exc:
        raise HTTPException(422, str(exc))
    return summary.__dict__


@app.get("/api/meta")
def meta(_: UserDep):
    """Read-only facts for the Rules and caps page. No secrets."""
    from app import rules as rules_module

    return {
        "rules_version": rules_module.RULES_VERSION,
        "limits": {"max_amount_per_recipient": str(settings.max_amount_per_recipient),
                   "max_batch_total": str(settings.max_batch_total), "max_batch_items": settings.max_batch_items,
                   "suspicious_amount_zscore": settings.suspicious_amount_zscore, "min_amount": str(settings.min_amount),
                   "approval_ttl_minutes": settings.approval_ttl_minutes},
        "currency": settings.paypal_currency,
        "paypal_mode": settings.paypal_mode,
        "webhook_configured": bool(settings.paypal_webhook_id),
        "ai": {"provider": settings.ai_provider, "model": settings.ai_model_name, "enabled": settings.ai_enabled},
        "poll_interval_seconds": settings.status_poll_interval_seconds,
    }


@app.get("/api/cron/tick")
def cron_tick(request: Request, store: StoreDep):
    """Vercel Cron's daily safety net: serverless hosting cannot keep the worker running, so this
    syncs batches PayPal is still working on and expires stale approvals."""
    secret = settings.cron_secret.get_secret_value()
    if not secret or not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {secret}"):
        raise HTTPException(401, "Not a scheduled call.")
    return {"polled": worker.poll_once(store, get_paypal()), "approvals_expired": worker.expire_approvals(store)}


@app.get("/api/batches/{batch_id}/preflight")
def batch_preflight(batch_id: str, store: StoreDep, _: UserDep):
    """What sending would run into: included rows PayPal turned away before, and the PayPal balance."""
    check = payouts.preflight(store, get_balances(), batch_id=batch_id, currency=store.get_batch(batch_id)["currency"])
    return {"total": str(check.total), "currency": check.currency, "turned_away": check.turned_away,
            "balance": None if check.balance is None else str(check.balance), "balance_error": check.balance_error,
            "short_by": None if check.short_by is None else str(check.short_by),
            "balance_blocks": settings.paypal_mode == "live"}


@app.post("/api/batches/{batch_id}/sync")
def sync(batch_id: str, store: StoreDep, _: UserDep):
    try:
        result = payouts.sync_batch_status(store, get_paypal(), batch_id=batch_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except PayPalError as exc:
        raise HTTPException(502, str(exc))
    except httpx.HTTPError as exc:  # timeout or network failure reaching PayPal
        raise HTTPException(504, f"Could not reach PayPal: {exc}")
    return result.__dict__


# --- Audit ---------------------------------------------------------------------


@app.get("/api/audit")
def audit(store: StoreDep, _: UserDep, batch_id: str | None = None, before_id: int | None = None, limit: int = 200):
    rows = store.audit(batch_id=batch_id, before_id=before_id, limit=min(max(limit, 1), 1000))
    users = {}
    for r in rows:
        if r["actor_id"] and r["actor_id"] not in users:
            u = store.user(r["actor_id"])
            users[r["actor_id"]] = u["email"] if u else None
        r["actor_email"] = users.get(r["actor_id"])
    return rows


@app.get("/api/audit/verify")
def audit_verify(store: StoreDep, _: UserDep):
    return store.verify_audit_chain()


# --- PayPal webhooks -----------------------------------------------------------


@app.post("/webhooks/paypal")
async def paypal_webhook(request: Request, store: StoreDep):
    raw = await request.body()
    try:
        event = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Body is not JSON.")
    if not isinstance(event, dict) or not event.get("id"):
        raise HTTPException(400, "Not a PayPal event.")
    # The checks below make blocking calls to PayPal and Supabase. Run them on the thread
    # pool: PayPal sends a whole batch's events at once and gives up on slow replies.
    return await run_in_threadpool(_handle_webhook, store, dict(request.headers), event)


def _handle_webhook(store: Store, headers: dict, event: dict) -> dict:
    try:
        genuine = get_paypal().verify_webhook_signature(headers=headers, event=event,
                                                        webhook_id=settings.paypal_webhook_id)
    except PayPalError as exc:
        log.warning("webhook %s: could not verify: %s", event.get("id"), exc)
        raise HTTPException(503, "Could not verify the signature; PayPal will retry.")
    if not genuine:
        store.write_audit(actor_type="system", action="webhook.rejected", entity_type="webhook_event",
                          entity_id=str(event.get("id"))[:100],
                          details={"event_type": str(event.get("event_type"))[:100], "reason": "signature check failed"})
        raise HTTPException(400, "Signature check failed.")
    result = payouts.process_webhook_event(store, event)
    log.info("webhook %s %s: %s", event["id"], event.get("event_type"), result)
    return {"ok": True, "result": result}


# ---------------------------------------------------------------------------


async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, f"The file is larger than {MAX_FILE_BYTES // 1_048_576} MB.")
    return data


def _mapping(text: str | None) -> dict[str, str] | None:
    if not text:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        raise HTTPException(422, "The column mapping is not valid JSON.")
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise HTTPException(422, "The column mapping must map field names to column names.")
    return {k: v for k, v in value.items() if v}


def _explain_quietly(batch_id: str) -> None:
    try:
        ai.explain_batch(get_store(), get_assistant(), batch_id)
    except Exception as exc:  # explanations are optional; the batch is usable without them
        log.warning("batch %s: AI explanations skipped: %s", batch_id, exc)

# Pay pap

An AI payout agent for bulk contractor payments. Upload a CSV; fixed rules validate every row and flag duplicates and risk; an LLM (Groq by default) explains each flag in plain language and answers questions about the batch; a manager approves; only then does the batch go to PayPal Payouts (sandbox), with status tracking and an audit log of every decision.

**The AI prepares and explains. Fixed rules and a human approval decide whether money moves.**

## Test logins

| Role | Email | Password |
|---|---|---|
| Admin (uploads and reviews batches) | `manager-bashir@paypap.com` | `Nairobi.1` |
| Approver (approves batches) | `approver@paypap.demo` | `Nairobi.2` |

The person who uploads a batch can't approve it: sign in as the admin to upload and review, then as the approver to approve. These are app logins only (set by `DEMO_ADMIN_PASSWORD` / `DEMO_APPROVER_PASSWORD` in `backend/.env`), not PayPal passwords.

## Run locally

```powershell
# Backend (Python 3.14)
cd backend
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env            # then fill in the values
.venv\Scripts\python check_env.py
.venv\Scripts\python -m uvicorn app.main:app --reload     # http://localhost:8000
.venv\Scripts\python -m app.worker                        # polling fallback (second terminal)

# Frontend (Node 24)
cd frontend
npm install
npm run dev                                                # http://localhost:3000
```

Database: apply [backend/db/migrations/0001_initial_schema.sql](backend/db/migrations/0001_initial_schema.sql) to Supabase once; see [docs/database.md](docs/database.md).

Full demo: `backend/seed/demo_video_250.csv` — 250 contractors, $18,500, 30 planted problems; rows 2–6 are real sandbox accounts and get paid, the rest stay pending (unclaimed).

Demo batches: `backend/seed/batch_1_5_people.csv` to `batch_5_5_people.csv`, each paying the same 5 PayPal sandbox Personal accounts once with its own invoice numbers, so every payout ends SUCCESS.

Large demo file: `.venv\Scripts\python scripts\seed_demo.py --load-directory` writes `backend/seed/demo_payouts.csv` (250 rows, about $18,500, with planted problems) and loads the recipient directory.

Command-line run of the whole flow: `python -m app.cli --help` (seed, import, show, decide, approve, submit, status, audit).

## Tests

```powershell
cd backend
.venv\Scripts\python -m pytest                             # rules, CSV import, approvals, payouts, AI layer, API
psql "<DATABASE_URL>" -f db/tests/test_schema.sql          # database guarantees; rolls itself back
```

## Deploy (free, on Vercel)

The API and the web app are two Vercel projects made from this one repo, both on the free Hobby plan.

1. **API:** Vercel > Add New > Project > this repo, **Root Directory `backend`**. Add the values from `backend/.env.example` as environment variables, plus `FRONTEND_URL` (the web app's address) and `CRON_SECRET` (any random string of 16+ characters). [backend/vercel.json](backend/vercel.json) gives requests up to 5 minutes (the AI review needs about one) and runs a daily status check.
2. **Web app:** Add New > Project > this repo again, **Root Directory `frontend`**, with `NEXT_PUBLIC_API_BASE_URL` (the API's address), `NEXT_PUBLIC_DEMO_ADMIN_EMAIL` and `NEXT_PUBLIC_DEMO_APPROVER_EMAIL`.
3. **PayPal webhook:** `.venv\Scripts\python scripts\register_webhook.py https://<api-address>` points PayPal's notifications at the API.

Vercel runs the API as short-lived functions, so there is no background worker there: payout statuses arrive by webhook, the batch page asks PayPal for news every 15 seconds while a batch is being paid, and the daily check catches anything left.

## Before money moves

Every send is checked first, and refused before anything reaches PayPal if it would fail:

- **Receivers PayPal turned away before** (for example `INVALID_EMAIL`) are blocked at upload and again at send, and so is an email domain where PayPal has turned away 3 or more addresses and paid none.
- **The PayPal balance** is read before sending and shown on the approval card. With real money (`PAYPAL_MODE=live`) a batch the balance cannot cover is refused. In the sandbox the balance is only shown: PayPal's reported balance there lags and leaves out balances set with the developer dashboard's edit button, which is what sandbox payouts draw on.

## APIMatic

- **APIMatic PayPal Python SDK** ([`backend/app/paypal_balances.py`](backend/app/paypal_balances.py)): the balance check calls `client.transaction_search.search_balances`, with typed error handling and tests against a stub transport ([`backend/tests/test_paypal_balances.py`](backend/tests/test_paypal_balances.py)). The SDK installs from GitHub (pinned in `requirements.txt`).
- **APIMatic Context Plugin for PayPal**: the PayPal SDK guidance used while building.
- Payouts, webhooks and payout status use a small hand-written client ([`backend/app/paypal_client.py`](backend/app/paypal_client.py)), because the APIMatic PayPal SDK has no Payouts API.

## Stack

PayPal Payouts API (sandbox) · APIMatic PayPal SDK · Groq (`openai/gpt-oss-120b`, set by `AI_PROVIDER` / `AI_MODEL`; Ollama also supported) · FastAPI · Supabase Postgres · Next.js with AG Grid · Vercel

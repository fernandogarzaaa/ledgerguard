# Ledgerguard · Payment Ops Incident Commander

> A supplier emails: *"invoice INV-1042 was never paid."* Ledgerguard pulls the
> Airwallex payout, FX, balance and beneficiary records into one timeline,
> classifies the case, proposes **one** action with evidence, cost and risk, and
> only after a person approves does it touch Airwallex: with an idempotent
> `request_id` and a duplicate-payout guard that refuses to pay the same invoice
> twice. Then it writes the incident report and a draft reply to the supplier.

Built for the **Airwallex Agentic Banking Hackathon**, Starter Kit 03 *Payment Ops
Incident Commander* ("Resolve a delayed supplier transfer without paying twice").

**The model reads. The code decides. A person approves. Airwallex moves the money.**

[![CI](https://github.com/fernandogarzaaa/ledgerguard/actions/workflows/ci.yml/badge.svg)](https://github.com/fernandogarzaaa/ledgerguard/actions)
MIT · Python 3.11+ · FastAPI · runs fully offline in mock mode · $0 to run

---

## Quick start (no keys, 60 seconds)

```bash
git clone https://github.com/fernandogarzaaa/ledgerguard && cd ledgerguard
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
make mock          # http://localhost:8000  (mock Airwallex sandbox + deterministic analyst)
make check         # ruff + mypy + the offline test suite
```

Docker: `make docker`. MCP (stdio): `make mcp`.

## What it decides

| Class | What the records show | The ONE action | Money? |
|---|---|---|---|
| `IN_FLIGHT` | payout `SCHEDULED/PROCESSING/SENT` inside its window | **Wait & monitor**, share the reference, recheck (webhook updates it) | no |
| `IN_FLIGHT` (stuck/overdue) | past 72 h LOCAL / 120 h SWIFT, or funding `OVERDUE` | **Escalate** to Airwallex to trace; never resend while it can still land | no |
| `PAID_CONFIRMED` | payout `PAID` | **Send proof of payment** (reference, date, amount) | no |
| `FAILED` (retryable) | `CANCELLED` + `CHANNEL_TIMEOUT` / `INSUFFICIENT_FUNDS` | **Replace payout**: new transfer, **new** `request_id`, original stays recorded | yes |
| `FAILED` (compliance) | `TM_SUSPENDED`, `CHANNEL_POLICY`, `RECALL_REQUESTED`, … | **Escalate**; resending after a compliance hold is how accounts get frozen | no |
| `RETURNED` | `BENEFICIARY_BANK_RETURNED` (even after `PAID`) | **Ask the supplier to confirm with their bank**; funds are back in the wallet | no |
| `BAD_BENEFICIARY` | `INVALID_ACCOUNT_NAME_OR_NUMBER`, name mismatch, bank details changed just before | **Hold & verify** by calling a contact already on file (BEC-fraud pattern) | no |
| `TRULY_UNPAID` | no payout for the invoice | **Pay invoice** (funds from another wallet via embedded FX if needed) | yes |
| `DUPLICATE_RISK` | ≥2 live payouts for one invoice | **Cancel the duplicate** while it is still cancellable, else escalate for recovery | yes |
| `UNKNOWN_INVOICE` | not in the AP book | **Escalate** | no |

The decision changes when the facts change: drain the wallet and a `REPLACE_PAYOUT`
becomes `ESCALATE: not enough funds`; let an in-flight payout reach `PAID` and an
approved `WAIT` is voided and re-proposed as `SEND_PROOF_OF_PAYMENT`.

## How it never pays twice

1. **Classification first.** Any live payout (`IN_APPROVAL/SCHEDULED/OVERDUE/PROCESSING/SENT/PAID`) for the
   invoice means no new payout is even proposed.
2. **Approval bound to evidence.** The approval token is an HMAC over the exact action
   (kind, amount, currency, source currency, beneficiary, `request_id`, target transfer,
   evidence refs and a digest of every transfer id/status it was decided on). It is
   single-use and expires in 15 min. At execution Ledgerguard re-reads Airwallex and
   re-decides; if anything differs, the approval is void and a fresh proposal is shown.
   There is **no approve tool** for agents/MCP, and names like `agent`/`llm` cannot approve.
3. **Duplicate-payout guard.** A per-invoice lock (persisted to `LEDGER_PATH`) is taken
   before calling Airwallex and records the `request_id`. A second payout for the same
   invoice is refused while it is held, even if Airwallex has not listed the first
   transfer yet. It is released only when the locked transfer is confirmed failed.
4. **Airwallex idempotency.** `request_id`s are deterministic per incident fact set
   (`lg-inv2001-<sha256>`), 10–50 chars. A timeout, 5xx, `409 request_pending` or
   duplicate-`request_id` error is resolved with `GET /transfers?request_id=` and returns
   the existing transfer instead of creating another.

Each layer has a test that switches the others off (list lag, lost ledger, outage after
commit) and proves the invoice still has exactly one payout.

## Architecture

```
 incident text ─► Agent loop (LLM or deterministic analyst; READ tools only)
                    │  lookup_invoice · find_invoice_transfers · get_beneficiary
                    │  get_balances · list_fx_conversions · submit_findings
                    ▼
                 Evidence collector (code) ──► Airwallex REST client ──► mock simulator | api.sandbox
                    │  transfers · beneficiary · FX conversions · balances · rates
                    ▼
                 Timeline ─► Classifier (code) ─► Policy (code: ONE action, cost, risk, request_id)
                    ▼
                 Approval gate (HMAC token, bound fingerprint, single-use, TTL, optional PIN)
                    ▼  human clicks Approve in the console
                 Executor: re-read + re-decide ─► duplicate guard ─► idempotent create/cancel
                    ▼
                 Incident report (markdown) + draft supplier message
      MCP server (stdio + POST /mcp) exposes the read tools, investigate_incident,
      get_incident and the guarded write tools. Webhooks (HMAC-verified, deduped) refresh incidents.
```

| Path | What |
|---|---|
| `app/airwallex/client.py` | async client: login + token cache, 401 re-login, 429 backoff, idempotent `create_transfer`, cancel, simulation |
| `app/airwallex/mock.py`, `fixtures.py` | in-memory sandbox as an httpx transport; 8 invoices covering every class; fault injection (outage after commit, list lag) |
| `app/airwallex/models.py` | normalised Transfer / Beneficiary / Conversion / Balance; reads both documented failure shapes |
| `app/incident/` | `evidence.py` (collect + timeline), `classifier.py`, `policy.py`, `guard.py`, `approval.py`, `report.py`, `invoices.py` |
| `app/agent/` | OpenAI-compatible client (Groq default), tool registry, agent loop, deterministic analyst |
| `app/service.py` | investigate → approve → execute → report; webhook handling |
| `app/mcp_server.py` | dependency-free MCP (JSON-RPC 2.0) over stdio and HTTP |
| `app/main.py`, `app/static/` | FastAPI API + web console |
| `scripts/` | `probe_airwallex.py` (read-only shape probe), `seed_sandbox.py` (stage the demo in your sandbox) |
| `docs/AIRWALLEX_VERIFY.md` | every assumption still to check against a live sandbox |

## 2-minute demo script (for the < 5 min video)

1. **(0:00) Hook.** "Suppliers say *we never got paid*. The fastest fix — paying again — is
   how companies pay twice. Ledgerguard resolves it without that risk." Show the console.
2. **(0:15) In flight.** Click **In flight · INV-1042**. Timeline: invoice due → payout
   created → `SENT` 30 h ago. Decision: **WAIT**, "SENT is in flight, not final". Show the
   supplier message quoting the Airwallex reference. No money moves.
3. **(0:40) Failed → replace.** Click **Failed · INV-2001**. Timeline shows the USD→EUR
   conversion that funded it and the payout `CANCELLED` with `CHANNEL_TIMEOUT`
   ("CANCELLED means failed, not a human"). Decision: **Replace payout**, cost note
   includes the second SWIFT fee. Type your name → **Approve & execute** → new transfer
   with a **new** `request_id`.
4. **(1:05) Try to pay twice.** Click **Try executing again** → refused (token used). Run
   the same incident again → now `IN_FLIGHT` → WAIT. Mention the guard + idempotency tests.
5. **(1:25) Fraud hold.** **Bad beneficiary · INV-2003**: bank details changed 24 h before the
   payout and the account name doesn't match → **Hold & verify**, risk HIGH.
6. **(1:40) Duplicate.** **Duplicate risk · INV-3007**: one `PAID`, one `SCHEDULED` retry a
   clerk queued → **Cancel duplicate**, approve, it's cancelled; the paid one is untouched.
7. **(1:55) Close.** Show the incident report markdown and the agent trace (model vs code
   steps). "The model reads, code decides, a person approves, Airwallex moves the money."

Optional live beat: run with `AIRWALLEX_MODE=sandbox` after `make seed`, and use
`POST /api/sim/transfers/{id}/transition` to flip the in-flight payout to `PAID` mid-demo;
the webhook (or a re-run) turns WAIT into proof-of-payment.

## Live Airwallex sandbox

1. Create a free sandbox account at <https://www.sandbox.airwallex.com/global/signup>
   (instant, no real money, no KYC for the default sandbox business). Web app →
   **Account › Developer › API keys** → Client ID + API key.
2. `cp .env.example .env`, set `AIRWALLEX_MODE=sandbox`, `AIRWALLEX_CLIENT_ID`,
   `AIRWALLEX_API_KEY`. **Never commit `.env`.**
3. `make probe` (read-only) and compare with `docs/AIRWALLEX_VERIFY.md`.
4. `make seed` creates a funded USD global account, beneficiaries and the staged
   payouts, and writes `data/invoices.sandbox.json` (git-ignored).
5. `INVOICE_BOOK_PATH=data/invoices.sandbox.json make run`.
6. Webhooks (optional): add `https://<your-host>/webhooks/airwallex` in the sandbox web
   app (Settings › Developer › Webhooks) and set `AIRWALLEX_WEBHOOK_SECRET`.

Base URL defaults to `https://api.sandbox.airwallex.com`; the client refuses the
production host.

## LLM

Any OpenAI-compatible endpoint with tool calling. Default: Groq free tier
(`LLM_BASE_URL=https://api.groq.com/openai/v1`, `LLM_MODEL=openai/gpt-oss-120b`,
`LLM_API_KEY=<groq key>`). Without a key, or if the model errors or stalls, the
deterministic analyst runs the same tool protocol, so demos and CI work offline. The
model only sees read tools; whatever it says, classification and action come from code,
and its prose is quoted in the report as an unverified analyst note.

## MCP

```bash
claude mcp add ledgerguard -- python -m app.mcp_server        # stdio
# or HTTP: POST http://localhost:8000/mcp  (JSON-RPC 2.0)
```

Tools: `lookup_invoice`, `search_invoices`, `find_invoice_transfers`, `get_transfer`,
`get_beneficiary`, `list_fx_conversions`, `get_balances`, `investigate_incident`,
`get_incident`, and the guarded writes `execute_approved_action` (needs a human-issued
token) and `simulate_transfer_transition` (sandbox/mock only).

## Deploy ($0)

`render.yaml` deploys the Docker image to a Render **free** web service in mock mode
(it sleeps when idle). For a public sandbox-mode deploy set `APPROVAL_PIN`.

## API

| Method | Path | |
|---|---|---|
| `POST` | `/api/incidents` `{text, use_llm}` | investigate; returns timeline, classification, action, trace, report |
| `POST` | `/api/incidents/{id}/approve` `{approver, pin?}` | human approval → single-use token |
| `POST` | `/api/incidents/{id}/execute` `{approval_id, approval_token}` | re-check, guard, execute |
| `GET` | `/api/incidents/{id}/report.md` | incident report |
| `POST` | `/webhooks/airwallex` | HMAC-verified `payout.transfer.*` events |
| `POST` | `/mcp` | MCP JSON-RPC |
| `POST` | `/api/sim/transfers/{id}/transition` | sandbox/mock status simulation |
| `POST` | `/api/mock/reset`, `/api/mock/balance` | mock-only staging |

## Safety

Sandbox only. Synthetic suppliers and accounts. No secrets in the repo: keys come from
environment variables only, `.env` is git-ignored. Account numbers are masked in every
output and never appear in supplier messages.

## License

MIT © 2026 Fernando Bautista Garza

# Airwallex behaviours to verify against a live sandbox

Ledgerguard's mock is built from the public docs and the hackathon builder
guide. These are the points where the docs are ambiguous, contradict each
other, or where third parties report live behaviour that differs. Each has a
probe step. Run `python scripts/probe_airwallex.py` first (read-only), then
`python scripts/seed_sandbox.py` (writes to *your* sandbox only).

Status legend: ☐ unverified · ☑ verified live (add date + what you saw).

## Auth & hosts
- ☐ **Base URL.** Docs + builder guide: `https://api.sandbox.airwallex.com`. The
  older `api-demo.airwallex.com` host appears in older material. Default is
  `api.sandbox`; override with `AIRWALLEX_BASE_URL` if needed.
- ☐ Login returns **201** `{token, expires_at}` with `expires_at` like
  `2021-06-18T16:30:00+0000`. Client refreshes ~25 min or 2 min before expiry.

## Balances
- ☐ `GET /api/v1/balances/current` returns a **bare array** (docs + everyai repo
  note). Client also accepts `{items: [...]}`.

## Transfers
- ☐ **Initial status** after `transfers/create`: docs example says
  `PROCESSING`; everyai repo reports live transfers start **`SCHEDULED`**. The
  mock uses `SCHEDULED`. Both are treated as in-flight, so the decision is the
  same either way.
- ☐ **`source_currency` required** on create (everyai observation; docs mark it
  optional). Ledgerguard always sends it.
- ☐ **Failure field shape.** Two documented shapes:
  - top-level `failure_type` + `failure_reason` (simulation response docs,
    builder guide);
  - nested `failure: {code, message, details: {type, reason_codes}}`
    (create/list response docs).
  `Transfer.from_api` reads both and records `failure_shape`. Probe a FAILED
  transfer and note which one live returns (it may depend on `x-api-version`).
- ☐ Live `failure_type` values on a simulated FAILED match the simulation enum
  (`CHANNEL_TIMEOUT`, `BENEFICIARY_BANK_RETURNED`, …). Nested shape example
  shows `TM_SUSPEND` (no `ED`) — check whether `TM_SUSPENDED` (simulation enum)
  and `TM_SUSPEND` both occur. Both fall into "escalate" today because neither
  is in a retryable set.
- ☐ **FAILED → CANCELLED timing**: docs say "automatically cancelled in
  seconds". The mock collapses it to immediate. Ledgerguard treats FAILED and
  CANCELLED identically, so a slow poll is safe.
- ☐ **`PAID → FAILED` via simulation** (late bank return) is allowed live (docs'
  state machine says yes; builder guide says failure_type applies after SENT).
- ☐ **Duplicate request_id error code**: docs list `request_id_duplicate`;
  builder guide says `duplicate_request_id`. Client accepts both.
- ☐ **409 `request_pending`** on a concurrent retry; client treats as ambiguous
  and looks the transfer up by `request_id`.
- ☐ `GET /api/v1/transfers?request_id=` filters exactly (docs). This is the
  backbone of "an uncertain create never becomes two payouts".
- ☐ `metadata` round-trips on create → list/get (docs show it in responses).
  Invoice matching uses `metadata.invoice_id`, then `reference`, then
  beneficiary+amount (flagged as inferred).
- ☐ **Cancel**: `POST /api/v1/transfers/cancel/{id}` works for `SCHEDULED`
  (and `PROCESSING`/`OVERDUE`?) and fails for `SENT`/`PAID`. The duplicate
  cancel path depends on this. Also check whether the cancelled transfer gets a
  `failure_type` (mock sets `CLIENT_REQUESTED`).
- ☐ Does a sandbox transfer **auto-advance** without simulation calls? If it
  auto-PAIDs quickly, stage the in-flight case right before the demo.
- ☐ **SWIFT fee**: guide quotes flat EUR 12.85; everyai reports live SWIFT fees
  are percentage-based. Ledgerguard prefers the transfer's own `fee_amount`
  once it exists; the pre-execution cost note says "verify live".

## Beneficiaries
- ☐ `POST /api/v1/beneficiaries/create` body is
  `{beneficiary: {...}, transfer_methods: ["LOCAL"], nickname, payer_entity_type}`
  (seed script). Builder guide: US corridor needs `account_routing_type1: "aba"`
  and `bank_account_category` exactly `"Checking"`/`"Savings"`; invalid routing
  fails with code `010`.
- ☐ `GET /api/v1/beneficiaries/{id}` returns the same object as list items
  (`{id, beneficiary: {...}, transfer_methods}`); parser accepts flat too.
- ☐ No `created_at`/`updated_at` on beneficiaries in the documented shape, so the
  "bank details changed just before payment" signal comes from the AP vendor
  master (`bank_details_changed_at` in the invoice book), not Airwallex.

## FX
- ☐ `GET /api/v1/fx/rates/current?buy_currency=EUR&sell_currency=USD` returns
  `rate` and the **direction** (sell units per 1 buy unit vs the reverse). The
  policy uses it only for an *indicative* cost note; the real conversion happens
  inside `transfers/create` (`source_currency != transfer_currency`) and the
  rate locks at creation (`lock_rate_on_create` defaults true).
- ☐ Transfers with an embedded conversion work in sandbox for USD→EUR.
- ☐ Linking a conversion to a payout is **inferred** (same bought currency,
  ≤48 h before the payout, enough volume). Airwallex has no documented link
  field between a standalone conversion and a transfer.

## Webhooks
- ☐ Signature = hex HMAC-SHA256(secret, `x-timestamp` + raw body), `x-timestamp`
  in milliseconds (docs). Receiver rejects >5 min skew.
- ☐ Payload `data.object` for `payout.transfer.*` is the transfer object.

## Simulation
- ☐ `SCHEDULED → PROCESSING` is accepted by
  `POST /api/v1/simulation/transfers/{id}/transition` (seed script relies on it).
- ☐ A `failure_type` passed on FAILED before SENT is ignored (guide: "applies
  only after SENT"); the mock mirrors that and falls back to `OTHER`.

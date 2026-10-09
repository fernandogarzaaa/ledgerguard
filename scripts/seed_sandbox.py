"""Stage the Ledgerguard demo incidents in a REAL Airwallex sandbox account.

UNVERIFIED against a live sandbox at the time of writing: run it once with your
own sandbox keys and fix anything docs/AIRWALLEX_VERIFY.md flags. It only ever
talks to api.sandbox.airwallex.com (the client refuses the production host).

    export AIRWALLEX_CLIENT_ID=... AIRWALLEX_API_KEY=...
    python scripts/seed_sandbox.py            # creates beneficiaries + transfers
    AIRWALLEX_MODE=sandbox INVOICE_BOOK_PATH=data/invoices.sandbox.json make run

What it does (USD LOCAL corridor only, the simplest one to get right):
1. finds or creates a USD Global Account and simulates a deposit;
2. creates one US beneficiary per scenario (synthetic names, ABA 021000021);
3. creates the payouts and drives them with the transfer simulation endpoint;
4. writes data/invoices.sandbox.json (the AP book Ledgerguard reads).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.airwallex.client import AirwallexClient, AirwallexError
from app.config import SANDBOX_BASE_URL

OUT = Path("data/invoices.sandbox.json")
RUN = datetime.now(UTC).strftime("%m%d%H%M")

SCENARIOS: list[dict[str, Any]] = [
    # invoice, supplier, amount, path of simulated statuses (+ failure_type on FAILED)
    {
        "inv": "INV-1042",
        "supplier": "Kestrel Supply Co",
        "amount": 320,
        "path": ["PROCESSING", "SENT"],
    },
    {
        "inv": "INV-1043",
        "supplier": "Northwind Logistics",
        "amount": 148,
        "path": ["PROCESSING", "SENT", "PAID"],
    },
    {
        "inv": "INV-2001",
        "supplier": "Atlas Components",
        "amount": 400,
        "path": ["PROCESSING", "SENT", ("FAILED", "CHANNEL_TIMEOUT")],
    },
    {
        "inv": "INV-2002",
        "supplier": "Lumen Packaging",
        "amount": 185,
        "path": ["PROCESSING", "SENT", "PAID", ("FAILED", "BENEFICIARY_BANK_RETURNED")],
    },
    {
        "inv": "INV-2003",
        "supplier": "Orchid Textiles",
        "amount": 240,
        "path": ["PROCESSING", "SENT", ("FAILED", "INVALID_ACCOUNT_NAME_OR_NUMBER")],
        "account_name": "Orchid Textile Holdings",
        "changed_hours_ago": 30,
    },
    {
        "inv": "INV-2004",
        "supplier": "Tamarind Freight",
        "amount": 610,
        "path": ["PROCESSING", "SENT", ("FAILED", "TM_SUSPENDED")],
    },
    {
        "inv": "INV-3007",
        "supplier": "Meridian Parts",
        "amount": 560,
        "path": ["PROCESSING", "SENT", "PAID"],
        "duplicate": True,
    },
    {"inv": "INV-4100", "supplier": "Brightwater Studio", "amount": 275, "path": None},
]


def rid(tag: str) -> str:
    return f"lg-seed-{RUN}-{tag}-{uuid.uuid4().hex[:8]}"[:50]


async def ensure_funds(c: AirwallexClient, amount: int) -> None:
    accounts = await c.request("GET", "/api/v1/global_accounts")
    items = accounts.get("items", []) if isinstance(accounts, dict) else accounts
    usd = None
    for a in items or []:
        feats = json.dumps(a.get("required_features") or a.get("supported_features") or a)
        if "USD" in feats:
            usd = a
            break
    if usd is None:
        usd = await c.request(
            "POST",
            "/api/v1/global_accounts/create",
            json={
                "request_id": rid("ga"),
                "country_code": "US",
                "required_features": [{"currency": "USD", "transfer_method": "LOCAL"}],
            },
        )
        print("created global account", usd.get("id"))
    await c.request(
        "POST",
        "/api/v1/simulation/deposit/create",
        json={"global_account_id": usd["id"], "amount": amount, "payer_name": "Ledgerguard seed"},
    )
    await asyncio.sleep(5)  # deposits post a few seconds after reporting PENDING
    print("balances:", [(b.currency, str(b.available)) for b in await c.get_balances()])


async def create_beneficiary(c: AirwallexClient, name: str, account_name: str, n: int) -> str:
    body = {
        "nickname": name,
        "payer_entity_type": "COMPANY",
        "transfer_methods": ["LOCAL"],
        "beneficiary": {
            "entity_type": "COMPANY",
            "company_name": name,
            "address": {
                "street_address": f"{100 + n} Market St",
                "city": "San Francisco",
                "state": "CA",
                "postcode": "94105",
                "country_code": "US",
            },
            "bank_details": {
                "account_currency": "USD",
                "account_name": account_name,
                "bank_country_code": "US",
                "account_number": f"0009{n:04d}{uuid.uuid4().int % 10000:04d}",
                "account_routing_type1": "aba",
                "account_routing_value1": "021000021",
                "bank_account_category": "Checking",
                "local_clearing_system": "ACH",
            },
        },
    }
    out = await c.request("POST", "/api/v1/beneficiaries/create", json=body)
    return str(out.get("id") or out.get("beneficiary_id"))


async def create_and_drive(c: AirwallexClient, sc: dict[str, Any], ben: str, tag: str) -> str:
    t, _ = await c.create_transfer(
        {
            "request_id": rid(tag),
            "beneficiary_id": ben,
            "transfer_amount": str(sc["amount"]),
            "transfer_currency": "USD",
            "source_currency": "USD",
            "transfer_method": "LOCAL",
            "reference": sc["inv"],
            "reason": "payment_to_supplier",
            "metadata": {"invoice_id": sc["inv"]},
        }
    )
    print(f"  {sc['inv']} transfer {t.id} {t.status}")
    for step in sc["path"] if tag == "a" else []:
        nxt, ftype = (step, None) if isinstance(step, str) else step
        await asyncio.sleep(1.0)
        try:
            t = await c.simulate_transition(t.id, nxt, ftype)
        except AirwallexError as exc:
            print(f"    transition {nxt} failed: {exc}")
            break
        print(f"    -> {t.status} {t.failure_type}")
    return t.id


async def main() -> None:
    cid, key = os.getenv("AIRWALLEX_CLIENT_ID", ""), os.getenv("AIRWALLEX_API_KEY", "")
    if not cid or not key:
        sys.exit("set AIRWALLEX_CLIENT_ID and AIRWALLEX_API_KEY (sandbox keys)")
    base = os.getenv("AIRWALLEX_BASE_URL", SANDBOX_BASE_URL)
    if "sandbox" not in base and "demo" not in base:
        sys.exit("refusing: seed_sandbox only runs against a sandbox host")
    c = AirwallexClient(base, cid, key)
    await ensure_funds(c, int(os.getenv("SEED_DEPOSIT", "10000")))
    now = datetime.now(UTC)
    book = []
    for n, sc in enumerate(SCENARIOS, start=1):
        ben = await create_beneficiary(c, sc["supplier"], sc.get("account_name", sc["supplier"]), n)
        print(f"{sc['inv']}: beneficiary {ben}")
        if sc["path"] is not None:
            await create_and_drive(c, sc, ben, "a")
        if sc.get("duplicate"):
            await create_and_drive(c, sc, ben, "b")  # second payout left SCHEDULED
        row: dict[str, Any] = {
            "invoice_id": sc["inv"],
            "supplier": sc["supplier"],
            "beneficiary_id": ben,
            "amount": f"{sc['amount']:.2f}",
            "currency": "USD",
            "transfer_method": "LOCAL",
            "due_date": (now - timedelta(days=1)).date().isoformat(),
            "supplier_contact": f"ap@{sc['supplier'].split()[0].lower()}.example",
        }
        if sc.get("changed_hours_ago"):
            row["bank_details_changed_at"] = (
                now - timedelta(hours=sc["changed_hours_ago"])
            ).strftime("%Y-%m-%dT%H:%M:%S+0000")
            row["bank_details_change_source"] = "email request (synthetic)"
        book.append(row)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"invoices": book}, indent=2), encoding="utf-8")
    print(f"wrote {OUT} ({len(book)} invoices). Do not commit it if it holds anything sensitive.")


if __name__ == "__main__":
    asyncio.run(main())

"""Read-only probe of a live Airwallex sandbox: prints the SHAPE of each response
Ledgerguard relies on (keys and types, never values), so docs/AIRWALLEX_VERIFY.md
items can be ticked off. It writes nothing to Airwallex.

    AIRWALLEX_CLIENT_ID=... AIRWALLEX_API_KEY=... python scripts/probe_airwallex.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.airwallex.client import AirwallexClient, AirwallexError
from app.config import SANDBOX_BASE_URL


def shape(obj: Any, depth: int = 0) -> Any:
    if depth > 2:
        return type(obj).__name__
    if isinstance(obj, dict):
        return {k: shape(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [shape(obj[0], depth + 1)] if obj else []
    return type(obj).__name__


async def probe(c: AirwallexClient, label: str, path: str, params: dict[str, Any] | None = None):
    try:
        data = await c.request("GET", path, params=params)
    except AirwallexError as exc:
        print(f"\n## {label}: ERROR {exc}")
        return None
    print(f"\n## {label} ({path})\n{shape(data)}")
    return data


async def main() -> None:
    cid, key = os.getenv("AIRWALLEX_CLIENT_ID", ""), os.getenv("AIRWALLEX_API_KEY", "")
    if not cid or not key:
        sys.exit("set AIRWALLEX_CLIENT_ID and AIRWALLEX_API_KEY")
    c = AirwallexClient(os.getenv("AIRWALLEX_BASE_URL", SANDBOX_BASE_URL), cid, key)
    await probe(c, "balances", "/api/v1/balances/current")
    tr = await probe(c, "transfers", "/api/v1/transfers", {"page_size": 5})
    items = (tr or {}).get("items") if isinstance(tr, dict) else tr
    if items:
        await probe(c, "transfer by id", f"/api/v1/transfers/{items[0]['id']}")
        await probe(
            c,
            "transfer by request_id",
            "/api/v1/transfers",
            {"request_id": items[0].get("request_id")},
        )
        failed = [i for i in items if i.get("status") in ("CANCELLED", "FAILED")]
        if failed:
            f = failed[0]
            print(
                "\nfailure fields on a failed transfer:",
                {k: f.get(k) for k in ("failure_type", "failure_reason", "failure")},
            )
    await probe(c, "beneficiaries", "/api/v1/beneficiaries", {"page_size": 2})
    await probe(c, "conversions", "/api/v1/fx/conversions", {"page_size": 2})
    await probe(
        c,
        "rate EUR/USD",
        "/api/v1/fx/rates/current",
        {"buy_currency": "EUR", "sell_currency": "USD"},
    )
    await probe(c, "global accounts", "/api/v1/global_accounts")


if __name__ == "__main__":
    asyncio.run(main())

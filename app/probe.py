"""Read-only shape probe of a live Airwallex sandbox (keys/types only, never values)."""

from __future__ import annotations

from typing import Any

from app.airwallex.client import AirwallexClient, AirwallexError


def shape(obj: Any, depth: int = 0) -> Any:
    if depth > 3:
        return type(obj).__name__
    if isinstance(obj, dict):
        return {k: shape(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [shape(obj[0], depth + 1)] if obj else []
    return type(obj).__name__


ENUM_KEYS = ("status", "failure_type", "transfer_method", "currency", "transfer_currency")


async def run_probe(c: AirwallexClient) -> dict[str, Any]:
    out: dict[str, Any] = {}

    async def get(label: str, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            data = await c.request("GET", path, params=params)
        except AirwallexError as exc:
            out[label] = {"error": str(exc)[:300]}
            return None
        out[label] = shape(data)
        return data

    await get("balances", "/api/v1/balances/current")
    tr = await get("transfers", "/api/v1/transfers", {"page_size": 20})
    items = (tr or {}).get("items") if isinstance(tr, dict) else tr
    if items:
        out["transfer_enums_seen"] = {
            k: sorted({str(i.get(k)) for i in items if i.get(k) is not None}) for k in ENUM_KEYS
        }
        await get("transfer_by_id", f"/api/v1/transfers/{items[0]['id']}")
    await get("beneficiaries", "/api/v1/beneficiaries", {"page_size": 2})
    await get("conversions", "/api/v1/fx/conversions", {"page_size": 2})
    await get(
        "rate_EUR_USD",
        "/api/v1/fx/rates/current",
        {"buy_currency": "EUR", "sell_currency": "USD"},
    )
    await get("global_accounts", "/api/v1/global_accounts")
    return out

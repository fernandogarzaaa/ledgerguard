"""Async Airwallex REST client (sandbox).

One code path for mock and live: mock mode swaps the httpx transport for the
in-memory simulator in ``app.airwallex.mock``. Everything above this module
works on the normalised records from ``app.airwallex.models``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from app.airwallex.models import Balance, Beneficiary, Conversion, Transfer, iso, parse_ts

log = logging.getLogger("ledgerguard.airwallex")

DUPLICATE_REQUEST_CODES = frozenset({"request_id_duplicate", "duplicate_request_id"})
INSUFFICIENT_FUNDS_CODES = frozenset({"balance_insufficient", "insufficient_funds"})


class AirwallexError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, body: Any = None) -> None:
        super().__init__(f"Airwallex HTTP {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.body = body

    @property
    def is_duplicate_request(self) -> bool:
        return self.code in DUPLICATE_REQUEST_CODES

    @property
    def is_insufficient_funds(self) -> bool:
        return self.code in INSUFFICIENT_FUNDS_CODES

    @property
    def is_ambiguous(self) -> bool:
        """The create may or may not have happened: look it up by request_id."""
        return self.status >= 500 or self.status in (408, 409) or self.is_duplicate_request


class AirwallexClient:
    def __init__(
        self,
        base_url: str,
        client_id: str,
        api_key: str,
        timeout_s: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._api_key = api_key
        self._timeout = timeout_s
        self._transport = transport
        self._max_retries = max_retries
        self._token: str = ""
        self._token_expiry: float = 0.0
        self._lock = asyncio.Lock()
        self.calls: list[dict[str, Any]] = []  # lightweight audit of HTTP calls

    # -- plumbing ------------------------------------------------------------
    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url, timeout=self._timeout, transport=self._transport
        )

    async def _login(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._token_expiry:
                return self._token
            async with self._http() as http:
                resp = await http.post(
                    "/api/v1/authentication/login",
                    headers={"x-client-id": self._client_id, "x-api-key": self._api_key},
                )
            if resp.status_code >= 400:
                raise _error(resp)
            data = resp.json()
            token = str(data.get("token") or "")
            if not token:
                raise AirwallexError(resp.status_code, "no_token", "login returned no token")
            expires = parse_ts(data.get("expires_at"))
            ttl = 25 * 60.0  # docs: tokens last 30 minutes; refresh early
            if expires is not None:
                ttl = max(60.0, min(ttl, (expires - datetime.now(UTC)).total_seconds() - 120))
            self._token = token
            self._token_expiry = time.monotonic() + ttl
            return token

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        attempt = 0
        while True:
            token = await self._login()
            started = time.perf_counter()
            async with self._http() as http:
                resp = await http.request(
                    method,
                    path,
                    params=clean or None,
                    json=json,
                    headers={"Authorization": f"Bearer {token}"},
                )
            self.calls.append(
                {
                    "method": method,
                    "path": path,
                    "status": resp.status_code,
                    "ms": round((time.perf_counter() - started) * 1000, 1),
                }
            )
            if resp.status_code == 401 and attempt < 1:
                self._token = ""
                attempt += 1
                continue
            if resp.status_code == 429 and attempt < self._max_retries:
                attempt += 1
                await asyncio.sleep(min(4.0, 0.5 * 2**attempt))
                continue
            if resp.status_code >= 400:
                raise _error(resp)
            if not resp.content:
                return {}
            return resp.json()

    # -- reads -----------------------------------------------------------------
    async def get_balances(self) -> list[Balance]:
        data = await self.request("GET", "/api/v1/balances/current")
        rows = data if isinstance(data, list) else (data or {}).get("items", [])
        return [Balance.from_api(r) for r in rows if isinstance(r, dict)]

    async def list_transfers(
        self,
        *,
        request_id: str | None = None,
        status: str | None = None,
        beneficiary_id: str | None = None,
        from_created_at: datetime | None = None,
        page_size: int = 200,
        max_pages: int = 10,
    ) -> list[Transfer]:
        params: dict[str, Any] = {
            "request_id": request_id,
            "status": status,
            "beneficiary_id": beneficiary_id,
            "page_size": page_size,
        }
        if from_created_at is not None:
            params["from_created_at"] = iso(from_created_at)
        out: list[Transfer] = []
        for _ in range(max_pages):
            data = await self.request("GET", "/api/v1/transfers", params=params)
            items = data.get("items", []) if isinstance(data, dict) else data
            out.extend(Transfer.from_api(i) for i in items if isinstance(i, dict))
            nxt = data.get("page_after") if isinstance(data, dict) else None
            if not nxt:
                break
            params["page"] = nxt
        return out

    async def get_transfer(self, transfer_id: str) -> Transfer:
        return Transfer.from_api(await self.request("GET", f"/api/v1/transfers/{transfer_id}"))

    async def find_transfer_by_request_id(self, request_id: str) -> Transfer | None:
        found = await self.list_transfers(request_id=request_id)
        for t in found:
            if t.request_id == request_id:
                return t
        return None

    async def list_beneficiaries(self) -> list[Beneficiary]:
        data = await self.request("GET", "/api/v1/beneficiaries", params={"page_size": 200})
        items = data.get("items", []) if isinstance(data, dict) else data
        return [Beneficiary.from_api(i) for i in items if isinstance(i, dict)]

    async def get_beneficiary(self, beneficiary_id: str) -> Beneficiary:
        data = await self.request("GET", f"/api/v1/beneficiaries/{beneficiary_id}")
        return Beneficiary.from_api(data)

    async def list_conversions(
        self, *, buy_currency: str | None = None, from_created_at: datetime | None = None
    ) -> list[Conversion]:
        params: dict[str, Any] = {"buy_currency": buy_currency, "page_size": 100}
        if from_created_at is not None:
            params["from_created_at"] = iso(from_created_at)
        data = await self.request("GET", "/api/v1/fx/conversions", params=params)
        items = data.get("items", []) if isinstance(data, dict) else data
        return [Conversion.from_api(i) for i in items if isinstance(i, dict)]

    async def get_rate(self, buy_currency: str, sell_currency: str) -> Decimal | None:
        """Indicative rate (sell units per 1 buy unit is NOT assumed: see notes)."""
        try:
            data = await self.request(
                "GET",
                "/api/v1/fx/rates/current",
                params={"buy_currency": buy_currency, "sell_currency": sell_currency},
            )
        except AirwallexError as exc:
            log.warning("rate lookup failed: %s", exc)
            return None
        rate = data.get("rate") if isinstance(data, dict) else None
        return Decimal(str(rate)) if rate is not None else None

    # -- writes (only ever called by the executor, after approval) ------------
    async def create_transfer(self, payload: dict[str, Any]) -> tuple[Transfer, bool]:
        """Create a transfer idempotently.

        Returns (transfer, created_now). Any ambiguous failure (timeout, 5xx,
        409 request_pending, duplicate request_id) is resolved by looking the
        transfer up by its request_id, so a retry can never create a second one.
        """
        request_id = str(payload.get("request_id") or "")
        if not 10 <= len(request_id) <= 50:
            raise ValueError("request_id must be 10-50 characters (Airwallex code 018)")
        try:
            data = await self.request("POST", "/api/v1/transfers/create", json=payload)
            return Transfer.from_api(data), True
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            log.warning("create_transfer transport error, resolving by request_id: %s", exc)
            existing = await self._await_by_request_id(request_id)
            if existing is None:
                raise
            return existing, False
        except AirwallexError as exc:
            if not exc.is_ambiguous:
                raise
            existing = await self._await_by_request_id(request_id)
            if existing is None:
                raise
            return existing, False

    async def _await_by_request_id(self, request_id: str, tries: int = 3) -> Transfer | None:
        for i in range(tries):
            found = await self.find_transfer_by_request_id(request_id)
            if found is not None:
                return found
            await asyncio.sleep(0.2 * (i + 1))
        return None

    async def cancel_transfer(self, transfer_id: str) -> Transfer:
        await self.request("POST", f"/api/v1/transfers/cancel/{transfer_id}", json={})
        return await self.get_transfer(transfer_id)

    async def simulate_transition(
        self, transfer_id: str, next_status: str, failure_type: str | None = None
    ) -> Transfer:
        body: dict[str, Any] = {"next_status": next_status}
        if failure_type:
            body["failure_type"] = failure_type
        data = await self.request(
            "POST", f"/api/v1/simulation/transfers/{transfer_id}/transition", json=body
        )
        return Transfer.from_api(data) if data else await self.get_transfer(transfer_id)


def _error(resp: httpx.Response) -> AirwallexError:
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    code = ""
    message = ""
    if isinstance(body, dict):
        code = str(body.get("code") or body.get("error") or "")
        message = str(body.get("message") or body.get("details") or "")
    return AirwallexError(resp.status_code, code or "http_error", message or str(body)[:200], body)


def lookback(days: int, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) - timedelta(days=days)

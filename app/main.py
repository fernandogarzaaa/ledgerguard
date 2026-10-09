"""FastAPI app: JSON API, MCP endpoint, webhook receiver and the web console."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import __version__
from app.agent.llm import ChatModel, OpenAICompatibleLLM
from app.airwallex import fixtures
from app.airwallex.client import AirwallexClient
from app.airwallex.mock import MOCK_API_KEY, MOCK_CLIENT_ID, MockAirwallex
from app.config import Settings, load_settings
from app.incident.approval import ApprovalError
from app.incident.invoices import InvoiceBook
from app.mcp_server import handle_message
from app.service import IncidentService

log = logging.getLogger("ledgerguard")
STATIC = Path(__file__).parent / "static"
WEBHOOK_TOLERANCE_MS = 5 * 60 * 1000


def build_service(
    settings: Settings | None = None,
    *,
    mock: MockAirwallex | None = None,
    llm: ChatModel | None = None,
) -> IncidentService:
    s = settings or load_settings()
    if s.use_mock:
        mock = mock or MockAirwallex()
        client = AirwallexClient(
            "https://mock.airwallex.local",
            MOCK_CLIENT_ID,
            MOCK_API_KEY,
            transport=mock.transport(),
        )
        book = (
            InvoiceBook.from_file(s.invoice_book_path)
            if s.invoice_book_path
            else InvoiceBook.mock(mock.now)
        )
    else:
        mock = None
        client = AirwallexClient(
            s.airwallex_base_url,
            s.airwallex_client_id,
            s.airwallex_api_key,
            timeout_s=s.airwallex_timeout_s,
        )
        path = s.invoice_book_path or "data/invoices.sandbox.json"
        book = InvoiceBook.from_file(path)
    if llm is None and not s.use_llm_mock:
        llm = OpenAICompatibleLLM(
            base_url=s.llm_base_url,
            api_key=s.llm_api_key,
            model=s.llm_model,
            timeout_s=s.llm_timeout_s,
        )
    return IncidentService(
        s,
        client,
        book,
        llm=llm,
        mock=mock,
        approval_pin=os.getenv("APPROVAL_PIN", ""),
    )


class IncidentIn(BaseModel):
    text: str = Field(min_length=5, max_length=2000)
    use_llm: bool = True


class ApproveIn(BaseModel):
    approver: str = Field(min_length=1, max_length=80)
    pin: str = ""


class ExecuteIn(BaseModel):
    approval_id: str
    approval_token: str


class TransitionIn(BaseModel):
    next_status: str
    failure_type: str | None = None


class BalanceIn(BaseModel):
    currency: str = Field(min_length=3, max_length=3)
    amount: str


def create_app(svc: IncidentService | None = None) -> FastAPI:
    service = svc or build_service()
    app = FastAPI(title="Ledgerguard", version=__version__)
    app.state.svc = service
    hits: dict[str, deque[float]] = defaultdict(deque)
    limit = int(os.getenv("RATE_LIMIT_PER_10MIN", "60"))

    def _rate(request: Request) -> None:
        ip = request.client.host if request.client else "?"
        now = time.time()
        q = hits[ip]
        while q and now - q[0] > 600:
            q.popleft()
        if len(q) >= limit:
            raise HTTPException(429, "rate limited; try again in a few minutes")
        q.append(now)

    def _inc(incident_id: str) -> Any:
        inc = service.get(incident_id)
        if inc is None:
            raise HTTPException(404, "unknown incident")
        return inc

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "airwallex_mode": service.mode,
            "llm": service.llm.name if service.llm else "scripted-analyst (no LLM)",
            "approval_pin_required": service.gate.requires_pin,
        }

    @app.get("/api/presets")
    async def presets() -> list[dict[str, str]]:
        return fixtures.PRESETS if service.mode == "mock" else []

    @app.get("/api/invoices")
    async def invoices() -> list[dict[str, Any]]:
        return [i.summary() for i in service.book.all()]

    @app.post("/api/incidents")
    async def create_incident(body: IncidentIn, request: Request) -> dict[str, Any]:
        _rate(request)
        inc = await service.investigate(body.text, use_llm=body.use_llm)
        return inc.public()

    @app.get("/api/incidents")
    async def list_incidents() -> list[dict[str, Any]]:
        return [i.public(brief=True) for i in service.incidents.values()]

    @app.get("/api/incidents/{incident_id}")
    async def get_incident(incident_id: str) -> dict[str, Any]:
        return _inc(incident_id).public()

    @app.get("/api/incidents/{incident_id}/report.md", response_class=PlainTextResponse)
    async def report(incident_id: str) -> str:
        return str(_inc(incident_id).report)

    @app.post("/api/incidents/{incident_id}/approve")
    async def approve(incident_id: str, body: ApproveIn) -> dict[str, Any]:
        _inc(incident_id)
        try:
            return service.approve(incident_id, body.approver, body.pin)
        except ApprovalError as exc:
            raise HTTPException(403, str(exc)) from exc

    @app.post("/api/incidents/{incident_id}/execute")
    async def execute(incident_id: str, body: ExecuteIn) -> dict[str, Any]:
        _inc(incident_id)
        return await service.execute(incident_id, body.approval_id, body.approval_token)

    # -- sandbox/mock staging helpers -------------------------------------------
    @app.post("/api/sim/transfers/{transfer_id}/transition")
    async def transition(transfer_id: str, body: TransitionIn) -> dict[str, Any]:
        t = await service.client.simulate_transition(
            transfer_id, body.next_status.upper(), body.failure_type
        )
        return t.summary()

    @app.post("/api/mock/reset")
    async def mock_reset() -> dict[str, Any]:
        if service.mock is None:
            raise HTTPException(400, "only available in mock mode")
        service.mock.reset()
        service.book = InvoiceBook.mock(service.mock.now)
        service.incidents.clear()
        service.guard.clear()
        return {"ok": True}

    @app.post("/api/mock/balance")
    async def mock_balance(body: BalanceIn) -> dict[str, Any]:
        if service.mock is None:
            raise HTTPException(400, "only available in mock mode")
        service.mock.set_balance(body.currency.upper(), body.amount)
        return {"ok": True, "balances": [b.summary() for b in await service.client.get_balances()]}

    # -- MCP over HTTP ----------------------------------------------------------
    @app.post("/mcp")
    async def mcp(request: Request) -> Any:
        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            )
        if isinstance(payload, list):
            out = [r for r in [await handle_message(service, m) for m in payload] if r]
            return JSONResponse(out) if out else JSONResponse(None, status_code=202)
        reply = await handle_message(service, payload)
        if reply is None:
            return JSONResponse(None, status_code=202)
        return JSONResponse(reply)

    # -- Airwallex webhooks -----------------------------------------------------
    @app.post("/webhooks/airwallex")
    async def webhook(request: Request) -> Any:
        raw = await request.body()
        secret = service.settings.webhook_secret
        if secret:
            ts = request.headers.get("x-timestamp", "")
            sig = request.headers.get("x-signature", "")
            expected = hmac.new(secret.encode(), ts.encode() + raw, hashlib.sha256).hexdigest()
            if not sig or not hmac.compare_digest(expected, sig):
                raise HTTPException(400, "failed to verify webhook signature")
            try:
                if abs(time.time() * 1000 - int(ts)) > WEBHOOK_TOLERANCE_MS:
                    raise HTTPException(400, "webhook timestamp outside tolerance")
            except ValueError as exc:
                raise HTTPException(400, "bad x-timestamp") from exc
        elif service.mode != "mock":
            raise HTTPException(400, "AIRWALLEX_WEBHOOK_SECRET is not configured")
        try:
            event = json.loads(raw)
        except ValueError as exc:
            raise HTTPException(400, "body is not JSON") from exc
        return await service.handle_event(event if isinstance(event, dict) else {})

    # -- console ----------------------------------------------------------------
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app


app = create_app()

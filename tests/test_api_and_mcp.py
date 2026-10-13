import hashlib
import hmac
import json
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.main import build_service, create_app
from tests.conftest import TEXT


@pytest.fixture
def api(svc):
    return TestClient(create_app(svc))


def rpc(api, method, params=None, mid=1):
    msg = {"jsonrpc": "2.0", "method": method, "id": mid}
    if params is not None:
        msg["params"] = params
    return api.post("/mcp", json=msg).json()


def test_health_and_console(api):
    h = api.get("/health").json()
    assert h["ok"] and h["airwallex_mode"] == "mock"
    assert "Ledgerguard" in api.get("/").text
    assert api.get("/static/app.js").status_code == 200
    assert len(api.get("/api/presets").json()) >= 7


def test_full_http_flow_with_replay_refused(api):
    inc = api.post("/api/incidents", json={"text": TEXT["FAILED"]}).json()
    assert inc["classification"]["case"] == "FAILED"
    assert inc["action"]["kind"] == "REPLACE_PAYOUT"
    assert inc["timeline"] and inc["trace"]
    appr = api.post(f"/api/incidents/{inc['id']}/approve", json={"approver": "Fernando"}).json()
    body = {"approval_id": appr["approval_id"], "approval_token": appr["approval_token"]}
    r1 = api.post(f"/api/incidents/{inc['id']}/execute", json=body).json()
    assert r1["outcome"] == "transfer_created"
    r2 = api.post(f"/api/incidents/{inc['id']}/execute", json=body).json()
    assert r2["outcome"] == "refused"
    md = api.get(f"/api/incidents/{inc['id']}/report.md").text
    assert md.startswith("# Incident") and "Approved by **Fernando**" in md


def test_agent_name_cannot_approve_over_http(api):
    inc = api.post("/api/incidents", json={"text": TEXT["TRULY_UNPAID"]}).json()
    r = api.post(f"/api/incidents/{inc['id']}/approve", json={"approver": "agent"})
    assert r.status_code == 403


def test_unknown_incident_404(api):
    assert api.get("/api/incidents/nope").status_code == 404


def test_mock_reset_and_balance(api):
    r = api.post("/api/mock/balance", json={"currency": "EUR", "amount": "5"}).json()
    assert any(b["currency"] == "EUR" and b["available"] == "5.00" for b in r["balances"])
    assert api.post("/api/mock/reset").json()["ok"]


def test_sim_transition_endpoint(api):
    t = api.post("/api/sim/transfers/trf_1042a/transition", json={"next_status": "PAID"}).json()
    assert t["status"] == "PAID"


# -- webhooks ----------------------------------------------------------------------
def _signed(secret, body: bytes, ts: str | None = None):
    ts = ts or str(int(time.time() * 1000))
    sig = hmac.new(secret.encode(), ts.encode() + body, hashlib.sha256).hexdigest()
    return {"x-timestamp": ts, "x-signature": sig, "content-type": "application/json"}


def test_webhook_signature_verified(settings, mock):
    svc = build_service(replace(settings, webhook_secret="whsec"), mock=mock)
    api = TestClient(create_app(svc))
    body = json.dumps(
        {"id": "evt_9", "name": "payout.transfer.paid", "data": {"object": {"id": "t"}}}
    ).encode()
    ok = api.post("/webhooks/airwallex", content=body, headers=_signed("whsec", body))
    assert ok.status_code == 200 and ok.json()["status"] == "processed"
    dup = api.post("/webhooks/airwallex", content=body, headers=_signed("whsec", body))
    assert dup.json()["status"] == "duplicate"
    bad = api.post("/webhooks/airwallex", content=body, headers=_signed("wrong", body))
    assert bad.status_code == 400
    old = api.post("/webhooks/airwallex", content=body, headers=_signed("whsec", body, ts="1000"))
    assert old.status_code == 400
    # A re-serialised body must fail: the signature covers the raw bytes.
    h = _signed("whsec", body)
    reser = json.dumps(json.loads(body), indent=2).encode()
    assert api.post("/webhooks/airwallex", content=reser, headers=h).status_code == 400


def test_webhook_non_ascii_signature_is_400_not_500(settings, mock):
    svc = build_service(replace(settings, webhook_secret="whsec"), mock=mock)
    api = TestClient(create_app(svc), raise_server_exceptions=False)
    headers = {b"x-timestamp": str(int(time.time() * 1000)).encode(), b"x-signature": b"\xe9\xe9"}
    r = api.post("/webhooks/airwallex", content=b"{}", headers=headers)
    assert r.status_code == 400


# -- MCP ---------------------------------------------------------------------------
def test_mcp_initialize_and_list(api):
    init = rpc(api, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    assert init["result"]["serverInfo"]["name"] == "ledgerguard"
    assert init["result"]["capabilities"]["tools"] is not None
    note = api.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note.status_code == 202
    tools = {t["name"]: t for t in rpc(api, "tools/list")["result"]["tools"]}
    assert {
        "find_invoice_transfers",
        "get_balances",
        "investigate_incident",
        "execute_approved_action",
    } <= set(tools)
    assert not [n for n in tools if n.startswith(("approve", "grant", "mint"))]  # by design
    assert "WRITE" in tools["execute_approved_action"]["description"]
    assert tools["get_transfer"]["inputSchema"]["required"] == ["transfer_id"]


def test_mcp_read_tool_and_investigate(api):
    r = rpc(
        api,
        "tools/call",
        {"name": "find_invoice_transfers", "arguments": {"invoice_id": "INV-3007"}},
    )
    items = r["result"]["structuredContent"]["items"]
    assert len(items) == 2 and not r["result"]["isError"]
    inv = rpc(
        api,
        "tools/call",
        {"name": "investigate_incident", "arguments": {"text": TEXT["DUPLICATE_RISK"]}},
    )
    out = inv["result"]["structuredContent"]
    assert out["action"]["kind"] == "CANCEL_DUPLICATE"
    assert out["approval"]["status"] == "pending"


def test_mcp_execute_without_human_token_refused(api, mock):
    inv = rpc(
        api,
        "tools/call",
        {"name": "investigate_incident", "arguments": {"text": TEXT["TRULY_UNPAID"]}},
    )
    out = inv["result"]["structuredContent"]
    r = rpc(
        api,
        "tools/call",
        {
            "name": "execute_approved_action",
            "arguments": {
                "incident_id": out["id"],
                "approval_id": out["approval"]["approval_id"],
                "approval_token": "guess",
            },
        },
    )
    assert r["result"]["isError"] is True
    assert not [t for t in mock.transfers.values() if t["metadata"].get("invoice_id") == "INV-4100"]


def test_mcp_execute_with_human_token_works(api):
    inv = rpc(
        api,
        "tools/call",
        {"name": "investigate_incident", "arguments": {"text": TEXT["TRULY_UNPAID"]}},
    )
    out = inv["result"]["structuredContent"]
    appr = api.post(f"/api/incidents/{out['id']}/approve", json={"approver": "Fernando"}).json()
    r = rpc(
        api,
        "tools/call",
        {
            "name": "execute_approved_action",
            "arguments": {
                "incident_id": out["id"],
                "approval_id": appr["approval_id"],
                "approval_token": appr["approval_token"],
            },
        },
    )
    assert r["result"]["structuredContent"]["outcome"] == "transfer_created"


def test_mcp_errors(api):
    assert rpc(api, "nope")["error"]["code"] == -32601
    assert rpc(api, "tools/call", {"name": "missing"})["error"]["code"] == -32602
    bad = api.post(
        "/mcp", content=b"{not json", headers={"content-type": "application/json"}
    ).json()
    assert bad["error"]["code"] == -32700
    assert rpc(api, "ping")["result"] == {}
    for params in ("x", [1], 5):
        assert rpc(api, "initialize", params)["error"]["code"] == -32602
        assert rpc(api, "tools/call", params)["error"]["code"] == -32602
    r = rpc(api, "tools/call", {"name": "get_transfer", "arguments": {}})
    assert r["result"]["isError"]

import httpx
import pytest

from app.airwallex.client import AirwallexClient, AirwallexError
from app.airwallex.mock import MOCK_API_KEY, MOCK_CLIENT_ID, MockAirwallex
from app.config import ConfigError, Settings


def client_for(mock: MockAirwallex) -> AirwallexClient:
    return AirwallexClient("https://mock", MOCK_CLIENT_ID, MOCK_API_KEY, transport=mock.transport())


PAYLOAD = {
    "request_id": "lg-test-0000000001",
    "beneficiary_id": "ben_brightwater_eur",
    "transfer_amount": "100",
    "transfer_currency": "EUR",
    "source_currency": "EUR",
    "transfer_method": "LOCAL",
    "reference": "INV-4100",
    "reason": "payment_to_supplier",
    "metadata": {"invoice_id": "INV-4100"},
}


async def test_login_once_and_token_reused(mock):
    c = client_for(mock)
    await c.get_balances()
    await c.get_balances()
    assert len(mock.tokens) == 1


async def test_login_requires_credentials(mock):
    c = AirwallexClient("https://mock", "", "", transport=mock.transport())
    with pytest.raises(AirwallexError) as e:
        await c.get_balances()
    assert e.value.status == 401


async def test_relogin_on_401(mock):
    c = client_for(mock)
    await c.get_balances()
    mock.tokens.clear()  # server-side expiry
    bals = await c.get_balances()
    assert bals and len(mock.tokens) == 1


async def test_backoff_on_429(monkeypatch):
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/login"):
            return httpx.Response(201, json={"token": "t"})
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"code": "too_many_requests"})
        return httpx.Response(200, json=[{"currency": "USD", "available_amount": 1}])

    async def nosleep(_s: float) -> None:
        return None

    monkeypatch.setattr("app.airwallex.client.asyncio.sleep", nosleep)
    c = AirwallexClient("https://x", "a", "b", transport=httpx.MockTransport(handler))
    bals = await c.get_balances()
    assert bals[0].currency == "USD" and calls["n"] == 3


async def test_balances_accepts_bare_array_or_items():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/login"):
            return httpx.Response(201, json={"token": "t"})
        return httpx.Response(200, json={"items": [{"currency": "EUR", "available_amount": 2}]})

    c = AirwallexClient("https://x", "a", "b", transport=httpx.MockTransport(handler))
    assert (await c.get_balances())[0].currency == "EUR"


async def test_create_transfer_and_duplicate_request_id_returns_existing(mock):
    c = client_for(mock)
    t1, created = await c.create_transfer(dict(PAYLOAD))
    assert created and t1.status == "SCHEDULED"
    t2, created2 = await c.create_transfer(dict(PAYLOAD))
    assert not created2 and t2.id == t1.id
    assert sum(1 for t in mock.transfers.values() if t["request_id"] == PAYLOAD["request_id"]) == 1


async def test_ambiguous_500_after_commit_resolves_by_request_id(mock):
    c = client_for(mock)
    mock.fail_next_create_after_commit = True
    t, created = await c.create_transfer(dict(PAYLOAD, request_id="lg-test-outage-0001"))
    assert not created
    assert t.request_id == "lg-test-outage-0001"
    assert sum(1 for x in mock.transfers.values() if x["request_id"] == t.request_id) == 1


async def test_clean_rejection_is_not_swallowed(mock):
    c = client_for(mock)
    mock.set_balance("EUR", "1")
    with pytest.raises(AirwallexError) as e:
        await c.create_transfer(dict(PAYLOAD, request_id="lg-test-broke-00001"))
    assert e.value.is_insufficient_funds


async def test_request_id_length_enforced(mock):
    c = client_for(mock)
    with pytest.raises(ValueError):
        await c.create_transfer(dict(PAYLOAD, request_id="short"))


async def test_cancel_and_simulation(mock):
    c = client_for(mock)
    t, _ = await c.create_transfer(dict(PAYLOAD, request_id="lg-test-cancel-0001"))
    t = await c.cancel_transfer(t.id)
    assert t.status == "CANCELLED"
    t2, _ = await c.create_transfer(dict(PAYLOAD, request_id="lg-test-sim-000001"))
    for s in ("PROCESSING", "SENT"):
        t2 = await c.simulate_transition(t2.id, s)
    t2 = await c.simulate_transition(t2.id, "FAILED", "BENEFICIARY_BANK_RETURNED")
    assert t2.status == "CANCELLED" and t2.failure_type == "BENEFICIARY_BANK_RETURNED"


async def test_cannot_cancel_sent(mock):
    c = client_for(mock)
    with pytest.raises(AirwallexError):
        await c.cancel_transfer("trf_1042a")  # SENT


def test_production_host_refused():
    with pytest.raises(ConfigError):
        Settings(airwallex_base_url="https://api.airwallex.com").validate()
    Settings(airwallex_base_url="https://api.sandbox.airwallex.com").validate()


def test_sandbox_mode_needs_keys():
    with pytest.raises(ConfigError):
        Settings(airwallex_mode="sandbox").validate()
    with pytest.raises(ConfigError):
        Settings(airwallex_mode="prod").validate()

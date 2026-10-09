from __future__ import annotations

import pytest

from app.airwallex.mock import MockAirwallex
from app.config import Settings
from app.main import build_service
from app.service import IncidentService


@pytest.fixture
def mock() -> MockAirwallex:
    return MockAirwallex()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(approval_secret="test-secret", ledger_path=str(tmp_path / "ledger.json"))


@pytest.fixture
def svc(settings: Settings, mock: MockAirwallex) -> IncidentService:
    return build_service(settings, mock=mock)


TEXT = {
    "IN_FLIGHT": "Kestrel Supply Co says invoice INV-1042 was never paid",
    "FAILED": "Atlas Components: EUR 4,000 for INV-2001 has not arrived",
    "RETURNED": "Lumen Packaging never received GBP 1,850 for INV-2002",
    "BAD_BENEFICIARY": "Orchid Textiles chasing INV-2003, pay the new account",
    "TRULY_UNPAID": "Brightwater Studio: INV-4100 for EUR 2,750 is overdue",
    "DUPLICATE_RISK": "Meridian Parts says INV-3007 is unpaid",
    "PAID_CONFIRMED": "Northwind Logistics claims INV-1043 was never paid",
    "COMPLIANCE": "Tamarind Freight says INV-2004 bounced, resend today",
}


async def approve_and_execute(svc: IncidentService, inc_id: str, who: str = "Fernando"):
    appr = svc.approve(inc_id, who)
    return appr, await svc.execute(inc_id, appr["approval_id"], appr["approval_token"])

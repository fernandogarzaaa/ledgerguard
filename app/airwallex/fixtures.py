"""Synthetic sandbox world for mock mode: one company, seven supplier invoices.

Every incident class Ledgerguard knows has at least one invoice here. Times are
offsets from "now" so the timeline always looks recent. All names, accounts and
routing numbers are synthetic (ABA 021000021 / sort code 040004 / IBANs with
valid checksums are public test identifiers, not real accounts).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.airwallex.models import iso

COMPANY = "Ledgerguard Demo Co"

BENEFICIARIES: list[dict[str, Any]] = [
    {
        "id": "ben_kestrel_usd",
        "nickname": "Kestrel Supply Co",
        "transfer_methods": ["LOCAL"],
        "payer_entity_type": "COMPANY",
        "beneficiary": {
            "entity_type": "COMPANY",
            "company_name": "Kestrel Supply Co",
            "address": {
                "street_address": "410 Harbor Way",
                "city": "Oakland",
                "state": "CA",
                "postcode": "94607",
                "country_code": "US",
            },
            "bank_details": {
                "account_currency": "USD",
                "account_name": "Kestrel Supply Co",
                "bank_country_code": "US",
                "account_number": "000123456789",
                "account_routing_type1": "aba",
                "account_routing_value1": "021000021",
                "bank_account_category": "Checking",
            },
        },
    },
    {
        "id": "ben_northwind_gbp",
        "nickname": "Northwind Logistics Ltd",
        "transfer_methods": ["LOCAL"],
        "payer_entity_type": "COMPANY",
        "beneficiary": {
            "entity_type": "COMPANY",
            "company_name": "Northwind Logistics Ltd",
            "address": {
                "street_address": "12 Dock Road",
                "city": "Felixstowe",
                "postcode": "IP11 3SX",
                "country_code": "GB",
            },
            "bank_details": {
                "account_currency": "GBP",
                "account_name": "Northwind Logistics Ltd",
                "bank_country_code": "GB",
                "bank_name": "Synthetic Bank UK",
                "account_number": "41234567",
                "account_routing_type1": "sort_code",
                "account_routing_value1": "040004",
            },
        },
    },
    {
        "id": "ben_atlas_eur",
        "nickname": "Atlas Components GmbH",
        "transfer_methods": ["SWIFT"],
        "payer_entity_type": "COMPANY",
        "beneficiary": {
            "entity_type": "COMPANY",
            "company_name": "Atlas Components GmbH",
            "address": {
                "street_address": "Industriestrasse 7",
                "city": "Stuttgart",
                "postcode": "70565",
                "country_code": "DE",
            },
            "bank_details": {
                "account_currency": "EUR",
                "account_name": "Atlas Components GmbH",
                "bank_country_code": "DE",
                "iban": "DE89370400440532013000",
                "swift_code": "COBADEFFXXX",
            },
        },
    },
    {
        "id": "ben_lumen_gbp",
        "nickname": "Lumen Packaging Ltd",
        "transfer_methods": ["LOCAL"],
        "payer_entity_type": "COMPANY",
        "beneficiary": {
            "entity_type": "COMPANY",
            "company_name": "Lumen Packaging Ltd",
            "address": {
                "street_address": "3 Mill Lane",
                "city": "Leeds",
                "postcode": "LS1 4AP",
                "country_code": "GB",
            },
            "bank_details": {
                "account_currency": "GBP",
                "account_name": "Lumen Packaging Ltd",
                "bank_country_code": "GB",
                "bank_name": "Synthetic Bank UK",
                "account_number": "55779911",
                "account_routing_type1": "sort_code",
                "account_routing_value1": "040004",
            },
        },
    },
    {
        "id": "ben_orchid_usd",
        "nickname": "Orchid Textiles",
        "transfer_methods": ["LOCAL"],
        "payer_entity_type": "COMPANY",
        "beneficiary": {
            "entity_type": "COMPANY",
            "company_name": "Orchid Textiles Inc",
            "address": {
                "street_address": "88 Garment Ave",
                "city": "Los Angeles",
                "state": "CA",
                "postcode": "90015",
                "country_code": "US",
            },
            "bank_details": {
                "account_currency": "USD",
                "account_name": "Orchid Textile Holdings",
                "bank_country_code": "US",
                "account_number": "000987650001",
                "account_routing_type1": "aba",
                "account_routing_value1": "021000021",
                "bank_account_category": "Checking",
            },
        },
    },
    {
        "id": "ben_meridian_usd",
        "nickname": "Meridian Parts",
        "transfer_methods": ["LOCAL"],
        "payer_entity_type": "COMPANY",
        "beneficiary": {
            "entity_type": "COMPANY",
            "company_name": "Meridian Parts LLC",
            "address": {
                "street_address": "900 Foundry St",
                "city": "Detroit",
                "state": "MI",
                "postcode": "48207",
                "country_code": "US",
            },
            "bank_details": {
                "account_currency": "USD",
                "account_name": "Meridian Parts LLC",
                "bank_country_code": "US",
                "account_number": "000555000777",
                "account_routing_type1": "aba",
                "account_routing_value1": "021000021",
                "bank_account_category": "Checking",
            },
        },
    },
    {
        "id": "ben_brightwater_eur",
        "nickname": "Brightwater Studio BV",
        "transfer_methods": ["LOCAL"],
        "payer_entity_type": "COMPANY",
        "beneficiary": {
            "entity_type": "COMPANY",
            "company_name": "Brightwater Studio BV",
            "address": {
                "street_address": "Keizersgracht 100",
                "city": "Amsterdam",
                "postcode": "1015 CV",
                "country_code": "NL",
            },
            "bank_details": {
                "account_currency": "EUR",
                "account_name": "Brightwater Studio BV",
                "bank_country_code": "NL",
                "iban": "NL91ABNA0417164300",
            },
        },
    },
    {
        "id": "ben_tamarind_usd",
        "nickname": "Tamarind Freight",
        "transfer_methods": ["LOCAL"],
        "payer_entity_type": "COMPANY",
        "beneficiary": {
            "entity_type": "COMPANY",
            "company_name": "Tamarind Freight Pte Ltd",
            "address": {
                "street_address": "1 Pasir Panjang Rd",
                "city": "Singapore",
                "postcode": "118479",
                "country_code": "SG",
            },
            "bank_details": {
                "account_currency": "USD",
                "account_name": "Tamarind Freight Pte Ltd",
                "bank_country_code": "US",
                "account_number": "000444333222",
                "account_routing_type1": "aba",
                "account_routing_value1": "021000021",
                "bank_account_category": "Checking",
            },
        },
    },
]


def invoice_book(now: datetime) -> list[dict[str, Any]]:
    """The company's AP ledger: what we owe, to whom, and vendor-master changes.

    Airwallex does not hold payable invoices, so this lives in the AP system
    (here a JSON book). ``bank_details_changed_at`` is the vendor-master audit
    trail, used for the "changed bank details just before payment" fraud hold.
    """

    def d(hours: float) -> str:
        return iso(now - timedelta(hours=hours))

    return [
        {
            "invoice_id": "INV-1042",
            "supplier": "Kestrel Supply Co",
            "beneficiary_id": "ben_kestrel_usd",
            "amount": "3200.00",
            "currency": "USD",
            "transfer_method": "LOCAL",
            "due_date": d(24)[:10],
            "supplier_contact": "ap@kestrel.example",
        },
        {
            "invoice_id": "INV-1043",
            "supplier": "Northwind Logistics Ltd",
            "beneficiary_id": "ben_northwind_gbp",
            "amount": "1480.00",
            "currency": "GBP",
            "transfer_method": "LOCAL",
            "due_date": d(96)[:10],
            "supplier_contact": "accounts@northwind.example",
        },
        {
            "invoice_id": "INV-2001",
            "supplier": "Atlas Components GmbH",
            "beneficiary_id": "ben_atlas_eur",
            "amount": "4000.00",
            "currency": "EUR",
            "transfer_method": "SWIFT",
            "due_date": d(48)[:10],
            "supplier_contact": "buchhaltung@atlas.example",
        },
        {
            "invoice_id": "INV-2002",
            "supplier": "Lumen Packaging Ltd",
            "beneficiary_id": "ben_lumen_gbp",
            "amount": "1850.00",
            "currency": "GBP",
            "transfer_method": "LOCAL",
            "due_date": d(72)[:10],
            "supplier_contact": "finance@lumen.example",
        },
        {
            "invoice_id": "INV-2003",
            "supplier": "Orchid Textiles",
            "beneficiary_id": "ben_orchid_usd",
            "amount": "2400.00",
            "currency": "USD",
            "transfer_method": "LOCAL",
            "due_date": d(30)[:10],
            "supplier_contact": "billing@orchid.example",
            "bank_details_changed_at": d(70),
            "bank_details_change_source": "email request from billing@orchid-textiles.example",
        },
        {
            "invoice_id": "INV-2004",
            "supplier": "Tamarind Freight",
            "beneficiary_id": "ben_tamarind_usd",
            "amount": "6100.00",
            "currency": "USD",
            "transfer_method": "LOCAL",
            "due_date": d(12)[:10],
            "supplier_contact": "ops@tamarind.example",
        },
        {
            "invoice_id": "INV-3007",
            "supplier": "Meridian Parts",
            "beneficiary_id": "ben_meridian_usd",
            "amount": "5600.00",
            "currency": "USD",
            "transfer_method": "LOCAL",
            "due_date": d(60)[:10],
            "supplier_contact": "ar@meridian.example",
        },
        {
            "invoice_id": "INV-4100",
            "supplier": "Brightwater Studio BV",
            "beneficiary_id": "ben_brightwater_eur",
            "amount": "2750.00",
            "currency": "EUR",
            "transfer_method": "LOCAL",
            "due_date": d(20)[:10],
            "supplier_contact": "studio@brightwater.example",
        },
    ]


def _transfer(
    now: datetime,
    *,
    tid: str,
    short_ref: str,
    request_id: str,
    invoice: str,
    ben: str,
    amount: str,
    currency: str,
    method: str,
    status: str,
    created_h: float,
    updated_h: float,
    source_currency: str | None = None,
    source_amount: str | None = None,
    fee: str = "0",
    failure_type: str = "",
    failure_reason: str = "",
    failure_code: str = "",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": tid,
        "short_reference_id": short_ref,
        "request_id": request_id,
        "status": status,
        "beneficiary_id": ben,
        "transfer_amount": float(amount),
        "transfer_currency": currency,
        "source_amount": float(source_amount or amount),
        "source_currency": source_currency or currency,
        "transfer_method": method,
        "reference": invoice,
        "reason": "payment_to_supplier",
        "metadata": {"invoice_id": invoice},
        "fee_amount": float(fee),
        "fee_currency": source_currency or currency,
        "fee_paid_by": "PAYER",
        "created_at": iso(now - timedelta(hours=created_h)),
        "updated_at": iso(now - timedelta(hours=updated_h)),
        "transfer_date": iso(now - timedelta(hours=created_h))[:10],
    }
    if failure_type:
        # Real responses use one of two shapes; the mock deliberately mixes
        # them so the parser is exercised on both (see AIRWALLEX_VERIFY.md).
        if failure_code:
            row["failure"] = {
                "code": failure_code,
                "message": failure_reason,
                "details": {"type": failure_type, "reason_codes": []},
            }
        else:
            row["failure_type"] = failure_type
            row["failure_reason"] = failure_reason
    return row


def transfers(now: datetime) -> list[dict[str, Any]]:
    return [
        # IN_FLIGHT: SENT 30h ago, not yet PAID.
        _transfer(
            now,
            tid="trf_1042a",
            short_ref="P261008-KST1042",
            request_id="lg-seed-inv1042-a1b2c3d4",
            invoice="INV-1042",
            ben="ben_kestrel_usd",
            amount="3200.00",
            currency="USD",
            method="LOCAL",
            status="SENT",
            created_h=34,
            updated_h=30,
        ),
        # PAID_CONFIRMED: paid 3 days ago; the supplier is mistaken.
        _transfer(
            now,
            tid="trf_1043a",
            short_ref="P261005-NWD1043",
            request_id="lg-seed-inv1043-e5f6a7b8",
            invoice="INV-1043",
            ben="ben_northwind_gbp",
            amount="1480.00",
            currency="GBP",
            method="LOCAL",
            status="PAID",
            created_h=80,
            updated_h=74,
        ),
        # FAILED (retryable): SWIFT payout timed out downstream, auto-cancelled.
        _transfer(
            now,
            tid="trf_2001a",
            short_ref="P261006-ATL2001",
            request_id="lg-seed-inv2001-c9d0e1f2",
            invoice="INV-2001",
            ben="ben_atlas_eur",
            amount="4000.00",
            currency="EUR",
            method="SWIFT",
            status="CANCELLED",
            created_h=52,
            updated_h=40,
            fee="12.85",
            failure_type="CHANNEL_TIMEOUT",
            failure_reason="91402 Channel timeout",
            failure_code="91402",
        ),
        # RETURNED: reached PAID, then the beneficiary bank returned it.
        _transfer(
            now,
            tid="trf_2002a",
            short_ref="P261006-LMN2002",
            request_id="lg-seed-inv2002-a3b4c5d6",
            invoice="INV-2002",
            ben="ben_lumen_gbp",
            amount="1850.00",
            currency="GBP",
            method="LOCAL",
            status="CANCELLED",
            created_h=76,
            updated_h=20,
            failure_type="BENEFICIARY_BANK_RETURNED",
            failure_reason="90802 Beneficiary bank returned",
        ),
        # BAD_BENEFICIARY: account name/number rejected; details had just changed.
        _transfer(
            now,
            tid="trf_2003a",
            short_ref="P261007-ORC2003",
            request_id="lg-seed-inv2003-e7f8a9b0",
            invoice="INV-2003",
            ben="ben_orchid_usd",
            amount="2400.00",
            currency="USD",
            method="LOCAL",
            status="CANCELLED",
            created_h=46,
            updated_h=28,
            failure_type="INVALID_ACCOUNT_NAME_OR_NUMBER",
            failure_reason="90101 Invalid account name/number",
            failure_code="90101",
        ),
        # FAILED (non-retryable): compliance hold, must escalate.
        _transfer(
            now,
            tid="trf_2004a",
            short_ref="P261008-TMR2004",
            request_id="lg-seed-inv2004-c1d2e3f4",
            invoice="INV-2004",
            ben="ben_tamarind_usd",
            amount="6100.00",
            currency="USD",
            method="LOCAL",
            status="CANCELLED",
            created_h=20,
            updated_h=14,
            failure_type="TM_SUSPENDED",
            failure_reason="90502 TM suspended",
            failure_code="90502",
        ),
        # DUPLICATE_RISK: paid 2 days ago, and someone queued a second payout today.
        _transfer(
            now,
            tid="trf_3007a",
            short_ref="P261008-MRD3007",
            request_id="lg-seed-inv3007-a5b6c7d8",
            invoice="INV-3007",
            ben="ben_meridian_usd",
            amount="5600.00",
            currency="USD",
            method="LOCAL",
            status="PAID",
            created_h=56,
            updated_h=48,
        ),
        _transfer(
            now,
            tid="trf_3007b",
            short_ref="P261010-MRD3007B",
            request_id="manual-retry-inv3007-0001",
            invoice="INV-3007",
            ben="ben_meridian_usd",
            amount="5600.00",
            currency="USD",
            method="LOCAL",
            status="SCHEDULED",
            created_h=3,
            updated_h=3,
        ),
        # INV-4100: nothing. Truly unpaid.
    ]


def conversions(now: datetime) -> list[dict[str, Any]]:
    return [
        {
            "conversion_id": "conv_usdeur_2001",
            "short_reference_id": "C261006-USDEUR",
            "request_id": "lg-seed-conv-2001-9a8b7c6d",
            "status": "SETTLED",
            "buy_currency": "EUR",
            "buy_amount": 4012.85,
            "sell_currency": "USD",
            "sell_amount": 4374.01,
            "client_rate": 0.917432,
            "currency_pair": "EURUSD",
            "created_at": iso(now - timedelta(hours=53)),
            "conversion_date": iso(now - timedelta(hours=53))[:10],
        },
        {
            "conversion_id": "conv_usdgbp_1043",
            "short_reference_id": "C261005-USDGBP",
            "request_id": "lg-seed-conv-1043-1a2b3c4d",
            "status": "SETTLED",
            "buy_currency": "GBP",
            "buy_amount": 3500.00,
            "sell_currency": "USD",
            "sell_amount": 4592.00,
            "client_rate": 0.762195,
            "currency_pair": "GBPUSD",
            "created_at": iso(now - timedelta(hours=82)),
            "conversion_date": iso(now - timedelta(hours=82))[:10],
        },
    ]


def balances() -> dict[str, str]:
    # After the INV-2001 refund (4000 back, fee kept) and the INV-2002 return.
    return {"USD": "18250.00", "EUR": "4120.00", "GBP": "2170.00"}


# Indicative mock rates: units of SELL currency per 1 unit of BUY currency.
MOCK_RATES: dict[tuple[str, str], str] = {
    ("EUR", "USD"): "1.0905",
    ("GBP", "USD"): "1.3120",
    ("USD", "EUR"): "0.9170",
    ("USD", "GBP"): "0.7622",
    ("EUR", "GBP"): "0.8312",
    ("GBP", "EUR"): "1.2031",
}

SWIFT_FLAT_FEE: dict[str, str] = {"EUR": "12.85"}

PRESETS: list[dict[str, str]] = [
    {
        "label": "In flight · INV-1042",
        "expect": "IN_FLIGHT",
        "text": "Kestrel Supply Co says invoice INV-1042 (USD 3,200) was never paid. "
        "Their due date was yesterday and they are threatening to hold our next shipment.",
    },
    {
        "label": "Failed · INV-2001",
        "expect": "FAILED",
        "text": "Atlas Components emailed: EUR 4,000 for INV-2001 still has not arrived, "
        "it is two days late. Can we check what happened?",
    },
    {
        "label": "Returned · INV-2002",
        "expect": "RETURNED",
        "text": "Lumen Packaging says they never received GBP 1,850 for INV-2002.",
    },
    {
        "label": "Bad beneficiary · INV-2003",
        "expect": "BAD_BENEFICIARY",
        "text": "Orchid Textiles chasing INV-2003, USD 2,400. They say please pay to the new "
        "account they sent last week.",
    },
    {
        "label": "Truly unpaid · INV-4100",
        "expect": "TRULY_UNPAID",
        "text": "Brightwater Studio: invoice INV-4100 for EUR 2,750 is overdue, nothing received.",
    },
    {
        "label": "Duplicate risk · INV-3007",
        "expect": "DUPLICATE_RISK",
        "text": "Meridian Parts says INV-3007 (USD 5,600) is unpaid. Our AP clerk already "
        "queued a retry this morning.",
    },
    {
        "label": "Paid · INV-1043",
        "expect": "PAID_CONFIRMED",
        "text": "Northwind Logistics claims INV-1043 was never paid.",
    },
    {
        "label": "Compliance hold · INV-2004",
        "expect": "FAILED",
        "text": "Tamarind Freight says USD 6,100 for INV-2004 bounced. Please resend today.",
    },
]

"""Duplicate-payout guard: one live payout per invoice, enforced in code.

Two independent checks, so a lagging Airwallex list cannot open a gap:

1. Airwallex truth: any transfer for the invoice that is in flight or PAID
   blocks a new payout.
2. Local lock: before calling Airwallex we take a per-invoice lock that records
   the request_id. A second payout for the same invoice with a different
   request_id is refused while that lock is held, even if Airwallex has not yet
   listed the first transfer. The lock is only released when the locked
   transfer is confirmed failed/cancelled.

The ledger is a small JSON file (or memory) so the lock survives restarts.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.airwallex.models import Transfer


class DuplicatePayoutError(RuntimeError):
    pass


@dataclass
class LockEntry:
    invoice_id: str
    request_id: str
    state: str  # pending | live | released
    transfer_id: str = ""
    incident_id: str = ""
    at: str = ""
    note: str = ""


class DuplicatePayoutGuard:
    def __init__(self, path: str | None = None) -> None:
        self._path = Path(path) if path else None
        self._mutex = threading.Lock()
        self._locks: dict[str, LockEntry] = {}
        self.refusals: list[dict[str, Any]] = []
        if self._path and self._path.exists():
            raw = json.loads(self._path.read_text(encoding="utf-8") or "{}")
            self._locks = {k: LockEntry(**v) for k, v in raw.items()}

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({k: asdict(v) for k, v in self._locks.items()}, indent=2), encoding="utf-8"
        )
        tmp.replace(self._path)

    def lock_for(self, invoice_id: str) -> LockEntry | None:
        return self._locks.get(invoice_id.upper())

    def _refuse(self, invoice_id: str, reason: str) -> None:
        self.refusals.append(
            {"invoice_id": invoice_id, "reason": reason, "at": datetime.now(UTC).isoformat()}
        )
        raise DuplicatePayoutError(reason)

    def acquire(
        self,
        invoice_id: str,
        request_id: str,
        transfers_for_invoice: list[Transfer],
        *,
        incident_id: str = "",
    ) -> LockEntry:
        """Check both rules and take the lock, atomically. Raises DuplicatePayoutError."""
        key = invoice_id.upper()
        with self._mutex:
            live = [t for t in transfers_for_invoice if t.is_live]
            if live:
                t = live[0]
                self._refuse(
                    invoice_id,
                    f"refused: {invoice_id} already has a live payout "
                    f"{t.short_reference_id or t.id} ({t.status}); a second payout would pay twice",
                )
            held = self._locks.get(key)
            if held is not None and held.state in ("pending", "live"):
                if held.request_id != request_id:
                    # Release only if Airwallex confirms the locked transfer failed.
                    locked = next(
                        (
                            t
                            for t in transfers_for_invoice
                            if held.transfer_id and t.id == held.transfer_id
                        ),
                        None,
                    )
                    if locked is None or not locked.is_failed:
                        self._refuse(
                            invoice_id,
                            f"refused: {invoice_id} is locked by payout request {held.request_id} "
                            f"({held.state}{', ' + held.transfer_id if held.transfer_id else ''})",
                        )
                elif held.state == "live":
                    self._refuse(
                        invoice_id,
                        f"refused: request {request_id} for {invoice_id} already executed "
                        f"(transfer {held.transfer_id})",
                    )
            entry = LockEntry(
                invoice_id=key,
                request_id=request_id,
                state="pending",
                incident_id=incident_id,
                at=datetime.now(UTC).isoformat(),
            )
            self._locks[key] = entry
            self._save()
            return entry

    def confirm(self, invoice_id: str, request_id: str, transfer: Transfer) -> None:
        key = invoice_id.upper()
        with self._mutex:
            entry = self._locks.get(key)
            if entry is None or entry.request_id != request_id:
                return
            entry.transfer_id = transfer.id
            entry.state = "released" if transfer.is_failed else "live"
            entry.at = datetime.now(UTC).isoformat()
            self._save()

    def abort(self, invoice_id: str, request_id: str, note: str) -> None:
        """The create definitely did not happen (a clean 4xx): free the lock."""
        key = invoice_id.upper()
        with self._mutex:
            entry = self._locks.get(key)
            if entry is not None and entry.request_id == request_id and entry.state == "pending":
                entry.state = "released"
                entry.note = note
                self._save()

    def clear(self) -> None:
        with self._mutex:
            self._locks.clear()
            self.refusals.clear()
            self._save()

    def snapshot(self) -> dict[str, Any]:
        return {k: asdict(v) for k, v in self._locks.items()}

"""Shared dataclasses.

CardSnapshot / ProbeResult / Event / PropertyRow are immutable per-run values.
SyncReport is the Sheets sync return value.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional
from uuid import UUID

from . import config


@dataclass(frozen=True)
class CardSnapshot:
    canonical_url: str
    listing_url: str
    address: str
    suburb_slug: str
    state_slug: str
    price_text: str
    agent_name: str
    agency_name: str
    bed: Optional[int]
    bath: Optional[int]
    car: Optional[int]
    property_type: Optional[str]
    status_badge: str
    raw_card: dict[str, Any] = field(default_factory=dict)

    def compute_fingerprint(self) -> str:
        parts = []
        for key in config.FINGERPRINT_FIELDS:
            v = getattr(self, key, None)
            parts.append((v or "").strip().lower())
        joined = "|".join(parts).encode("utf-8")
        return hashlib.sha256(joined).hexdigest()


@dataclass(frozen=True)
class ProbeResult:
    outcome: str  # "SOLD" | "STILL_LISTED" | "NOT_FOUND"
    sold_price: Optional[str] = None
    sold_date: Optional[date] = None


@dataclass(frozen=True)
class Event:
    property_id: UUID
    event_type: str  # NEW | PRICE_CHANGED | SOLD | WITHDRAWN | RELISTED
    payload: dict[str, Any]
    occurred_at: Optional[datetime] = None  # server-side default if None


@dataclass(frozen=True)
class PropertyRow:
    id: UUID
    canonical_url: str
    current_price: Optional[str]
    current_status: str
    consecutive_misses: int
    last_seen_at: datetime
    fingerprint: str


@dataclass(frozen=True)
class SyncReport:
    properties_upserted: int
    events_appended: int
    duration_ms: int

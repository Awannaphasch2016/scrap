"""In-memory store satisfying the diff.engine.Store protocol.

Used by --dry-store smoke tests and by unit tests. Two-tier dict layout:
  properties[id] = dict of property row fields
  by_canonical[canonical_url] = id   (index)
  events = list[Event]               (append-only)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional
from uuid import UUID, uuid4

from ..types import CardSnapshot, Event, PropertyRow


class MemoryStore:
    def __init__(self) -> None:
        self.properties: dict[UUID, dict] = {}
        self.by_canonical: dict[str, UUID] = {}
        self.events: list[Event] = []

    # ---- diff.engine.Store protocol ----

    def get_by_canonical(self, canonical_url: str) -> Optional[PropertyRow]:
        pid = self.by_canonical.get(canonical_url)
        if pid is None:
            return None
        row = self.properties[pid]
        return PropertyRow(
            id=pid,
            canonical_url=row["canonical_url"],
            current_price=row.get("current_price"),
            current_status=row.get("current_status", "ACTIVE"),
            consecutive_misses=row.get("consecutive_misses", 0),
            last_seen_at=row.get("last_seen_at", datetime.now()),
            fingerprint=row.get("fingerprint", ""),
        )

    def insert_property(self, snap: CardSnapshot, fingerprint: str) -> UUID:
        pid = uuid4()
        now = datetime.now()
        self.properties[pid] = {
            "canonical_url": snap.canonical_url,
            "listing_url": snap.listing_url,
            "address": snap.address,
            "suburb_slug": snap.suburb_slug,
            "state_slug": snap.state_slug,
            "bed": snap.bed,
            "bath": snap.bath,
            "car": snap.car,
            "property_type": snap.property_type,
            "listed_at_observed": now.date(),
            "first_seen_at": now,
            "last_seen_at": now,
            "consecutive_misses": 0,
            "current_price": snap.price_text,
            "current_status": "ACTIVE",
            "current_agent": snap.agent_name,
            "current_agency": snap.agency_name,
            "fingerprint": fingerprint,
            "raw_card": snap.raw_card,
        }
        self.by_canonical[snap.canonical_url] = pid
        return pid

    def update_property(self, id: UUID, snap: CardSnapshot, fingerprint: str) -> None:
        row = self.properties[id]
        row.update({
            "listing_url": snap.listing_url,
            "current_price": snap.price_text,
            "current_agent": snap.agent_name,
            "current_agency": snap.agency_name,
            "fingerprint": fingerprint,
            "raw_card": snap.raw_card,
        })

    def mark_seen(self, id: UUID) -> None:
        row = self.properties[id]
        row["last_seen_at"] = datetime.now()
        row["consecutive_misses"] = 0

    def mark_miss(self, id: UUID) -> int:
        row = self.properties[id]
        row["consecutive_misses"] = row.get("consecutive_misses", 0) + 1
        return row["consecutive_misses"]

    def update_status(self, id: UUID, status: str) -> None:
        self.properties[id]["current_status"] = status

    def insert_event(self, ev: Event) -> None:
        if ev.occurred_at is None:
            ev = Event(
                property_id=ev.property_id,
                event_type=ev.event_type,
                payload=ev.payload,
                occurred_at=datetime.now(),
            )
        self.events.append(ev)

    def select_missing_since(self, d: date) -> list[PropertyRow]:
        threshold = datetime.combine(d, datetime.min.time())
        out: list[PropertyRow] = []
        for pid, row in self.properties.items():
            if (
                row.get("current_status") == "ACTIVE"
                and row.get("last_seen_at", datetime.now()) < threshold
            ):
                out.append(PropertyRow(
                    id=pid,
                    canonical_url=row["canonical_url"],
                    current_price=row.get("current_price"),
                    current_status=row["current_status"],
                    consecutive_misses=row.get("consecutive_misses", 0),
                    last_seen_at=row["last_seen_at"],
                    fingerprint=row.get("fingerprint", ""),
                ))
        return out

    # ---- additional helpers for run_daily reporting & Sheets sync (used later) ----

    def select_active(self) -> list[dict]:
        return [dict(r) for r in self.properties.values() if r.get("current_status") == "ACTIVE"]

    def all_properties(self) -> list[dict]:
        return [dict(r, id=str(pid)) for pid, r in self.properties.items()]

    def recent_events(self, limit: int = 200) -> list[Event]:
        return self.events[-limit:]

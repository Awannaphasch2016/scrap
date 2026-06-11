"""Diff engine — pure Python, no I/O.

apply_snapshot:    per-card → INSERT or UPDATE in `re_property`; emits NEW / PRICE_CHANGED events
withdrawal_pass:   missed-today scan → bumps misses → probes canonical → emits SOLD / WITHDRAWN

The Store dependency is a Protocol — concrete implementation is `storage.supabase.RealestateStore`.
For dry-store tests, an in-memory implementation satisfies the same surface.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional, Protocol
from uuid import UUID

from .. import config
from ..fetchers.property_page import probe_property
from ..types import CardSnapshot, Event, PropertyRow

log = logging.getLogger(__name__)


class Store(Protocol):
    def get_by_canonical(self, canonical_url: str) -> Optional[PropertyRow]: ...
    def insert_property(self, snap: CardSnapshot, fingerprint: str) -> UUID: ...
    def update_property(self, id: UUID, snap: CardSnapshot, fingerprint: str) -> None: ...
    def mark_seen(self, id: UUID) -> None: ...
    def mark_miss(self, id: UUID) -> int: ...
    def update_status(self, id: UUID, status: str) -> None: ...
    def insert_event(self, ev: Event) -> None: ...
    def select_missing_since(self, d: date) -> list[PropertyRow]: ...


def apply_snapshot(snapshots: list[CardSnapshot], store: Store) -> list[Event]:
    """For each fresh snapshot, upsert into the store and emit events for material changes."""
    events: list[Event] = []
    for snap in snapshots:
        fp_new = snap.compute_fingerprint()
        existing = store.get_by_canonical(snap.canonical_url)

        if existing is None:
            new_id = store.insert_property(snap, fp_new)
            ev = Event(
                property_id=new_id,
                event_type="NEW",
                payload={"address": snap.address, "price": snap.price_text},
            )
            store.insert_event(ev)
            events.append(ev)
            continue

        if existing.fingerprint == fp_new:
            store.mark_seen(existing.id)
            continue

        # fingerprint differs — figure out what changed (we care about price specifically)
        if (existing.current_price or "") != (snap.price_text or ""):
            ev = Event(
                property_id=existing.id,
                event_type="PRICE_CHANGED",
                payload={
                    "old_price": existing.current_price,
                    "new_price": snap.price_text,
                    "address": snap.address,
                },
            )
            store.insert_event(ev)
            events.append(ev)
        # Always sync row state when fingerprint differs (whether price moved or not — could be
        # agent/agency change). mark_seen happens inside update_property.
        store.update_property(existing.id, snap, fp_new)
        store.mark_seen(existing.id)
    return events


def withdrawal_pass(
    store: Store,
    today: date,
    grace_days: int = config.WITHDRAWN_GRACE_DAYS,
    probe_fn=probe_property,
) -> list[Event]:
    """Find ACTIVE properties not seen today; for those over the grace period, probe and classify.

    Returns events emitted (SOLD or WITHDRAWN). Side effects on store: bumps misses, updates status.

    `probe_fn` is injected for testing — defaults to the real Playwright/opencli probe.
    """
    events: list[Event] = []
    candidates = store.select_missing_since(today)
    log.info("withdrawal_pass · %d candidates", len(candidates))
    for row in candidates:
        misses = store.mark_miss(row.id)
        if misses < grace_days:
            log.info("  %s misses=%d < grace=%d · skip probe", row.canonical_url, misses, grace_days)
            continue
        # Probe the listing's URL. The site auto-redirects /property-X → /sold/property-X when SOLD.
        # `row.canonical_url` is the prefix-stripped form, which still loads correctly.
        result = probe_fn(row.canonical_url)
        log.info("  %s · probe=%s", row.canonical_url, result.outcome)
        if result.outcome == "SOLD":
            ev = Event(
                property_id=row.id,
                event_type="SOLD",
                payload={
                    "sold_price": result.sold_price,
                    "sold_date": result.sold_date.isoformat() if result.sold_date else None,
                },
            )
            store.insert_event(ev)
            store.update_status(row.id, "SOLD")
            events.append(ev)
        elif result.outcome in ("NOT_FOUND",):
            ev = Event(
                property_id=row.id,
                event_type="WITHDRAWN",
                payload={"reason": "absent_from_search_and_probe_failed", "misses": misses},
            )
            store.insert_event(ev)
            store.update_status(row.id, "WITHDRAWN")
            events.append(ev)
        else:
            # STILL_LISTED — bizarre (it left the suburb's Newest sort but is still active).
            # Don't emit an event yet; leave consecutive_misses incremented so we re-evaluate
            # next run. If this persists for many days, future logic may emit a STALE event.
            log.info("  %s · STILL_LISTED — left search but listing alive; no event", row.canonical_url)
    return events

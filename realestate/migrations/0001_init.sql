-- realestate.com.au daily tracker — initial schema.
-- Separate from curator.* on purpose (D2 in the plan).

CREATE SCHEMA IF NOT EXISTS realestate;

CREATE TABLE IF NOT EXISTS realestate.re_watch (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    suburb_slug text NOT NULL,
    state_slug  text NOT NULL,
    enabled     boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (suburb_slug, state_slug)
);

CREATE TABLE IF NOT EXISTS realestate.re_property (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_url       text UNIQUE NOT NULL,
    listing_url         text,
    address             text NOT NULL,
    suburb_slug         text NOT NULL,
    state_slug          text NOT NULL,
    postcode            text,
    bed                 int,
    bath                int,
    car                 int,
    property_type       text,
    listed_at_observed  date,
    first_seen_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now(),
    consecutive_misses  int NOT NULL DEFAULT 0,
    current_price       text,
    current_status      text NOT NULL DEFAULT 'ACTIVE',
    current_agent       text,
    current_agency      text,
    fingerprint         text NOT NULL,
    raw_card            jsonb
);

CREATE INDEX IF NOT EXISTS idx_re_property_status_last_seen
    ON realestate.re_property(current_status, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_re_property_suburb
    ON realestate.re_property(suburb_slug, state_slug);

CREATE TABLE IF NOT EXISTS realestate.re_event (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    property_id  uuid NOT NULL REFERENCES realestate.re_property(id) ON DELETE CASCADE,
    occurred_at  timestamptz NOT NULL DEFAULT now(),
    event_type   text NOT NULL,
    payload      jsonb
);

CREATE INDEX IF NOT EXISTS idx_re_event_property_time
    ON realestate.re_event(property_id, occurred_at DESC);

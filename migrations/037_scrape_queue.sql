-- Migration 037: production-crawler scheduling tables for auto_sync
-- target: player
-- requires-table: fut_players

-- Persistent per-card-per-worktype scheduling, replacing auto_sync's old
-- full-table-sweep design (bin_sales_history_sync.py re-scanned its whole
-- Tier A/B candidate pool every invocation). Workers now claim a bounded,
-- prioritized batch via `FOR UPDATE SKIP LOCKED` instead.
--
-- failure_reason/failure_expires_at double as the TTL'd failure cache
-- (404, no-market-page, missing-sales-link) directly on the row - a card
-- in a live failure window is simply excluded by the claiming query, no
-- separate cache table needed.
--
-- newest_known_sale_at is the sales-worker's early-stop cursor: it stops
-- paging futbin's sales-history endpoint the moment it reaches a sale at
-- or before this timestamp, instead of always scanning a fixed depth.
CREATE TABLE IF NOT EXISTS scrape_queue (
    card_id              BIGINT NOT NULL,
    worktype              TEXT NOT NULL CHECK (worktype IN ('bin', 'sales', 'metadata')),
    priority               INT NOT NULL DEFAULT 0,
    next_due_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_attempt_at        TIMESTAMPTZ,
    last_success_at        TIMESTAMPTZ,
    consecutive_failures   INT NOT NULL DEFAULT 0,
    failure_reason         TEXT,
    failure_expires_at     TIMESTAMPTZ,
    newest_known_sale_at   TIMESTAMPTZ,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (card_id, worktype)
);

CREATE INDEX IF NOT EXISTS idx_scrape_queue_claim
    ON scrape_queue (worktype, priority DESC, next_due_at ASC);

CREATE INDEX IF NOT EXISTS idx_scrape_queue_failure_expiry
    ON scrape_queue (failure_expires_at)
    WHERE failure_expires_at IS NOT NULL;

-- Global (not per-worker, per user decision) token-bucket rate-limiter
-- state. Every auto_sync worker process shares this row via
-- SELECT ... FOR UPDATE read-modify-write, since workers are separate OS
-- processes/Railway containers with no shared memory.
CREATE TABLE IF NOT EXISTS crawler_rate_state (
    scope              TEXT PRIMARY KEY,
    tokens_available    DOUBLE PRECISION NOT NULL,
    requests_per_sec    DOUBLE PRECISION NOT NULL,
    burst_capacity      DOUBLE PRECISION NOT NULL,
    last_refill_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Global circuit breaker: trip on ~20x429 or ~5x403 (config-driven, see
-- auto_sync/config.py), cooldown persisted here so a trip survives across
-- Cron container restarts and future runs respect it.
CREATE TABLE IF NOT EXISTS crawler_circuit_breaker (
    scope               TEXT PRIMARY KEY,
    tripped_at           TIMESTAMPTZ,
    cooldown_until       TIMESTAMPTZ,
    trip_reason          TEXT,
    consecutive_429      INT NOT NULL DEFAULT 0,
    consecutive_403      INT NOT NULL DEFAULT 0,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO crawler_rate_state (scope, tokens_available, requests_per_sec, burst_capacity)
VALUES ('global', 3.0, 3.0, 3.0)
ON CONFLICT (scope) DO NOTHING;

INSERT INTO crawler_circuit_breaker (scope, consecutive_429, consecutive_403)
VALUES ('global', 0, 0)
ON CONFLICT (scope) DO NOTHING;

-- Append-only per-run metrics, feeding the pipeline dashboard: throughput,
-- latency, 429/403 rate, cache-hit ratio, batch duration, and
-- estimated-hours-to-full-cycle (queue_depth_at_start / observed rate).
CREATE TABLE IF NOT EXISTS crawler_metrics (
    id                   BIGSERIAL PRIMARY KEY,
    worktype              TEXT NOT NULL,
    started_at            TIMESTAMPTZ NOT NULL,
    finished_at           TIMESTAMPTZ,
    batch_size            INT NOT NULL,
    succeeded             INT NOT NULL DEFAULT 0,
    failed_429            INT NOT NULL DEFAULT 0,
    failed_403            INT NOT NULL DEFAULT 0,
    failed_other          INT NOT NULL DEFAULT 0,
    cache_hits            INT NOT NULL DEFAULT 0,
    avg_latency_ms         DOUBLE PRECISION,
    queue_depth_at_start   INT
);

CREATE INDEX IF NOT EXISTS idx_crawler_metrics_worktype_time
    ON crawler_metrics (worktype, started_at DESC);

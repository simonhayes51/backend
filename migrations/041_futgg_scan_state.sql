-- Migration 041: per-card market scanner state
-- target: core
-- requires-table: futgg_players
--
-- Recommendation snapshots (040) deliberately record only actionable
-- calls. This much smaller table tracks EVERY successful evaluation so
-- segment selection can rotate through the market instead of repeatedly
-- choosing the same high-volume cards. It contains no invented prices.

CREATE TABLE IF NOT EXISTS futgg_scan_state (
    source_card_id BIGINT PRIMARY KEY,
    last_evaluated_at TIMESTAMPTZ NOT NULL,
    last_signal TEXT NOT NULL,
    last_trend_state TEXT,
    last_observed_bin INTEGER,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS futgg_scan_state_last_eval_idx
    ON futgg_scan_state (last_evaluated_at);

COMMENT ON TABLE futgg_scan_state IS
    'Latest completed FUT.GG scanner evaluation per card, including non-actionable results. Used for fair candidate rotation; not a recommendation or outcome history.';

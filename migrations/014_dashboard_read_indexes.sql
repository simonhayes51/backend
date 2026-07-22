-- Migration 014: read indexes for the /api/dashboard/* endpoints
-- target: player
-- requires-table: sales_history, bin_history
--
-- Migration 011's indexes are (player_id, sold_at DESC) / (player_id,
-- captured_at DESC) - built to speed up fair_value_mv's per-player refresh
-- join. They don't help a plain "WHERE sold_at >= X" with no player_id
-- filter, which is exactly what the dashboard's 24h counts, "cards tracked
-- today", and recent-activity "latest N" queries do. These single-column
-- indexes make those queries index-backed instead of full-table scans.

CREATE INDEX IF NOT EXISTS idx_sales_history_sold_at ON sales_history (sold_at DESC);
CREATE INDEX IF NOT EXISTS idx_bin_history_captured_at ON bin_history (captured_at DESC);

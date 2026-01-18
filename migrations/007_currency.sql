
-- Migration 007: Add currency preference to user settings
-- ============================================================================

ALTER TABLE usersettings
ADD COLUMN IF NOT EXISTS currency VARCHAR(3) DEFAULT 'GBP';

-- Update existing rows to default to GBP
UPDATE usersettings
SET currency = 'GBP'
WHERE currency IS NULL;

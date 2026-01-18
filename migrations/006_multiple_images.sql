
-- Migration 006: Add multiple images support to social posts
-- ============================================================================

-- Add image_urls column to social_posts table
ALTER TABLE social_posts
ADD COLUMN IF NOT EXISTS image_urls TEXT[];

-- Update existing image_url to image_urls if it exists (assuming image_url was added previously)
-- We check if image_url exists first (dynamic check in code, but here we can try)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'social_posts' AND column_name = 'image_url') THEN
        UPDATE social_posts
        SET image_urls = ARRAY[image_url]
        WHERE image_url IS NOT NULL AND image_urls IS NULL;
    END IF;
END $$;

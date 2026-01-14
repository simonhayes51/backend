-- Migration 004: Social feed post updates (sell points, views, shares)

ALTER TABLE social_posts
ADD COLUMN IF NOT EXISTS title VARCHAR(255),
ADD COLUMN IF NOT EXISTS sell_at TIMESTAMP,
ADD COLUMN IF NOT EXISTS views_count INTEGER DEFAULT 0,
ADD COLUMN IF NOT EXISTS shares_count INTEGER DEFAULT 0;

CREATE OR REPLACE FUNCTION update_post_engagement()
RETURNS TRIGGER AS $$
DECLARE
    target_post_id BIGINT;
BEGIN
    target_post_id := COALESCE(NEW.post_id, OLD.post_id);

    IF TG_TABLE_NAME = 'post_reactions' THEN
        UPDATE social_posts
        SET
            likes_count = (
                SELECT COUNT(*)
                FROM post_reactions
                WHERE post_id = target_post_id AND reaction_type = 'like'
            ),
            dislikes_count = (
                SELECT COUNT(*)
                FROM post_reactions
                WHERE post_id = target_post_id AND reaction_type = 'dislike'
            )
        WHERE id = target_post_id;
    END IF;

    IF TG_TABLE_NAME = 'post_comments' THEN
        UPDATE social_posts
        SET comments_count = (
            SELECT COUNT(*)
            FROM post_comments
            WHERE post_id = target_post_id AND deleted_at IS NULL
        )
        WHERE id = target_post_id;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_update_post_comments ON post_comments;

CREATE TRIGGER trigger_update_post_comments
AFTER INSERT OR UPDATE OR DELETE ON post_comments
FOR EACH ROW
EXECUTE FUNCTION update_post_engagement();

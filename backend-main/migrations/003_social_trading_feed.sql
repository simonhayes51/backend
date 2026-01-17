-- Migration 003: Social Trading Feed Feature
-- This migration adds all necessary tables for the social trading platform

-- ============================================================================
-- USER ACCOUNT TYPES
-- ============================================================================

-- Add account type to users table
ALTER TABLE users
ADD COLUMN IF NOT EXISTS account_type VARCHAR(20) DEFAULT 'user' CHECK (account_type IN ('user', 'trader'));

-- Add trader-specific profile information
CREATE TABLE IF NOT EXISTS trader_profiles (
    user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    bio TEXT,
    specialties TEXT[], -- e.g., ['Quick Flips', 'SBC Investments', 'Long Term']
    verified BOOLEAN DEFAULT FALSE,
    subscription_price DECIMAL(10, 2) DEFAULT 0, -- Monthly subscription price in user's currency
    total_followers INTEGER DEFAULT 0,
    total_posts INTEGER DEFAULT 0,
    avg_rating DECIMAL(3, 2) DEFAULT 0,
    total_ratings INTEGER DEFAULT 0,
    achievements JSONB DEFAULT '[]'::JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_trader_profiles_verified ON trader_profiles(verified);
CREATE INDEX idx_trader_profiles_followers ON trader_profiles(total_followers DESC);
CREATE INDEX idx_trader_profiles_rating ON trader_profiles(avg_rating DESC);

-- ============================================================================
-- SOCIAL POSTS
-- ============================================================================

CREATE TABLE IF NOT EXISTS social_posts (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    post_type VARCHAR(50) NOT NULL CHECK (post_type IN ('quick_flip', 'prediction', 'tip', 'analysis')),
    content TEXT NOT NULL,

    -- Trading-related data (optional, depends on post type)
    player_name VARCHAR(255),
    player_card_id VARCHAR(100),
    buy_range_min DECIMAL(12, 2),
    buy_range_max DECIMAL(12, 2),
    sell_target DECIMAL(12, 2),
    confidence_level INTEGER CHECK (confidence_level BETWEEN 1 AND 100),

    -- Metadata
    tags TEXT[],
    is_premium BOOLEAN DEFAULT FALSE, -- Premium content for subscribers only

    -- Engagement stats
    likes_count INTEGER DEFAULT 0,
    dislikes_count INTEGER DEFAULT 0,
    comments_count INTEGER DEFAULT 0,

    -- Timestamps
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP -- For time-sensitive tips
);

CREATE INDEX idx_social_posts_user_id ON social_posts(user_id);
CREATE INDEX idx_social_posts_created_at ON social_posts(created_at DESC);
CREATE INDEX idx_social_posts_post_type ON social_posts(post_type);
CREATE INDEX idx_social_posts_is_premium ON social_posts(is_premium);
CREATE INDEX idx_social_posts_player_card ON social_posts(player_card_id) WHERE player_card_id IS NOT NULL;

-- ============================================================================
-- SUBSCRIPTIONS / FOLLOWS
-- ============================================================================

CREATE TABLE IF NOT EXISTS trader_subscriptions (
    id BIGSERIAL PRIMARY KEY,
    subscriber_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    trader_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    is_active BOOLEAN DEFAULT TRUE,
    subscription_type VARCHAR(20) DEFAULT 'free' CHECK (subscription_type IN ('free', 'paid')),

    -- Payment tracking (if paid subscription)
    stripe_subscription_id VARCHAR(255),
    amount DECIMAL(10, 2),
    currency VARCHAR(3) DEFAULT 'USD',

    -- Timestamps
    subscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    unsubscribed_at TIMESTAMP,

    UNIQUE(subscriber_id, trader_id)
);

CREATE INDEX idx_trader_subscriptions_subscriber ON trader_subscriptions(subscriber_id);
CREATE INDEX idx_trader_subscriptions_trader ON trader_subscriptions(trader_id);
CREATE INDEX idx_trader_subscriptions_active ON trader_subscriptions(is_active) WHERE is_active = TRUE;

-- ============================================================================
-- POST INTERACTIONS
-- ============================================================================

-- Post Likes/Dislikes
CREATE TABLE IF NOT EXISTS post_reactions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    post_id BIGINT NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
    reaction_type VARCHAR(20) NOT NULL CHECK (reaction_type IN ('like', 'dislike')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(user_id, post_id)
);

CREATE INDEX idx_post_reactions_post ON post_reactions(post_id);
CREATE INDEX idx_post_reactions_user ON post_reactions(user_id);

-- Post Comments
CREATE TABLE IF NOT EXISTS post_comments (
    id BIGSERIAL PRIMARY KEY,
    post_id BIGINT NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    parent_comment_id BIGINT REFERENCES post_comments(id) ON DELETE CASCADE, -- For threaded comments
    content TEXT NOT NULL,
    likes_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMP
);

CREATE INDEX idx_post_comments_post ON post_comments(post_id);
CREATE INDEX idx_post_comments_user ON post_comments(user_id);
CREATE INDEX idx_post_comments_parent ON post_comments(parent_comment_id) WHERE parent_comment_id IS NOT NULL;
CREATE INDEX idx_post_comments_created ON post_comments(created_at DESC);

-- Comment Likes
CREATE TABLE IF NOT EXISTS comment_likes (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    comment_id BIGINT NOT NULL REFERENCES post_comments(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(user_id, comment_id)
);

CREATE INDEX idx_comment_likes_comment ON comment_likes(comment_id);
CREATE INDEX idx_comment_likes_user ON comment_likes(user_id);

-- ============================================================================
-- TRADER RATINGS
-- ============================================================================

CREATE TABLE IF NOT EXISTS trader_ratings (
    id BIGSERIAL PRIMARY KEY,
    trader_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    rater_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    review TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(trader_id, rater_id)
);

CREATE INDEX idx_trader_ratings_trader ON trader_ratings(trader_id);
CREATE INDEX idx_trader_ratings_rating ON trader_ratings(rating DESC);
CREATE INDEX idx_trader_ratings_created ON trader_ratings(created_at DESC);

-- ============================================================================
-- INSTANT MESSAGING
-- ============================================================================

-- Conversations (Direct Messages between two users)
CREATE TABLE IF NOT EXISTS conversations (
    id BIGSERIAL PRIMARY KEY,
    user1_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user2_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    last_message_id BIGINT,
    last_message_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- Ensure user1_id < user2_id for consistent ordering
    CHECK (user1_id < user2_id),
    UNIQUE(user1_id, user2_id)
);

CREATE INDEX idx_conversations_user1 ON conversations(user1_id);
CREATE INDEX idx_conversations_user2 ON conversations(user2_id);
CREATE INDEX idx_conversations_last_message ON conversations(last_message_at DESC);

-- Messages
CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY,
    conversation_id BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    sender_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    recipient_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    read_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMP
);

CREATE INDEX idx_messages_conversation ON messages(conversation_id, created_at DESC);
CREATE INDEX idx_messages_sender ON messages(sender_id);
CREATE INDEX idx_messages_recipient ON messages(recipient_id);
CREATE INDEX idx_messages_unread ON messages(recipient_id, read_at) WHERE read_at IS NULL;

-- ============================================================================
-- NOTIFICATIONS
-- ============================================================================

CREATE TABLE IF NOT EXISTS notifications (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    notification_type VARCHAR(50) NOT NULL CHECK (notification_type IN (
        'new_follower', 'post_like', 'post_comment', 'new_message',
        'new_rating', 'mention', 'subscription'
    )),
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,

    -- Reference to related entity
    related_user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
    related_post_id BIGINT REFERENCES social_posts(id) ON DELETE CASCADE,
    related_comment_id BIGINT REFERENCES post_comments(id) ON DELETE CASCADE,
    related_message_id BIGINT REFERENCES messages(id) ON DELETE CASCADE,

    read_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_notifications_user ON notifications(user_id, created_at DESC);
CREATE INDEX idx_notifications_unread ON notifications(user_id, read_at) WHERE read_at IS NULL;
CREATE INDEX idx_notifications_type ON notifications(notification_type);

-- ============================================================================
-- FUNCTIONS AND TRIGGERS
-- ============================================================================

-- Function to update trader profile stats
CREATE OR REPLACE FUNCTION update_trader_stats()
RETURNS TRIGGER AS $$
BEGIN
    -- Update follower count
    IF TG_TABLE_NAME = 'trader_subscriptions' THEN
        UPDATE trader_profiles
        SET total_followers = (
            SELECT COUNT(*)
            FROM trader_subscriptions
            WHERE trader_id = NEW.trader_id AND is_active = TRUE
        )
        WHERE user_id = NEW.trader_id;
    END IF;

    -- Update post count
    IF TG_TABLE_NAME = 'social_posts' THEN
        UPDATE trader_profiles
        SET total_posts = (
            SELECT COUNT(*)
            FROM social_posts
            WHERE user_id = NEW.user_id
        )
        WHERE user_id = NEW.user_id;
    END IF;

    -- Update rating stats
    IF TG_TABLE_NAME = 'trader_ratings' THEN
        UPDATE trader_profiles
        SET
            avg_rating = (
                SELECT ROUND(AVG(rating)::numeric, 2)
                FROM trader_ratings
                WHERE trader_id = NEW.trader_id
            ),
            total_ratings = (
                SELECT COUNT(*)
                FROM trader_ratings
                WHERE trader_id = NEW.trader_id
            )
        WHERE user_id = NEW.trader_id;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Triggers for trader stats
CREATE TRIGGER trigger_update_followers
AFTER INSERT OR UPDATE ON trader_subscriptions
FOR EACH ROW
EXECUTE FUNCTION update_trader_stats();

CREATE TRIGGER trigger_update_posts
AFTER INSERT ON social_posts
FOR EACH ROW
EXECUTE FUNCTION update_trader_stats();

CREATE TRIGGER trigger_update_ratings
AFTER INSERT OR UPDATE ON trader_ratings
FOR EACH ROW
EXECUTE FUNCTION update_trader_stats();

-- Function to update post engagement counts
CREATE OR REPLACE FUNCTION update_post_engagement()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_TABLE_NAME = 'post_reactions' THEN
        UPDATE social_posts
        SET
            likes_count = (
                SELECT COUNT(*)
                FROM post_reactions
                WHERE post_id = NEW.post_id AND reaction_type = 'like'
            ),
            dislikes_count = (
                SELECT COUNT(*)
                FROM post_reactions
                WHERE post_id = NEW.post_id AND reaction_type = 'dislike'
            )
        WHERE id = NEW.post_id;
    END IF;

    IF TG_TABLE_NAME = 'post_comments' THEN
        UPDATE social_posts
        SET comments_count = (
            SELECT COUNT(*)
            FROM post_comments
            WHERE post_id = NEW.post_id AND deleted_at IS NULL
        )
        WHERE id = NEW.post_id;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Triggers for post engagement
CREATE TRIGGER trigger_update_post_reactions
AFTER INSERT OR DELETE ON post_reactions
FOR EACH ROW
EXECUTE FUNCTION update_post_engagement();

CREATE TRIGGER trigger_update_post_comments
AFTER INSERT OR UPDATE ON post_comments
FOR EACH ROW
EXECUTE FUNCTION update_post_engagement();

-- Function to update comment likes count
CREATE OR REPLACE FUNCTION update_comment_likes()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE post_comments
    SET likes_count = (
        SELECT COUNT(*)
        FROM comment_likes
        WHERE comment_id = NEW.comment_id
    )
    WHERE id = NEW.comment_id;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_update_comment_likes
AFTER INSERT OR DELETE ON comment_likes
FOR EACH ROW
EXECUTE FUNCTION update_comment_likes();

-- Function to update conversation last message
CREATE OR REPLACE FUNCTION update_conversation_last_message()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE conversations
    SET
        last_message_id = NEW.id,
        last_message_at = NEW.created_at
    WHERE id = NEW.conversation_id;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_update_conversation
AFTER INSERT ON messages
FOR EACH ROW
EXECUTE FUNCTION update_conversation_last_message();

-- ============================================================================
-- VIEWS FOR COMMON QUERIES
-- ============================================================================

-- View for active traders with stats
CREATE OR REPLACE VIEW active_traders AS
SELECT
    u.id,
    up.username,
    up.avatar_url,
    tp.bio,
    tp.specialties,
    tp.verified,
    tp.subscription_price,
    tp.total_followers,
    tp.total_posts,
    tp.avg_rating,
    tp.total_ratings,
    tp.created_at as trader_since
FROM users u
JOIN user_profiles up ON u.id = up.user_id
JOIN trader_profiles tp ON u.id = tp.user_id
WHERE u.account_type = 'trader';

-- View for feed with engagement stats
CREATE OR REPLACE VIEW feed_posts_with_stats AS
SELECT
    sp.*,
    up.username,
    up.avatar_url,
    tp.verified,
    tp.avg_rating,
    tp.total_followers
FROM social_posts sp
JOIN user_profiles up ON sp.user_id = up.user_id
LEFT JOIN trader_profiles tp ON sp.user_id = tp.user_id;

COMMENT ON TABLE trader_profiles IS 'Profiles for users with trader account type';
COMMENT ON TABLE social_posts IS 'Trading tips, predictions, and analysis posts from traders';
COMMENT ON TABLE trader_subscriptions IS 'User subscriptions to traders (free or paid)';
COMMENT ON TABLE post_reactions IS 'Likes and dislikes on social posts';
COMMENT ON TABLE post_comments IS 'Comments on social posts with threading support';
COMMENT ON TABLE trader_ratings IS 'User ratings and reviews for traders';
COMMENT ON TABLE conversations IS 'Direct message conversations between users';
COMMENT ON TABLE messages IS 'Individual messages in conversations';
COMMENT ON TABLE notifications IS 'User notifications for various platform events';

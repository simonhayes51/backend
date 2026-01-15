"""
Pydantic models for social trading feed features
"""
from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, validator, root_validator
from decimal import Decimal


# ============================================================================
# USER & TRADER MODELS
# ============================================================================

class TraderProfileCreate(BaseModel):
    bio: Optional[str] = None
    specialties: Optional[List[str]] = []
    subscription_price: Optional[Decimal] = Decimal('0')
    tier_basic_price: Optional[Decimal] = Decimal('4.99')
    tier_premium_price: Optional[Decimal] = Decimal('9.99')
    tier_elite_price: Optional[Decimal] = Decimal('19.99')
    tier_basic_cap: Optional[int] = None
    tier_premium_cap: Optional[int] = None
    tier_elite_cap: Optional[int] = None


class TraderProfileUpdate(BaseModel):
    bio: Optional[str] = None
    specialties: Optional[List[str]] = None
    subscription_price: Optional[Decimal] = None
    tier_basic_price: Optional[Decimal] = None
    tier_premium_price: Optional[Decimal] = None
    tier_elite_price: Optional[Decimal] = None
    tier_basic_cap: Optional[int] = None
    tier_premium_cap: Optional[int] = None
    tier_elite_cap: Optional[int] = None


class TraderProfile(BaseModel):
    user_id: str
    bio: Optional[str]
    specialties: List[str]
    verified: bool
    subscription_price: Decimal
    tier_basic_price: Decimal
    tier_premium_price: Decimal
    tier_elite_price: Decimal
    tier_basic_cap: Optional[int]
    tier_premium_cap: Optional[int]
    tier_elite_cap: Optional[int]
    total_followers: int
    total_posts: int
    avg_rating: float
    total_ratings: int
    achievements: List[dict]
    created_at: datetime
    updated_at: datetime


class TraderPublicProfile(BaseModel):
    id: str
    username: str
    avatar_url: Optional[str]
    bio: Optional[str]
    header_image_url: Optional[str] = None
    location: Optional[str] = None
    website_url: Optional[str] = None
    twitter_url: Optional[str] = None
    youtube_url: Optional[str] = None
    twitch_url: Optional[str] = None
    specialties: List[str]
    verified: bool
    subscription_price: Decimal
    tier_basic_price: Decimal
    tier_premium_price: Decimal
    tier_elite_price: Decimal
    tier_basic_cap: Optional[int]
    tier_premium_cap: Optional[int]
    tier_elite_cap: Optional[int]
    total_followers: int
    total_posts: int
    avg_rating: float
    total_ratings: int
    trader_since: datetime
    is_subscribed: Optional[bool] = False  # Will be populated based on current user


# ============================================================================
# SOCIAL POST MODELS
# ============================================================================

class SocialPostCreate(BaseModel):
    post_type: str = Field(..., description="Type: quick_flip, prediction, tip, analysis")
    title: Optional[str] = None
    content: str = Field(..., min_length=1, max_length=5000)
    player_name: Optional[str] = None
    player_card_id: Optional[str] = None
    buy_range_min: Optional[Decimal] = None
    buy_range_max: Optional[Decimal] = None
    sell_target: Optional[Decimal] = None
    sell_at: Optional[datetime] = None
    confidence_level: Optional[int] = Field(None, ge=1, le=100)
    tags: Optional[List[str]] = []
    image_url: Optional[str] = None
    is_premium: bool = False
    expires_at: Optional[datetime] = None

    @validator('post_type')
    def validate_post_type(cls, v):
        allowed = ['quick_flip', 'prediction', 'tip', 'analysis']
        if v not in allowed:
            raise ValueError(f'post_type must be one of {allowed}')
        return v


class SocialPostUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = Field(None, min_length=1, max_length=5000)
    post_type: Optional[str] = None
    player_name: Optional[str] = None
    player_card_id: Optional[str] = None
    buy_price: Optional[Decimal] = None
    buy_range_min: Optional[Decimal] = None
    buy_range_max: Optional[Decimal] = None
    sell_target: Optional[Decimal] = None
    sell_at: Optional[datetime] = None
    confidence_level: Optional[int] = Field(None, ge=1, le=100)
    tags: Optional[List[str]] = None
    image_url: Optional[str] = None
    is_premium: Optional[bool] = None
    expires_at: Optional[datetime] = None


class SocialPost(BaseModel):
    id: int
    user_id: str
    post_type: str
    title: Optional[str] = None
    content: str
    player_name: Optional[str]
    player_card_id: Optional[str]
    buy_range_min: Optional[Decimal]
    buy_range_max: Optional[Decimal]
    sell_target: Optional[Decimal]
    sell_at: Optional[datetime] = None
    confidence_level: Optional[int]
    tags: List[str]
    image_url: Optional[str]
    is_premium: bool
    likes_count: int
    dislikes_count: int
    comments_count: int
    views_count: int = 0
    shares_count: int = 0
    created_at: datetime
    updated_at: datetime
    expires_at: Optional[datetime]


class SocialPostWithAuthor(SocialPost):
    username: Optional[str] = "Anonymous"  # Allow None, default to Anonymous
    avatar_url: Optional[str] = None
    verified: bool = False
    avg_rating: Optional[Decimal] = None
    total_followers: Optional[int] = None
    user_reaction: Optional[str] = None  # 'like', 'dislike', or None
    is_author: bool = False  # True if current user is the author
    title: Optional[str] = None
    author: Optional[Dict[str, Any]] = None
    stats: Optional[Dict[str, Any]] = None


class FeedResponse(BaseModel):
    posts: List[SocialPostWithAuthor]
    total: int
    has_more: bool
    offset: int
    limit: int


# ============================================================================
# SUBSCRIPTION MODELS
# ============================================================================

class SubscriptionCreate(BaseModel):
    trader_id: str
    tier: str = "free"  # "free", "basic", "premium", "elite"


class Subscription(BaseModel):
    id: int
    subscriber_id: str
    trader_id: str
    is_active: bool
    subscription_type: str
    stripe_subscription_id: Optional[str]
    amount: Optional[Decimal]
    currency: str
    subscribed_at: datetime
    unsubscribed_at: Optional[datetime]


class SubscriptionWithTrader(BaseModel):
    id: int
    trader_id: str
    trader_username: str
    trader_avatar: Optional[str]
    verified: bool
    is_active: bool
    subscription_type: str
    subscribed_at: datetime


# ============================================================================
# POST INTERACTION MODELS
# ============================================================================

class PostReactionCreate(BaseModel):
    post_id: int
    reaction_type: str = Field(..., description="like or dislike")

    @validator('reaction_type')
    def validate_reaction(cls, v):
        if v not in ['like', 'dislike']:
            raise ValueError('reaction_type must be like or dislike')
        return v


class PostReaction(BaseModel):
    id: int
    user_id: str
    post_id: int
    reaction_type: str
    created_at: datetime


class CommentCreate(BaseModel):
    post_id: int
    content: str = Field(..., min_length=1, max_length=2000)
    parent_comment_id: Optional[int] = None


class CommentUpdate(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000)


class Comment(BaseModel):
    id: int
    post_id: int
    user_id: str
    parent_comment_id: Optional[int]
    content: str
    likes_count: int
    created_at: datetime
    updated_at: datetime
    deleted_at: Optional[datetime]


class CommentWithAuthor(Comment):
    username: str
    avatar_url: Optional[str]
    verified: Optional[bool]
    user_has_liked: bool = False
    is_author: bool = False
    author: Optional[Dict[str, Any]] = None
    likes: Optional[int] = None


class CommentLikeCreate(BaseModel):
    comment_id: int


# ============================================================================
# RATING MODELS
# ============================================================================

class RatingCreate(BaseModel):
    trader_id: str
    rating: int = Field(..., ge=1, le=5, description="Rating from 1 to 5 stars")
    review: Optional[str] = Field(None, max_length=1000)


class RatingUpdate(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    review: Optional[str] = Field(None, max_length=1000)


class Rating(BaseModel):
    id: int
    trader_id: str
    rater_id: str
    rating: int
    review: Optional[str]
    created_at: datetime
    updated_at: datetime


class RatingWithAuthor(Rating):
    rater_username: str
    rater_avatar: Optional[str]


# ============================================================================
# MESSAGING MODELS
# ============================================================================

class MessageCreate(BaseModel):
    recipient_id: str
    content: str = Field(..., min_length=1, max_length=5000)

    @root_validator(pre=True)
    def normalize_message_fields(cls, values):
        if values.get("recipient_id") is None:
            for key in ("recipientId", "recipient", "user_id", "userId"):
                if values.get(key):
                    values["recipient_id"] = values[key]
                    break
        if values.get("content") is None:
            for key in ("message", "text"):
                if values.get(key):
                    values["content"] = values[key]
                    break
        return values


class MessagePayload(BaseModel):
    content: str

    @root_validator(pre=True)
    def normalize_payload_fields(cls, values):
        if values.get("content") is None:
            for key in ("message", "text"):
                if values.get(key):
                    values["content"] = values[key]
                    break
        return values


class Message(BaseModel):
    id: int
    conversation_id: int
    sender_id: str
    recipient_id: str
    content: str
    read_at: Optional[datetime]
    created_at: datetime
    deleted_at: Optional[datetime]


class MessageWithUser(Message):
    sender_username: str
    sender_avatar: Optional[str]
    recipient_username: str
    recipient_avatar: Optional[str]
    is_sender: bool = False


class Conversation(BaseModel):
    id: int
    user1_id: str
    user2_id: str
    last_message_id: Optional[int]
    last_message_at: Optional[datetime]
    created_at: datetime


class ConversationWithDetails(Conversation):
    other_user_id: str
    other_user_username: str
    other_user_avatar: Optional[str]
    last_message_content: Optional[str]
    unread_count: int
    title: Optional[str] = None
    participant: Optional[Dict[str, Any]] = None
    last_message: Optional[Dict[str, Any]] = None


# ============================================================================
# NOTIFICATION MODELS
# ============================================================================

class Notification(BaseModel):
    id: int
    user_id: str
    notification_type: str
    title: str
    message: str
    related_user_id: Optional[str]
    related_post_id: Optional[int]
    related_comment_id: Optional[int]
    related_message_id: Optional[int]
    read_at: Optional[datetime]
    created_at: datetime


class NotificationWithDetails(Notification):
    related_username: Optional[str]
    related_avatar: Optional[str]


# ============================================================================
# STATISTICS MODELS
# ============================================================================

class UserStats(BaseModel):
    account_type: str
    total_posts: int = 0
    total_followers: int = 0
    total_following: int = 0
    total_likes_received: int = 0
    total_comments_received: int = 0


class TraderStats(UserStats):
    avg_rating: Decimal = Decimal('0')
    total_ratings: int = 0
    verified: bool = False
    subscription_price: Decimal = Decimal('0')


class TraderAnalytics(BaseModel):
    total_active_subscribers: int
    active_basic_subscribers: int
    active_premium_subscribers: int
    active_elite_subscribers: int
    monthly_earnings_estimated: Decimal
    total_followers: int
    views_last_30_days: int = 0

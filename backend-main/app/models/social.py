"""
Pydantic models for social trading feed features (Pydantic v2 compatible)
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional, List, Dict, Any

from pydantic import (
    BaseModel,
    Field,
    ConfigDict,
    field_validator,
    model_validator,
    AliasChoices,
)

# ============================================================================
# USER & TRADER MODELS
# ============================================================================


class TraderProfileCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bio: Optional[str] = None
    specialties: List[str] = Field(default_factory=list)
    subscription_price: Decimal = Decimal("0")
    # Deprecated: tier fields kept for backward compatibility
    tier_basic_price: Decimal = Decimal("4.99")
    tier_premium_price: Decimal = Decimal("9.99")
    tier_elite_price: Decimal = Decimal("19.99")
    tier_basic_cap: Optional[int] = None
    tier_premium_cap: Optional[int] = None
    tier_elite_cap: Optional[int] = None


class TraderProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bio: Optional[str] = None
    specialties: Optional[List[str]] = None
    subscription_price: Optional[Decimal] = None
    tier_basic_price: Optional[Decimal] = None
    tier_premium_price: Optional[Decimal] = None
    tier_elite_price: Optional[Decimal] = None
    tier_basic_cap: Optional[int] = None
    tier_premium_cap: Optional[int] = None
    tier_elite_cap: Optional[int] = None

    # User Profile Fields
    header_image_url: Optional[str] = Field(None, max_length=2000)
    location: Optional[str] = Field(None, max_length=255)
    website_url: Optional[str] = Field(None, max_length=2000)
    twitter_url: Optional[str] = Field(None, max_length=2000)
    youtube_url: Optional[str] = Field(None, max_length=2000)
    twitch_url: Optional[str] = Field(None, max_length=2000)


class TraderProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    user_id: str
    bio: Optional[str] = None
    specialties: List[str] = Field(default_factory=list)
    verified: bool = False
    subscription_price: Decimal = Decimal("0")
    tier_basic_price: Decimal = Decimal("4.99")
    tier_premium_price: Decimal = Decimal("9.99")
    tier_elite_price: Decimal = Decimal("19.99")
    tier_basic_cap: Optional[int] = None
    tier_premium_cap: Optional[int] = None
    tier_elite_cap: Optional[int] = None
    total_followers: int = 0
    total_posts: int = 0
    avg_rating: float = 0.0
    total_ratings: int = 0
    achievements: List[dict] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @field_validator("avg_rating", mode="before")
    @classmethod
    def _default_avg_rating(cls, v):
        if v in (None, ""):
            return 0.0
        try:
            return float(v)
        except Exception:
            return 0.0

    @field_validator("achievements", mode="before")
    @classmethod
    def _coerce_achievements(cls, v):
        if v in (None, ""):
            return []
        if isinstance(v, str):
            try:
                import json

                parsed = json.loads(v)
                return parsed if isinstance(parsed, list) else []
            except Exception:
                return []
        return v


class TraderPublicProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    username: str
    avatar_url: Optional[str] = None
    bio: Optional[str] = None

    header_image_url: Optional[str] = None
    location: Optional[str] = None
    website_url: Optional[str] = None
    twitter_url: Optional[str] = None
    youtube_url: Optional[str] = None
    twitch_url: Optional[str] = None

    specialties: List[str] = Field(default_factory=list)
    verified: bool = False
    subscription_price: Decimal = Decimal("0")
    tier_basic_price: Decimal = Decimal("4.99")
    tier_premium_price: Decimal = Decimal("9.99")
    tier_elite_price: Decimal = Decimal("19.99")
    tier_basic_cap: Optional[int] = None
    tier_premium_cap: Optional[int] = None
    tier_elite_cap: Optional[int] = None
    total_followers: int = 0
    total_posts: int = 0
    avg_rating: float = 0.0
    total_ratings: int = 0
    trader_since: datetime
    is_subscribed: bool = False

    @field_validator("avg_rating", mode="before")
    @classmethod
    def _avg_rating(cls, v):
        if v in (None, ""):
            return 0.0
        try:
            return float(v)
        except Exception:
            return 0.0

    @field_validator("specialties", mode="before")
    @classmethod
    def _specialties(cls, v):
        if v in (None, ""):
            return []
        return v


# ============================================================================
# SOCIAL POST MODELS
# ============================================================================


class SocialPostCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

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
    tags: List[str] = Field(default_factory=list)
    image_url: Optional[str] = None
    is_premium: bool = False  # Subscriber-only content
    requires_purchase: bool = False  # One-off purchase content
    price: Optional[Decimal] = Field(None, ge=0)  # Price for one-off purchase
    expires_at: Optional[datetime] = None

    @field_validator("post_type")
    @classmethod
    def validate_post_type(cls, v):
        allowed = {"quick_flip", "prediction", "tip", "analysis"}
        if v not in allowed:
            raise ValueError(f"post_type must be one of {sorted(allowed)}")
        return v

    @model_validator(mode="after")
    def validate_pricing(self):
        if self.requires_purchase and (self.price is None or self.price <= 0):
            raise ValueError("price must be set and greater than 0 when requires_purchase is True")
        if not self.requires_purchase and self.price is not None:
            raise ValueError("price should be None when requires_purchase is False")
        return self


class SocialPostUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

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
    requires_purchase: Optional[bool] = None
    price: Optional[Decimal] = Field(None, ge=0)
    expires_at: Optional[datetime] = None


class SocialPost(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    user_id: str
    post_type: str
    title: Optional[str] = None
    content: str
    player_name: Optional[str] = None
    player_card_id: Optional[str] = None
    buy_range_min: Optional[Decimal] = None
    buy_range_max: Optional[Decimal] = None
    sell_target: Optional[Decimal] = None
    sell_at: Optional[datetime] = None
    confidence_level: Optional[int] = None
    tags: List[str] = Field(default_factory=list)
    image_url: Optional[str] = None
    is_premium: bool = False
    requires_purchase: bool = False
    price: Optional[Decimal] = None
    likes_count: int = 0
    dislikes_count: int = 0
    comments_count: int = 0
    views_count: int = 0
    shares_count: int = 0
    created_at: datetime
    updated_at: datetime
    expires_at: Optional[datetime] = None


class SocialPostWithAuthor(SocialPost):
    username: Optional[str] = "Anonymous"
    avatar_url: Optional[str] = None
    verified: bool = False
    avg_rating: Optional[Decimal] = None
    total_followers: Optional[int] = None
    user_reaction: Optional[str] = None
    is_author: bool = False
    author: Optional[Dict[str, Any]] = None
    stats: Optional[Dict[str, Any]] = None

    @field_validator("username", mode="before")
    @classmethod
    def _username(cls, v):
        return "Anonymous" if v in (None, "") else v


class FeedResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    posts: List[SocialPostWithAuthor]
    total: int
    has_more: bool
    offset: int
    limit: int


# ============================================================================
# SUBSCRIPTION MODELS
# ============================================================================


class SubscriptionCreate(BaseModel):
    """
    Accepts both trader_id and traderId (frontend safety).
    """
    model_config = ConfigDict(extra="ignore")

    trader_id: str = Field(..., validation_alias=AliasChoices("trader_id", "traderId"))
    tier: str = "free"  # free or paid (basic/premium/elite deprecated but supported)

    @field_validator("tier", mode="before")
    @classmethod
    def _tier(cls, v):
        if v in (None, ""):
            return "free"
        return str(v).lower()


class Subscription(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    subscriber_id: str
    trader_id: str
    is_active: bool
    subscription_type: str
    stripe_subscription_id: Optional[str] = None
    paypal_subscription_id: Optional[str] = None
    payment_provider: str = "stripe"
    amount: Optional[Decimal] = None
    currency: str = "GBP"
    subscribed_at: datetime
    unsubscribed_at: Optional[datetime] = None


class SubscriptionWithTrader(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    trader_id: str
    trader_username: str
    trader_avatar: Optional[str] = None
    verified: bool = False
    is_active: bool = True
    subscription_type: str = "free"
    subscribed_at: datetime
    avg_rating: float = 0.0
    total_ratings: int = 0


# ============================================================================
# POST INTERACTION MODELS
# ============================================================================


class PostReactionCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    post_id: int
    reaction_type: str = Field(..., description="like or dislike")

    @field_validator("reaction_type")
    @classmethod
    def validate_reaction(cls, v):
        if v not in {"like", "dislike"}:
            raise ValueError("reaction_type must be like or dislike")
        return v


class PostReaction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    user_id: str
    post_id: int
    reaction_type: str
    created_at: datetime


class CommentCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    post_id: int
    content: str = Field(..., min_length=1, max_length=2000)
    parent_comment_id: Optional[int] = None


class CommentUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    content: str = Field(..., min_length=1, max_length=2000)


class Comment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    post_id: int
    user_id: str
    parent_comment_id: Optional[int] = None
    content: str
    likes_count: int = 0
    created_at: datetime
    updated_at: datetime
    deleted_at: Optional[datetime] = None


class CommentWithAuthor(Comment):
    username: str
    avatar_url: Optional[str] = None
    verified: Optional[bool] = None
    user_has_liked: bool = False
    is_author: bool = False
    author: Optional[Dict[str, Any]] = None
    likes: Optional[int] = None


class CommentLikeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    comment_id: int


# ============================================================================
# RATING MODELS
# ============================================================================


class RatingCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    trader_id: str = Field(..., validation_alias=AliasChoices("trader_id", "traderId"))
    rating: int = Field(..., ge=1, le=5, description="Rating from 1 to 5 stars")
    review: Optional[str] = Field(None, max_length=1000)

    @field_validator("trader_id", mode="before")
    @classmethod
    def _trader_id(cls, v):
        return str(v) if v is not None else v


class RatingUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rating: int = Field(..., ge=1, le=5)
    review: Optional[str] = Field(None, max_length=1000)


class Rating(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    trader_id: str
    rater_id: str
    rating: int
    review: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class RatingWithAuthor(Rating):
    rater_username: str
    rater_avatar: Optional[str] = None


# ============================================================================
# MESSAGING MODELS
# ============================================================================


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    recipient_id: str
    content: str = Field(..., min_length=1, max_length=5000)

    @model_validator(mode="before")
    @classmethod
    def normalize_message_fields(cls, values):
        if not isinstance(values, dict):
            return values

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
    model_config = ConfigDict(extra="ignore")

    content: str

    @model_validator(mode="before")
    @classmethod
    def normalize_payload_fields(cls, values):
        if not isinstance(values, dict):
            return values
        if values.get("content") is None:
            for key in ("message", "text"):
                if values.get(key):
                    values["content"] = values[key]
                    break
        return values


class Message(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    conversation_id: int
    sender_id: str
    recipient_id: str
    content: str
    read_at: Optional[datetime] = None
    created_at: datetime
    deleted_at: Optional[datetime] = None


class MessageWithUser(Message):
    sender_username: str
    sender_avatar: Optional[str] = None
    recipient_username: str
    recipient_avatar: Optional[str] = None
    is_sender: bool = False


class Conversation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    user1_id: str
    user2_id: str
    last_message_id: Optional[int] = None
    last_message_at: Optional[datetime] = None
    created_at: datetime


class ConversationWithDetails(Conversation):
    other_user_id: str
    other_user_username: str
    other_user_avatar: Optional[str] = None
    last_message_content: Optional[str] = None
    unread_count: int = 0
    title: Optional[str] = None
    participant: Optional[Dict[str, Any]] = None
    last_message: Optional[Dict[str, Any]] = None


# ============================================================================
# NOTIFICATION MODELS
# ============================================================================


class Notification(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    user_id: str
    notification_type: str
    title: str
    message: str
    related_user_id: Optional[str] = None
    related_post_id: Optional[int] = None
    related_comment_id: Optional[int] = None
    related_message_id: Optional[int] = None
    read_at: Optional[datetime] = None
    created_at: datetime


class NotificationWithDetails(Notification):
    related_username: Optional[str] = None
    related_avatar: Optional[str] = None


# ============================================================================
# STATISTICS MODELS
# ============================================================================


class UserStats(BaseModel):
    model_config = ConfigDict(extra="ignore")

    account_type: str
    total_posts: int = 0
    total_followers: int = 0
    total_following: int = 0
    total_likes_received: int = 0
    total_comments_received: int = 0


class TraderStats(UserStats):
    avg_rating: Decimal = Decimal("0")
    total_ratings: int = 0
    verified: bool = False
    subscription_price: Decimal = Decimal("0")


class TraderAnalytics(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total_active_subscribers: int
    active_basic_subscribers: int  # Deprecated
    active_premium_subscribers: int  # Deprecated
    active_elite_subscribers: int  # Deprecated
    active_paid_subscribers: int = 0  # New: single-tier paid subscribers
    monthly_earnings_estimated: Decimal
    total_content_revenue: Decimal = Decimal("0")  # New: one-off content sales
    total_tips_received: Decimal = Decimal("0")  # Tips
    total_followers: int
    views_last_30_days: int = 0


# ============================================================================
# CONTENT PURCHASE MODELS
# ============================================================================


class ContentPurchaseCreate(BaseModel):
    """
    Create a content purchase request
    """
    model_config = ConfigDict(extra="ignore")

    post_id: int = Field(..., gt=0)
    payment_provider: str = Field(..., description="stripe or paypal")

    @field_validator("payment_provider")
    @classmethod
    def validate_provider(cls, v):
        if v not in {"stripe", "paypal"}:
            raise ValueError("payment_provider must be stripe or paypal")
        return v


class ContentPurchase(BaseModel):
    """
    Content purchase record
    """
    model_config = ConfigDict(extra="ignore")

    id: int
    user_id: str
    post_id: int
    amount: Decimal
    currency: str = "GBP"
    payment_provider: str
    stripe_payment_intent_id: Optional[str] = None
    paypal_order_id: Optional[str] = None
    paypal_capture_id: Optional[str] = None
    status: str  # pending, completed, failed, refunded
    purchased_at: datetime
    completed_at: Optional[datetime] = None
    refunded_at: Optional[datetime] = None


class PaymentIntent(BaseModel):
    """
    Payment intent for one-off purchase
    """
    model_config = ConfigDict(extra="ignore")

    client_secret: Optional[str] = None  # Stripe
    payment_intent_id: Optional[str] = None  # Stripe
    order_id: Optional[str] = None  # PayPal
    approval_url: Optional[str] = None  # PayPal
    amount: Decimal
    currency: str = "GBP"
    provider: str
# Social Trading Feed API Documentation

This document outlines all the new API endpoints for the social trading feed feature. This feature allows traders to share tips, predictions, and analysis with their followers, similar to OnlyFans/Twitter for trading.

## Table of Contents

1. [Authentication](#authentication)
2. [User Account Types](#user-account-types)
3. [Social Feed Endpoints](#social-feed-endpoints)
4. [Subscription/Follow System](#subscriptionfollow-system)
5. [Post Interactions](#post-interactions)
6. [Messaging System](#messaging-system)
7. [Rating System](#rating-system)
8. [Trader Discovery](#trader-discovery)
9. [Notifications](#notifications)

---

## Authentication

All endpoints require authentication via session cookies (Discord OAuth). The user session contains the user ID and other profile information.

**Authenticated User Access:**
- Session-based authentication using Discord OAuth
- User information stored in `request.session["user"]`

---

## User Account Types

There are two types of accounts:
- **User** - Regular users who can follow traders and view their content
- **Trader** - Users who can create posts and share trading tips

### Upgrade to Trader Account

**Endpoint:** `POST /api/traders/upgrade-to-trader`

**Request Body:**
```json
{
  "bio": "Professional FIFA trader with 5 years experience",
  "specialties": ["Quick Flips", "SBC Investments", "Long Term Trading"],
  "subscription_price": 0
}
```

**Response:**
```json
{
  "message": "Successfully upgraded to trader account",
  "profile": {
    "user_id": 123,
    "bio": "Professional FIFA trader with 5 years experience",
    "specialties": ["Quick Flips", "SBC Investments"],
    "verified": false,
    "subscription_price": 0,
    "total_followers": 0,
    "total_posts": 0,
    "avg_rating": 0,
    "total_ratings": 0,
    "achievements": [],
    "created_at": "2025-01-11T10:00:00Z",
    "updated_at": "2025-01-11T10:00:00Z"
  }
}
```

---

## Social Feed Endpoints

### Create a Post

**Endpoint:** `POST /api/feed/posts`

**Auth Required:** Yes (Trader only)

**Request Body:**
```json
{
  "post_type": "quick_flip",
  "content": "🚨 QUICK FLIP ALERT! Mbappé is about to spike due to upcoming SBC requirements. Get in NOW before it's too late. This is a time-sensitive opportunity!",
  "player_name": "Kylian Mbappé",
  "player_card_id": "123456",
  "buy_range_min": 1100000,
  "buy_range_max": 1150000,
  "sell_target": 1350000,
  "confidence_level": 75,
  "tags": ["quick_flip", "sbc", "meta"],
  "is_premium": false,
  "expires_at": "2025-01-12T18:00:00Z"
}
```

**Post Types:**
- `quick_flip` - Time-sensitive trading opportunities
- `prediction` - Market predictions and analysis
- `tip` - General trading advice
- `analysis` - In-depth market analysis

**Response:**
```json
{
  "id": 1,
  "user_id": 123,
  "post_type": "quick_flip",
  "content": "🚨 QUICK FLIP ALERT! Mbappé is about to spike...",
  "player_name": "Kylian Mbappé",
  "player_card_id": "123456",
  "buy_range_min": 1100000,
  "buy_range_max": 1150000,
  "sell_target": 1350000,
  "confidence_level": 75,
  "tags": ["quick_flip", "sbc", "meta"],
  "is_premium": false,
  "likes_count": 0,
  "dislikes_count": 0,
  "comments_count": 0,
  "created_at": "2025-01-11T10:00:00Z",
  "updated_at": "2025-01-11T10:00:00Z",
  "expires_at": "2025-01-12T18:00:00Z"
}
```

### Get Feed

**Endpoint:** `GET /api/feed/posts`

**Auth Required:** No (but recommended for personalized feed)

**Query Parameters:**
- `feed_type` (string, optional): `all`, `trades`, or `predictions` (default: `all`)
- `trader_id` (integer, optional): Filter by specific trader
- `offset` (integer, optional): Pagination offset (default: 0)
- `limit` (integer, optional): Items per page (default: 20, max: 100)

**Response:**
```json
{
  "posts": [
    {
      "id": 1,
      "user_id": 123,
      "post_type": "quick_flip",
      "content": "🚨 QUICK FLIP ALERT!...",
      "player_name": "Kylian Mbappé",
      "buy_range_min": 1100000,
      "buy_range_max": 1150000,
      "sell_target": 1350000,
      "confidence_level": 75,
      "tags": ["quick_flip"],
      "is_premium": false,
      "likes_count": 45,
      "dislikes_count": 2,
      "comments_count": 12,
      "created_at": "2025-01-11T10:00:00Z",
      "username": "FlipKingFC",
      "avatar_url": "https://cdn.discord.com/avatars/...",
      "verified": false,
      "avg_rating": 4.5,
      "total_followers": 1250,
      "user_reaction": "like",
      "is_author": false
    }
  ],
  "total": 150,
  "has_more": true,
  "offset": 0,
  "limit": 20
}
```

### Get Single Post

**Endpoint:** `GET /api/feed/posts/{post_id}`

**Auth Required:** No (unless premium content)

**Response:** Same as post object above, with author details.

### Update Post

**Endpoint:** `PATCH /api/feed/posts/{post_id}`

**Auth Required:** Yes (Author only)

**Request Body:**
```json
{
  "content": "Updated content",
  "tags": ["updated", "tags"],
  "is_premium": true
}
```

### Delete Post

**Endpoint:** `DELETE /api/feed/posts/{post_id}`

**Auth Required:** Yes (Author only)

**Response:**
```json
{
  "message": "Post deleted successfully"
}
```

### Get Post Statistics

**Endpoint:** `GET /api/feed/posts/{post_id}/stats`

**Auth Required:** No

**Response:**
```json
{
  "post_id": 1,
  "likes_count": 45,
  "dislikes_count": 2,
  "comments_count": 12,
  "top_likers": [
    {
      "username": "trader123",
      "avatar_url": "https://..."
    }
  ],
  "created_at": "2025-01-11T10:00:00Z",
  "engagement_rate": 0.96
}
```

---

## Subscription/Follow System

### Subscribe to Trader

**Endpoint:** `POST /api/subscriptions/subscribe`

**Auth Required:** Yes

**Request Body:**
```json
{
  "trader_id": 123
}
```

**Response:**
```json
{
  "id": 1,
  "subscriber_id": 456,
  "trader_id": 123,
  "is_active": true,
  "subscription_type": "free",
  "stripe_subscription_id": null,
  "amount": null,
  "currency": "USD",
  "subscribed_at": "2025-01-11T10:00:00Z",
  "unsubscribed_at": null
}
```

### Unsubscribe from Trader

**Endpoint:** `DELETE /api/subscriptions/unsubscribe/{trader_id}`

**Auth Required:** Yes

**Response:**
```json
{
  "message": "Successfully unsubscribed"
}
```

### Get My Subscriptions

**Endpoint:** `GET /api/subscriptions/my-subscriptions`

**Auth Required:** Yes

**Query Parameters:**
- `active_only` (boolean, optional): Only show active subscriptions (default: true)

**Response:**
```json
[
  {
    "id": 1,
    "trader_id": 123,
    "trader_username": "FlipKingFC",
    "trader_avatar": "https://...",
    "verified": false,
    "is_active": true,
    "subscription_type": "free",
    "subscribed_at": "2025-01-11T10:00:00Z"
  }
]
```

### Get My Followers

**Endpoint:** `GET /api/subscriptions/followers`

**Auth Required:** Yes (Trader only)

**Response:**
```json
[
  {
    "user_id": 456,
    "username": "trader123",
    "avatar_url": "https://...",
    "subscribed_at": "2025-01-11T10:00:00Z",
    "subscription_type": "free"
  }
]
```

### Check Subscription Status

**Endpoint:** `GET /api/subscriptions/check/{trader_id}`

**Auth Required:** Yes

**Response:**
```json
{
  "is_subscribed": true,
  "subscription": {
    "id": 1,
    "subscription_type": "free",
    "subscribed_at": "2025-01-11T10:00:00Z"
  }
}
```

### Get Subscription Stats

**Endpoint:** `GET /api/subscriptions/stats`

**Auth Required:** Yes

**Response:**
```json
{
  "account_type": "trader",
  "following_count": 25,
  "followers_count": 1250
}
```

### Get Recommended Traders

**Endpoint:** `GET /api/subscriptions/recommended-traders`

**Auth Required:** No (optional)

**Query Parameters:**
- `limit` (integer, optional): Number of traders to return (default: 10, max: 50)

**Response:**
```json
[
  {
    "id": 123,
    "username": "FlipKingFC",
    "avatar_url": "https://...",
    "bio": "Professional FIFA trader...",
    "specialties": ["Quick Flips", "SBC Investments"],
    "verified": false,
    "total_followers": 1250,
    "total_posts": 450,
    "avg_rating": 4.5,
    "total_ratings": 87,
    "is_subscribed": false
  }
]
```

---

## Post Interactions

### Add Reaction (Like/Dislike)

**Endpoint:** `POST /api/interactions/reactions`

**Auth Required:** Yes

**Request Body:**
```json
{
  "post_id": 1,
  "reaction_type": "like"
}
```

**Reaction Types:** `like` or `dislike`

**Response:**
```json
{
  "id": 1,
  "user_id": 456,
  "post_id": 1,
  "reaction_type": "like",
  "created_at": "2025-01-11T10:00:00Z"
}
```

### Remove Reaction

**Endpoint:** `DELETE /api/interactions/reactions/{post_id}`

**Auth Required:** Yes

**Response:**
```json
{
  "message": "Reaction removed"
}
```

### Get Post Reactions

**Endpoint:** `GET /api/interactions/reactions/{post_id}`

**Auth Required:** No

**Response:**
```json
{
  "likes": [
    {
      "id": 1,
      "user_id": 456,
      "username": "trader123",
      "avatar_url": "https://...",
      "created_at": "2025-01-11T10:00:00Z"
    }
  ],
  "dislikes": [],
  "likes_count": 45,
  "dislikes_count": 2
}
```

### Add Comment

**Endpoint:** `POST /api/interactions/comments`

**Auth Required:** Yes

**Request Body:**
```json
{
  "post_id": 1,
  "content": "Great tip! Just bought 10 cards.",
  "parent_comment_id": null
}
```

**Response:**
```json
{
  "id": 1,
  "post_id": 1,
  "user_id": 456,
  "parent_comment_id": null,
  "content": "Great tip! Just bought 10 cards.",
  "likes_count": 0,
  "created_at": "2025-01-11T10:00:00Z",
  "updated_at": "2025-01-11T10:00:00Z",
  "deleted_at": null,
  "username": "trader123",
  "avatar_url": "https://...",
  "verified": false,
  "user_has_liked": false,
  "is_author": true
}
```

### Get Comments

**Endpoint:** `GET /api/interactions/comments/{post_id}`

**Auth Required:** No (optional)

**Query Parameters:**
- `offset` (integer, optional): Pagination offset (default: 0)
- `limit` (integer, optional): Items per page (default: 50, max: 100)

**Response:** Array of comment objects (same as add comment response).

### Update Comment

**Endpoint:** `PATCH /api/interactions/comments/{comment_id}`

**Auth Required:** Yes (Author only)

**Request Body:**
```json
{
  "content": "Updated comment text"
}
```

### Delete Comment

**Endpoint:** `DELETE /api/interactions/comments/{comment_id}`

**Auth Required:** Yes (Author only)

**Response:**
```json
{
  "message": "Comment deleted"
}
```

### Like Comment

**Endpoint:** `POST /api/interactions/comments/like`

**Auth Required:** Yes

**Request Body:**
```json
{
  "comment_id": 1
}
```

**Response:**
```json
{
  "message": "Comment liked"
}
```

### Unlike Comment

**Endpoint:** `DELETE /api/interactions/comments/like/{comment_id}`

**Auth Required:** Yes

**Response:**
```json
{
  "message": "Like removed"
}
```

---

## Messaging System

### Send Message

**Endpoint:** `POST /api/messages/send`

**Auth Required:** Yes

**Request Body:**
```json
{
  "recipient_id": 123,
  "content": "Hey, great trading tip! Can you share more details?"
}
```

**Response:**
```json
{
  "id": 1,
  "conversation_id": 1,
  "sender_id": 456,
  "recipient_id": 123,
  "content": "Hey, great trading tip!...",
  "read_at": null,
  "created_at": "2025-01-11T10:00:00Z",
  "deleted_at": null,
  "sender_username": "trader123",
  "sender_avatar": "https://...",
  "recipient_username": "FlipKingFC",
  "recipient_avatar": "https://..."
}
```

### Get Conversations

**Endpoint:** `GET /api/messages/conversations`

**Auth Required:** Yes

**Response:**
```json
[
  {
    "id": 1,
    "user1_id": 123,
    "user2_id": 456,
    "last_message_id": 10,
    "last_message_at": "2025-01-11T10:00:00Z",
    "created_at": "2025-01-10T15:00:00Z",
    "other_user_id": 123,
    "other_user_username": "FlipKingFC",
    "other_user_avatar": "https://...",
    "last_message_content": "Thanks for the tip!",
    "unread_count": 2
  }
]
```

### Get Conversation Messages

**Endpoint:** `GET /api/messages/conversations/{other_user_id}/messages`

**Auth Required:** Yes

**Query Parameters:**
- `offset` (integer, optional): Pagination offset (default: 0)
- `limit` (integer, optional): Items per page (default: 50, max: 100)

**Response:** Array of message objects.

**Note:** This endpoint automatically marks messages as read.

### Delete Message

**Endpoint:** `DELETE /api/messages/messages/{message_id}`

**Auth Required:** Yes (Sender only)

**Response:**
```json
{
  "message": "Message deleted"
}
```

### Get Unread Count

**Endpoint:** `GET /api/messages/unread-count`

**Auth Required:** Yes

**Response:**
```json
{
  "total_unread": 5,
  "by_conversation": [
    {
      "conversation_id": 1,
      "unread_count": 2
    },
    {
      "conversation_id": 2,
      "unread_count": 3
    }
  ]
}
```

### Mark Conversation as Read

**Endpoint:** `POST /api/messages/mark-read/{conversation_id}`

**Auth Required:** Yes

**Response:**
```json
{
  "message": "Messages marked as read"
}
```

### Search Users for Messaging

**Endpoint:** `GET /api/messages/search`

**Auth Required:** Yes

**Query Parameters:**
- `query` (string, required): Search term (minimum 1 character)

**Response:**
```json
[
  {
    "id": 123,
    "username": "FlipKingFC",
    "avatar_url": "https://...",
    "account_type": "trader",
    "verified": false
  }
]
```

---

## Rating System

### Rate a Trader

**Endpoint:** `POST /api/ratings/rate`

**Auth Required:** Yes

**Request Body:**
```json
{
  "trader_id": 123,
  "rating": 5,
  "review": "Excellent trader! His tips helped me make 2M coins this week."
}
```

**Rating:** 1-5 stars (integer)

**Response:**
```json
{
  "id": 1,
  "trader_id": 123,
  "rater_id": 456,
  "rating": 5,
  "review": "Excellent trader! His tips helped me...",
  "created_at": "2025-01-11T10:00:00Z",
  "updated_at": "2025-01-11T10:00:00Z"
}
```

### Get Trader Ratings

**Endpoint:** `GET /api/ratings/trader/{trader_id}`

**Auth Required:** No

**Query Parameters:**
- `offset` (integer, optional): Pagination offset (default: 0)
- `limit` (integer, optional): Items per page (default: 20, max: 100)

**Response:**
```json
[
  {
    "id": 1,
    "trader_id": 123,
    "rater_id": 456,
    "rating": 5,
    "review": "Excellent trader!...",
    "created_at": "2025-01-11T10:00:00Z",
    "updated_at": "2025-01-11T10:00:00Z",
    "rater_username": "trader123",
    "rater_avatar": "https://..."
  }
]
```

### Get Trader Rating Summary

**Endpoint:** `GET /api/ratings/trader/{trader_id}/summary`

**Auth Required:** No

**Response:**
```json
{
  "avg_rating": 4.5,
  "total_ratings": 87,
  "five_stars": 52,
  "four_stars": 25,
  "three_stars": 8,
  "two_stars": 2,
  "one_star": 0
}
```

### Get My Rating for Trader

**Endpoint:** `GET /api/ratings/my-rating/{trader_id}`

**Auth Required:** Yes

**Response:**
```json
{
  "has_rated": true,
  "rating": {
    "id": 1,
    "trader_id": 123,
    "rater_id": 456,
    "rating": 5,
    "review": "Excellent trader!...",
    "created_at": "2025-01-11T10:00:00Z",
    "updated_at": "2025-01-11T10:00:00Z"
  }
}
```

### Delete Rating

**Endpoint:** `DELETE /api/ratings/rating/{trader_id}`

**Auth Required:** Yes

**Response:**
```json
{
  "message": "Rating deleted"
}
```

### Get Top Rated Traders

**Endpoint:** `GET /api/ratings/leaderboard`

**Auth Required:** No

**Query Parameters:**
- `limit` (integer, optional): Number of traders (default: 20, max: 100)
- `min_ratings` (integer, optional): Minimum number of ratings required (default: 5)

**Response:**
```json
[
  {
    "id": 123,
    "username": "FlipKingFC",
    "avatar_url": "https://...",
    "bio": "Professional FIFA trader...",
    "verified": false,
    "total_followers": 1250,
    "total_posts": 450,
    "avg_rating": 4.8,
    "total_ratings": 120
  }
]
```

---

## Trader Discovery

### Update Trader Profile

**Endpoint:** `PATCH /api/traders/profile`

**Auth Required:** Yes (Trader only)

**Request Body:**
```json
{
  "bio": "Updated bio",
  "specialties": ["Quick Flips", "Icon Trading"],
  "subscription_price": 0
}
```

### Get Trader Profile

**Endpoint:** `GET /api/traders/profile/{trader_id}`

**Auth Required:** No (optional)

**Response:**
```json
{
  "id": 123,
  "username": "FlipKingFC",
  "avatar_url": "https://...",
  "bio": "Professional FIFA trader with 5 years experience",
  "specialties": ["Quick Flips", "SBC Investments"],
  "verified": false,
  "subscription_price": 0,
  "total_followers": 1250,
  "total_posts": 450,
  "avg_rating": 4.5,
  "total_ratings": 87,
  "trader_since": "2024-01-01T00:00:00Z",
  "is_subscribed": false
}
```

### Browse Traders

**Endpoint:** `GET /api/traders/browse`

**Auth Required:** No (optional)

**Query Parameters:**
- `search` (string, optional): Search by username or bio
- `specialty` (string, optional): Filter by specialty
- `verified_only` (boolean, optional): Only show verified traders (default: false)
- `sort_by` (string, optional): `followers`, `rating`, `posts`, or `recent` (default: `followers`)
- `offset` (integer, optional): Pagination offset (default: 0)
- `limit` (integer, optional): Items per page (default: 20, max: 100)

**Response:**
```json
{
  "traders": [
    {
      "id": 123,
      "username": "FlipKingFC",
      "avatar_url": "https://...",
      "bio": "Professional FIFA trader...",
      "specialties": ["Quick Flips", "SBC Investments"],
      "verified": false,
      "subscription_price": 0,
      "total_followers": 1250,
      "total_posts": 450,
      "avg_rating": 4.5,
      "total_ratings": 87,
      "trader_since": "2024-01-01T00:00:00Z",
      "is_subscribed": false
    }
  ],
  "total": 500,
  "has_more": true,
  "offset": 0,
  "limit": 20
}
```

### Get Trader Stats

**Endpoint:** `GET /api/traders/stats/{trader_id}`

**Auth Required:** No

**Response:**
```json
{
  "account_type": "trader",
  "total_posts": 450,
  "total_followers": 1250,
  "total_following": 25,
  "total_likes_received": 15000,
  "total_comments_received": 3500,
  "avg_rating": 4.5,
  "total_ratings": 87,
  "verified": false,
  "subscription_price": 0
}
```

### Get My Trader Profile

**Endpoint:** `GET /api/traders/my-profile`

**Auth Required:** Yes

**Response:**
```json
{
  "account_type": "trader",
  "is_trader": true,
  "profile": {
    "user_id": 123,
    "bio": "Professional FIFA trader...",
    "specialties": ["Quick Flips"],
    "verified": false,
    "subscription_price": 0,
    "total_followers": 1250,
    "total_posts": 450,
    "avg_rating": 4.5,
    "total_ratings": 87,
    "achievements": [],
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2025-01-11T10:00:00Z",
    "username": "FlipKingFC",
    "avatar_url": "https://..."
  }
}
```

### Get Available Specialties

**Endpoint:** `GET /api/traders/specialties`

**Auth Required:** No

**Response:**
```json
{
  "specialties": [
    "Budget Trading",
    "Icon Trading",
    "Long Term Trading",
    "Market Analysis",
    "Mass Bidding",
    "Quick Flips",
    "SBC Investments"
  ]
}
```

---

## Notifications

### Get Notifications

**Endpoint:** `GET /api/notifications/`

**Auth Required:** Yes

**Query Parameters:**
- `unread_only` (boolean, optional): Only show unread notifications (default: false)
- `notification_type` (string, optional): Filter by type
- `offset` (integer, optional): Pagination offset (default: 0)
- `limit` (integer, optional): Items per page (default: 50, max: 100)

**Notification Types:**
- `new_follower` - Someone followed you
- `post_like` - Someone liked your post
- `post_comment` - Someone commented on your post
- `new_message` - New direct message
- `new_rating` - Someone rated you
- `mention` - Someone mentioned you
- `subscription` - Subscription-related notification

**Response:**
```json
[
  {
    "id": 1,
    "user_id": 123,
    "notification_type": "post_like",
    "title": "New Like",
    "message": "Someone liked your post",
    "related_user_id": 456,
    "related_post_id": 10,
    "related_comment_id": null,
    "related_message_id": null,
    "read_at": null,
    "created_at": "2025-01-11T10:00:00Z",
    "related_username": "trader123",
    "related_avatar": "https://..."
  }
]
```

### Get Unread Notification Count

**Endpoint:** `GET /api/notifications/unread-count`

**Auth Required:** Yes

**Response:**
```json
{
  "total_unread": 15,
  "by_type": {
    "new_follower": 3,
    "post_like": 8,
    "post_comment": 4
  }
}
```

### Mark Notification as Read

**Endpoint:** `POST /api/notifications/mark-read/{notification_id}`

**Auth Required:** Yes

**Response:**
```json
{
  "message": "Notification marked as read"
}
```

### Mark All Notifications as Read

**Endpoint:** `POST /api/notifications/mark-all-read`

**Auth Required:** Yes

**Query Parameters:**
- `notification_type` (string, optional): Only mark specific type as read

**Response:**
```json
{
  "message": "All notifications marked as read"
}
```

### Delete Notification

**Endpoint:** `DELETE /api/notifications/{notification_id}`

**Auth Required:** Yes

**Response:**
```json
{
  "message": "Notification deleted"
}
```

### Clear All Notifications

**Endpoint:** `DELETE /api/notifications/clear-all`

**Auth Required:** Yes

**Query Parameters:**
- `read_only` (boolean, optional): Only delete read notifications (default: true)

**Response:**
```json
{
  "message": "Notifications cleared"
}
```

---

## Database Migration

To set up the database for the social feed feature, run the migration script:

```bash
psql $DATABASE_URL -f migrations/003_social_trading_feed.sql
```

This will create all necessary tables, indexes, triggers, and views.

---

## Error Responses

All endpoints return standard HTTP status codes:

- `200 OK` - Success
- `201 Created` - Resource created
- `400 Bad Request` - Invalid request data
- `401 Unauthorized` - Not authenticated
- `403 Forbidden` - Not authorized (e.g., not a trader)
- `404 Not Found` - Resource not found
- `500 Internal Server Error` - Server error

**Error Response Format:**
```json
{
  "detail": "Error message describing what went wrong"
}
```

---

## Best Practices

1. **Pagination**: Always use pagination for list endpoints to avoid performance issues
2. **Rate Limiting**: Consider implementing rate limiting on post creation and messaging
3. **Caching**: Cache trader profiles and feed data where appropriate
4. **Real-time Updates**: Consider using WebSockets for real-time notifications and messages
5. **Content Moderation**: Implement content moderation for posts and comments
6. **Premium Content**: Use `is_premium` flag to gate exclusive content for paid subscribers

---

## Future Enhancements

Potential features to add:
- WebSocket support for real-time updates
- Push notifications
- Post reporting and moderation
- Paid subscriptions via Stripe
- Post scheduling
- Analytics dashboard for traders
- Image/video attachments for posts
- Trending posts algorithm
- Post bookmarking
- Private/public account settings

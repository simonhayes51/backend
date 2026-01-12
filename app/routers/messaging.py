"""
Instant Messaging Router - Direct messages between users
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from typing import List
import asyncpg

from app.models.social import (
    MessageCreate,
    Message,
    MessageWithUser,
    Conversation,
    ConversationWithDetails,
)
from app.db import get_pool

router = APIRouter(prefix="/api/messages", tags=["Messaging"])


def get_current_user(request):
    """Extract user from session"""
    if "user" not in request.session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return request.session["user"]


async def get_db():
    """Database connection dependency"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


async def get_or_create_conversation(
    db: asyncpg.Connection,
    user1_id: int,
    user2_id: int
) -> int:
    """
    Get or create a conversation between two users
    Always stores user IDs with smaller ID first (user1_id < user2_id)
    """
    # Ensure user1_id < user2_id
    if user1_id > user2_id:
        user1_id, user2_id = user2_id, user1_id

    # Check if conversation exists
    conversation_id = await db.fetchval(
        """
        SELECT id FROM conversations
        WHERE user1_id = $1 AND user2_id = $2
        """,
        user1_id,
        user2_id
    )

    if conversation_id:
        return conversation_id

    # Create new conversation
    conversation_id = await db.fetchval(
        """
        INSERT INTO conversations (user1_id, user2_id)
        VALUES ($1, $2)
        RETURNING id
        """,
        user1_id,
        user2_id
    )

    return conversation_id


@router.post("/send", response_model=MessageWithUser)
async def send_message(
    message: MessageCreate,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Send a direct message to another user
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Can't message yourself
    if user_id == message.recipient_id:
        raise HTTPException(status_code=400, detail="Cannot message yourself")

    # Check if recipient exists
    recipient = await db.fetchval(
        "SELECT id FROM users WHERE id = $1",
        message.recipient_id
    )

    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient not found")

    # Get or create conversation
    conversation_id = await get_or_create_conversation(
        db, user_id, message.recipient_id
    )

    # Create message
    msg_row = await db.fetchrow(
        """
        INSERT INTO messages (
            conversation_id, sender_id, recipient_id, content
        )
        VALUES ($1, $2, $3, $4)
        RETURNING *
        """,
        conversation_id,
        user_id,
        message.recipient_id,
        message.content
    )

    # Get user profiles for response
    sender_info = await db.fetchrow(
        "SELECT username, avatar_url FROM user_profiles WHERE user_id = $1",
        user_id
    )

    recipient_info = await db.fetchrow(
        "SELECT username, avatar_url FROM user_profiles WHERE user_id = $1",
        message.recipient_id
    )

    # Create notification for recipient
    await db.execute(
        """
        INSERT INTO notifications (
            user_id, notification_type, title, message,
            related_user_id, related_message_id
        )
        VALUES ($1, 'new_message', 'New Message', $2, $3, $4)
        """,
        message.recipient_id,
        f"{sender_info['username']} sent you a message",
        user_id,
        msg_row["id"]
    )

    # Combine message data with user info
    msg_dict = dict(msg_row)
    msg_dict["sender_username"] = sender_info["username"]
    msg_dict["sender_avatar"] = sender_info["avatar_url"]
    msg_dict["recipient_username"] = recipient_info["username"]
    msg_dict["recipient_avatar"] = recipient_info["avatar_url"]

    return msg_dict


@router.get("/conversations", response_model=List[ConversationWithDetails])
async def get_conversations(
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get all conversations for the current user
    """
    user = get_current_user(request)
    user_id = user["id"]

    query = """
        SELECT
            c.*,
            CASE
                WHEN c.user1_id = $1 THEN c.user2_id
                ELSE c.user1_id
            END as other_user_id,
            CASE
                WHEN c.user1_id = $1 THEN up2.username
                ELSE up1.username
            END as other_user_username,
            CASE
                WHEN c.user1_id = $1 THEN up2.avatar_url
                ELSE up1.avatar_url
            END as other_user_avatar,
            m.content as last_message_content,
            (
                SELECT COUNT(*)
                FROM messages
                WHERE conversation_id = c.id
                    AND recipient_id = $1
                    AND read_at IS NULL
            ) as unread_count
        FROM conversations c
        LEFT JOIN user_profiles up1 ON c.user1_id = up1.user_id
        LEFT JOIN user_profiles up2 ON c.user2_id = up2.user_id
        LEFT JOIN messages m ON c.last_message_id = m.id
        WHERE c.user1_id = $1 OR c.user2_id = $1
        ORDER BY c.last_message_at DESC NULLS LAST
    """

    rows = await db.fetch(query, user_id)
    return [dict(row) for row in rows]


@router.get("/conversations/{other_user_id}/messages", response_model=List[MessageWithUser])
async def get_conversation_messages(
    other_user_id: int,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get messages in a conversation with another user
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Get conversation ID
    user1_id = min(user_id, other_user_id)
    user2_id = max(user_id, other_user_id)

    conversation = await db.fetchrow(
        "SELECT * FROM conversations WHERE user1_id = $1 AND user2_id = $2",
        user1_id,
        user2_id
    )

    if not conversation:
        # No conversation exists yet
        return []

    conversation_id = conversation["id"]

    # Mark messages as read
    await db.execute(
        """
        UPDATE messages
        SET read_at = NOW()
        WHERE conversation_id = $1
            AND recipient_id = $2
            AND read_at IS NULL
        """,
        conversation_id,
        user_id
    )

    # Get messages with user info
    query = """
        SELECT
            m.*,
            up1.username as sender_username,
            up1.avatar_url as sender_avatar,
            up2.username as recipient_username,
            up2.avatar_url as recipient_avatar
        FROM messages m
        JOIN user_profiles up1 ON m.sender_id = up1.user_id
        JOIN user_profiles up2 ON m.recipient_id = up2.user_id
        WHERE m.conversation_id = $1 AND m.deleted_at IS NULL
        ORDER BY m.created_at DESC
        LIMIT $2 OFFSET $3
    """

    rows = await db.fetch(query, conversation_id, limit, offset)
    return [dict(row) for row in rows]


@router.delete("/messages/{message_id}")
async def delete_message(
    message_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Delete a message (sender only) - soft delete
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Check if message belongs to user
    message = await db.fetchrow(
        "SELECT * FROM messages WHERE id = $1",
        message_id
    )

    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    if message["sender_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this message")

    # Soft delete
    await db.execute(
        "UPDATE messages SET deleted_at = NOW() WHERE id = $1",
        message_id
    )

    return {"message": "Message deleted"}


@router.get("/unread-count")
async def get_unread_count(
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get total count of unread messages for current user
    """
    user = get_current_user(request)
    user_id = user["id"]

    count = await db.fetchval(
        """
        SELECT COUNT(*)
        FROM messages
        WHERE recipient_id = $1 AND read_at IS NULL AND deleted_at IS NULL
        """,
        user_id
    )

    # Get unread count per conversation
    conversations = await db.fetch(
        """
        SELECT
            conversation_id,
            COUNT(*) as unread_count
        FROM messages
        WHERE recipient_id = $1 AND read_at IS NULL AND deleted_at IS NULL
        GROUP BY conversation_id
        """,
        user_id
    )

    return {
        "total_unread": count,
        "by_conversation": [dict(row) for row in conversations]
    }


@router.post("/mark-read/{conversation_id}")
async def mark_conversation_read(
    conversation_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Mark all messages in a conversation as read
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Verify user is part of conversation
    conversation = await db.fetchrow(
        "SELECT * FROM conversations WHERE id = $1",
        conversation_id
    )

    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if conversation["user1_id"] != user_id and conversation["user2_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not authorized")

    # Mark messages as read
    result = await db.execute(
        """
        UPDATE messages
        SET read_at = NOW()
        WHERE conversation_id = $1
            AND recipient_id = $2
            AND read_at IS NULL
        """,
        conversation_id,
        user_id
    )

    return {"message": "Messages marked as read"}


@router.get("/search")
async def search_users_for_messaging(
    request: Request,
    query: str = Query(..., min_length=1),
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Search for users to start a conversation with
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Search users by username
    rows = await db.fetch(
        """
        SELECT
            u.id,
            up.username,
            up.avatar_url,
            u.account_type,
            COALESCE(tp.verified, FALSE) as verified
        FROM users u
        JOIN user_profiles up ON u.id = up.user_id
        LEFT JOIN trader_profiles tp ON u.id = tp.user_id
        WHERE u.id != $1
            AND up.username ILIKE $2
        LIMIT 20
        """,
        user_id,
        f"%{query}%"
    )

    return [dict(row) for row in rows]


@router.get("/users/search")
async def search_users_for_messaging_alias(
    request: Request,
    query: str = Query(..., min_length=1),
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Alias: Search for users to start a conversation with
    """
    return await search_users_for_messaging(request=request, query=query, db=db)


@router.get("/search/users")
async def search_users_for_messaging_alt_alias(
    request: Request,
    query: str = Query(..., min_length=1),
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Alias: Search for users to start a conversation with
    """
    return await search_users_for_messaging(request=request, query=query, db=db)

"""
Instant Messaging Router - Direct messages between users
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from typing import List, Optional
from pydantic import BaseModel, root_validator
import asyncpg

from app.models.social import (
    MessageCreate,
    Message,
    MessageWithUser,
    MessagePayload,
    Conversation,
    ConversationWithDetails,
)
from app.db import get_db

router = APIRouter(prefix="/api/messages", tags=["Messaging"])
social_router = APIRouter(prefix="/api/social/messages", tags=["Messaging"])


def get_current_user(request):
    """Extract user from session"""
    if "user" not in request.session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return request.session["user"]


async def get_or_create_conversation(
    db: asyncpg.Connection,
    user1_id: str,
    user2_id: str
) -> int:
    """
    Get or create a conversation between two users
    Always stores user IDs with smaller ID first (user1_id < user2_id)
    """
    user1_id, user2_id = sorted([user1_id, user2_id])

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


async def _get_conversation(
    db: asyncpg.Connection,
    conversation_id: int,
    user_id: str,
) -> asyncpg.Record:
    conversation = await db.fetchrow(
        "SELECT * FROM conversations WHERE id = $1",
        conversation_id,
    )
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conversation["user1_id"] != user_id and conversation["user2_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    return conversation


def _format_message(row: dict, user_id: str) -> dict:
    message = dict(row)
    message["is_sender"] = message.get("sender_id") == user_id
    return message


def _format_conversation(row: dict) -> dict:
    conversation = dict(row)
    conversation["title"] = conversation.get("other_user_username")
    conversation["participant"] = {
        "id": conversation.get("other_user_id"),
        "username": conversation.get("other_user_username"),
        "avatar_url": conversation.get("other_user_avatar"),
    }
    conversation["last_message"] = conversation.get("last_message_content")
    return conversation


async def _get_conversation_columns(db: asyncpg.Connection) -> set:
    rows = await db.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'conversations'
        """
    )
    return {row["column_name"] for row in rows}


async def _table_exists(db: asyncpg.Connection, table_name: str) -> bool:
    return await db.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_name = $1
        )
        """,
        table_name,
    )


async def _user_profiles_exist(db: asyncpg.Connection) -> bool:
    return await _table_exists(db, "user_profiles")


async def _update_conversation_last_message(
    db: asyncpg.Connection,
    conversation_id: int,
    message_id: int,
) -> None:
    columns = await _get_conversation_columns(db)
    set_parts = []
    params = []
    param_index = 1

    if "last_message_id" in columns:
        set_parts.append(f"last_message_id = ${param_index}")
        params.append(message_id)
        param_index += 1
    if "last_message_at" in columns:
        set_parts.append("last_message_at = NOW()")

    if not set_parts:
        return

    params.append(conversation_id)
    query = f"UPDATE conversations SET {', '.join(set_parts)} WHERE id = ${param_index}"
    await db.execute(query, *params)


async def _notifications_supports_related_message(db: asyncpg.Connection) -> bool:
    if not await _table_exists(db, "notifications"):
        return False
    return await db.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = 'notifications'
              AND column_name = 'related_message_id'
        )
        """
    )


async def _create_message_notification(
    db: asyncpg.Connection,
    recipient_id: str,
    sender_id: str,
    sender_name: str,
    message_id: int,
) -> None:
    if await _notifications_supports_related_message(db):
        await db.execute(
            """
            INSERT INTO notifications (
                user_id, notification_type, title, message,
                related_user_id, related_message_id
            )
            VALUES ($1, 'new_message', 'New Message', $2, $3, $4)
            """,
            recipient_id,
            f"{sender_name} sent you a message",
            sender_id,
            message_id,
        )
        return

    await db.execute(
        """
        INSERT INTO notifications (
            user_id, notification_type, title, message,
            related_user_id
        )
        VALUES ($1, 'new_message', 'New Message', $2, $3)
        """,
        recipient_id,
        f"{sender_name} sent you a message",
        sender_id,
    )


class StartConversationRequest(BaseModel):
    recipient_id: Optional[str] = None
    recipientId: Optional[str] = None
    recipient: Optional[str] = None
    user_id: Optional[str] = None
    userId: Optional[str] = None
    content: Optional[str] = None
    message: Optional[str] = None
    text: Optional[str] = None

    @root_validator(pre=True)
    def normalize_start_fields(cls, values):
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
    await _update_conversation_last_message(
        db=db,
        conversation_id=conversation_id,
        message_id=msg_row["id"],
    )

    # Get user profiles for response
    sender_info = None
    recipient_info = None
    if await _user_profiles_exist(db):
        sender_info = await db.fetchrow(
            "SELECT username, avatar_url FROM user_profiles WHERE user_id = $1",
            user_id
        )

        recipient_info = await db.fetchrow(
            "SELECT username, avatar_url FROM user_profiles WHERE user_id = $1",
            message.recipient_id
        )

    # Create notification for recipient
    sender_name = (
        (sender_info or {}).get("username")
        or user.get("username")
        or "Someone"
    )
    await _create_message_notification(
        db=db,
        recipient_id=message.recipient_id,
        sender_id=user_id,
        sender_name=sender_name,
        message_id=msg_row["id"],
    )

    # Combine message data with user info
    msg_dict = dict(msg_row)
    msg_dict["sender_username"] = (
        (sender_info or {}).get("username") or user.get("username")
    )
    msg_dict["sender_avatar"] = (
        (sender_info or {}).get("avatar_url") or user.get("avatar_url")
    )
    msg_dict["recipient_username"] = (recipient_info or {}).get("username")
    msg_dict["recipient_avatar"] = (recipient_info or {}).get("avatar_url")

    return _format_message(msg_dict, user_id)


@router.get("/conversations", response_model=List[ConversationWithDetails])
@social_router.get("/conversations", response_model=List[ConversationWithDetails])
async def get_conversations(
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get all conversations for the current user
    """
    user = get_current_user(request)
    user_id = user["id"]

    columns = await _get_conversation_columns(db)
    profiles_exist = await _user_profiles_exist(db)
    last_message_select = (
        "m.content as last_message_content"
        if "last_message_id" in columns
        else "NULL as last_message_content"
    )
    last_message_join = (
        "LEFT JOIN messages m ON c.last_message_id = m.id"
        if "last_message_id" in columns
        else ""
    )
    order_clause = (
        "ORDER BY c.last_message_at DESC NULLS LAST"
        if "last_message_at" in columns
        else ("ORDER BY c.created_at DESC" if "created_at" in columns else "ORDER BY c.id DESC")
    )

    username_select = (
        "CASE WHEN c.user1_id = $1 THEN up2.username ELSE up1.username END as other_user_username"
        if profiles_exist
        else "NULL as other_user_username"
    )
    avatar_select = (
        "CASE WHEN c.user1_id = $1 THEN up2.avatar_url ELSE up1.avatar_url END as other_user_avatar"
        if profiles_exist
        else "NULL as other_user_avatar"
    )
    profiles_join = (
        "LEFT JOIN user_profiles up1 ON c.user1_id = up1.user_id\n"
        "LEFT JOIN user_profiles up2 ON c.user2_id = up2.user_id"
        if profiles_exist
        else ""
    )

    query = f"""
        SELECT
            c.*,
            CASE
                WHEN c.user1_id = $1 THEN c.user2_id
            ELSE c.user1_id
            END as other_user_id,
            {username_select},
            {avatar_select},
            {last_message_select},
            (
                SELECT COUNT(*)
                FROM messages
                WHERE conversation_id = c.id
                    AND recipient_id = $1
                    AND read_at IS NULL
            ) as unread_count
        FROM conversations c
        {profiles_join}
        {last_message_join}
        WHERE c.user1_id = $1 OR c.user2_id = $1
        {order_clause}
    """

    rows = await db.fetch(query, user_id)
    return [_format_conversation(dict(row)) for row in rows]


@router.post("/conversations")
@social_router.post("/conversations")
async def start_conversation(
    payload: StartConversationRequest,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    user = get_current_user(request)
    user_id = user["id"]

    recipient_id = payload.recipient_id
    if not recipient_id:
        raise HTTPException(status_code=400, detail="Recipient id required")

    if user_id == recipient_id:
        raise HTTPException(status_code=400, detail="Cannot message yourself")

    recipient = await db.fetchval(
        "SELECT id FROM users WHERE id = $1",
        recipient_id,
    )
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient not found")

    conversation_id = await get_or_create_conversation(
        db, user_id, recipient_id
    )
    if payload.content:
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
            recipient_id,
            payload.content,
        )
        await _update_conversation_last_message(
            db=db,
            conversation_id=conversation_id,
            message_id=msg_row["id"],
        )
        sender_name = user.get("username") or "Someone"
        if await _user_profiles_exist(db):
            sender_info = await db.fetchrow(
                "SELECT username FROM user_profiles WHERE user_id = $1",
                user_id,
            )
            sender_name = (sender_info or {}).get("username", sender_name)
        await _create_message_notification(
            db=db,
            recipient_id=recipient_id,
            sender_id=user_id,
            sender_name=sender_name,
            message_id=msg_row["id"],
        )

    columns = await _get_conversation_columns(db)
    profiles_exist = await _user_profiles_exist(db)
    last_message_select = (
        "m.content as last_message_content"
        if "last_message_id" in columns
        else "NULL as last_message_content"
    )
    last_message_join = (
        "LEFT JOIN messages m ON c.last_message_id = m.id"
        if "last_message_id" in columns
        else ""
    )
    username_select = (
        "CASE WHEN c.user1_id = $1 THEN up2.username ELSE up1.username END as other_user_username"
        if profiles_exist
        else "NULL as other_user_username"
    )
    avatar_select = (
        "CASE WHEN c.user1_id = $1 THEN up2.avatar_url ELSE up1.avatar_url END as other_user_avatar"
        if profiles_exist
        else "NULL as other_user_avatar"
    )
    profiles_join = (
        "LEFT JOIN user_profiles up1 ON c.user1_id = up1.user_id\n"
        "LEFT JOIN user_profiles up2 ON c.user2_id = up2.user_id"
        if profiles_exist
        else ""
    )

    row = await db.fetchrow(
        f"""
        SELECT
            c.*,
            CASE
                WHEN c.user1_id = $1 THEN c.user2_id
            ELSE c.user1_id
            END as other_user_id,
            {username_select},
            {avatar_select},
            {last_message_select},
            (
                SELECT COUNT(*)
                FROM messages
                WHERE conversation_id = c.id
                    AND recipient_id = $1
                    AND read_at IS NULL
            ) as unread_count
        FROM conversations c
        {profiles_join}
        {last_message_join}
        WHERE c.id = $2
        """,
        user_id,
        conversation_id,
    )

    return _format_conversation(dict(row))


@router.get("/conversations/{other_user_id}/messages", response_model=List[MessageWithUser])
async def get_conversation_messages(
    other_user_id: str,
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
    user1_id, user2_id = sorted([user_id, other_user_id])

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

    profiles_exist = await _user_profiles_exist(db)
    if profiles_exist:
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
    else:
        query = """
            SELECT
                m.*,
                NULL as sender_username,
                NULL as sender_avatar,
                NULL as recipient_username,
                NULL as recipient_avatar
            FROM messages m
            WHERE m.conversation_id = $1 AND m.deleted_at IS NULL
            ORDER BY m.created_at DESC
            LIMIT $2 OFFSET $3
        """

    rows = await db.fetch(query, conversation_id, limit, offset)
    return [_format_message(dict(row), user_id) for row in rows]


@router.get("/conversations/{conversation_id}/messages")
@social_router.get("/conversations/{conversation_id}/messages")
async def get_conversation_messages_by_id(
    conversation_id: int,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: asyncpg.Connection = Depends(get_db),
):
    user = get_current_user(request)
    user_id = user["id"]

    conversation = await _get_conversation(db, conversation_id, user_id)

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

    profiles_exist = await _user_profiles_exist(db)
    if profiles_exist:
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
    else:
        query = """
            SELECT
                m.*,
                NULL as sender_username,
                NULL as sender_avatar,
                NULL as recipient_username,
                NULL as recipient_avatar
            FROM messages m
            WHERE m.conversation_id = $1 AND m.deleted_at IS NULL
            ORDER BY m.created_at DESC
            LIMIT $2 OFFSET $3
        """

    rows = await db.fetch(query, conversation["id"], limit, offset)
    return [_format_message(dict(row), user_id) for row in rows]


@router.post("/conversations/{conversation_id}/messages")
@social_router.post("/conversations/{conversation_id}/messages")
async def send_message_in_conversation(
    conversation_id: int,
    payload: MessagePayload,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    user = get_current_user(request)
    user_id = user["id"]

    conversation = await _get_conversation(db, conversation_id, user_id)
    recipient_id = (
        conversation["user2_id"]
        if conversation["user1_id"] == user_id
        else conversation["user1_id"]
    )

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
        recipient_id,
        payload.content,
    )
    await _update_conversation_last_message(
        db=db,
        conversation_id=conversation_id,
        message_id=msg_row["id"],
    )

    sender_info = None
    recipient_info = None
    if await _user_profiles_exist(db):
        sender_info = await db.fetchrow(
            "SELECT username, avatar_url FROM user_profiles WHERE user_id = $1",
            user_id
        )

        recipient_info = await db.fetchrow(
            "SELECT username, avatar_url FROM user_profiles WHERE user_id = $1",
            recipient_id
        )

    sender_name = (
        (sender_info or {}).get("username")
        or user.get("username")
        or "Someone"
    )
    await _create_message_notification(
        db=db,
        recipient_id=recipient_id,
        sender_id=user_id,
        sender_name=sender_name,
        message_id=msg_row["id"],
    )

    msg_dict = dict(msg_row)
    msg_dict["sender_username"] = (
        (sender_info or {}).get("username") or user.get("username")
    )
    msg_dict["sender_avatar"] = (
        (sender_info or {}).get("avatar_url") or user.get("avatar_url")
    )
    msg_dict["recipient_username"] = (recipient_info or {}).get("username")
    msg_dict["recipient_avatar"] = (recipient_info or {}).get("avatar_url")

    return _format_message(msg_dict, user_id)


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


@social_router.get("/unread-count")
async def get_unread_count_social(
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    return await get_unread_count(request=request, db=db)


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


@router.post("/conversations/{conversation_id}/read")
@social_router.post("/conversations/{conversation_id}/read")
async def mark_conversation_read_alias(
    conversation_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    return await mark_conversation_read(
        conversation_id=conversation_id,
        request=request,
        db=db,
    )


@router.get("/search")
async def search_users_for_messaging(
    request: Request,
    query: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    searchTerm: Optional[str] = Query(None),
    term: Optional[str] = Query(None),
    name: Optional[str] = Query(None),
    value: Optional[str] = Query(None),
    username: Optional[str] = Query(None),
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Search for users to start a conversation with
    """
    user = get_current_user(request)
    user_id = user["id"]

    search_term = (
        query
        or q
        or search
        or searchTerm
        or term
        or name
        or value
        or username
    )
    if not search_term or not search_term.strip():
        return []
    if not await _user_profiles_exist(db):
        return []

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
        f"%{search_term}%"
    )

    return [dict(row) for row in rows]


@router.get("/users/search")
@social_router.get("/users/search")
async def search_users_for_messaging_alias(
    request: Request,
    query: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    searchTerm: Optional[str] = Query(None),
    term: Optional[str] = Query(None),
    name: Optional[str] = Query(None),
    value: Optional[str] = Query(None),
    username: Optional[str] = Query(None),
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Alias: Search for users to start a conversation with
    """
    return await search_users_for_messaging(
        request=request,
        query=query,
        q=q,
        search=search,
        searchTerm=searchTerm,
        term=term,
        name=name,
        value=value,
        username=username,
        db=db,
    )


@router.get("/search/users")
@social_router.get("/search/users")
async def search_users_for_messaging_alt_alias(
    request: Request,
    query: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    searchTerm: Optional[str] = Query(None),
    term: Optional[str] = Query(None),
    name: Optional[str] = Query(None),
    value: Optional[str] = Query(None),
    username: Optional[str] = Query(None),
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Alias: Search for users to start a conversation with
    """
    return await search_users_for_messaging(
        request=request,
        query=query,
        q=q,
        search=search,
        searchTerm=searchTerm,
        term=term,
        name=name,
        value=value,
        username=username,
        db=db,
    )

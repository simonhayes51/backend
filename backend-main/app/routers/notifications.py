"""
Notifications Router - User notifications for social interactions
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from typing import List, Optional
import asyncpg

from app.models.social import Notification, NotificationWithDetails
from app.db import get_pool

router = APIRouter(prefix="/api/notifications", tags=["Notifications"])


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


@router.get("", response_model=List[NotificationWithDetails])
@router.get("/", response_model=List[NotificationWithDetails], include_in_schema=False)
async def get_notifications(
    request: Request,
    unread_only: bool = Query(False),
    notification_type: Optional[str] = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get notifications for the current user
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Build query conditions
    conditions = ["n.user_id = $1"]
    params = [user_id]
    param_idx = 2

    if unread_only:
        conditions.append("n.read_at IS NULL")

    if notification_type:
        conditions.append(f"n.notification_type = ${param_idx}")
        params.append(notification_type)
        param_idx += 1

    where_clause = " AND ".join(conditions)

    # Add pagination params
    params.extend([limit, offset])

    query = f"""
        SELECT
            n.*,
            up.username as related_username,
            up.avatar_url as related_avatar
        FROM notifications n
        LEFT JOIN user_profiles up ON n.related_user_id = up.user_id
        WHERE {where_clause}
        ORDER BY n.created_at DESC
        LIMIT ${param_idx} OFFSET ${param_idx + 1}
    """

    rows = await db.fetch(query, *params)
    return [dict(row) for row in rows]


@router.get("/unread-count")
@router.get("/unread-count/", include_in_schema=False)
async def get_unread_count(
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get count of unread notifications
    """
    user = get_current_user(request)
    user_id = user["id"]

    count = await db.fetchval(
        """
        SELECT COUNT(*)
        FROM notifications
        WHERE user_id = $1 AND read_at IS NULL
        """,
        user_id
    )

    # Get counts by type
    by_type = await db.fetch(
        """
        SELECT notification_type, COUNT(*) as count
        FROM notifications
        WHERE user_id = $1 AND read_at IS NULL
        GROUP BY notification_type
        """,
        user_id
    )

    return {
        "total_unread": count,
        "by_type": {row["notification_type"]: row["count"] for row in by_type}
    }


@router.post("/mark-read/{notification_id}")
async def mark_notification_read(
    notification_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Mark a notification as read
    """
    user = get_current_user(request)
    user_id = user["id"]

    result = await db.fetchrow(
        """
        UPDATE notifications
        SET read_at = NOW()
        WHERE id = $1 AND user_id = $2
        RETURNING *
        """,
        notification_id,
        user_id
    )

    if not result:
        raise HTTPException(status_code=404, detail="Notification not found")

    return {"message": "Notification marked as read"}


@router.post("/mark-all-read")
async def mark_all_notifications_read(
    request: Request,
    notification_type: Optional[str] = Query(None),
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Mark all notifications as read, optionally filtered by type
    """
    user = get_current_user(request)
    user_id = user["id"]

    if notification_type:
        await db.execute(
            """
            UPDATE notifications
            SET read_at = NOW()
            WHERE user_id = $1 AND read_at IS NULL AND notification_type = $2
            """,
            user_id,
            notification_type
        )
    else:
        await db.execute(
            """
            UPDATE notifications
            SET read_at = NOW()
            WHERE user_id = $1 AND read_at IS NULL
            """,
            user_id
        )

    return {"message": "All notifications marked as read"}


@router.delete("/{notification_id}")
async def delete_notification(
    notification_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Delete a notification
    """
    user = get_current_user(request)
    user_id = user["id"]

    result = await db.execute(
        """
        DELETE FROM notifications
        WHERE id = $1 AND user_id = $2
        """,
        notification_id,
        user_id
    )

    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Notification not found")

    return {"message": "Notification deleted"}


@router.delete("/clear-all")
async def clear_all_notifications(
    request: Request,
    read_only: bool = Query(True, description="Only delete read notifications"),
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Clear all notifications (read ones by default)
    """
    user = get_current_user(request)
    user_id = user["id"]

    if read_only:
        result = await db.execute(
            """
            DELETE FROM notifications
            WHERE user_id = $1 AND read_at IS NOT NULL
            """,
            user_id
        )
    else:
        result = await db.execute(
            """
            DELETE FROM notifications
            WHERE user_id = $1
            """,
            user_id
        )

    return {"message": "Notifications cleared"}

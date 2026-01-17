"""
Content Requests Router - Subscriber content request system
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from typing import List, Optional
import asyncpg
from pydantic import BaseModel
from datetime import datetime

from app.db import get_db

router = APIRouter(prefix="/api/content-requests", tags=["Content Requests"])


def get_current_user(request: Request):
    """Extract user from session"""
    if "user" not in request.session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return request.session["user"]


# ============================================================================
# MODELS
# ============================================================================

class ContentRequestCreate(BaseModel):
    trader_id: str
    title: str
    description: Optional[str] = None
    category: Optional[str] = None


class ContentRequestUpdate(BaseModel):
    status: str  # pending, in_progress, completed, declined


class ContentRequestResponse(BaseModel):
    id: int
    trader_id: str
    requester_id: str
    title: str
    description: Optional[str]
    category: Optional[str]
    upvotes: int
    status: str
    created_at: datetime
    user_has_voted: bool = False
    requester_username: Optional[str] = None


# ============================================================================
# CONTENT REQUEST ENDPOINTS
# ============================================================================

@router.post("/create")
async def create_content_request(
    request_data: ContentRequestCreate,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    """
    Create a new content request for a trader
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Check if user is subscribed to this trader
    subscription = await db.fetchrow(
        """
        SELECT * FROM trader_subscriptions
        WHERE subscriber_id = $1 AND trader_id = $2 AND is_active = TRUE
        """,
        user_id,
        request_data.trader_id
    )

    if not subscription:
        raise HTTPException(
            status_code=403,
            detail="You must be subscribed to this trader to request content"
        )

    # Check rate limiting (max 5 pending requests per user per trader)
    pending_count = await db.fetchval(
        """
        SELECT COUNT(*) FROM content_requests
        WHERE requester_id = $1 AND trader_id = $2 AND status = 'pending'
        """,
        user_id,
        request_data.trader_id
    )

    if pending_count >= 5:
        raise HTTPException(
            status_code=400,
            detail="You have reached the maximum number of pending requests for this trader"
        )

    # Create request
    new_request = await db.fetchrow(
        """
        INSERT INTO content_requests (
            trader_id, requester_id, title, description, category
        )
        VALUES ($1, $2, $3, $4, $5)
        RETURNING *
        """,
        request_data.trader_id,
        user_id,
        request_data.title,
        request_data.description,
        request_data.category
    )

    # Auto-upvote own request
    await db.execute(
        """
        INSERT INTO content_request_votes (request_id, user_id)
        VALUES ($1, $2)
        """,
        new_request["id"],
        user_id
    )

    # Notify trader
    await db.execute(
        """
        INSERT INTO notifications (
            user_id, notification_type, title, message, related_user_id
        )
        VALUES ($1, 'content_request', 'New Content Request', $2, $3)
        """,
        request_data.trader_id,
        f"New content request: {request_data.title}",
        user_id
    )

    result = dict(new_request)
    result["user_has_voted"] = True
    result["upvotes"] = 1

    return {
        "success": True,
        "request": result,
        "message": "Content request created successfully!"
    }


@router.get("/trader/{trader_id}")
async def get_trader_content_requests(
    trader_id: str,
    request: Request,
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: asyncpg.Connection = Depends(get_db),
):
    """
    Get content requests for a trader, sorted by upvotes
    """
    try:
        user = get_current_user(request)
        user_id = user["id"]
        is_authenticated = True
    except HTTPException:
        user_id = None
        is_authenticated = False

    # Build query
    where_clauses = ["cr.trader_id = $1"]
    params = [trader_id]
    param_count = 1

    if status:
        param_count += 1
        where_clauses.append(f"cr.status = ${param_count}")
        params.append(status)

    where_clause = " AND ".join(where_clauses)

    query = f"""
        SELECT
            cr.*,
            up.username as requester_username,
            up.avatar_url as requester_avatar,
            {f"EXISTS(SELECT 1 FROM content_request_votes WHERE request_id = cr.id AND user_id = '{user_id}') as user_has_voted" if is_authenticated else "FALSE as user_has_voted"}
        FROM content_requests cr
        JOIN user_profiles up ON cr.requester_id = up.user_id
        WHERE {where_clause}
        ORDER BY cr.upvotes DESC, cr.created_at DESC
        LIMIT ${param_count + 1} OFFSET ${param_count + 2}
    """

    params.extend([limit, offset])
    requests = await db.fetch(query, *params)

    return {
        "requests": [dict(row) for row in requests],
        "total": len(requests)
    }


@router.post("/{request_id}/upvote")
async def upvote_content_request(
    request_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    """
    Upvote a content request (toggle)
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Check if request exists
    content_request = await db.fetchrow(
        "SELECT * FROM content_requests WHERE id = $1",
        request_id
    )

    if not content_request:
        raise HTTPException(status_code=404, detail="Content request not found")

    # Check if user is subscribed to the trader
    subscription = await db.fetchrow(
        """
        SELECT * FROM trader_subscriptions
        WHERE subscriber_id = $1 AND trader_id = $2 AND is_active = TRUE
        """,
        user_id,
        content_request["trader_id"]
    )

    if not subscription:
        raise HTTPException(
            status_code=403,
            detail="You must be subscribed to upvote content requests"
        )

    # Check if already voted
    existing_vote = await db.fetchval(
        """
        SELECT id FROM content_request_votes
        WHERE request_id = $1 AND user_id = $2
        """,
        request_id,
        user_id
    )

    if existing_vote:
        # Remove vote (toggle off)
        await db.execute(
            "DELETE FROM content_request_votes WHERE id = $1",
            existing_vote
        )

        upvotes = await db.fetchval(
            "SELECT upvotes FROM content_requests WHERE id = $1",
            request_id
        )

        return {
            "success": True,
            "voted": False,
            "upvotes": upvotes,
            "message": "Vote removed"
        }
    else:
        # Add vote
        await db.execute(
            """
            INSERT INTO content_request_votes (request_id, user_id)
            VALUES ($1, $2)
            """,
            request_id,
            user_id
        )

        upvotes = await db.fetchval(
            "SELECT upvotes FROM content_requests WHERE id = $1",
            request_id
        )

        return {
            "success": True,
            "voted": True,
            "upvotes": upvotes,
            "message": "Vote added"
        }


@router.patch("/{request_id}/status")
async def update_request_status(
    request_id: int,
    status_update: ContentRequestUpdate,
    request: Request,
    post_id: Optional[int] = Query(None),
    db: asyncpg.Connection = Depends(get_db),
):
    """
    Update content request status (trader only)
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Get request
    content_request = await db.fetchrow(
        "SELECT * FROM content_requests WHERE id = $1",
        request_id
    )

    if not content_request:
        raise HTTPException(status_code=404, detail="Content request not found")

    # Check if user is the trader
    if content_request["trader_id"] != user_id:
        raise HTTPException(
            status_code=403,
            detail="Only the trader can update request status"
        )

    # Validate status
    valid_statuses = ["pending", "in_progress", "completed", "declined"]
    if status_update.status not in valid_statuses:
        raise HTTPException(status_code=400, detail="Invalid status")

    # Update request
    update_fields = {
        "status": status_update.status,
        "updated_at": datetime.utcnow()
    }

    if status_update.status == "completed":
        update_fields["completed_at"] = datetime.utcnow()
        if post_id:
            update_fields["completed_post_id"] = post_id

    set_clause = ", ".join([f"{k} = ${i+2}" for i, k in enumerate(update_fields.keys())])
    values = [request_id] + list(update_fields.values())

    updated = await db.fetchrow(
        f"""
        UPDATE content_requests
        SET {set_clause}
        WHERE id = $1
        RETURNING *
        """,
        *values
    )

    # Notify requester
    status_messages = {
        "in_progress": "Your content request is now being worked on!",
        "completed": "Your content request has been completed!",
        "declined": "Your content request was declined"
    }

    if status_update.status in status_messages:
        await db.execute(
            """
            INSERT INTO notifications (
                user_id, notification_type, title, message,
                related_user_id, related_post_id
            )
            VALUES ($1, 'content_request_update', 'Content Request Update', $2, $3, $4)
            """,
            content_request["requester_id"],
            status_messages[status_update.status],
            user_id,
            post_id
        )

    return {
        "success": True,
        "request": dict(updated),
        "message": f"Status updated to {status_update.status}"
    }


@router.delete("/{request_id}")
async def delete_content_request(
    request_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    """
    Delete a content request (requester or trader only)
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Get request
    content_request = await db.fetchrow(
        "SELECT * FROM content_requests WHERE id = $1",
        request_id
    )

    if not content_request:
        raise HTTPException(status_code=404, detail="Content request not found")

    # Check if user is requester or trader
    if content_request["requester_id"] != user_id and content_request["trader_id"] != user_id:
        raise HTTPException(
            status_code=403,
            detail="Only the requester or trader can delete this request"
        )

    # Delete request (cascade will delete votes)
    await db.execute(
        "DELETE FROM content_requests WHERE id = $1",
        request_id
    )

    return {
        "success": True,
        "message": "Content request deleted"
    }


@router.get("/my-requests")
async def get_my_content_requests(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: asyncpg.Connection = Depends(get_db),
):
    """
    Get content requests created by current user
    """
    user = get_current_user(request)
    user_id = user["id"]

    requests = await db.fetch(
        """
        SELECT
            cr.*,
            up.username as trader_username,
            up.avatar_url as trader_avatar,
            tp.verified as trader_verified
        FROM content_requests cr
        JOIN user_profiles up ON cr.trader_id = up.user_id
        LEFT JOIN trader_profiles tp ON cr.trader_id = tp.user_id
        WHERE cr.requester_id = $1
        ORDER BY cr.created_at DESC
        LIMIT $2 OFFSET $3
        """,
        user_id,
        limit,
        offset
    )

    return {
        "requests": [dict(row) for row in requests],
        "total": len(requests)
    }

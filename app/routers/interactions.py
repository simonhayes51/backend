"""
Post Interactions Router - Likes, dislikes, comments
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from typing import List, Optional
import asyncpg
from pydantic import BaseModel

from app.models.social import (
    PostReactionCreate,
    PostReaction,
    CommentCreate,
    CommentUpdate,
    Comment,
    CommentWithAuthor,
    CommentLikeCreate,
)
from app.db import get_db

router = APIRouter(prefix="/api/interactions", tags=["Post Interactions"])
social_router = APIRouter(prefix="/api/social/interactions", tags=["Post Interactions"])
social_posts_router = APIRouter(prefix="/api/social", tags=["Post Interactions"])


def get_current_user(request):
    """Extract user from session"""
    if "user" not in request.session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return request.session["user"]


async def column_exists(
    db: asyncpg.Connection,
    table_name: str,
    column_name: str,
) -> bool:
    return await db.fetchval(
        """
        SELECT EXISTS(
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = $1 AND column_name = $2
        )
        """,
        table_name,
        column_name,
    )


# ============================================================================
# POST REACTIONS (Likes/Dislikes)
# ============================================================================

class ReactionPayload(BaseModel):
    reaction: str


class CommentPayload(BaseModel):
    content: Optional[str] = None
    comment: Optional[str] = None
    text: Optional[str] = None
    parent_comment_id: Optional[int] = None


def _format_comment_with_author(comment: dict) -> dict:
    likes_count = comment.get("likes_count")
    author = {
        "id": comment.get("user_id"),
        "username": comment.get("username"),
        "avatar_url": comment.get("avatar_url"),
        "is_verified": comment.get("verified"),
    }
    comment["author"] = author
    if likes_count is not None:
        comment["likes"] = likes_count
    return comment


async def _add_or_update_reaction(
    post_id: int,
    reaction_type: str,
    request: Request,
    db: asyncpg.Connection,
) -> dict:
    reaction = PostReactionCreate(post_id=post_id, reaction_type=reaction_type)
    return await add_reaction(reaction=reaction, request=request, db=db)


@router.post("/reactions", response_model=PostReaction)
async def add_reaction(
    reaction: PostReactionCreate,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Add or update a reaction to a post (like/dislike)
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Check if post exists
    post = await db.fetchval(
        "SELECT id FROM social_posts WHERE id = $1",
        reaction.post_id
    )

    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    # Check if user already reacted
    existing = await db.fetchrow(
        "SELECT * FROM post_reactions WHERE user_id = $1 AND post_id = $2",
        user_id,
        reaction.post_id
    )

    if existing:
        # If same reaction, remove it (toggle off)
        if existing["reaction_type"] == reaction.reaction_type:
            await db.execute(
                "DELETE FROM post_reactions WHERE user_id = $1 AND post_id = $2",
                user_id,
                reaction.post_id
            )
            # Get updated counts
            stats = await db.fetchrow(
                """
                SELECT
                    (SELECT COUNT(*) FROM post_reactions WHERE post_id = $1 AND reaction_type = 'like') as likes,
                    (SELECT COUNT(*) FROM post_reactions WHERE post_id = $1 AND reaction_type = 'dislike') as dislikes
                """,
                reaction.post_id
            )
            return {
                "removed": True,
                "reaction_type": None,
                "stats": dict(stats)
            }
        else:
            # Different reaction, update it
            row = await db.fetchrow(
                """
                UPDATE post_reactions
                SET reaction_type = $1
                WHERE user_id = $2 AND post_id = $3
                RETURNING *
                """,
                reaction.reaction_type,
                user_id,
                reaction.post_id
            )
    else:
        # Create new reaction
        row = await db.fetchrow(
            """
            INSERT INTO post_reactions (user_id, post_id, reaction_type)
            VALUES ($1, $2, $3)
            RETURNING *
            """,
            user_id,
            reaction.post_id,
            reaction.reaction_type
        )

        # Create notification for post author
        post_author = await db.fetchval(
            "SELECT user_id FROM social_posts WHERE id = $1",
            reaction.post_id
        )

        if post_author != user_id:
            user_info = await db.fetchrow(
                """
                SELECT up.username, up.avatar_url
                FROM user_profiles up
                WHERE up.user_id = $1
                """,
                user_id
            )

            username = user_info["username"] if user_info and user_info.get("username") else "Someone"
            await db.execute(
                """
                INSERT INTO notifications (
                    user_id, notification_type, title, message,
                    related_user_id, related_post_id
                )
                VALUES ($1, 'post_like', 'New Like', $2, $3, $4)
                """,
                post_author,
                f"{username} {'liked' if reaction.reaction_type == 'like' else 'disliked'} your post",
                user_id,
                reaction.post_id
            )
    
    # Get updated stats
    stats = await db.fetchrow(
        """
        SELECT
            (SELECT COUNT(*) FROM post_reactions WHERE post_id = $1 AND reaction_type = 'like') as likes,
            (SELECT COUNT(*) FROM post_reactions WHERE post_id = $1 AND reaction_type = 'dislike') as dislikes
        """,
        reaction.post_id
    )
    
    result = dict(row) if row else {}
    result["stats"] = dict(stats) if stats else {"likes": 0, "dislikes": 0}
    return result


@router.post("/posts/{post_id}/reactions")
@social_router.post("/posts/{post_id}/reactions")
async def add_reaction_for_post(
    post_id: int,
    payload: ReactionPayload,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    if payload.reaction not in {"like", "dislike"}:
        raise HTTPException(status_code=400, detail="Invalid reaction type")
    return await _add_or_update_reaction(
        post_id=post_id,
        reaction_type=payload.reaction,
        request=request,
        db=db,
    )


@router.delete("/posts/{post_id}/reactions")
@social_router.delete("/posts/{post_id}/reactions")
async def remove_reaction_for_post(
    post_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    return await remove_reaction(post_id=post_id, request=request, db=db)


@router.delete("/reactions/{post_id}")
async def remove_reaction(
    post_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Remove user's reaction from a post
    """
    user = get_current_user(request)
    user_id = user["id"]

    result = await db.execute(
        "DELETE FROM post_reactions WHERE user_id = $1 AND post_id = $2",
        user_id,
        post_id
    )

    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Reaction not found")

    stats = await db.fetchrow(
        """
        SELECT
            (SELECT COUNT(*) FROM post_reactions WHERE post_id = $1 AND reaction_type = 'like') as likes,
            (SELECT COUNT(*) FROM post_reactions WHERE post_id = $1 AND reaction_type = 'dislike') as dislikes
        """,
        post_id
    )

    return {"message": "Reaction removed", "stats": dict(stats)}


@router.get("/reactions/{post_id}")
async def get_post_reactions(
    post_id: int,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get all reactions for a post
    """
    likes = await db.fetch(
        """
        SELECT pr.*, up.username, up.avatar_url
        FROM post_reactions pr
        JOIN user_profiles up ON pr.user_id = up.user_id
        WHERE pr.post_id = $1 AND pr.reaction_type = 'like'
        ORDER BY pr.created_at DESC
        """,
        post_id
    )

    dislikes = await db.fetch(
        """
        SELECT pr.*, up.username, up.avatar_url
        FROM post_reactions pr
        JOIN user_profiles up ON pr.user_id = up.user_id
        WHERE pr.post_id = $1 AND pr.reaction_type = 'dislike'
        ORDER BY pr.created_at DESC
        """,
        post_id
    )

    return {
        "likes": [dict(row) for row in likes],
        "dislikes": [dict(row) for row in dislikes],
        "likes_count": len(likes),
        "dislikes_count": len(dislikes)
    }


@social_posts_router.post("/posts/{post_id}/reactions")
async def add_reaction_for_social_post(
    post_id: int,
    payload: ReactionPayload,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    return await add_reaction_for_post(
        post_id=post_id,
        payload=payload,
        request=request,
        db=db,
    )


@social_posts_router.delete("/posts/{post_id}/reactions")
async def remove_reaction_for_social_post(
    post_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    return await remove_reaction_for_post(
        post_id=post_id,
        request=request,
        db=db,
    )


# ============================================================================
# POST SHARES / VIEWS
# ============================================================================

@router.post("/posts/{post_id}/share")
@social_router.post("/posts/{post_id}/share")
@social_posts_router.post("/posts/{post_id}/share")
async def record_share(
    post_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    get_current_user(request)
    if not await column_exists(db, "social_posts", "shares_count"):
        return {"post_id": post_id, "shares_count": 0, "supported": False}
    post = await db.fetchval(
        "SELECT id FROM social_posts WHERE id = $1",
        post_id
    )
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    row = await db.fetchrow(
        """
        UPDATE social_posts
        SET shares_count = shares_count + 1
        WHERE id = $1
        RETURNING shares_count
        """,
        post_id
    )
    return {"post_id": post_id, "shares_count": row["shares_count"]}


@router.post("/posts/{post_id}/shares")
@social_router.post("/posts/{post_id}/shares")
@social_posts_router.post("/posts/{post_id}/shares")
async def record_share_alias(
    post_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    return await record_share(post_id=post_id, request=request, db=db)


@router.post("/posts/{post_id}/view")
@social_router.post("/posts/{post_id}/view")
@social_posts_router.post("/posts/{post_id}/view")
async def record_view(
    post_id: int,
    db: asyncpg.Connection = Depends(get_db),
):
    if not await column_exists(db, "social_posts", "views_count"):
        return {"post_id": post_id, "views_count": 0, "supported": False}
    post = await db.fetchval(
        "SELECT id FROM social_posts WHERE id = $1",
        post_id
    )
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    row = await db.fetchrow(
        """
        UPDATE social_posts
        SET views_count = views_count + 1
        WHERE id = $1
        RETURNING views_count
        """,
        post_id
    )
    return {"post_id": post_id, "views_count": row["views_count"]}


@router.post("/posts/{post_id}/views")
@social_router.post("/posts/{post_id}/views")
@social_posts_router.post("/posts/{post_id}/views")
async def record_view_alias(
    post_id: int,
    db: asyncpg.Connection = Depends(get_db),
):
    return await record_view(post_id=post_id, db=db)


# ============================================================================
# COMMENTS
# ============================================================================

@router.post("/comments", response_model=CommentWithAuthor)
async def add_comment(
    comment: CommentCreate,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Add a comment to a post
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Check if post exists
    post = await db.fetchval(
        "SELECT id FROM social_posts WHERE id = $1",
        comment.post_id
    )

    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    # Check if parent comment exists (if replying)
    if comment.parent_comment_id:
        parent = await db.fetchval(
            "SELECT id FROM post_comments WHERE id = $1",
            comment.parent_comment_id
        )

        if not parent:
            raise HTTPException(status_code=404, detail="Parent comment not found")

    # Create comment
    row = await db.fetchrow(
        """
        INSERT INTO post_comments (
            post_id, user_id, parent_comment_id, content
        )
        VALUES ($1, $2, $3, $4)
        RETURNING *
        """,
        comment.post_id,
        user_id,
        comment.parent_comment_id,
        comment.content
    )

    # Get user info
    user_info = await db.fetchrow(
        """
        SELECT up.username, up.avatar_url, COALESCE(tp.verified, FALSE) as verified
        FROM user_profiles up
        LEFT JOIN trader_profiles tp ON up.user_id = tp.user_id
        WHERE up.user_id = $1
        """,
        user_id
    )

    # Create notification for post author
    post_author = await db.fetchval(
        "SELECT user_id FROM social_posts WHERE id = $1",
        comment.post_id
    )

    if post_author != user_id:  # Don't notify yourself
        await db.execute(
            """
            INSERT INTO notifications (
                user_id, notification_type, title, message,
                related_user_id, related_post_id, related_comment_id
            )
            VALUES ($1, 'post_comment', 'New Comment', $2, $3, $4, $5)
            """,
            post_author,
            f"{user_info['username']} commented on your post",
            user_id,
            comment.post_id,
            row["id"]
        )

    # Combine comment data with user info
    comment_dict = dict(row)
    comment_dict.update(dict(user_info))
    comment_dict["user_has_liked"] = False
    comment_dict["is_author"] = True

    return _format_comment_with_author(comment_dict)


@router.post("/posts/{post_id}/comments")
@social_router.post("/posts/{post_id}/comments")
async def add_comment_for_post(
    post_id: int,
    payload: CommentPayload,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    content = payload.content or payload.comment or payload.text
    if not content:
        raise HTTPException(status_code=400, detail="Comment content is required")
    comment = CommentCreate(
        post_id=post_id,
        content=content,
        parent_comment_id=payload.parent_comment_id,
    )
    return await add_comment(comment=comment, request=request, db=db)


@social_posts_router.post("/posts/{post_id}/comments")
async def add_comment_for_social_post(
    post_id: int,
    payload: CommentPayload,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    return await add_comment_for_post(
        post_id=post_id,
        payload=payload,
        request=request,
        db=db,
    )


@router.post("/posts/{post_id}/comment")
@social_router.post("/posts/{post_id}/comment")
@social_posts_router.post("/posts/{post_id}/comment")
async def add_comment_for_post_alias(
    post_id: int,
    payload: CommentPayload,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    return await add_comment_for_post(
        post_id=post_id,
        payload=payload,
        request=request,
        db=db,
    )


@router.get("/comments/{post_id}", response_model=List[CommentWithAuthor])
async def get_comments(
    post_id: int,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get comments for a post
    """
    try:
        user = get_current_user(request)
        user_id = user["id"]
        is_authenticated = True
    except HTTPException:
        user_id = None
        is_authenticated = False

    # Get comments with user info
    query = """
        SELECT
            pc.*,
            up.username,
            up.avatar_url,
            COALESCE(tp.verified, FALSE) as verified
        FROM post_comments pc
        JOIN user_profiles up ON pc.user_id = up.user_id
        LEFT JOIN trader_profiles tp ON pc.user_id = tp.user_id
        WHERE pc.post_id = $1 AND pc.deleted_at IS NULL
        ORDER BY pc.created_at DESC
        LIMIT $2 OFFSET $3
    """

    rows = await db.fetch(query, post_id, limit, offset)

    comments = []
    for row in rows:
        comment_dict = dict(row)

        # Check if current user has liked this comment
        if is_authenticated:
            has_liked = await db.fetchval(
                "SELECT EXISTS(SELECT 1 FROM comment_likes WHERE user_id = $1 AND comment_id = $2)",
                user_id,
                comment_dict["id"]
            )
            comment_dict["user_has_liked"] = has_liked
            comment_dict["is_author"] = user_id == comment_dict["user_id"]
        else:
            comment_dict["user_has_liked"] = False
            comment_dict["is_author"] = False

        comments.append(_format_comment_with_author(comment_dict))

    return comments


@router.get("/posts/{post_id}/comments", response_model=List[CommentWithAuthor])
@social_router.get("/posts/{post_id}/comments", response_model=List[CommentWithAuthor])
async def get_comments_for_post(
    post_id: int,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: asyncpg.Connection = Depends(get_db),
):
    return await get_comments(
        post_id=post_id,
        request=request,
        offset=offset,
        limit=limit,
        db=db,
    )


@social_posts_router.get("/posts/{post_id}/comments", response_model=List[CommentWithAuthor])
async def get_comments_for_social_post(
    post_id: int,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: asyncpg.Connection = Depends(get_db),
):
    return await get_comments_for_post(
        post_id=post_id,
        request=request,
        offset=offset,
        limit=limit,
        db=db,
    )


@router.patch("/comments/{comment_id}", response_model=Comment)
async def update_comment(
    comment_id: int,
    comment_update: CommentUpdate,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Update a comment (author only)
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Check ownership
    comment_owner = await db.fetchval(
        "SELECT user_id FROM post_comments WHERE id = $1",
        comment_id
    )

    if not comment_owner:
        raise HTTPException(status_code=404, detail="Comment not found")

    if comment_owner != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to edit this comment")

    # Update comment
    row = await db.fetchrow(
        """
        UPDATE post_comments
        SET content = $1, updated_at = NOW()
        WHERE id = $2
        RETURNING *
        """,
        comment_update.content,
        comment_id
    )

    return dict(row)


@router.post("/comments/{comment_id}")
@social_router.post("/comments/{comment_id}")
async def update_comment_alias(
    comment_id: int,
    comment_update: CommentUpdate,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    return await update_comment(
        comment_id=comment_id,
        comment_update=comment_update,
        request=request,
        db=db,
    )


@router.delete("/comments/{comment_id}")
@social_router.delete("/comments/{comment_id}")
async def delete_comment(
    comment_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Delete a comment (author only) - soft delete
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Check ownership
    comment_owner = await db.fetchval(
        "SELECT user_id FROM post_comments WHERE id = $1",
        comment_id
    )

    if not comment_owner:
        raise HTTPException(status_code=404, detail="Comment not found")

    if comment_owner != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this comment")

    # Soft delete
    await db.execute(
        "UPDATE post_comments SET deleted_at = NOW() WHERE id = $1",
        comment_id
    )

    return {"message": "Comment deleted"}


# ============================================================================
# COMMENT LIKES
# ============================================================================

@router.post("/comments/like")
async def like_comment(
    like: CommentLikeCreate,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Like a comment
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Check if comment exists
    comment = await db.fetchval(
        "SELECT id FROM post_comments WHERE id = $1 AND deleted_at IS NULL",
        like.comment_id
    )

    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")

    # Check if already liked
    existing = await db.fetchval(
        "SELECT id FROM comment_likes WHERE user_id = $1 AND comment_id = $2",
        user_id,
        like.comment_id
    )

    if existing:
        raise HTTPException(status_code=400, detail="Already liked this comment")

    # Create like
    await db.execute(
        "INSERT INTO comment_likes (user_id, comment_id) VALUES ($1, $2)",
        user_id,
        like.comment_id
    )

    return {"message": "Comment liked"}


@router.post("/comments/{comment_id}/like")
@social_router.post("/comments/{comment_id}/like")
async def like_comment_by_id(
    comment_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    user = get_current_user(request)
    user_id = user["id"]
    
    comment = await db.fetchval(
        "SELECT id FROM post_comments WHERE id = $1 AND deleted_at IS NULL",
        comment_id
    )
    
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    
    existing = await db.fetchval(
        "SELECT id FROM comment_likes WHERE user_id = $1 AND comment_id = $2",
        user_id,
        comment_id
    )
    
    if existing:
        await db.execute(
            "DELETE FROM comment_likes WHERE user_id = $1 AND comment_id = $2",
            user_id,
            comment_id
        )
        likes_count = await db.fetchval(
            "SELECT COUNT(*) FROM comment_likes WHERE comment_id = $1",
            comment_id
        )
        return {"removed": True, "likes_count": likes_count}
    else:
        await db.execute(
            "INSERT INTO comment_likes (user_id, comment_id) VALUES ($1, $2)",
            user_id,
            comment_id
        )
        likes_count = await db.fetchval(
            "SELECT COUNT(*) FROM comment_likes WHERE comment_id = $1",
            comment_id
        )
        return {"removed": False, "likes_count": likes_count}


@router.delete("/comments/like/{comment_id}")
async def unlike_comment(
    comment_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Remove like from a comment
    """
    user = get_current_user(request)
    user_id = user["id"]

    result = await db.execute(
        "DELETE FROM comment_likes WHERE user_id = $1 AND comment_id = $2",
        user_id,
        comment_id
    )

    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Like not found")

    return {"message": "Like removed"}

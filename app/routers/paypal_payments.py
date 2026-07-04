"""
PayPal Payments Router
Handles PayPal subscriptions and one-off purchases
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
import asyncpg
from typing import Optional

from app.db import get_db
from app.payments.paypal import paypal_client
import os

router = APIRouter(prefix="/api/paypal", tags=["PayPal Payments"])


def get_current_user(request: Request):
    """Extract user from session"""
    if "user" not in request.session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return request.session["user"]


class SubscriptionRequest(BaseModel):
    trader_id: str


class ContentPurchaseRequest(BaseModel):
    post_id: int


# ============================================================================
# PAYPAL SUBSCRIPTIONS
# ============================================================================

@router.post("/subscribe")
async def create_paypal_subscription(
    request: Request,
    subscription_request: SubscriptionRequest,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Create PayPal subscription for a trader
    """
    user = get_current_user(request)
    user_id = user["id"]
    trader_id = subscription_request.trader_id

    if user_id == trader_id:
        raise HTTPException(status_code=400, detail="Cannot subscribe to yourself")

    # Get trader details, subscription price, and PayPal account
    trader = await db.fetchrow(
        """
        SELECT tp.subscription_price, tp.paypal_email, tp.paypal_merchant_status, up.username
        FROM trader_profiles tp
        JOIN user_profiles up ON tp.user_id = up.user_id
        WHERE tp.user_id = $1
        """,
        trader_id
    )

    if not trader:
        raise HTTPException(status_code=404, detail="Trader not found")

    if not trader["subscription_price"] or float(trader["subscription_price"]) <= 0:
        raise HTTPException(status_code=400, detail="Trader has not set a subscription price")

    # Check if trader has PayPal account set up
    if not trader["paypal_email"]:
        raise HTTPException(
            status_code=400,
            detail="This trader has not set up their PayPal account yet"
        )

    # Check if already subscribed
    existing = await db.fetchrow(
        """
        SELECT * FROM trader_subscriptions
        WHERE subscriber_id = $1 AND trader_id = $2 AND is_active = TRUE
        """,
        user_id,
        trader_id
    )

    if existing:
        raise HTTPException(status_code=400, detail="Already subscribed to this trader")

    try:
        # Create PayPal product (or use existing)
        product_id = f"futhub_trader_{trader_id}"

        # Create subscription plan
        plan_name = f"Subscription to {trader['username']}"
        # Note: In production, you'd want to cache plan IDs to avoid creating duplicates
        # For now, we'll create a new plan each time (you should implement caching)

        # Create subscription
        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
        custom_id = f"{user_id}:{trader_id}"

        subscription = await paypal_client.create_subscription(
            plan_id="",  # You need to create and cache plan IDs
            return_url=f"{frontend_url}/subscription/success?trader_id={trader_id}",
            cancel_url=f"{frontend_url}/subscription/cancel",
            custom_id=custom_id
        )

        return {
            "success": True,
            "approval_url": subscription["approval_url"],
            "subscription_id": subscription["subscription_id"],
            "message": "Redirect user to approval_url to complete subscription"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PayPal subscription creation failed: {str(e)}")


@router.post("/subscription/{subscription_id}/complete")
async def complete_paypal_subscription(
    subscription_id: str,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Complete PayPal subscription after user approval
    """
    user = get_current_user(request)
    user_id = user["id"]

    try:
        # Get subscription details from PayPal
        subscription = await paypal_client.get_subscription(subscription_id)

        # Parse custom_id to get trader_id
        custom_id = subscription.get("custom_id", "")
        parts = custom_id.split(":")
        if len(parts) != 2:
            raise HTTPException(status_code=400, detail="Invalid subscription data")

        sub_user_id, trader_id = parts

        if sub_user_id != user_id:
            raise HTTPException(status_code=403, detail="Subscription does not belong to you")

        # Get subscription price from trader profile
        price = await db.fetchval(
            "SELECT subscription_price FROM trader_profiles WHERE user_id = $1",
            trader_id
        )

        # Create subscription record
        await db.execute(
            """
            INSERT INTO trader_subscriptions (
                subscriber_id, trader_id, is_active, subscription_type,
                payment_provider, paypal_subscription_id, amount, currency
            )
            VALUES ($1, $2, TRUE, 'paid', 'paypal', $3, $4, 'GBP')
            ON CONFLICT (subscriber_id, trader_id)
            DO UPDATE SET
                is_active = TRUE,
                subscription_type = 'paid',
                payment_provider = 'paypal',
                paypal_subscription_id = $3,
                amount = $4,
                unsubscribed_at = NULL,
                subscribed_at = NOW()
            """,
            user_id, trader_id, subscription_id, price
        )

        # Create notification
        await db.execute(
            """
            INSERT INTO notifications (
                user_id, notification_type, title, message, related_user_id
            )
            VALUES ($1, 'subscription', 'New Subscriber', $2, $3)
            """,
            trader_id,
            "You have a new subscriber!",
            user_id
        )

        return {
            "success": True,
            "message": "Subscription activated successfully"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to complete subscription: {str(e)}")


@router.post("/subscription/{subscription_id}/cancel")
async def cancel_paypal_subscription(
    subscription_id: str,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Cancel PayPal subscription
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Verify subscription belongs to user
    subscription = await db.fetchrow(
        """
        SELECT * FROM trader_subscriptions
        WHERE subscriber_id = $1 AND paypal_subscription_id = $2 AND is_active = TRUE
        """,
        user_id,
        subscription_id
    )

    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")

    try:
        # Cancel in PayPal
        await paypal_client.cancel_subscription(subscription_id)

        # Update database
        await db.execute(
            """
            UPDATE trader_subscriptions
            SET is_active = FALSE, unsubscribed_at = NOW()
            WHERE id = $1
            """,
            subscription["id"]
        )

        return {
            "success": True,
            "message": "Subscription cancelled successfully"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to cancel subscription: {str(e)}")


# ============================================================================
# PAYPAL ONE-OFF PURCHASES
# ============================================================================

@router.post("/purchase")
async def create_paypal_purchase(
    request: Request,
    purchase_request: ContentPurchaseRequest,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Create PayPal order for one-off content purchase
    """
    user = get_current_user(request)
    user_id = user["id"]
    post_id = purchase_request.post_id

    # Get post details and author's PayPal account
    post = await db.fetchrow(
        """
        SELECT sp.*, tp.paypal_email, tp.paypal_merchant_status
        FROM social_posts sp
        LEFT JOIN trader_profiles tp ON sp.user_id = tp.user_id
        WHERE sp.id = $1
        """,
        post_id
    )

    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    if not post["requires_purchase"] or not post["price"]:
        raise HTTPException(status_code=400, detail="Post does not require purchase")

    # Check if author has PayPal account set up
    if not post.get("paypal_email"):
        raise HTTPException(
            status_code=400,
            detail="Content creator has not set up their PayPal account yet"
        )

    # Check if already purchased
    existing = await db.fetchval(
        "SELECT id FROM content_purchases WHERE user_id = $1 AND post_id = $2 AND status = 'completed'",
        user_id, post_id
    )
    if existing:
        raise HTTPException(status_code=400, detail="Content already purchased")

    try:
        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
        custom_id = f"{user_id}:{post_id}"

        # Create order with payee routing (10% platform fee)
        order = await paypal_client.create_order(
            amount=post["price"],
            currency="GBP",
            description=post["title"] or f"Content: {post['post_type']}",
            return_url=f"{frontend_url}/post/{post_id}?payment=success",
            cancel_url=f"{frontend_url}/post/{post_id}?payment=cancelled",
            custom_id=custom_id,
            payee_email=post["paypal_email"],
            platform_fee_percent=10.0
        )

        # Create pending purchase record
        await db.execute(
            """
            INSERT INTO content_purchases (
                user_id, post_id, amount, currency, payment_provider,
                paypal_order_id, status
            )
            VALUES ($1, $2, $3, 'GBP', 'paypal', $4, 'pending')
            ON CONFLICT (user_id, post_id) DO UPDATE
            SET paypal_order_id = $4, status = 'pending'
            """,
            user_id, post_id, post["price"], order["order_id"]
        )

        return {
            "success": True,
            "approval_url": order["approval_url"],
            "order_id": order["order_id"],
            "message": "Redirect user to approval_url to complete payment"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PayPal order creation failed: {str(e)}")


@router.post("/purchase/{order_id}/complete")
async def complete_paypal_purchase(
    order_id: str,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Complete PayPal purchase after user approval
    """
    user = get_current_user(request)
    user_id = user["id"]

    try:
        # Capture the payment
        capture = await paypal_client.capture_order(order_id)

        # Get capture ID
        capture_id = None
        if capture["status"] == "COMPLETED":
            purchase_units = capture.get("purchase_units", [])
            if purchase_units:
                captures = purchase_units[0].get("payments", {}).get("captures", [])
                if captures:
                    capture_id = captures[0]["id"]

        # Parse custom_id to get post_id
        custom_id = capture.get("purchase_units", [{}])[0].get("custom_id", "")
        parts = custom_id.split(":")
        if len(parts) != 2:
            raise HTTPException(status_code=400, detail="Invalid purchase data")

        purchase_user_id, post_id = parts

        if purchase_user_id != user_id:
            raise HTTPException(status_code=403, detail="Purchase does not belong to you")

        # Get post author for notification
        author_id = await db.fetchval(
            "SELECT user_id FROM social_posts WHERE id = $1",
            int(post_id)
        )

        # Update purchase record
        await db.execute(
            """
            UPDATE content_purchases
            SET status = 'completed',
                completed_at = NOW(),
                paypal_capture_id = $3
            WHERE user_id = $1 AND post_id = $2
            """,
            user_id, int(post_id), capture_id
        )

        # Create notifications
        if author_id:
            await db.execute(
                """
                INSERT INTO notifications (
                    user_id, notification_type, title, message,
                    related_user_id, related_post_id
                )
                VALUES ($1, 'content_purchase', 'Content Sold!', $2, $3, $4)
                """,
                author_id, "Someone purchased your content!", user_id, int(post_id)
            )

        await db.execute(
            """
            INSERT INTO notifications (
                user_id, notification_type, title, message, related_post_id
            )
            VALUES ($1, 'content_purchase', 'Purchase Complete', $2, $3)
            """,
            user_id, "Your content purchase is complete!", int(post_id)
        )

        return {
            "success": True,
            "message": "Purchase completed successfully"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to complete purchase: {str(e)}")


# ============================================================================
# PAYPAL WEBHOOKS
# ============================================================================

async def _verify_paypal_webhook_signature(request: Request, payload: dict) -> bool:
    """
    Verify a PayPal webhook using PayPal's verify-webhook-signature API.
    https://developer.paypal.com/api/rest/webhooks/rest/#link-verifywebhooksignature
    """
    webhook_id = os.getenv("PAYPAL_WEBHOOK_ID")
    if not webhook_id:
        # Refuse to trust unverifiable webhooks rather than silently accepting them
        return False

    headers = request.headers
    transmission_id = headers.get("paypal-transmission-id")
    transmission_time = headers.get("paypal-transmission-time")
    transmission_sig = headers.get("paypal-transmission-sig")
    cert_url = headers.get("paypal-cert-url")
    auth_algo = headers.get("paypal-auth-algo")

    if not all([transmission_id, transmission_time, transmission_sig, cert_url, auth_algo]):
        return False

    verification_request = {
        "auth_algo": auth_algo,
        "cert_url": cert_url,
        "transmission_id": transmission_id,
        "transmission_sig": transmission_sig,
        "transmission_time": transmission_time,
        "webhook_id": webhook_id,
        "webhook_event": payload,
    }

    try:
        result = await paypal_client._make_request(
            "POST",
            "/v1/notifications/verify-webhook-signature",
            verification_request,
        )
    except Exception as e:
        print(f"PayPal webhook signature verification failed: {e}")
        return False

    return result.get("verification_status") == "SUCCESS"


@router.post("/webhooks")
async def paypal_webhook(
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Handle PayPal webhook events
    """
    payload = await request.json()

    if not await _verify_paypal_webhook_signature(request, payload):
        raise HTTPException(status_code=401, detail="Webhook signature verification failed")

    event_type = payload.get("event_type")

    try:
        # Handle subscription events
        if event_type == "BILLING.SUBSCRIPTION.ACTIVATED":
            # Subscription activated
            resource = payload.get("resource", {})
            subscription_id = resource.get("id")
            custom_id = resource.get("custom_id", "")

            if ":" in custom_id:
                user_id, trader_id = custom_id.split(":")
                # Update subscription status if needed
                await db.execute(
                    """
                    UPDATE trader_subscriptions
                    SET is_active = TRUE
                    WHERE paypal_subscription_id = $1
                    """,
                    subscription_id
                )

        elif event_type == "BILLING.SUBSCRIPTION.CANCELLED":
            # Subscription cancelled
            resource = payload.get("resource", {})
            subscription_id = resource.get("id")

            await db.execute(
                """
                UPDATE trader_subscriptions
                SET is_active = FALSE, unsubscribed_at = NOW()
                WHERE paypal_subscription_id = $1
                """,
                subscription_id
            )

        elif event_type == "PAYMENT.CAPTURE.COMPLETED":
            # Payment captured (one-off purchase)
            # This is handled in the complete_paypal_purchase endpoint
            pass

        return {"received": True}

    except Exception as e:
        # Log error but return 200 to prevent webhook retries
        print(f"PayPal webhook error: {e}")
        return {"received": True, "error": str(e)}

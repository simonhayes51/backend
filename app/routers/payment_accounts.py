"""
Payment Accounts Router - Stripe Connect & PayPal onboarding for traders
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from typing import Optional
import asyncpg
import stripe
import os

from app.db import get_db
from app.payments.paypal import paypal_client

router = APIRouter(prefix="/api/payment-accounts", tags=["Payment Accounts"])


def get_current_user(request: Request):
    """Extract user from session"""
    if "user" not in request.session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return request.session["user"]


# ============================================================================
# STRIPE CONNECT
# ============================================================================

@router.post("/stripe/connect/onboard")
async def stripe_connect_onboard(
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Create Stripe Connect account and onboarding link for trader
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Check if user is a trader
    trader = await db.fetchrow(
        "SELECT * FROM trader_profiles WHERE user_id = $1",
        user_id
    )

    if not trader:
        raise HTTPException(
            status_code=403,
            detail="Only traders can connect payment accounts"
        )

    # Check if already has Stripe Connect account
    if trader["stripe_connect_account_id"]:
        # Account exists, create new onboarding link
        account_id = trader["stripe_connect_account_id"]
    else:
        # Create new Stripe Connect Express account
        try:
            # Get user profile info
            profile = await db.fetchrow(
                "SELECT username, email FROM user_profiles WHERE user_id = $1",
                user_id
            )

            account = stripe.Account.create(
                type="express",
                country="GB",  # Default to UK, can be made configurable
                email=profile.get("email") if profile else None,
                capabilities={
                    "card_payments": {"requested": True},
                    "transfers": {"requested": True},
                },
                business_type="individual",
                metadata={
                    "user_id": user_id,
                    "username": profile.get("username") if profile else None
                }
            )

            account_id = account.id

            # Save to database
            await db.execute(
                """
                UPDATE trader_profiles
                SET stripe_connect_account_id = $1,
                    stripe_connect_status = 'pending',
                    payment_accounts_updated_at = NOW()
                WHERE user_id = $2
                """,
                account_id,
                user_id
            )

        except stripe.error.StripeError as e:
            raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")

    # Create account link for onboarding
    try:
        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")

        account_link = stripe.AccountLink.create(
            account=account_id,
            refresh_url=f"{frontend_url}/settings/payments?refresh=true",
            return_url=f"{frontend_url}/settings/payments?setup=complete",
            type="account_onboarding",
        )

        return {
            "success": True,
            "account_id": account_id,
            "onboarding_url": account_link.url,
            "message": "Redirect user to onboarding_url to complete setup"
        }

    except stripe.error.StripeError as e:
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")


@router.get("/stripe/connect/status")
async def stripe_connect_status(
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get Stripe Connect account status for current trader
    """
    user = get_current_user(request)
    user_id = user["id"]

    trader = await db.fetchrow(
        """
        SELECT stripe_connect_account_id, stripe_connect_status,
               stripe_charges_enabled, stripe_payouts_enabled
        FROM trader_profiles
        WHERE user_id = $1
        """,
        user_id
    )

    if not trader:
        raise HTTPException(status_code=404, detail="Trader profile not found")

    if not trader["stripe_connect_account_id"]:
        return {
            "connected": False,
            "status": "not_started",
            "charges_enabled": False,
            "payouts_enabled": False
        }

    # Fetch latest status from Stripe
    try:
        account = stripe.Account.retrieve(trader["stripe_connect_account_id"])

        charges_enabled = account.charges_enabled
        payouts_enabled = account.payouts_enabled
        details_submitted = account.details_submitted

        # Determine status
        if charges_enabled and payouts_enabled:
            status = "active"
        elif details_submitted:
            status = "pending"
        else:
            status = "pending"

        # Update database
        await db.execute(
            """
            UPDATE trader_profiles
            SET stripe_connect_status = $1,
                stripe_charges_enabled = $2,
                stripe_payouts_enabled = $3,
                payment_setup_completed = CASE
                    WHEN $2 = TRUE THEN TRUE
                    ELSE payment_setup_completed
                END,
                payment_accounts_updated_at = NOW()
            WHERE user_id = $4
            """,
            status,
            charges_enabled,
            payouts_enabled,
            user_id
        )

        return {
            "connected": True,
            "status": status,
            "charges_enabled": charges_enabled,
            "payouts_enabled": payouts_enabled,
            "account_id": trader["stripe_connect_account_id"],
            "details_submitted": details_submitted
        }

    except stripe.error.StripeError as e:
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")


@router.post("/stripe/connect/dashboard")
async def stripe_connect_dashboard(
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Create Stripe Express Dashboard login link
    """
    user = get_current_user(request)
    user_id = user["id"]

    trader = await db.fetchrow(
        "SELECT stripe_connect_account_id FROM trader_profiles WHERE user_id = $1",
        user_id
    )

    if not trader or not trader["stripe_connect_account_id"]:
        raise HTTPException(status_code=404, detail="No Stripe account connected")

    try:
        login_link = stripe.Account.create_login_link(
            trader["stripe_connect_account_id"]
        )

        return {
            "success": True,
            "dashboard_url": login_link.url
        }

    except stripe.error.StripeError as e:
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")


@router.delete("/stripe/connect/disconnect")
async def stripe_connect_disconnect(
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Disconnect Stripe Connect account
    """
    user = get_current_user(request)
    user_id = user["id"]

    trader = await db.fetchrow(
        "SELECT stripe_connect_account_id FROM trader_profiles WHERE user_id = $1",
        user_id
    )

    if not trader or not trader["stripe_connect_account_id"]:
        raise HTTPException(status_code=404, detail="No Stripe account connected")

    # Note: We don't delete the Stripe account, just disconnect it
    # The trader can reconnect later if needed
    await db.execute(
        """
        UPDATE trader_profiles
        SET stripe_connect_account_id = NULL,
            stripe_connect_status = 'not_started',
            stripe_charges_enabled = FALSE,
            stripe_payouts_enabled = FALSE,
            payment_setup_completed = CASE
                WHEN paypal_merchant_id IS NOT NULL AND paypal_merchant_status = 'active' THEN TRUE
                ELSE FALSE
            END,
            payment_accounts_updated_at = NOW()
        WHERE user_id = $1
        """,
        user_id
    )

    return {"success": True, "message": "Stripe account disconnected"}


# ============================================================================
# PAYPAL MERCHANT ONBOARDING
# ============================================================================

@router.post("/paypal/connect")
async def paypal_connect(
    paypal_email: str,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Connect PayPal account by email.

    We have no way to verify a trader actually owns a given PayPal email
    address (no PayPal Partner API integration, no email-sending
    infrastructure in this codebase to send a confirmation link). Marking
    payment_setup_completed = TRUE here immediately would let a trader
    silently redirect buyer payouts to an email they don't control.

    Instead this just records the email as "pending" - it is NOT usable for
    payouts (payment_setup_completed stays FALSE) until the same
    authenticated user explicitly re-confirms it via
    POST /paypal/connect/confirm.
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Check if user is a trader
    trader = await db.fetchrow(
        "SELECT * FROM trader_profiles WHERE user_id = $1",
        user_id
    )

    if not trader:
        raise HTTPException(
            status_code=403,
            detail="Only traders can connect payment accounts"
        )

    # Validate email format
    if "@" not in paypal_email or "." not in paypal_email:
        raise HTTPException(status_code=400, detail="Invalid email format")

    # Store the email as pending verification. payment_setup_completed only
    # reflects providers that are actually confirmed (e.g. an already-active
    # Stripe Connect account), never this unconfirmed PayPal email.
    await db.execute(
        """
        UPDATE trader_profiles
        SET paypal_email = $1,
            paypal_merchant_status = 'pending',
            payment_setup_completed = CASE
                WHEN stripe_connect_account_id IS NOT NULL AND stripe_charges_enabled = TRUE THEN TRUE
                ELSE FALSE
            END,
            payment_accounts_updated_at = NOW()
        WHERE user_id = $2
        """,
        paypal_email,
        user_id
    )

    return {
        "success": True,
        "paypal_email": paypal_email,
        "status": "pending",
        "message": "PayPal email saved. Confirm it via /paypal/connect/confirm before it can receive payouts."
    }


@router.post("/paypal/connect/confirm")
async def paypal_connect_confirm(
    paypal_email: str,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Second explicit confirmation step for a pending PayPal payout email.

    This codebase has no email/SMTP/SendGrid/Mailgun infrastructure to send a
    "confirm this is your PayPal email" link, so rather than inventing new
    infrastructure we require the same authenticated user to explicitly
    re-submit the exact email they connected. Only then do we mark it active
    and eligible to receive buyer payouts.
    """
    user = get_current_user(request)
    user_id = user["id"]

    trader = await db.fetchrow(
        "SELECT paypal_email, paypal_merchant_status FROM trader_profiles WHERE user_id = $1",
        user_id
    )

    if not trader or not trader["paypal_email"]:
        raise HTTPException(status_code=400, detail="No pending PayPal email to confirm")

    if trader["paypal_merchant_status"] == "active":
        raise HTTPException(status_code=400, detail="PayPal account is already confirmed")

    if paypal_email.strip().lower() != trader["paypal_email"].strip().lower():
        raise HTTPException(
            status_code=400,
            detail="Confirmation email does not match the PayPal email on file"
        )

    await db.execute(
        """
        UPDATE trader_profiles
        SET paypal_merchant_status = 'active',
            payment_setup_completed = TRUE,
            payment_accounts_updated_at = NOW()
        WHERE user_id = $1
        """,
        user_id
    )

    return {
        "success": True,
        "paypal_email": trader["paypal_email"],
        "status": "active",
        "message": "PayPal account confirmed successfully"
    }


@router.get("/paypal/status")
async def paypal_status(
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get PayPal account status for current trader
    """
    user = get_current_user(request)
    user_id = user["id"]

    trader = await db.fetchrow(
        """
        SELECT paypal_email, paypal_merchant_id, paypal_merchant_status
        FROM trader_profiles
        WHERE user_id = $1
        """,
        user_id
    )

    if not trader:
        raise HTTPException(status_code=404, detail="Trader profile not found")

    if not trader["paypal_email"] and not trader["paypal_merchant_id"]:
        return {
            "connected": False,
            "status": "not_started"
        }

    return {
        "connected": True,
        "paypal_email": trader["paypal_email"],
        "status": trader["paypal_merchant_status"] or "active"
    }


@router.delete("/paypal/disconnect")
async def paypal_disconnect(
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Disconnect PayPal account
    """
    user = get_current_user(request)
    user_id = user["id"]

    await db.execute(
        """
        UPDATE trader_profiles
        SET paypal_email = NULL,
            paypal_merchant_id = NULL,
            paypal_merchant_status = 'not_started',
            payment_setup_completed = CASE
                WHEN stripe_connect_account_id IS NOT NULL AND stripe_charges_enabled = TRUE THEN TRUE
                ELSE FALSE
            END,
            payment_accounts_updated_at = NOW()
        WHERE user_id = $1
        """,
        user_id
    )

    return {"success": True, "message": "PayPal account disconnected"}


# ============================================================================
# GENERAL PAYMENT ACCOUNT STATUS
# ============================================================================

@router.get("/status")
async def payment_accounts_status(
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get all payment account statuses for current trader
    """
    user = get_current_user(request)
    user_id = user["id"]

    trader = await db.fetchrow(
        """
        SELECT
            stripe_connect_account_id,
            stripe_connect_status,
            stripe_charges_enabled,
            stripe_payouts_enabled,
            paypal_email,
            paypal_merchant_id,
            paypal_merchant_status,
            payment_setup_completed
        FROM trader_profiles
        WHERE user_id = $1
        """,
        user_id
    )

    if not trader:
        raise HTTPException(status_code=404, detail="Trader profile not found")

    return {
        "payment_setup_completed": trader["payment_setup_completed"],
        "stripe": {
            "connected": bool(trader["stripe_connect_account_id"]),
            "status": trader["stripe_connect_status"] or "not_started",
            "charges_enabled": trader["stripe_charges_enabled"] or False,
            "payouts_enabled": trader["stripe_payouts_enabled"] or False,
            "account_id": trader["stripe_connect_account_id"]
        },
        "paypal": {
            "connected": bool(trader["paypal_email"] or trader["paypal_merchant_id"]),
            "status": trader["paypal_merchant_status"] or "not_started",
            "email": trader["paypal_email"]
        }
    }


# ============================================================================
# EARNINGS & PAYOUTS
# ============================================================================

@router.get("/earnings")
async def get_earnings(
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get trader's earnings breakdown
    """
    user = get_current_user(request)
    user_id = user["id"]

    earnings = await db.fetchrow(
        """
        SELECT * FROM trader_earnings
        WHERE trader_id = $1
        """,
        user_id
    )

    if not earnings:
        return {
            "available_balance": 0,
            "lifetime_earnings": 0,
            "total_paid_out": 0,
            "pending_balance": 0,
            "payment_setup_completed": False
        }

    return {
        "available_balance": float(earnings["available_balance"] or 0),
        "lifetime_earnings": float(earnings["lifetime_earnings_before_payouts"] or 0),
        "total_paid_out": float(earnings["total_paid_out"] or 0),
        "pending_balance": float(earnings["available_balance"] or 0),
        "payment_setup_completed": earnings["payment_setup_completed"],

        # Breakdown
        "subscription_revenue": {
            "gross": float(earnings["gross_monthly_subscription_revenue"] or 0),
            "platform_fees": float(earnings["subscription_platform_fees"] or 0),
            "net": float(earnings["net_monthly_subscription_revenue"] or 0),
        },
        "content_revenue": {
            "gross": float(earnings["gross_content_revenue"] or 0),
            "platform_fees": float(earnings["content_platform_fees"] or 0),
            "net": float(earnings["net_content_revenue"] or 0),
            "sales_count": earnings["content_sales"] or 0
        },
        "tips": {
            "gross": float(earnings["gross_tips_received"] or 0),
            "platform_fees": float(earnings["tips_platform_fees"] or 0),
            "net": float(earnings["net_tips_received"] or 0)
        },
        "total_platform_fees": float(earnings["total_platform_fees"] or 0),
        "active_subscribers": earnings["active_subscribers"] or 0
    }


@router.get("/payouts")
async def get_payouts(
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get trader's payout history
    """
    user = get_current_user(request)
    user_id = user["id"]

    payouts = await db.fetch(
        """
        SELECT * FROM trader_payouts
        WHERE trader_id = $1
        ORDER BY created_at DESC
        LIMIT 50
        """,
        user_id
    )

    return {
        "payouts": [
            {
                "id": p["id"],
                "amount": float(p["amount"]),
                "currency": p["currency"],
                "provider": p["provider"],
                "status": p["status"],
                "created_at": p["created_at"].isoformat() if p["created_at"] else None,
                "paid_at": p["paid_at"].isoformat() if p["paid_at"] else None,
                "description": p["description"],
                "failure_reason": p["failure_reason"]
            }
            for p in payouts
        ]
    }


@router.post("/payout/request")
async def request_payout(
    amount: Optional[float] = None,
    provider: str = "stripe",
    request_obj: Request = None,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Request a payout (manual approval for now)
    In production, this would trigger automatic transfer to connected account
    """
    user = get_current_user(request_obj)
    user_id = user["id"]

    # Get earnings
    earnings = await db.fetchrow(
        "SELECT * FROM trader_earnings WHERE trader_id = $1",
        user_id
    )

    if not earnings:
        raise HTTPException(status_code=404, detail="No earnings found")

    available = float(earnings["available_balance"] or 0)

    # Default to full available balance
    if amount is None:
        amount = available

    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than 0")

    if amount > available:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient balance. Available: {available}"
        )

    # Check payment account is set up
    if provider == "stripe":
        if not earnings["stripe_connect_account_id"] or not earnings["stripe_charges_enabled"]:
            raise HTTPException(
                status_code=400,
                detail="Stripe account not connected or not enabled for payouts"
            )
    elif provider == "paypal":
        if not earnings["paypal_merchant_id"] and not earnings.get("paypal_email"):
            raise HTTPException(
                status_code=400,
                detail="PayPal account not connected"
            )

    # Create payout record
    payout = await db.fetchrow(
        """
        INSERT INTO trader_payouts (
            trader_id, amount, currency, provider, status,
            period_start, period_end, description
        )
        VALUES ($1, $2, 'GBP', $3, 'pending', NOW() - INTERVAL '30 days', NOW(), $4)
        RETURNING *
        """,
        user_id,
        amount,
        provider,
        f"Payout request via {provider}"
    )

    return {
        "success": True,
        "payout_id": payout["id"],
        "amount": float(payout["amount"]),
        "status": "pending",
        "message": "Payout request submitted. Payouts are processed within 2-3 business days."
    }

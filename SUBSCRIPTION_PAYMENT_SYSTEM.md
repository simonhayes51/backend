# Subscription & Payment System Documentation

## Overview

This document provides comprehensive instructions for implementing the new subscription and payment system that supports:

1. **Single-tier subscriptions** - Traders set their own monthly subscription price
2. **One-off content purchases** - Users can buy individual pieces of content (guides, signals, etc.)
3. **Premium content access control** - Subscriber-only content vs paid content
4. **Dual payment processor support** - Both Stripe and PayPal

---

## Table of Contents

1. [Database Changes](#database-changes)
2. [API Endpoints](#api-endpoints)
3. [Frontend Integration](#frontend-integration)
4. [Payment Flows](#payment-flows)
5. [Environment Variables](#environment-variables)
6. [Database Setup (pgAdmin)](#database-setup-pgadmin)

---

## Database Changes

### Migration Required

Run the migration file: `migrations/005_single_tier_subscriptions.sql`

**Key changes:**
- Added `price` and `requires_purchase` columns to `social_posts`
- Created `content_purchases` table for one-off purchases
- Added PayPal support columns to `trader_subscriptions` and `payments`
- Created `user_can_access_content()` function for access control
- Created `trader_earnings` view for analytics

### New Tables

#### `content_purchases`
```sql
- id: BIGSERIAL PRIMARY KEY
- user_id: BIGINT (buyer)
- post_id: BIGINT (content)
- amount: DECIMAL(10,2)
- currency: VARCHAR(3)
- payment_provider: VARCHAR(50) (stripe/paypal)
- stripe_payment_intent_id: VARCHAR(255)
- paypal_order_id: VARCHAR(255)
- paypal_capture_id: VARCHAR(255)
- status: VARCHAR(50) (pending/completed/failed/refunded)
- purchased_at: TIMESTAMP
- completed_at: TIMESTAMP
- refunded_at: TIMESTAMP
```

### Updated Tables

#### `trader_profiles`
- `subscription_price` - Single monthly subscription price (NEW PRIMARY FIELD)
- `tier_basic_price`, `tier_premium_price`, `tier_elite_price` - Deprecated but kept for backward compatibility

#### `trader_subscriptions`
- `subscription_type` - Now supports: 'free', 'paid' (new), 'basic', 'premium', 'elite' (deprecated)
- `payment_provider` - 'stripe' or 'paypal'
- `paypal_subscription_id` - PayPal recurring subscription ID

#### `social_posts`
- `is_premium` - TRUE for subscriber-only content (existing)
- `requires_purchase` - TRUE for one-off purchase content (NEW)
- `price` - Price for one-off purchase (NEW)

---

## API Endpoints

### Subscription Management

#### Get Trader Subscription Price
```
GET /api/subscriptions/{trader_id}/subscription-price
```

Response:
```json
{
  "trader_id": "123",
  "username": "ProTrader",
  "subscription_price": 9.99,
  "currency": "GBP",
  "has_paid_subscription": true
}
```

#### Subscribe to Trader (Free)
```
POST /api/subscriptions/subscribe
Body: { "trader_id": "123" }
```

#### Subscribe to Trader (Paid - Initiate Payment)
```
POST /api/subscriptions/subscribe/{trader_id}/paid
```

Response:
```json
{
  "success": true,
  "trader_id": "123",
  "price": 9.99,
  "currency": "GBP",
  "message": "Use /api/billing/create-checkout-session with traderId to complete payment"
}
```

### Stripe Payments

#### Create Checkout Session (Subscription or Content Purchase)
```
POST /api/billing/create-checkout-session
```

**For Subscription:**
```json
{
  "traderId": "123",
  "successUrl": "https://yoursite.com/success",
  "cancelUrl": "https://yoursite.com/cancel"
}
```

**For One-Off Content:**
```json
{
  "postId": 456,
  "successUrl": "https://yoursite.com/post/456?purchase=success",
  "cancelUrl": "https://yoursite.com/post/456?purchase=cancelled"
}
```

Response:
```json
{
  "sessionId": "cs_test_...",
  "checkoutUrl": "https://checkout.stripe.com/..."
}
```

### PayPal Payments

#### Create PayPal Subscription
```
POST /api/paypal/subscribe
Body: { "trader_id": "123" }
```

Response:
```json
{
  "success": true,
  "approval_url": "https://www.paypal.com/...",
  "subscription_id": "I-...",
  "message": "Redirect user to approval_url to complete subscription"
}
```

#### Complete PayPal Subscription (After User Approval)
```
POST /api/paypal/subscription/{subscription_id}/complete
```

#### Cancel PayPal Subscription
```
POST /api/paypal/subscription/{subscription_id}/cancel
```

#### Create PayPal Content Purchase
```
POST /api/paypal/purchase
Body: { "post_id": 456 }
```

Response:
```json
{
  "success": true,
  "approval_url": "https://www.paypal.com/...",
  "order_id": "ORDER-...",
  "message": "Redirect user to approval_url to complete payment"
}
```

#### Complete PayPal Purchase (After User Approval)
```
POST /api/paypal/purchase/{order_id}/complete
```

### Content Access Control

#### Check Content Access
```
POST /api/content-purchases/check-access/{post_id}
```

Response:
```json
{
  "has_access": false,
  "is_author": false,
  "access_type": "requires_purchase",
  "requires_subscription": false,
  "requires_purchase": true,
  "price": 4.99
}
```

**Possible `access_type` values:**
- `"free"` - Public content
- `"author"` - User is the author
- `"subscribed"` - User has active subscription
- `"purchased"` - User purchased this content
- `"requires_subscription"` - Needs subscription to access
- `"requires_purchase"` - Needs to purchase to access

#### Get My Purchases
```
GET /api/content-purchases/my-purchases
```

Response:
```json
{
  "purchases": [
    {
      "id": 1,
      "post_id": 456,
      "amount": 4.99,
      "title": "Advanced Trading Guide",
      "author_username": "ProTrader",
      "purchased_at": "2026-01-16T12:00:00Z"
    }
  ],
  "total": 1
}
```

### Social Posts (Updated)

#### Create Post with Pricing Options
```
POST /api/feed/posts
```

**Free Post:**
```json
{
  "post_type": "tip",
  "title": "Market Analysis",
  "content": "Here's my analysis...",
  "is_premium": false,
  "requires_purchase": false
}
```

**Subscriber-Only Post:**
```json
{
  "post_type": "analysis",
  "title": "Premium Market Insights",
  "content": "Exclusive analysis for subscribers...",
  "is_premium": true,
  "requires_purchase": false
}
```

**Paid One-Off Content:**
```json
{
  "post_type": "analysis",
  "title": "Complete Trading Guide",
  "content": "Comprehensive guide...",
  "is_premium": false,
  "requires_purchase": true,
  "price": 9.99
}
```

---

## Frontend Integration

### 1. Trader Profile Setup

Allow traders to set their subscription price:

```typescript
// Update trader profile
async function updateSubscriptionPrice(price: number) {
  await fetch('/api/traders/profile', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ subscription_price: price })
  });
}
```

### 2. Subscription Flow

```typescript
// Step 1: Check subscription price
const response = await fetch(`/api/subscriptions/${traderId}/subscription-price`);
const { subscription_price, has_paid_subscription } = await response.json();

if (!has_paid_subscription) {
  // Free follow
  await fetch('/api/subscriptions/subscribe', {
    method: 'POST',
    body: JSON.stringify({ trader_id: traderId })
  });
} else {
  // Step 2: Choose payment provider
  const provider = 'stripe'; // or 'paypal'

  if (provider === 'stripe') {
    // Stripe flow
    const checkoutResponse = await fetch('/api/billing/create-checkout-session', {
      method: 'POST',
      body: JSON.stringify({
        traderId: traderId,
        successUrl: window.location.origin + '/subscription/success',
        cancelUrl: window.location.origin + '/subscription/cancel'
      })
    });
    const { checkoutUrl } = await checkoutResponse.json();
    window.location.href = checkoutUrl;

  } else if (provider === 'paypal') {
    // PayPal flow
    const paypalResponse = await fetch('/api/paypal/subscribe', {
      method: 'POST',
      body: JSON.stringify({ trader_id: traderId })
    });
    const { approval_url, subscription_id } = await paypalResponse.json();

    // Save subscription_id to complete later
    sessionStorage.setItem('paypal_subscription_id', subscription_id);

    // Redirect to PayPal
    window.location.href = approval_url;
  }
}

// Step 3: Handle return from payment provider
// For PayPal, on success page:
const subscriptionId = sessionStorage.getItem('paypal_subscription_id');
await fetch(`/api/paypal/subscription/${subscriptionId}/complete`, {
  method: 'POST'
});
```

### 3. Content Purchase Flow

```typescript
// Step 1: Check if user has access
const accessResponse = await fetch(`/api/content-purchases/check-access/${postId}`, {
  method: 'POST'
});
const accessData = await accessResponse.json();

if (accessData.has_access) {
  // Show full content
  showContent();
} else if (accessData.requires_purchase) {
  // Show purchase button
  const price = accessData.price;
  showPurchaseButton(price);
}

// Step 2: Purchase content
async function purchaseContent(postId: number, provider: 'stripe' | 'paypal') {
  if (provider === 'stripe') {
    const response = await fetch('/api/billing/create-checkout-session', {
      method: 'POST',
      body: JSON.stringify({
        postId: postId,
        successUrl: `${window.location.origin}/post/${postId}?purchase=success`,
        cancelUrl: `${window.location.origin}/post/${postId}?purchase=cancelled`
      })
    });
    const { checkoutUrl } = await response.json();
    window.location.href = checkoutUrl;

  } else if (provider === 'paypal') {
    const response = await fetch('/api/paypal/purchase', {
      method: 'POST',
      body: JSON.stringify({ post_id: postId })
    });
    const { approval_url, order_id } = await response.json();

    // Save order_id to complete later
    sessionStorage.setItem('paypal_order_id', order_id);

    // Redirect to PayPal
    window.location.href = approval_url;
  }
}

// Step 3: Handle return from PayPal
// On success page with ?payment=success:
const orderId = sessionStorage.getItem('paypal_order_id');
await fetch(`/api/paypal/purchase/${orderId}/complete`, {
  method: 'POST'
});
```

### 4. Create Post with Pricing

```typescript
interface PostCreation {
  post_type: string;
  title: string;
  content: string;
  is_premium?: boolean;       // Subscriber-only
  requires_purchase?: boolean; // One-off purchase
  price?: number;             // Only if requires_purchase=true
}

// UI Component
function PostCreationForm() {
  const [contentType, setContentType] = useState<'free' | 'premium' | 'paid'>('free');
  const [price, setPrice] = useState<number>(0);

  return (
    <form>
      <select value={contentType} onChange={(e) => setContentType(e.target.value)}>
        <option value="free">Free (Public)</option>
        <option value="premium">Premium (Subscribers Only)</option>
        <option value="paid">Paid (One-Off Purchase)</option>
      </select>

      {contentType === 'paid' && (
        <input
          type="number"
          value={price}
          onChange={(e) => setPrice(parseFloat(e.target.value))}
          placeholder="Price (GBP)"
        />
      )}

      <button onClick={handleSubmit}>Create Post</button>
    </form>
  );

  async function handleSubmit() {
    const postData: PostCreation = {
      post_type: 'analysis',
      title: 'My Post',
      content: 'Content here...',
      is_premium: contentType === 'premium',
      requires_purchase: contentType === 'paid',
      price: contentType === 'paid' ? price : undefined
    };

    await fetch('/api/feed/posts', {
      method: 'POST',
      body: JSON.stringify(postData)
    });
  }
}
```

### 5. Display Content with Access Control

```typescript
function PostDisplay({ postId }: { postId: number }) {
  const [post, setPost] = useState<any>(null);
  const [access, setAccess] = useState<any>(null);

  useEffect(() => {
    // Check access
    fetch(`/api/content-purchases/check-access/${postId}`, {
      method: 'POST'
    })
      .then(res => res.json())
      .then(setAccess);

    // Get post
    fetch(`/api/feed/posts/${postId}`)
      .then(res => res.json())
      .then(setPost)
      .catch(err => {
        // Handle 403 for restricted content
        console.error('Access denied:', err);
      });
  }, [postId]);

  if (!access) return <div>Loading...</div>;

  if (access.has_access) {
    return <div>{post?.content}</div>;
  } else if (access.requires_purchase) {
    return (
      <div>
        <p>This content requires purchase</p>
        <p>Price: £{access.price}</p>
        <button onClick={() => purchaseContent(postId, 'stripe')}>
          Buy with Card
        </button>
        <button onClick={() => purchaseContent(postId, 'paypal')}>
          Buy with PayPal
        </button>
      </div>
    );
  } else if (access.requires_subscription) {
    return (
      <div>
        <p>This content is for subscribers only</p>
        <button onClick={() => subscribeTo(post.user_id)}>
          Subscribe
        </button>
      </div>
    );
  }

  return <div>Access denied</div>;
}
```

---

## Payment Flows

### Stripe Subscription Flow

```
1. User clicks "Subscribe" on trader profile
2. Frontend calls POST /api/billing/create-checkout-session with traderId
3. Backend creates Stripe checkout session
4. Frontend redirects to Stripe checkout URL
5. User completes payment on Stripe
6. Stripe redirects back to successUrl
7. Stripe sends webhook to /api/webhooks/stripe
8. Backend creates trader_subscriptions record
9. Backend sends notification to trader
```

### PayPal Subscription Flow

```
1. User clicks "Subscribe" on trader profile
2. Frontend calls POST /api/paypal/subscribe with trader_id
3. Backend creates PayPal subscription
4. Frontend saves subscription_id and redirects to PayPal approval_url
5. User approves on PayPal
6. PayPal redirects back to returnUrl
7. Frontend calls POST /api/paypal/subscription/{subscription_id}/complete
8. Backend creates trader_subscriptions record
9. Backend sends notification to trader
```

### Content Purchase Flow (Similar for Stripe/PayPal)

```
1. User views locked content
2. Frontend calls POST /api/content-purchases/check-access/{post_id}
3. Backend returns requires_purchase=true with price
4. User clicks "Purchase"
5. Frontend initiates Stripe or PayPal flow
6. User completes payment
7. Backend updates content_purchases to "completed"
8. Backend sends notifications to buyer and seller
9. User can now access content
```

---

## Environment Variables

Add these to your `.env` file:

```bash
# Stripe (existing)
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_MONTHLY_PRICE_ID=price_...  # For platform subscriptions
STRIPE_SEASON_PRICE_ID=price_...   # For platform subscriptions

# PayPal (new)
PAYPAL_CLIENT_ID=your_paypal_client_id
PAYPAL_CLIENT_SECRET=your_paypal_client_secret
PAYPAL_MODE=sandbox  # or 'live' for production

# URLs
FRONTEND_URL=http://localhost:3000
BILLING_SUCCESS_URL=http://localhost:3000/billing/success
BILLING_CANCEL_URL=http://localhost:3000/billing/cancel
```

### Getting PayPal Credentials

1. Go to https://developer.paypal.com/
2. Log in / Create account
3. Navigate to "My Apps & Credentials"
4. Create a new app
5. Copy Client ID and Secret
6. Set webhook URL: `https://yourdomain.com/api/paypal/webhooks`

### PayPal Webhook Events to Subscribe To

- `BILLING.SUBSCRIPTION.ACTIVATED`
- `BILLING.SUBSCRIPTION.CANCELLED`
- `PAYMENT.CAPTURE.COMPLETED`

---

## Database Setup (pgAdmin)

### 1. Run Migration

1. Open pgAdmin
2. Connect to your database
3. Right-click on your database → Query Tool
4. Open file: `migrations/005_single_tier_subscriptions.sql`
5. Execute (F5)

### 2. Verify Tables

Check that these tables exist:
```sql
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
AND table_name IN ('content_purchases', 'trader_subscriptions', 'social_posts');
```

### 3. Check Columns

Verify new columns on `social_posts`:
```sql
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'social_posts'
AND column_name IN ('price', 'requires_purchase');
```

### 4. Test Access Control Function

```sql
-- Test the access control function
SELECT user_can_access_content(
  123,  -- user_id
  456   -- post_id
);
```

### 5. View Trader Earnings

```sql
-- Check trader earnings view
SELECT * FROM trader_earnings
WHERE trader_id = '123';
```

### 6. Sample Queries

**Get all paid content:**
```sql
SELECT id, title, price, user_id
FROM social_posts
WHERE requires_purchase = TRUE
ORDER BY created_at DESC;
```

**Get all purchases for a post:**
```sql
SELECT
  cp.*,
  up.username as buyer_username
FROM content_purchases cp
JOIN user_profiles up ON cp.user_id = up.user_id
WHERE cp.post_id = 456
AND cp.status = 'completed';
```

**Get user's subscriptions:**
```sql
SELECT
  ts.*,
  up.username as trader_username,
  tp.subscription_price
FROM trader_subscriptions ts
JOIN user_profiles up ON ts.trader_id = up.user_id
JOIN trader_profiles tp ON ts.trader_id = tp.user_id
WHERE ts.subscriber_id = '123'
AND ts.is_active = TRUE;
```

**Get trader's monthly revenue:**
```sql
SELECT
  trader_id,
  username,
  active_subscribers,
  monthly_subscription_revenue,
  total_content_revenue,
  total_tips_received
FROM trader_earnings
WHERE trader_id = '123';
```

---

## Testing Checklist

### Backend Testing

- [ ] Run migration successfully
- [ ] Create trader profile with subscription_price
- [ ] Create post with requires_purchase=true and price
- [ ] Test Stripe subscription checkout
- [ ] Test Stripe content purchase checkout
- [ ] Test PayPal subscription flow
- [ ] Test PayPal content purchase flow
- [ ] Test access control for premium content
- [ ] Test access control for paid content
- [ ] Verify webhook handlers work

### Frontend Testing

- [ ] Display subscription price on trader profile
- [ ] Show "Subscribe" button with pricing
- [ ] Implement Stripe checkout redirect
- [ ] Implement PayPal checkout redirect
- [ ] Handle payment completion callbacks
- [ ] Show locked content preview
- [ ] Show purchase button for paid content
- [ ] Display price and payment options
- [ ] Unlock content after purchase
- [ ] Show "Subscribers Only" badge
- [ ] Show "Purchase Required" badge
- [ ] Test error handling for failed payments

---

## Troubleshooting

### Content Not Unlocking After Purchase

1. Check `content_purchases` table:
   ```sql
   SELECT * FROM content_purchases
   WHERE user_id = ? AND post_id = ?;
   ```
2. Verify `status = 'completed'`
3. Check webhook logs for payment completion

### Subscription Not Active

1. Check `trader_subscriptions` table:
   ```sql
   SELECT * FROM trader_subscriptions
   WHERE subscriber_id = ? AND trader_id = ?;
   ```
2. Verify `is_active = TRUE`
3. Check `payment_provider` matches what was used
4. Check Stripe/PayPal subscription status

### PayPal Webhooks Not Working

1. Verify webhook URL is publicly accessible
2. Check PayPal webhook event subscriptions
3. Review PayPal webhook logs in dashboard
4. Check backend logs for errors

---

## Security Considerations

1. **Always verify payment on backend** - Never trust client-side payment confirmations
2. **Use webhook secrets** - Verify Stripe/PayPal webhooks with signatures
3. **Check subscription status** - Use `user_can_access_content()` function
4. **Prevent duplicate purchases** - UNIQUE constraint on (user_id, post_id)
5. **Validate pricing** - Ensure price matches post.price in database

---

## Support

For issues or questions:
1. Check backend logs: `logs/app.log`
2. Check Stripe dashboard: https://dashboard.stripe.com/
3. Check PayPal dashboard: https://developer.paypal.com/dashboard/
4. Review webhook events and retries

---

## Future Enhancements

Potential features to consider:
- [ ] Annual subscription discounts
- [ ] Bundle pricing (multiple posts)
- [ ] Promo codes / discount coupons
- [ ] Gifting subscriptions
- [ ] Refund automation
- [ ] Revenue sharing / referral commissions
- [ ] Content preview (show first 100 words)
- [ ] Subscription tiers (basic/premium/elite) per trader
- [ ] Free trial periods
- [ ] Pay-per-view livestreams

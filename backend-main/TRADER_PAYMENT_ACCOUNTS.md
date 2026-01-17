# Trader Payment Accounts - Receive Payments Directly

## Overview

This guide explains how traders connect their own payment accounts (Stripe Connect & PayPal) to receive payments directly from subscribers and content purchasers. **Payments go directly to the trader's account, not the platform account**, with an automatic platform fee deducted.

---

## 🎯 Key Features

### Payment Routing
- ✅ **Direct deposits**: Subscribers and content buyers pay traders directly
- ✅ **Automatic platform fees**: 10% for subscriptions/content, 5% for tips
- ✅ **Multi-provider**: Traders can connect Stripe, PayPal, or both
- ✅ **Instant payouts**: Stripe Connect handles automatic payouts to trader's bank account
- ✅ **Transparent earnings**: Traders see gross revenue, platform fees, and net earnings

### Supported Payment Methods
1. **Stripe Connect** - Full bank account integration with instant payouts
2. **PayPal** - Simple email-based payment routing

---

## Database Changes

### Migration 006: Trader Payment Accounts

Run migration: `migrations/006_trader_payment_accounts.sql`

**New columns on `trader_profiles`:**
- `stripe_connect_account_id` - Stripe Connect account ID
- `stripe_connect_status` - Status: not_started, pending, active, rejected, disabled
- `stripe_charges_enabled` - Can receive payments
- `stripe_payouts_enabled` - Can receive payouts
- `paypal_merchant_id` - PayPal merchant ID
- `paypal_merchant_status` - Status: not_started, pending, active, restricted, disabled
- `paypal_email` - PayPal email for receiving payments
- `payment_setup_completed` - TRUE if at least one method is set up

**New tables:**
- `platform_fees` - Configurable platform fee percentages
- `trader_payouts` - Payout history and tracking

**Updated view:**
- `trader_earnings` - Now includes platform fees and available balance

---

## API Endpoints

### Payment Account Management

#### 1. Get All Payment Account Status
```
GET /api/payment-accounts/status
```

Response:
```json
{
  "payment_setup_completed": true,
  "stripe": {
    "connected": true,
    "status": "active",
    "charges_enabled": true,
    "payouts_enabled": true,
    "account_id": "acct_..."
  },
  "paypal": {
    "connected": true,
    "status": "active",
    "email": "trader@example.com"
  }
}
```

### Stripe Connect

#### 2. Start Stripe Connect Onboarding
```
POST /api/payment-accounts/stripe/connect/onboard
```

Response:
```json
{
  "success": true,
  "account_id": "acct_...",
  "onboarding_url": "https://connect.stripe.com/setup/...",
  "message": "Redirect user to onboarding_url to complete setup"
}
```

**Frontend Action:** Redirect user to `onboarding_url`

#### 3. Check Stripe Connect Status
```
GET /api/payment-accounts/stripe/connect/status
```

Response:
```json
{
  "connected": true,
  "status": "active",
  "charges_enabled": true,
  "payouts_enabled": true,
  "account_id": "acct_...",
  "details_submitted": true
}
```

#### 4. Open Stripe Express Dashboard
```
POST /api/payment-accounts/stripe/connect/dashboard
```

Response:
```json
{
  "success": true,
  "dashboard_url": "https://connect.stripe.com/express/..."
}
```

**Frontend Action:** Open `dashboard_url` in new tab

#### 5. Disconnect Stripe Account
```
DELETE /api/payment-accounts/stripe/connect/disconnect
```

### PayPal

#### 6. Connect PayPal Account
```
POST /api/payment-accounts/paypal/connect
Body: { "paypal_email": "trader@example.com" }
```

Response:
```json
{
  "success": true,
  "paypal_email": "trader@example.com",
  "status": "active",
  "message": "PayPal account connected successfully"
}
```

#### 7. Check PayPal Status
```
GET /api/payment-accounts/paypal/status
```

#### 8. Disconnect PayPal
```
DELETE /api/payment-accounts/paypal/disconnect
```

### Earnings & Payouts

#### 9. Get Earnings Breakdown
```
GET /api/payment-accounts/earnings
```

Response:
```json
{
  "available_balance": 450.00,
  "lifetime_earnings": 2500.00,
  "total_paid_out": 2050.00,
  "pending_balance": 450.00,
  "payment_setup_completed": true,
  "subscription_revenue": {
    "gross": 1000.00,
    "platform_fees": 100.00,
    "net": 900.00
  },
  "content_revenue": {
    "gross": 1500.00,
    "platform_fees": 150.00,
    "net": 1350.00,
    "sales_count": 30
  },
  "tips": {
    "gross": 300.00,
    "platform_fees": 15.00,
    "net": 285.00
  },
  "total_platform_fees": 265.00,
  "active_subscribers": 50
}
```

#### 10. Get Payout History
```
GET /api/payment-accounts/payouts
```

Response:
```json
{
  "payouts": [
    {
      "id": 1,
      "amount": 2050.00,
      "currency": "GBP",
      "provider": "stripe",
      "status": "paid",
      "created_at": "2026-01-01T00:00:00Z",
      "paid_at": "2026-01-03T00:00:00Z",
      "description": "Monthly payout",
      "failure_reason": null
    }
  ]
}
```

---

## Frontend Integration

### 1. Payment Account Setup Page

```typescript
// /settings/payments

interface PaymentStatus {
  payment_setup_completed: boolean;
  stripe: {
    connected: boolean;
    status: string;
    charges_enabled: boolean;
    payouts_enabled: boolean;
  };
  paypal: {
    connected: boolean;
    status: string;
    email?: string;
  };
}

function PaymentAccountsPage() {
  const [status, setStatus] = useState<PaymentStatus | null>(null);
  const [earnings, setEarnings] = useState<any>(null);

  useEffect(() => {
    // Load status
    fetch('/api/payment-accounts/status')
      .then(r => r.json())
      .then(setStatus);

    // Load earnings
    fetch('/api/payment-accounts/earnings')
      .then(r => r.json())
      .then(setEarnings);
  }, []);

  async function connectStripe() {
    const response = await fetch('/api/payment-accounts/stripe/connect/onboard', {
      method: 'POST'
    });
    const data = await response.json();

    // Redirect to Stripe onboarding
    window.location.href = data.onboarding_url;
  }

  async function connectPayPal() {
    const email = prompt('Enter your PayPal email:');
    if (!email) return;

    await fetch('/api/payment-accounts/paypal/connect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paypal_email: email })
    });

    // Refresh status
    window.location.reload();
  }

  async function openStripeDashboard() {
    const response = await fetch('/api/payment-accounts/stripe/connect/dashboard', {
      method: 'POST'
    });
    const data = await response.json();
    window.open(data.dashboard_url, '_blank');
  }

  return (
    <div>
      <h1>Payment Accounts</h1>

      {!status?.payment_setup_completed && (
        <Alert variant="warning">
          You must connect a payment account to receive payments from subscribers and content buyers.
        </Alert>
      )}

      {/* Stripe Section */}
      <Card>
        <h2>Stripe Connect</h2>
        {status?.stripe.connected ? (
          <div>
            <Badge variant="success">Connected</Badge>
            <p>Status: {status.stripe.status}</p>
            <p>Charges Enabled: {status.stripe.charges_enabled ? 'Yes' : 'No'}</p>
            <p>Payouts Enabled: {status.stripe.payouts_enabled ? 'Yes' : 'No'}</p>
            <button onClick={openStripeDashboard}>Open Dashboard</button>
          </div>
        ) : (
          <div>
            <p>Connect your Stripe account to receive payments via credit/debit cards and have funds deposited directly to your bank account.</p>
            <button onClick={connectStripe}>Connect Stripe</button>
          </div>
        )}
      </Card>

      {/* PayPal Section */}
      <Card>
        <h2>PayPal</h2>
        {status?.paypal.connected ? (
          <div>
            <Badge variant="success">Connected</Badge>
            <p>Email: {status.paypal.email}</p>
          </div>
        ) : (
          <div>
            <p>Connect your PayPal email to receive payments via PayPal.</p>
            <button onClick={connectPayPal}>Connect PayPal</button>
          </div>
        )}
      </Card>

      {/* Earnings Section */}
      {earnings && (
        <Card>
          <h2>Earnings Overview</h2>
          <div>
            <h3>Available Balance: £{earnings.available_balance.toFixed(2)}</h3>
            <p>Total Lifetime Earnings: £{earnings.lifetime_earnings.toFixed(2)}</p>
            <p>Total Paid Out: £{earnings.total_paid_out.toFixed(2)}</p>
            <p>Total Platform Fees: £{earnings.total_platform_fees.toFixed(2)}</p>
          </div>

          <h4>Revenue Breakdown</h4>
          <table>
            <tr>
              <td>Subscriptions (Gross)</td>
              <td>£{earnings.subscription_revenue.gross.toFixed(2)}</td>
            </tr>
            <tr>
              <td>Platform Fees (10%)</td>
              <td>-£{earnings.subscription_revenue.platform_fees.toFixed(2)}</td>
            </tr>
            <tr>
              <td><strong>Net Subscription Revenue</strong></td>
              <td><strong>£{earnings.subscription_revenue.net.toFixed(2)}</strong></td>
            </tr>
            <tr><td colspan="2"><hr/></td></tr>
            <tr>
              <td>Content Sales (Gross)</td>
              <td>£{earnings.content_revenue.gross.toFixed(2)}</td>
            </tr>
            <tr>
              <td>Platform Fees (10%)</td>
              <td>-£{earnings.content_revenue.platform_fees.toFixed(2)}</td>
            </tr>
            <tr>
              <td><strong>Net Content Revenue</strong></td>
              <td><strong>£{earnings.content_revenue.net.toFixed(2)}</strong></td>
            </tr>
          </table>

          <p>Active Subscribers: {earnings.active_subscribers}</p>
        </Card>
      )}
    </div>
  );
}
```

### 2. Handling Stripe Onboarding Return

When traders return from Stripe onboarding, check their connection status:

```typescript
// /settings/payments?setup=complete

useEffect(() => {
  const params = new URLSearchParams(window.location.search);

  if (params.get('setup') === 'complete') {
    // Check Stripe connection status
    fetch('/api/payment-accounts/stripe/connect/status')
      .then(r => r.json())
      .then(data => {
        if (data.charges_enabled) {
          alert('Stripe account connected successfully! You can now receive payments.');
        } else {
          alert('Setup in progress. You may need to complete additional steps.');
        }
      });
  }

  if (params.get('refresh') === 'true') {
    // User needs to refresh onboarding
    alert('Please complete the account setup.');
  }
}, []);
```

### 3. Show Payment Setup Status on Profile

Warn traders if they haven't set up payments:

```typescript
function TraderProfile() {
  const [paymentSetup, setPaymentSetup] = useState(false);

  useEffect(() => {
    fetch('/api/payment-accounts/status')
      .then(r => r.json())
      .then(data => setPaymentSetup(data.payment_setup_completed));
  }, []);

  return (
    <div>
      {!paymentSetup && (
        <Alert variant="warning">
          You haven't set up your payment account yet.
          <Link to="/settings/payments">Connect now</Link> to receive payments.
        </Alert>
      )}
    </div>
  );
}
```

### 4. Block Subscription Pricing Without Payment Account

```typescript
function SubscriptionPricing() {
  const [paymentSetup, setPaymentSetup] = useState(false);
  const [price, setPrice] = useState(9.99);

  async function savePrice() {
    if (!paymentSetup) {
      alert('Please set up your payment account first.');
      window.location.href = '/settings/payments';
      return;
    }

    await fetch('/api/traders/profile', {
      method: 'PATCH',
      body: JSON.stringify({ subscription_price: price })
    });
  }

  return (
    <div>
      <input type="number" value={price} onChange={e => setPrice(parseFloat(e.target.value))} />
      <button onClick={savePrice}>Save Subscription Price</button>
    </div>
  );
}
```

---

## Platform Fees

### Default Fee Structure

Configured in `platform_fees` table:

| Transaction Type | Platform Fee |
|------------------|--------------|
| Subscriptions    | 10%          |
| Content Purchases| 10%          |
| Tips             | 5%           |

### How Fees Work

#### Stripe Connect
- **Subscriptions**: 10% application fee on each recurring payment
- **Content Purchases**: 10% application fee on one-time payment
- Trader receives net amount (90%) automatically
- Platform receives 10% fee automatically

#### PayPal
- **Orders**: Platform fee included in `platform_fees` field
- Trader receives net amount after PayPal processes payment
- Platform receives fee separately

### Example: £10 Subscription

```
Customer pays: £10.00
Platform fee:  £1.00 (10%)
Trader gets:   £9.00 (90%)
```

---

## Payment Flows

### Stripe Connect Subscription Flow

```
1. User subscribes to trader
2. Frontend calls POST /api/billing/create-checkout-session
3. Backend checks trader has Stripe Connect account
4. Backend creates checkout session with:
   - transfer_data.destination = trader's Stripe account
   - application_fee_percent = 10
5. User pays on Stripe checkout
6. Stripe automatically:
   - Deposits £9 to trader's connected account
   - Deposits £1 to platform account
7. Trader receives funds via Stripe payout (auto or manual)
```

### PayPal Content Purchase Flow

```
1. User purchases content
2. Frontend calls POST /api/paypal/purchase
3. Backend checks trader has PayPal email
4. Backend creates PayPal order with:
   - payee.email = trader's PayPal email
   - platform_fees = 10% of amount
5. User pays on PayPal
6. PayPal routes payment:
   - £9 to trader's PayPal account
   - £1 to platform PayPal account
7. Trader sees funds immediately in PayPal balance
```

---

## Stripe Connect Setup Steps

### For Platform (You)

1. **Enable Stripe Connect in Dashboard**
   - Go to https://dashboard.stripe.com/connect/overview
   - Enable Connect
   - Choose "Express" or "Standard" accounts (Express recommended)

2. **Set Redirect URLs**
   - Settings → Connect → Integration
   - Add redirect URIs:
     - `https://yourdomain.com/settings/payments?setup=complete`
     - `https://yourdomain.com/settings/payments?refresh=true`

3. **Configure Branding**
   - Settings → Connect → Branding
   - Add your logo and brand colors

### For Traders

Traders complete onboarding through your app:
1. Click "Connect Stripe" button
2. Redirect to Stripe onboarding
3. Provide business information
4. Connect bank account
5. Verify identity (may require documents)
6. Complete setup

Stripe reviews and activates account (usually instant, can take up to 2 days).

---

## PayPal Setup Steps

### For Platform (You)

For full marketplace features (optional):
1. Sign up for PayPal Commerce Platform
2. Get API credentials
3. Update `.env` with PayPal credentials

### For Traders

Simple email-based connection:
1. Trader enters their PayPal email
2. System stores email
3. Payments route to that email automatically

**Note:** Traders must have a PayPal Business account to receive commercial payments.

---

## Testing

### Stripe Connect Test Accounts

1. Use Stripe test mode (test API keys)
2. Create test connected account:
   ```bash
   curl https://api.stripe.com/v1/accounts \
     -u "sk_test_..." \
     -d type=express
   ```
3. Test onboarding with test credentials:
   - Use any email
   - Phone: +1 000 000 0000
   - Business: Use test EIN 00-0000000

### PayPal Sandbox

1. Create sandbox accounts at https://developer.paypal.com/dashboard/accounts
2. Use sandbox credentials in `.env`:
   ```
   PAYPAL_MODE=sandbox
   PAYPAL_CLIENT_ID=<sandbox_client_id>
   PAYPAL_CLIENT_SECRET=<sandbox_secret>
   ```
3. Test with sandbox buyer and seller accounts

---

## Troubleshooting

### Trader Can't Receive Payments

1. Check payment account status:
   ```sql
   SELECT stripe_connect_account_id, stripe_charges_enabled,
          paypal_email, payment_setup_completed
   FROM trader_profiles
   WHERE user_id = 'trader_id';
   ```

2. Verify Stripe account:
   - Check `stripe_charges_enabled = TRUE`
   - Check account status in Stripe dashboard

3. Verify PayPal:
   - Check `paypal_email` is set
   - Verify email is correct

### Payments Going to Platform Instead of Trader

1. Check payment routing in Stripe dashboard → Payments
2. Look for `application_fee_amount` and `transfer_data`
3. Verify connected account ID matches trader's account

### Platform Fee Not Being Deducted

1. Check `platform_fees` table configuration
2. Verify webhook is processing correctly
3. Check Stripe checkout session has `application_fee_percent`

---

## Security Considerations

1. **Never share Stripe Secret Key** - Only use in backend
2. **Validate trader ownership** - Only let traders modify their own accounts
3. **Verify payment routing** - Always check trader has payment account before creating checkout
4. **Webhook verification** - Always verify Stripe/PayPal webhook signatures
5. **Payout verification** - Log all payouts for audit trail

---

## Database Queries

### Check Trader's Payment Setup
```sql
SELECT
  user_id,
  stripe_connect_account_id,
  stripe_charges_enabled,
  paypal_email,
  payment_setup_completed
FROM trader_profiles
WHERE user_id = 'trader_id';
```

### Get Trader Earnings with Fees
```sql
SELECT * FROM trader_earnings
WHERE trader_id = 'trader_id';
```

### Find Traders Without Payment Accounts
```sql
SELECT user_id, username
FROM trader_profiles tp
JOIN user_profiles up ON tp.user_id = up.user_id
WHERE payment_setup_completed = FALSE
AND subscription_price > 0;
```

---

## Environment Variables

Add to `.env`:

```bash
# Stripe (existing)
STRIPE_SECRET_KEY=sk_test_...
STRIPE_PUBLISHABLE_KEY=pk_test_...

# PayPal (existing)
PAYPAL_CLIENT_ID=...
PAYPAL_CLIENT_SECRET=...
PAYPAL_MODE=sandbox  # or 'live'

# URLs
FRONTEND_URL=http://localhost:3000
```

---

## Summary

✅ **Traders receive payments directly** to their own Stripe or PayPal accounts
✅ **Platform fees automatically deducted** (10% for subs/content, 5% for tips)
✅ **Simple onboarding** via Stripe Connect and PayPal email
✅ **Transparent earnings** with detailed breakdowns
✅ **Automatic payouts** via Stripe (or manual via PayPal)

Traders can now monetize their content while you earn platform fees on all transactions! 🚀

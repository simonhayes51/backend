# Railway Deployment Guide - Backend

## 1. Create Railway Project

1. Go to [railway.app](https://railway.app)
2. Click **New Project**
3. Select **Deploy from GitHub repo**
4. Choose `simonhayes51/backend`

## 2. Add PostgreSQL Database

1. In your Railway project, click **New**
2. Select **Database** → **PostgreSQL**
3. Railway will automatically create a database and set `DATABASE_URL`

## 3. Configure Environment Variables

In Railway project settings → **Variables**, add:

```bash
# Auto-set by Railway (don't add manually)
DATABASE_URL=${DATABASE_URL}
PORT=${PORT}

# Discord OAuth - GET FROM DISCORD DEVELOPER PORTAL
DISCORD_CLIENT_ID=your-discord-client-id
DISCORD_CLIENT_SECRET=your-discord-client-secret
DISCORD_REDIRECT_URI=https://your-backend-url.railway.app/auth/callback
DISCORD_BOT_TOKEN=your-discord-bot-token
DISCORD_SERVER_ID=your-discord-server-id

# Security - GENERATE RANDOM STRINGS
SECRET_KEY=generate-random-32-char-string-here
JWT_PRIVATE_KEY=generate-another-random-string
JWT_ISSUER=fut-dashboard
JWT_TTL_SECONDS=2592000

# Stripe - GET FROM STRIPE DASHBOARD
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...

# Frontend URL - SET AFTER FRONTEND DEPLOYED
FRONTEND_URL=https://your-frontend-url.railway.app
COOKIE_DOMAIN=.railway.app

# Environment
ENV=production

# Optional: Use same database for all
PLAYER_DATABASE_URL=${DATABASE_URL}
WATCHLIST_DATABASE_URL=${DATABASE_URL}

# Watchlist Settings
WATCHLIST_POLL_INTERVAL=60
```

### Quick Generate Secrets (PowerShell):
```powershell
# Generate SECRET_KEY
-join ((65..90) + (97..122) + (48..57) | Get-Random -Count 32 | % {[char]$_})

# Generate JWT_PRIVATE_KEY  
-join ((65..90) + (97..122) + (48..57) | Get-Random -Count 32 | % {[char]$_})
```

## 4. Run Database Migration

After first deployment:

1. Go to Railway project → Backend service
2. Click **Settings** → **Deploy** tab
3. Add a **One-off Command**:

```bash
python -c "
import asyncio
import asyncpg
import os

async def run_migration():
    conn = await asyncpg.connect(os.getenv('DATABASE_URL'))
    
    with open('migrations/001_subscription_enhancements.sql', 'r') as f:
        sql = f.read()
    
    await conn.execute(sql)
    await conn.close()
    print('Migration completed!')

asyncio.run(run_migration())
"
```

**OR** manually connect to database and run the SQL file.

## 5. Configure Custom Domain (Optional)

1. Go to **Settings** → **Domains**
2. Click **Generate Domain** for free `.railway.app` domain
3. OR add your custom domain

## 6. Setup Stripe Webhooks

1. Go to Stripe Dashboard → **Developers** → **Webhooks**
2. Add endpoint: `https://your-backend-url.railway.app/webhook/stripe`
3. Select events:
   - `customer.subscription.created`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.payment_succeeded`
   - `invoice.payment_failed`
4. Copy webhook signing secret → Add to Railway as `STRIPE_WEBHOOK_SECRET`

## 7. Discord OAuth Setup

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Create/select your application
3. Go to **OAuth2** → **General**
4. Add redirect URL: `https://your-backend-url.railway.app/auth/callback`
5. Copy Client ID and Client Secret → Add to Railway variables
6. Go to **Bot** tab → Copy bot token → Add as `DISCORD_BOT_TOKEN`
7. Get your Discord server ID → Add as `DISCORD_SERVER_ID`

## 8. Verify Deployment

After deployment completes:

```bash
# Check health
curl https://your-backend-url.railway.app/

# Check API docs
# Visit: https://your-backend-url.railway.app/docs
```

## 9. Monitor Logs

- Go to Railway project → Backend service
- Click **Deployments** → View logs
- Check for any errors during startup

## Common Issues

### Database Connection Errors
- Ensure PostgreSQL database is in same project
- Check `DATABASE_URL` is set automatically
- Verify migrations ran successfully

### CORS Errors
- Add frontend URL to `FRONTEND_URL` variable
- Ensure `COOKIE_DOMAIN` matches your domain

### Discord Auth Not Working
- Verify redirect URI matches exactly
- Check Discord app has proper OAuth2 scopes
- Ensure bot is in your Discord server with permissions

## Next Steps

1. Deploy frontend (see RAILWAY_SETUP.md in frontend repo)
2. Update `FRONTEND_URL` variable in backend with frontend URL
3. Update backend URL in frontend environment variables
4. Test full OAuth flow

# LighthouseAI — Deskmate

Academic co-pilot and daily planner. Hosted on Vercel.

## Deploy to Vercel

### 1. Get a Neon (Postgres) Database
- Sign up free at https://neon.tech
- Create a project → copy the **Connection String** (starts with `postgresql://...`)

### 2. Set Environment Variables in Vercel Dashboard
Go to your Vercel project → Settings → Environment Variables and add:

| Variable | Value |
|---|---|
| `DATABASE_URL` | Your Neon connection string |
| `TELEGRAM_TOKEN` | Your Telegram bot token |
| `ALLOWED_CHAT_ID` | Your Telegram chat ID |
| `GEMINI_API_KEY` | Your Google Gemini API key |
| `GROQ_API_KEY` | Your Groq API key |
| `TELEGRAM_WEBHOOK_SECRET` | Any random string (e.g. `mysecret123`) |

### 3. Deploy
```bash
npm i -g vercel
vercel --prod
```

### 4. Register Telegram Webhook (one-time)
After deploying, run:
```bash
python setup_webhook.py your-app.vercel.app
```

This points your Telegram bot to your live Vercel endpoint.

## Architecture

- **Dashboard**: Flask app served via Vercel WSGI (`dashboard.py`)
- **Telegram Bot**: Webhook-based via `/api/telegram` serverless function
- **Reminders**: Vercel Cron Job runs `/api/cron/reminders` every minute
- **Database**: PostgreSQL (Neon) — replaces local SQLite

## Local Development
For local dev, just run:
```bash
pip install -r requirements.txt
python dashboard.py    # Dashboard on http://localhost:8420
python agent.py        # Telegram bot (polling mode)
```
No `DATABASE_URL` → automatically uses local `agent.db` (SQLite).

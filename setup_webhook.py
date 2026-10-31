"""
LighthouseAI — One-time Telegram Webhook Setup Script

Run this ONCE after deploying to Vercel to point your Telegram bot
to the new webhook URL.

Usage:
    python setup_webhook.py <your-vercel-domain>

Example:
    python setup_webhook.py deskmate.vercel.app

Your bot will then receive updates at:
    https://deskmate.vercel.app/api/telegram
"""

import sys
import os
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")


def load_env():
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()


load_env()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

if not TELEGRAM_TOKEN:
    print("ERROR: TELEGRAM_TOKEN not found in .env or environment variables.")
    sys.exit(1)

if len(sys.argv) < 2:
    print(__doc__)
    print("ERROR: Please provide your Vercel domain as an argument.")
    sys.exit(1)

domain = sys.argv[1].strip().rstrip("/").replace("https://", "").replace("http://", "")
webhook_url = f"https://{domain}/api/telegram"

print(f"\n🔧 Setting Telegram webhook to: {webhook_url}")

payload = {"url": webhook_url}
if WEBHOOK_SECRET:
    payload["secret_token"] = WEBHOOK_SECRET
    print(f"🔐 Using webhook secret: {WEBHOOK_SECRET[:8]}...")

resp = requests.post(
    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
    json=payload
)
data = resp.json()

if data.get("ok"):
    print(f"\n✅ Webhook successfully set!")
    print(f"   URL: {webhook_url}")
    print(f"\nYour Telegram bot will now receive updates via Vercel.")
    print("The cron job at /api/cron/reminders will handle scheduled reminders every minute.")
else:
    print(f"\n❌ Failed to set webhook: {data}")
    sys.exit(1)

# Verify webhook info
info_resp = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getWebhookInfo")
info = info_resp.json().get("result", {})
print(f"\n📋 Webhook Info:")
print(f"   URL: {info.get('url')}")
print(f"   Pending updates: {info.get('pending_update_count', 0)}")
print(f"   Last error: {info.get('last_error_message', 'None')}")


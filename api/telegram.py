"""
LighthouseAI — Telegram Webhook Handler (Vercel Serverless Function)

Receives POST updates from Telegram (via webhook) and processes them
using the same handler logic from agent.py, now adapted for a stateless
serverless environment.

Replaces: agent.py's run_polling() + Application.builder() setup.
"""

import sys
import os

# Add project root to path so we can import lighthouse_core and agent handlers
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import hmac
import hashlib
import logging
from flask import Flask, request, jsonify

import lighthouse_core

log = logging.getLogger("lighthouse-webhook")
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")


def verify_telegram_secret(req) -> bool:
    """Verify the X-Telegram-Bot-Api-Secret-Token header."""
    if not WEBHOOK_SECRET:
        # If no secret is configured, allow all (dev mode only — set a secret in prod!)
        return True
    secret_token = req.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    return hmac.compare_digest(secret_token, WEBHOOK_SECRET)


@app.route("/api/telegram", methods=["POST"])
def telegram_webhook():
    """Main Telegram webhook endpoint. Receives JSON updates from Telegram."""
    if not verify_telegram_secret(request):
        return jsonify({"error": "Unauthorized"}), 403

    try:
        update_data = request.get_json(force=True, silent=True) or {}
        if not update_data:
            return jsonify({"ok": True})

        # Process the update using the agent module
        import asyncio
        from agent_webhook import process_update
        asyncio.run(process_update(update_data))

    except Exception as e:
        log.error(f"Webhook processing error: {e}", exc_info=True)
        # Always return 200 to Telegram so it doesn't retry infinitely
        return jsonify({"ok": True, "error": str(e)})

    return jsonify({"ok": True})


# Vercel WSGI entrypoint
handler = app


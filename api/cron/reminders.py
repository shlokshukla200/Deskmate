"""
LighthouseAI — Cron Job: Task Reminders (Vercel Cron)

Runs every minute via Vercel Cron (configured in vercel.json).
Checks for due tasks and sends Telegram notifications.

Replaces: agent.py's job_queue.run_repeating(check_reminders, interval=15)
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import html
import logging
import asyncio
from flask import Flask, request, jsonify
import requests as http_requests

import lighthouse_core

log = logging.getLogger("lighthouse-cron")
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")  # Optional: secure your cron endpoint


def send_telegram_message(text: str, chat_id: str = None) -> bool:
    """Send a Telegram message via the Bot API (simple HTTP, no library overhead)."""
    if not TELEGRAM_TOKEN:
        return False
    cid = chat_id or ALLOWED_CHAT_ID
    if not cid:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = http_requests.post(url, json={
            "chat_id": int(cid),
            "text": text,
            "parse_mode": "HTML"
        }, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        log.warning(f"Failed to send Telegram message: {e}")
        return False


@app.route("/api/cron/reminders", methods=["GET", "POST"])
def cron_reminders():
    """Vercel Cron Job endpoint — checks for due tasks and sends reminders."""

    # Optional: Verify Vercel cron secret header
    if CRON_SECRET:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {CRON_SECRET}":
            return jsonify({"error": "Unauthorized"}), 403

    if not TELEGRAM_TOKEN or not ALLOWED_CHAT_ID:
        return jsonify({"ok": True, "skipped": "No Telegram config"})

    reminded = []
    errors = []

    # 1. Check Due Tasks → Send Reminders
    try:
        due_items = lighthouse_core.get_due_unnotified()
        for task_id, content in due_items:
            msg = (
                f"⏰ <b>Scheduled Reminder</b>\n\n"
                f"<b>#{task_id}</b>: {html.escape(content)}"
            )
            ok = send_telegram_message(msg)
            if ok:
                lighthouse_core.mark_notified(task_id)
                reminded.append(task_id)
            else:
                errors.append(f"task_{task_id}")
    except Exception as e:
        log.error(f"Error checking reminders: {e}", exc_info=True)
        errors.append(str(e))

    # 2. Update heartbeat (so dashboard shows bot as connected)
    try:
        lighthouse_core.record_heartbeat("lighthouse_bot_webhook")
    except Exception:
        pass

    # 3. Battery alerts (client pushes battery data via /api/battery/status;
    #    cron picks up stored alerts and fires Telegram messages)
    try:
        def send_tg(text_msg):
            send_telegram_message(text_msg)

        lighthouse_core.check_and_send_battery_alerts(send_telegram_fn=send_tg)
    except Exception as e:
        log.warning(f"Battery alert check failed: {e}")

    return jsonify({
        "ok": True,
        "reminded_tasks": reminded,
        "errors": errors
    })


# Vercel WSGI entrypoint
handler = app


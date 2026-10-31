"""
LighthouseAI — Telegram Webhook Processor (Vercel-Compatible)

This module replaces agent.py's run_polling() mode.
Instead of a long-running process, Telegram sends updates via webhook to
/api/telegram, which calls process_update() here.

All handler logic from agent.py is preserved — only the startup/runner is replaced.
"""

import os
import json
import logging
import html
from datetime import datetime, timedelta
from functools import wraps

from telegram import Update, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import telegram.error

import lighthouse_core

log = logging.getLogger("lighthouse-agent-webhook")
logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
raw_allowed_chat_id = os.environ.get("ALLOWED_CHAT_ID")
try:
    ALLOWED_CHAT_ID = int(raw_allowed_chat_id) if raw_allowed_chat_id else None
except (ValueError, TypeError):
    ALLOWED_CHAT_ID = None

# In-memory pending confirmations (ephemeral per invocation — acceptable for serverless)
pending_confirmations = {}


# =========================================================
# ACCESS CONTROL
# =========================================================

def require_auth(handler_func):
    @wraps(handler_func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not update or not update.effective_chat or ALLOWED_CHAT_ID is None or update.effective_chat.id != ALLOWED_CHAT_ID:
            sender_id = update.effective_chat.id if (update and update.effective_chat) else "unknown"
            log.warning(f"Unauthorized access attempt silently ignored from chat_id: {sender_id}")
            return
        return await handler_func(update, context, *args, **kwargs)
    return wrapper


# =========================================================
# INTENT PARSER
# =========================================================

def parse_full_agent_intent(user_text: str) -> dict:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tasks = lighthouse_core.get_all_tasks()
    notes = lighthouse_core.get_notes(include_archived=False)
    timetables = lighthouse_core.get_saved_timetables()
    roadmap = lighthouse_core.get_saved_roadmap()

    tasks_summary = "\n".join([
        f"- #{t[0]}: {t[1]} (due: {t[2] or 'No time'}, {'DONE' if t[3] else 'PENDING'}{f' | note: {t[5]}' if len(t) > 5 and t[5] else ''})"
        for t in tasks[:15]
    ]) or "No active tasks."

    notes_summary = "\n".join([
        f"- Note #{n['id']}: '{n['title'] or 'Untitled'}' -> {n['content'][:50]}... ({'PINNED' if n['is_pinned'] else 'NORMAL'})"
        for n in notes[:8]
    ]) or "No sticky notes."

    tt_summary = "\n".join([
        f"- Timetable #{t['id']}: '{t['title']}' ({len(t.get('slots', []))} slots, date: {t.get('date', '')})"
        for t in timetables[:4]
    ]) or "No saved timetables."

    rm_summary = f"Exam Date: {roadmap.get('examDate')}, {len(roadmap.get('phases', []))} Phases, Subjects: {', '.join(roadmap.get('subjects', []))}" if roadmap else "No active roadmap."

    system_prompt = f"""You are LighthouseAI, an intelligent personal academic agent for Shlok.
Current Date & Time: {now_str}.

WORKSPACE CONTEXT:
[Active Tasks]:
{tasks_summary}

[Sticky Notes]:
{notes_summary}

[Saved Timetables]:
{tt_summary}

[Study Roadmap]:
{rm_summary}

Analyze the user's message and return a STRICT JSON object:
{{
  "intent": "add_task" | "edit_task" | "rename_task" | "delete_task" | "complete_task" | "uncomplete_task" | "list_tasks" | "query_schedule" | "add_note" | "edit_note" | "delete_note" | "list_notes" | "search_notes" | "generate_roadmap" | "get_roadmap" | "generate_timetable" | "create_essential_schedule" | "list_timetables" | "get_timetable" | "rename_timetable" | "delete_timetable" | "sync_timetable" | "add_slot_note" | "send_pdf" | "chat",
  "task_id": <int or null>,
  "content": "<string or null>",
  "due_time": "<YYYY-MM-DD HH:MM or YYYY-MM-DDTHH:MM:SS or null>",
  "notes": "<string or null>",
  "note_id": <int or null>,
  "note_title": "<string or null>",
  "note_content": "<string or null>",
  "note_color": "<string or null>",
  "is_pinned": <bool or null>,
  "exam_date": "<YYYY-MM-DD or null>",
  "study_hours": "<number string or null>",
  "subjects": <array of strings or null>,
  "syllabus": "<string or null>",
  "wake_time": "<HH:MM or null>",
  "bed_time": "<HH:MM or null>",
  "sync_to_tasks": <bool or null>,
  "pdf_target": "roadmap" | "timetable" | null,
  "timetable_id": "<string or null>",
  "timetable_title": "<string or null>",
  "essential_text": "<string or null>",
  "slot_index": <int or null>,
  "slot_note": "<string or null>",
  "search_query": "<string or null>",
  "reply": "<direct, crisp, friendly response to Shlok>"
}}
Output pure JSON without markdown backticks."""

    raw = lighthouse_core.call_ai_model(user_text, system_prompt=system_prompt, json_mode=True)
    if raw:
        try:
            parsed = json.loads(lighthouse_core.clean_json_str(raw))
            if isinstance(parsed, dict) and "intent" in parsed:
                return parsed
        except Exception as e:
            log.warning(f"Failed to parse LLM intent: {e}")

    return {
        "intent": "chat",
        "reply": f"I heard: '{user_text}'. Could you clarify what you'd like to update in your tasks, notes, or schedule?"
    }


def resolve_task_id(val) -> int | None:
    if val is None:
        return None
    try:
        clean_val = int(str(val).replace("#", "").strip())
    except (ValueError, TypeError):
        return None

    today_str = datetime.now().strftime("%Y-%m-%d")
    today_tasks = lighthouse_core.get_tasks(today_str)

    t = lighthouse_core.get_task_by_id(clean_val)
    if t:
        return clean_val

    if 1 <= clean_val <= len(today_tasks):
        return today_tasks[clean_val - 1][0]

    pending = [tk for tk in today_tasks if not tk[3]]
    if 1 <= clean_val <= len(pending):
        return pending[clean_val - 1][0]

    return clean_val


# =========================================================
# COMMAND HANDLERS (identical logic to agent.py)
# =========================================================

@require_auth
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    await update.message.reply_text(
        "🪷 <b>Radhe Radhe, Shlok!</b>\n\n"
        "Welcome to your <b>LighthouseAI</b> co-pilot console.\n"
        "I have complete control over your schedule, notes, timetables, and roadmap.\n\n"
        "<b>Core Commands:</b>\n"
        "• <code>/list</code> — View pending tasks & schedule\n"
        "• <code>/done &lt;id&gt;</code> — Mark task complete\n"
        "• <code>/notes</code> — View your sticky notes\n"
        "• <code>/timetable</code> — View active study timetable\n"
        "• <code>/roadmap</code> — View exam roadmap status\n"
        "• <code>/pdf [roadmap|timetable]</code> — Send instant PDF\n"
        "• <code>/help</code> — Full conversational guide\n\n"
        f"🔐 <i>Workspace Locked (Chat ID: <code>{chat_id}</code>)</i>",
        parse_mode="HTML"
    )


@require_auth
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    await update.message.reply_text(
        "⚡ <b>LighthouseAI — Capabilities Guide</b>\n\n"
        "You can type naturally in plain English or use slash commands:\n\n"
        "📋 <b>Tasks & Schedule:</b>\n"
        "• <i>'Add Calculus problem set at 4 PM tomorrow'</i>\n"
        "• <i>'Reschedule task #3 to Friday 10 AM'</i>\n"
        "• <i>'Mark task #2 done'</i> or <i>'Delete task #5'</i>\n"
        "• <i>'What is on my schedule today?'</i>\n\n"
        "📝 <b>Sticky Notes:</b>\n"
        "• <i>'Create a note: Quantum Physics Formulas'</i>\n"
        "• <i>'Search notes for organic chemistry'</i>\n\n"
        "⏱️ <b>Timetables & Roadmaps:</b>\n"
        "• <i>'Build a 6 hour study schedule from 7 AM to 11 PM'</i>\n"
        "• <i>'Create an exam roadmap for Nov 20 on Physics and Math'</i>\n"
        "• <i>'Send me my study roadmap as a PDF'</i>",
        parse_mode="HTML"
    )


@require_auth
async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_tasks = lighthouse_core.get_tasks(today_str)

    if not today_tasks:
        await update.message.reply_text("✨ <b>Your schedule is clear!</b> No tasks found for today.", parse_mode="HTML")
        return

    msg = f"📋 <b>Today's Schedule & Tasks ({len(today_tasks)} total):</b>\n\n"
    for idx, t in enumerate(today_tasks, 1):
        tid, content, due_time, done = t[0], t[1], t[2], bool(t[3])
        notes = t[5] if len(t) > 5 else ""
        status_icon = "✅" if done else "⏳"
        due_badge = f" ⏰ <code>{html.escape(due_time)}</code>" if due_time else ""
        notes_badge = f"\n   ↳ <i>Note: {html.escape(notes)}</i>" if notes else ""
        msg += f"{status_icon} <b>#{idx}</b>: {html.escape(content)}{due_badge}{notes_badge}\n"

    msg += "\n<i>Tip: Type '/done 1' to complete task #1, or tell me naturally!</i>"
    await update.message.reply_text(msg, parse_mode="HTML")


@require_auth
async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    if not context.args:
        await update.message.reply_text("Usage: <code>/done &lt;task_number&gt;</code>", parse_mode="HTML")
        return
    raw_arg = context.args[0]
    task_id = resolve_task_id(raw_arg)
    if task_id and lighthouse_core.mark_done(task_id, 1):
        t_data = lighthouse_core.get_task_by_id(task_id)
        t_title = t_data[1] if t_data else "Task"
        await update.message.reply_text(f"✅ <b>Task marked complete!</b>\n• <b>{html.escape(t_title)}</b> ⚡", parse_mode="HTML")
    else:
        await update.message.reply_text(f"Task #{raw_arg} not found or already completed.")


@require_auth
async def notes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    notes = lighthouse_core.get_notes(include_archived=False)
    if not notes:
        await update.message.reply_text("📝 No sticky notes found. Tell me to create one!", parse_mode="HTML")
        return
    msg = "📝 <b>Your Sticky Notes:</b>\n\n"
    for n in notes[:10]:
        pin_icon = "📌 " if n["is_pinned"] else "📄 "
        title_str = html.escape(n["title"]) if n["title"] else "Untitled Note"
        content_snippet = html.escape(n["content"][:80]) + ("..." if len(n["content"]) > 80 else "")
        msg += f"{pin_icon}<b>#{n['id']} {title_str}</b>\n   {content_snippet}\n\n"
    await update.message.reply_text(msg, parse_mode="HTML")


@require_auth
async def roadmap_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    rm = lighthouse_core.get_saved_roadmap()
    if not rm:
        await update.message.reply_text(
            "🗺️ No active exam roadmap found.\nTell me: <i>'Create a roadmap for exam on 2026-11-20 for Math and Physics'</i>",
            parse_mode="HTML"
        )
        return
    exam_date = rm.get("examDate", "Target Date")
    phases = rm.get("phases", [])
    msg = f"🗺️ <b>Exam Study Roadmap (Target: {html.escape(exam_date)})</b>\n\n"
    for p in phases:
        msg += f"<b>{html.escape(p.get('name', 'Phase'))}</b> ({html.escape(p.get('duration', ''))})\n"
        msg += f"• <i>Target:</i> {html.escape(p.get('dailyTarget', ''))}\n"
        msg += f"• <i>Milestone:</i> {html.escape(p.get('milestone', ''))}\n\n"
    msg += "<i>Tip: Type '/pdf roadmap' to export as a formatted PDF.</i>"
    await update.message.reply_text(msg, parse_mode="HTML")


@require_auth
async def timetable_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    tts = lighthouse_core.get_saved_timetables()
    if not tts:
        await update.message.reply_text(
            "⏱️ No saved study timetables found.\nTell me: <i>'Generate a daily timetable from 7 AM to 11 PM'</i>",
            parse_mode="HTML"
        )
        return
    latest = tts[0]
    title = latest.get("title", "Active Protocol")
    slots = latest.get("slots", [])
    msg = f"⏱️ <b>{html.escape(title)}</b>\n\n"
    for s in slots[:10]:
        t_str = html.escape(s.get("time", ""))
        act_str = html.escape(s.get("activity", ""))
        type_icon = "📚" if s.get("type") == "study" else ("☕" if s.get("type") == "break" else "⚡")
        msg += f"{type_icon} <code>{t_str}</code> — {act_str}\n"
    if len(slots) > 10:
        msg += f"\n<i>...and {len(slots) - 10} more slots.</i>"
    await update.message.reply_text(msg, parse_mode="HTML")


@require_auth
async def schedules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    tts = lighthouse_core.get_saved_timetables()
    if not tts:
        await update.message.reply_text("⏱️ No saved schedules found.", parse_mode="HTML")
        return
    msg = "⏱️ <b>Your Saved Schedules:</b>\n\n"
    for idx, t in enumerate(tts, 1):
        t_id = t.get("id")
        title = html.escape(t.get("title", "Protocol"))
        slots_count = len(t.get("slots", []))
        date_str = html.escape(t.get("date", ""))
        msg += f"<b>{idx}. #{t_id} — {title}</b>\n   ↳ {slots_count} slots | Date: {date_str}\n\n"
    msg += "<i>Commands: /rename_schedule &lt;id&gt; &lt;name&gt;, /delete_schedule &lt;id&gt;, /sync_schedule &lt;id&gt;</i>"
    await update.message.reply_text(msg, parse_mode="HTML")


@require_auth
async def rename_schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    if len(context.args) < 2:
        await update.message.reply_text("Usage: <code>/rename_schedule &lt;id&gt; &lt;new_name&gt;</code>", parse_mode="HTML")
        return
    t_id = context.args[0].replace("#", "")
    new_title = " ".join(context.args[1:])
    if lighthouse_core.rename_timetable(t_id, new_title):
        await update.message.reply_text(f"🏷️ <b>Renamed Schedule #{t_id}</b> to: <b>{html.escape(new_title)}</b>", parse_mode="HTML")
    else:
        await update.message.reply_text(f"Schedule #{t_id} not found.")


@require_auth
async def delete_schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    if not context.args:
        await update.message.reply_text("Usage: <code>/delete_schedule &lt;id&gt;</code>", parse_mode="HTML")
        return
    t_id = context.args[0].replace("#", "")
    lighthouse_core.delete_timetable(t_id)
    await update.message.reply_text(f"🗑️ <b>Deleted Schedule #{t_id}</b>", parse_mode="HTML")


@require_auth
async def sync_schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    if not context.args:
        await update.message.reply_text("Usage: <code>/sync_schedule &lt;id&gt;</code>", parse_mode="HTML")
        return
    t_id = context.args[0].replace("#", "")
    tt = lighthouse_core.get_timetable_by_id(t_id)
    if not tt:
        await update.message.reply_text(f"Schedule #{t_id} not found.")
        return
    today_str = datetime.now().strftime("%Y-%m-%d")
    slots = tt.get("slots", [])
    for s in slots:
        time_part = s.get("time", "").split("-")[0].strip()
        due = s.get("iso_time") or (f"{today_str}T{time_part.replace(' ', '')}:00" if time_part else None)
        note_str = s.get("note", "") or f"Protocol slot | Category: {s.get('type', 'study')}"
        lighthouse_core.add_task(content=s.get("activity", "Study Slot"), due_time=due, notes=note_str, timetable_id=t_id)
    await update.message.reply_text(f"⚡ <b>Synced {len(slots)} slots from '{html.escape(tt.get('title', 'Schedule'))}' to Today's Dashboard!</b>", parse_mode="HTML")


@require_auth
async def rename_task_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    if len(context.args) < 2:
        await update.message.reply_text("Usage: <code>/rename_task &lt;task_number&gt; &lt;new_title&gt;</code>", parse_mode="HTML")
        return
    raw_arg = context.args[0]
    t_id = resolve_task_id(raw_arg)
    new_title = " ".join(context.args[1:])
    if t_id and lighthouse_core.edit_task(t_id, content=new_title):
        await update.message.reply_text(f"✏️ Renamed task to: <b>{html.escape(new_title)}</b>", parse_mode="HTML")
    else:
        await update.message.reply_text(f"Task #{raw_arg} not found.")


@require_auth
async def delete_task_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    if not context.args:
        await update.message.reply_text("Usage: <code>/delete_task &lt;task_number&gt;</code>", parse_mode="HTML")
        return
    raw_arg = context.args[0]
    t_id = resolve_task_id(raw_arg)
    if t_id and lighthouse_core.delete_task(t_id):
        await update.message.reply_text(f"🗑️ Deleted task #{raw_arg}.", parse_mode="HTML")
    else:
        await update.message.reply_text(f"Task #{raw_arg} not found.")


@require_auth
async def pdf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    target = (context.args[0].lower() if context.args else "roadmap")

    if target in ["roadmap", "exam"]:
        rm = lighthouse_core.get_saved_roadmap()
        if not rm:
            rm = lighthouse_core.generate_roadmap_ai(exam_date=(datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"))
            lighthouse_core.save_roadmap(rm.get("examDate"), 5, ["Core Subjects"], "Comprehensive Syllabus", rm.get("phases", []))
        pdf_bytes = lighthouse_core.generate_roadmap_pdf(rm)
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=pdf_bytes,
            filename=f"exam_roadmap_{datetime.now().strftime('%Y%m%d')}.pdf",
            caption="📄 <b>LighthouseAI — Exam Study Roadmap</b>",
            parse_mode="HTML"
        )
    else:
        tts = lighthouse_core.get_saved_timetables()
        if not tts:
            tt_data = lighthouse_core.generate_timetable_ai()
            t_id = lighthouse_core.save_timetable(title="Daily Focus Routine", slots=tt_data.get("slots", []))
            tt_data["title"] = "Daily Focus Routine"
            tt_data["id"] = t_id
            latest = tt_data
        else:
            latest = tts[0]
        pdf_bytes = lighthouse_core.generate_timetable_pdf(latest)
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=pdf_bytes,
            filename=f"study_timetable_{datetime.now().strftime('%Y%m%d')}.pdf",
            caption=f"📄 <b>LighthouseAI — {html.escape(latest.get('title', 'Study Timetable'))}</b>",
            parse_mode="HTML"
        )


@require_auth
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    b_info = lighthouse_core.get_battery_info()
    percent = b_info.get("percent", 100)
    is_charging = bool(b_info.get("is_charging", False))

    if is_charging:
        batt_display = f"⚡ <b>{percent}% — CHARGING (Plugged In / AC Power)</b>"
    else:
        batt_icon = "🔋" if percent > 40 else "🪫"
        batt_display = f"{batt_icon} <b>{percent}% — On Battery (Discharging)</b>"

    alarms = lighthouse_core.get_active_alarms()
    alarm_str = f"{len(alarms)} active" if alarms else "None"

    today_str = datetime.now().strftime("%Y-%m-%d")
    today_tasks = lighthouse_core.get_tasks(today_str)
    done_count = sum(1 for t in today_tasks if t[3])
    total_tasks = len(today_tasks)

    rm = lighthouse_core.get_saved_roadmap()
    rm_str = f"Target: {rm.get('examDate')} ({len(rm.get('phases', []))} Phases)" if rm else "None active"

    tts = lighthouse_core.get_saved_timetables()
    tt_str = f"{len(tts)} saved protocols" if tts else "None saved"

    notes = lighthouse_core.get_notes(include_archived=False)
    pinned_count = sum(1 for n in notes if n.get("is_pinned"))

    msg = (
        f"🌐 <b>LighthouseAI — Full Workspace & Device Scope</b>\n\n"
        f"🔌 <b>Power & Battery:</b> {batt_display}\n"
        f"⏰ <b>Active Alarms:</b> {alarm_str}\n"
        f"📋 <b>Today's Schedule:</b> {done_count}/{total_tasks} completed\n"
        f"🗺️ <b>Exam Roadmap:</b> {html.escape(rm_str)}\n"
        f"⏱️ <b>Saved Schedules:</b> {tt_str}\n"
        f"📝 <b>Sticky Notes:</b> {len(notes)} notes ({pinned_count} pinned)\n\n"
        f"<i>All modules connected. Hosted on Vercel.</i>"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


@require_auth
async def battery_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    b_info = lighthouse_core.get_battery_info()
    percent = b_info.get("percent", 100)
    is_charging = bool(b_info.get("is_charging", False))
    active_alarms = lighthouse_core.get_active_alarms()

    status_icon = "⚡" if is_charging else ("🔋" if percent > 40 else "🪫")
    charging_text = "⚡ <b>CHARGING (Plugged In / AC Power)</b>" if is_charging else "🔋 <b>On Battery (Discharging)</b>"

    warning_text = ""
    if percent < 40 and not is_charging and active_alarms:
        warning_text = "\n\n🚨 <b>CRITICAL WARNING:</b> Battery is below 40% with active alarms!"
    elif percent < 50 and not is_charging:
        warning_text = "\n\n⚠️ <b>Notice:</b> Battery is below 50%. Please connect charger soon."
    elif is_charging:
        warning_text = "\n\n⚡ <i>Device is actively charging. Safe for all alarms.</i>"

    alarm_str = "\n\n⏰ <b>Active Alarms:</b>\n" + "\n".join([f"• <b>{a['time']}</b> — {html.escape(a['label'])}" for a in active_alarms]) if active_alarms else "\n\n⏰ <b>Active Alarms:</b> None"
    await update.message.reply_text(
        f"{status_icon} <b>Device Battery & Power Status</b>\n\n"
        f"• <b>Battery Level:</b> {percent}%\n"
        f"• <b>Power State:</b> {charging_text}"
        f"{alarm_str}{warning_text}",
        parse_mode="HTML"
    )


@require_auth
async def alarm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    if not context.args:
        alarms = lighthouse_core.get_alarms()
        if not alarms:
            await update.message.reply_text("⏰ No alarms set yet.\n\nUsage: <code>/alarm &lt;time&gt; &lt;label&gt;</code>", parse_mode="HTML")
            return
        lines = [f"• [#{a['id']}] <b>{a['time']}</b> — {html.escape(a['label'])} ({'🟢 Enabled' if a.get('enabled') else '⚪ Disabled'})" for a in alarms]
        await update.message.reply_text("⏰ <b>Saved Alarms:</b>\n\n" + "\n".join(lines), parse_mode="HTML")
        return

    time_arg = context.args[0]
    label_arg = " ".join(context.args[1:]) if len(context.args) > 1 else "Wake Up Alarm"
    aid = lighthouse_core.add_alarm(time_str=time_arg, label=label_arg)
    await update.message.reply_text(
        f"⏰ <b>Alarm Set!</b>\n\n• <b>Time:</b> {html.escape(time_arg)}\n• <b>Label:</b> {html.escape(label_arg)}\n• <b>ID:</b> #{aid}",
        parse_mode="HTML"
    )


@require_auth
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Natural language message handler — full agent intent processing."""
    chat_id = update.effective_chat.id
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    raw_text = update.message.text.strip()

    # Parse intent via AI
    parsed = parse_full_agent_intent(raw_text)
    intent = parsed.get("intent", "chat")
    reply_text = parsed.get("reply", "")

    # --- Task Intents ---
    if intent == "add_task":
        content = parsed.get("content", "").strip()
        due_time = parsed.get("due_time")
        notes = parsed.get("notes", "") or ""
        if content:
            task_id = lighthouse_core.add_task(content, due_time=due_time, notes=notes)
            due_str = f" ⏰ <code>{html.escape(due_time)}</code>" if due_time else ""
            await update.message.reply_text(
                f"✅ <b>Task Added!</b>\n• <b>#{task_id}:</b> {html.escape(content)}{due_str}",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(reply_text or "What task would you like to add?")

    elif intent in ["edit_task", "rename_task"]:
        t_id = resolve_task_id(parsed.get("task_id"))
        new_content = parsed.get("content")
        new_due = parsed.get("due_time")
        new_notes = parsed.get("notes")
        if t_id:
            lighthouse_core.edit_task(t_id, content=new_content, due_time=new_due, notes=new_notes)
            await update.message.reply_text(reply_text or f"✏️ Task #{t_id} updated!", parse_mode="HTML")
        else:
            await update.message.reply_text("Which task should I update? Please specify the task number.")

    elif intent in ["complete_task"]:
        t_id = resolve_task_id(parsed.get("task_id"))
        if t_id and lighthouse_core.mark_done(t_id, 1):
            t = lighthouse_core.get_task_by_id(t_id)
            await update.message.reply_text(f"✅ <b>{html.escape(t[1] if t else 'Task')}</b> marked complete! 🎉", parse_mode="HTML")
        else:
            await update.message.reply_text("Which task number should I mark as done?")

    elif intent == "uncomplete_task":
        t_id = resolve_task_id(parsed.get("task_id"))
        if t_id:
            lighthouse_core.mark_done(t_id, 0)
            await update.message.reply_text(f"↩️ Task #{t_id} marked as pending again.", parse_mode="HTML")
        else:
            await update.message.reply_text("Which task should I unmark?")

    elif intent == "delete_task":
        t_id = resolve_task_id(parsed.get("task_id"))
        if t_id:
            t = lighthouse_core.get_task_by_id(t_id)
            desc = t[1] if t else ""
            # Ask for confirmation
            pending_confirmations[chat_id] = {"action": "delete_task", "id": t_id, "desc": desc}
            await update.message.reply_text(
                f"⚠️ Delete task #{t_id}: <b>{html.escape(desc)}</b>?\nReply <code>yes</code> to confirm.",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text("Which task number should I delete?")

    elif intent in ["list_tasks", "query_schedule"]:
        await list_cmd(update, context)

    # --- Note Intents ---
    elif intent == "add_note":
        note_title = parsed.get("note_title", "").strip() or "New Note"
        note_content = parsed.get("note_content", "").strip() or parsed.get("notes", "").strip()
        color = parsed.get("note_color", "default")
        is_pinned = parsed.get("is_pinned", False)
        if note_content:
            nid = lighthouse_core.add_note(note_title, note_content, color=color, is_pinned=is_pinned)
            await update.message.reply_text(f"📝 <b>Note #{nid} saved!</b>\n<b>{html.escape(note_title)}</b>\n{html.escape(note_content[:100])}", parse_mode="HTML")
        else:
            await update.message.reply_text("What should the note say?")

    elif intent == "list_notes":
        await notes_cmd(update, context)

    elif intent == "search_notes":
        query = parsed.get("search_query", "").lower()
        notes = lighthouse_core.get_notes(include_archived=False)
        matches = [n for n in notes if query in n["title"].lower() or query in n["content"].lower()]
        if matches:
            msg = f"🔍 <b>Search results for '{html.escape(query)}':</b>\n\n"
            for n in matches[:5]:
                msg += f"📄 <b>#{n['id']} {html.escape(n['title'] or 'Untitled')}</b>\n   {html.escape(n['content'][:80])}\n\n"
            await update.message.reply_text(msg, parse_mode="HTML")
        else:
            await update.message.reply_text(f"No notes found matching '{html.escape(query)}'.")

    elif intent == "delete_note":
        n_id = parsed.get("note_id")
        if n_id:
            pending_confirmations[chat_id] = {"action": "delete_note", "id": n_id, "desc": ""}
            await update.message.reply_text(f"⚠️ Delete note #{n_id}? Reply <code>yes</code> to confirm.", parse_mode="HTML")
        else:
            await update.message.reply_text("Which note ID should I delete?")

    # --- Timetable/Schedule Intents ---
    elif intent == "generate_timetable":
        wake = parsed.get("wake_time", "07:00 AM")
        bed = parsed.get("bed_time", "10:00 PM")
        subjects = parsed.get("subjects", ["Deep Focus Work"])
        await update.message.reply_text("⏳ <i>Generating your optimized timetable...</i>", parse_mode="HTML")
        tt_data = lighthouse_core.generate_timetable_ai(wake_time=wake, sleep_time=bed, subjects=subjects)
        t_id = lighthouse_core.save_timetable(title=f"Schedule {datetime.now().strftime('%b %d')}", slots=tt_data.get("slots", []))
        await update.message.reply_text(
            f"⏱️ <b>Timetable Generated & Saved!</b>\n#{t_id} — {len(tt_data.get('slots', []))} slots saved to your workspace.\n\nView it on your dashboard or type <code>/timetable</code>.",
            parse_mode="HTML"
        )

    elif intent == "create_essential_schedule":
        text = parsed.get("essential_text") or raw_text
        target_date = datetime.now().strftime("%Y-%m-%d")
        slots = lighthouse_core.parse_essential_schedule_text(text, target_date=target_date)
        t_id = lighthouse_core.save_timetable(title=f"Essential Schedule {datetime.now().strftime('%b %d')}", slots=slots, date_label=target_date)
        await update.message.reply_text(
            f"⚡ <b>Essential Schedule Created!</b>\n{len(slots)} slots saved. Type <code>/sync_schedule {t_id}</code> to push to Today's tasks.",
            parse_mode="HTML"
        )

    elif intent == "list_timetables":
        await schedules_cmd(update, context)

    elif intent == "rename_timetable":
        t_id = parsed.get("timetable_id")
        new_title = parsed.get("timetable_title", "")
        if t_id and new_title:
            lighthouse_core.rename_timetable(t_id, new_title)
            await update.message.reply_text(f"🏷️ Schedule #{t_id} renamed to <b>{html.escape(new_title)}</b>", parse_mode="HTML")
        else:
            await update.message.reply_text("Please provide the schedule ID and new name.")

    elif intent == "delete_timetable":
        t_id = parsed.get("timetable_id")
        if t_id:
            tt = lighthouse_core.get_timetable_by_id(t_id)
            desc = tt.get("title", "") if tt else ""
            pending_confirmations[chat_id] = {"action": "delete_timetable", "id": t_id, "desc": desc}
            await update.message.reply_text(f"⚠️ Delete schedule #{t_id}: <b>{html.escape(desc)}</b>?\nReply <code>yes</code> to confirm.", parse_mode="HTML")
        else:
            await update.message.reply_text("Which schedule ID should I delete?")

    elif intent == "sync_timetable":
        t_id = parsed.get("timetable_id")
        if t_id:
            context.args = [t_id]
            await sync_schedule_cmd(update, context)
        else:
            await update.message.reply_text("Which schedule ID should I sync to today?")

    # --- Roadmap Intents ---
    elif intent == "generate_roadmap":
        exam_date = parsed.get("exam_date") or (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        study_hours = parsed.get("study_hours", "4")
        subjects = parsed.get("subjects", ["Core Subjects"])
        syllabus = parsed.get("syllabus", "Comprehensive Syllabus")
        await update.message.reply_text("⏳ <i>Building your study roadmap...</i>", parse_mode="HTML")
        rm = lighthouse_core.generate_roadmap_ai(exam_date=exam_date, study_hours=study_hours, subjects=subjects, syllabus=syllabus)
        rid = lighthouse_core.save_roadmap(rm.get("examDate", exam_date), float(study_hours), subjects, syllabus, rm.get("phases", []))
        phases = rm.get("phases", [])
        phase_lines = "\n".join([f"• <b>{html.escape(p.get('name', 'Phase'))}</b> ({len(p.get('days', []))} days)" for p in phases[:4]])
        await update.message.reply_text(
            f"🗺️ <b>Study Roadmap Saved!</b>\n\n📅 <b>Target:</b> {exam_date}\n📚 <b>Phases:</b>\n{phase_lines}\n\nView full roadmap on your dashboard.",
            parse_mode="HTML"
        )

    elif intent == "get_roadmap":
        await roadmap_cmd(update, context)

    # --- PDF ---
    elif intent == "send_pdf":
        context.args = [parsed.get("pdf_target", "roadmap")]
        await pdf_cmd(update, context)

    # --- Default Chat ---
    else:
        if reply_text:
            await update.message.reply_text(reply_text, parse_mode="HTML")
        else:
            # AI general chat fallback
            now_str = datetime.now().strftime("%A, %B %d, %Y %I:%M %p")
            tasks = lighthouse_core.get_tasks()
            task_ctx = ", ".join([t[1] for t in tasks if not t[3]][:5]) or "No pending tasks"
            system_p = f"""You are LighthouseAI, Shlok's academic co-pilot. Current time: {now_str}.
Active goals: {task_ctx}. Be concise (2-4 sentences), direct, and motivating."""
            ai_reply = lighthouse_core.call_ai_model(raw_text, system_prompt=system_p)
            await update.message.reply_text(ai_reply or "Radhe Radhe, Shlok! How can I help with your schedule today?", parse_mode="HTML")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Photo handler — not available in serverless (photos too large for cold-start timeout)."""
    await update.message.reply_text(
        "📸 Photo analysis is not supported in this hosted version. "
        "Please use the web dashboard to upload images.",
        parse_mode="HTML"
    )


async def telegram_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, telegram.error.Conflict):
        log.warning("⚠️ Telegram 409 Conflict: Another bot instance is active.")
        return
    if isinstance(err, (telegram.error.NetworkError, telegram.error.TimedOut)):
        log.warning(f"⚠️ Telegram network timeout: {err}")
        return
    log.error(f"Telegram Exception: {err}", exc_info=err)


# =========================================================
# WEBHOOK PROCESSOR (called from api/telegram.py)
# =========================================================

_app = None


def _build_app():
    """Build and cache the Application instance (handlers registered once)."""
    global _app
    if _app is not None:
        return _app

    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN is not set.")

    _app = Application.builder().token(TELEGRAM_TOKEN).build()
    _app.add_error_handler(telegram_error_handler)

    _app.add_handler(CommandHandler("start", start_cmd))
    _app.add_handler(CommandHandler("help", help_cmd))
    _app.add_handler(CommandHandler("status", status_cmd))
    _app.add_handler(CommandHandler("device", status_cmd))
    _app.add_handler(CommandHandler("list", list_cmd))
    _app.add_handler(CommandHandler("done", done_cmd))
    _app.add_handler(CommandHandler("rename_task", rename_task_cmd))
    _app.add_handler(CommandHandler("delete_task", delete_task_cmd))
    _app.add_handler(CommandHandler("notes", notes_cmd))
    _app.add_handler(CommandHandler("roadmap", roadmap_cmd))
    _app.add_handler(CommandHandler("timetable", timetable_cmd))
    _app.add_handler(CommandHandler("schedules", schedules_cmd))
    _app.add_handler(CommandHandler("rename_schedule", rename_schedule_cmd))
    _app.add_handler(CommandHandler("delete_schedule", delete_schedule_cmd))
    _app.add_handler(CommandHandler("sync_schedule", sync_schedule_cmd))
    _app.add_handler(CommandHandler("alarm", alarm_cmd))
    _app.add_handler(CommandHandler("battery", battery_cmd))
    _app.add_handler(CommandHandler("pdf", pdf_cmd))
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    _app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    return _app


async def process_update(update_data: dict):
    """
    Entry point called by the Vercel webhook handler.
    Initializes the bot Application and processes a single Telegram update.
    """
    lighthouse_core.init_db()
    app = _build_app()

    # Initialize (sets up bot context without starting polling)
    async with app:
        update = Update.de_json(update_data, app.bot)
        await app.process_update(update)


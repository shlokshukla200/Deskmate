"""
LighthouseAI — Full Agent Telegram Engine.
Conversational co-pilot with deep bidirectional control across Tasks, Sticky Notes,
Saved Timetables, Study Roadmaps, and Server-Side PDF Exports.
"""

from functools import wraps

import os
import json
import logging
import html
from datetime import datetime, timedelta
import requests

from telegram import Update
import telegram.error
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import lighthouse_core

# =========================================================
# LOGGING & CONFIGURATION
# =========================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("lighthouse-agent")

lighthouse_core.load_env()
CONFIG = lighthouse_core.get_config()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN") or CONFIG.get("telegram_token", "")
raw_allowed_chat_id = os.environ.get("ALLOWED_CHAT_ID") or CONFIG.get("allowed_chat_id")
try:
    ALLOWED_CHAT_ID = int(raw_allowed_chat_id) if raw_allowed_chat_id is not None and str(raw_allowed_chat_id).strip() != "" else None
except (ValueError, TypeError):
    ALLOWED_CHAT_ID = None

# Pending confirmations dictionary: chat_id -> dict
pending_confirmations = {}


# =========================================================
# ACCESS CONTROL & SECURITY GUARD
# =========================================================

def require_auth(handler_func):
    """
    Authorization decorator restricting execution to ALLOWED_CHAT_ID.
    Unauthorized requests are silently ignored (no response sent).
    """
    @wraps(handler_func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not update or not update.effective_chat or ALLOWED_CHAT_ID is None or update.effective_chat.id != ALLOWED_CHAT_ID:
            sender_id = update.effective_chat.id if (update and update.effective_chat) else "unknown"
            log.warning(f"Unauthorized access attempt silently ignored from chat_id: {sender_id}")
            return  # silently ignore — no reply
        return await handler_func(update, context, *args, **kwargs)
    return wrapper


# =========================================================
# CONFLICT DETECTION HELPER
# =========================================================

def check_task_conflict(due_time_str: str) -> tuple | None:
    """Check if any active task is scheduled within +/- 45 minutes of due_time_str."""
    if not due_time_str:
        return None
    try:
        # Support formats: YYYY-MM-DDTHH:MM:SS or YYYY-MM-DD HH:MM
        clean_due = due_time_str.replace("T", " ")
        if len(clean_due) == 16:
            req_dt = datetime.strptime(clean_due, "%Y-%m-%d %H:%M")
        elif len(clean_due) >= 19:
            req_dt = datetime.strptime(clean_due[:19], "%Y-%m-%d %H:%M:%S")
        else:
            return None
    except Exception:
        return None

    tasks = lighthouse_core.get_pending_tasks()
    for t in tasks:
        due_str = t[2]
        if not due_str:
            continue
        try:
            clean_t = due_str.replace("T", " ")
            if len(clean_t) == 16:
                task_dt = datetime.strptime(clean_t, "%Y-%m-%d %H:%M")
            elif len(clean_t) >= 19:
                task_dt = datetime.strptime(clean_t[:19], "%Y-%m-%d %H:%M:%S")
            else:
                continue

            diff_mins = abs((req_dt - task_dt).total_seconds()) / 60
            if diff_mins <= 45:
                return (t[0], t[1], due_str)
        except Exception:
            continue
    return None


# =========================================================
# FULL-AGENT INTENT PARSER
# =========================================================

def parse_full_agent_intent(user_text: str) -> dict:
    """
    Conversational intent classification with full workspace context.
    Covers tasks, sticky notes, saved timetables, study roadmaps, and PDF exports.
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tasks = lighthouse_core.get_all_tasks()
    notes = lighthouse_core.get_notes(include_archived=False)
    timetables = lighthouse_core.get_saved_timetables()
    roadmap = lighthouse_core.get_saved_roadmap()

    # Context Summaries
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

Rules:
1. When user asks to rename or edit a task (e.g., 'rename task 3 to Calculus Revision'), extract task_id and new content.
2. When user asks to rename a schedule/timetable (e.g., 'rename schedule 1 to Exam Week'), extract timetable_id and timetable_title.
3. When user asks to delete a schedule/timetable (e.g., 'delete schedule 1'), extract timetable_id.
4. When user asks to sync a schedule/timetable to today (e.g., 'sync schedule 1 to today'), extract timetable_id.
5. If user gives essential tasks to put at specific times (e.g., 'Math at 9am-11am, DSA at 2pm-4pm, gym at 6pm'), use intent 'create_essential_schedule'.
6. If user asks to add/edit a note for a schedule slot, use intent 'add_slot_note'.
7. Output pure JSON without markdown backticks."""


    raw = lighthouse_core.call_ai_model(user_text, system_prompt=system_prompt, json_mode=True)
    if raw:
        try:
            parsed = json.loads(lighthouse_core.clean_json_str(raw))
            if isinstance(parsed, dict) and "intent" in parsed:
                return parsed
        except Exception as e:
            log.warning(f"Failed to parse LLM intent: {e}")

    # Fallback to chat / honest response
    return {
        "intent": "chat",
        "reply": f"I heard: '{user_text}'. Could you clarify what you'd like to update in your tasks, notes, or schedule?"
    }


# =========================================================
# TELEGRAM COMMAND HANDLERS
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
        "• <i>'Search notes for organic chemistry'</i>\n"
        "• <i>'Show my pinned notes'</i>\n\n"
        "⏱️ <b>Timetables & Roadmaps:</b>\n"
        "• <i>'Build a 6 hour study schedule from 7 AM to 11 PM'</i>\n"
        "• <i>'Create an exam roadmap for Nov 20 on Physics and Math'</i>\n"
        "• <i>'Send me my study roadmap as a PDF'</i>\n"
        "• <i>'PDF of today\\'s timetable'</i>",
        parse_mode="HTML"
    )


def resolve_task_id(val) -> int | None:
    """
    Resolve task identifier. Supports either direct DB id (e.g. 43)
    or today's sequence number (e.g. #1, #2, #3).
    """
    if val is None:
        return None
    try:
        clean_val = int(str(val).replace("#", "").strip())
    except (ValueError, TypeError):
        return None

    today_str = datetime.now().strftime("%Y-%m-%d")
    today_tasks = lighthouse_core.get_tasks(today_str)

    # 1. Direct DB id match
    t = lighthouse_core.get_task_by_id(clean_val)
    if t:
        return clean_val

    # 2. Today's sequence match (1-based index)
    if 1 <= clean_val <= len(today_tasks):
        return today_tasks[clean_val - 1][0]

    # 3. Pending sequence match (1-based index)
    pending = [tk for tk in today_tasks if not tk[3]]
    if 1 <= clean_val <= len(pending):
        return pending[clean_val - 1][0]

    return clean_val


@require_auth
async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_tasks = lighthouse_core.get_tasks(today_str)

    if not today_tasks:
        await update.message.reply_text(
            "✨ <b>Your schedule is clear!</b> No tasks found for today.",
            parse_mode="HTML"
        )
        return

    msg = f"📋 <b>Today's Schedule & Tasks ({len(today_tasks)} total):</b>\n\n"
    for idx, t in enumerate(today_tasks, 1):
        tid = t[0]
        content = t[1]
        due_time = t[2]
        done = bool(t[3])
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
        await update.message.reply_text(
            "Usage: <code>/done &lt;task_number&gt;</code>\nExample: <code>/done 1</code>",
            parse_mode="HTML"
        )
        return

    raw_arg = context.args[0]
    task_id = resolve_task_id(raw_arg)
    if task_id and lighthouse_core.mark_done(task_id, 1):
        t_data = lighthouse_core.get_task_by_id(task_id)
        t_title = t_data[1] if t_data else "Task"
        await update.message.reply_text(
            f"✅ <b>Task marked complete!</b>\n• <b>{html.escape(t_title)}</b> ⚡",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            f"Task #{raw_arg} not found or already completed."
        )


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
    msg += "\n\n<i>Commands: /schedules, /rename_schedule <id> <name>, /delete_schedule <id>, /sync_schedule <id></i>"
    await update.message.reply_text(msg, parse_mode="HTML")


@require_auth
async def schedules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    tts = lighthouse_core.get_saved_timetables()
    if not tts:
        await update.message.reply_text("⏱️ No saved schedules found. Tell me to generate or build one!", parse_mode="HTML")
        return

    msg = "⏱️ <b>Your Saved Schedules:</b>\n\n"
    for idx, t in enumerate(tts, 1):
        t_id = t.get("id")
        title = html.escape(t.get("title", "Protocol"))
        slots_count = len(t.get("slots", []))
        date_str = html.escape(t.get("date", ""))
        msg += f"<b>{idx}. #{t_id} — {title}</b>\n"
        msg += f"   ↳ {slots_count} slots | Date: {date_str}\n\n"

    msg += "<i>Commands:</i>\n"
    msg += "• <code>/rename_schedule &lt;id&gt; &lt;new_name&gt;</code>\n"
    msg += "• <code>/delete_schedule &lt;id&gt;</code>\n"
    msg += "• <code>/sync_schedule &lt;id&gt;</code>\n"
    await update.message.reply_text(msg, parse_mode="HTML")


@require_auth
async def rename_schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    if len(context.args) < 2:
        await update.message.reply_text("Usage: <code>/rename_schedule &lt;id&gt; &lt;new_name&gt;</code>\nExample: <code>/rename_schedule proto_123 Weekend Grind</code>", parse_mode="HTML")
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
    await update.message.reply_text(f"🗑️ <b>Deleted Schedule #{t_id}</b> and removed its synced slots from Today's Schedule.", parse_mode="HTML")


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
        await update.message.reply_text("Usage: <code>/rename_task &lt;task_number&gt; &lt;new_title&gt;</code>\nExample: <code>/rename_task 1 Calculus Revision</code>", parse_mode="HTML")
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
        await update.message.reply_text("Usage: <code>/delete_task &lt;task_number&gt;</code>\nExample: <code>/delete_task 1</code>", parse_mode="HTML")
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
            # Auto-generate a starter roadmap if empty
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


# =========================================================
# NATURAL LANGUAGE MESSAGE HANDLER (FULL AGENT)
# =========================================================

@require_auth
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    raw_text = update.message.text.strip()
    lower_text = raw_text.lower()

    # 1. Check Pending Destructive Confirmations
    if chat_id in pending_confirmations:
        pending = pending_confirmations[chat_id]
        if lower_text in ["yes", "y", "confirm", "proceed", "delete it", "do it"]:
            p_action = pending.get("action")
            p_id = pending.get("id")
            p_desc = pending.get("desc", "")
            del pending_confirmations[chat_id]

            if p_action == "delete_task":
                if lighthouse_core.delete_task(p_id):
                    await update.message.reply_text(f"🗑️ Deleted task #<b>{p_id}</b> (<i>{html.escape(p_desc)}</i>).", parse_mode="HTML")
                else:
                    await update.message.reply_text(f"Task #{p_id} could not be deleted or was already removed.")
                return

            elif p_action == "delete_note":
                if lighthouse_core.delete_note(p_id):
                    await update.message.reply_text(f"🗑️ Deleted note #<b>{p_id}</b>.", parse_mode="HTML")
                else:
                    await update.message.reply_text(f"Note #{p_id} could not be deleted or was already removed.")
                return

            elif p_action == "delete_timetable":
                lighthouse_core.delete_timetable(p_id)
                await update.message.reply_text(f"🗑️ <b>Deleted Schedule #{p_id}</b> (<i>{html.escape(p_desc)}</i>) and cleared all its synced slots from Today's Schedule.", parse_mode="HTML")
                return

        elif lower_text in ["no", "n", "cancel", "stop", "nevermind"]:
            del pending_confirmations[chat_id]
            await update.message.reply_text("❌ Action cancelled. Your data remains unchanged.")
            return


    # Quick device/battery/charging query check
    if any(q in lower_text for q in ["battery", "device status", "is it charging", "is laptop charging", "is device charging", "charge status", "device battery", "power status"]):
        await status_cmd(update, context)
        return

    # 2. Parse User Intent with Full Workspace Context
    intent_data = parse_full_agent_intent(raw_text)
    intent = intent_data.get("intent", "chat")
    reply_text = intent_data.get("reply", "")

    # -----------------------------------------------------
    # INTENT: ADD TASK
    # -----------------------------------------------------
    if intent == "add_task":
        content = intent_data.get("content") or raw_text
        due_time = intent_data.get("due_time")
        notes = intent_data.get("notes") or ""

        conflict = check_task_conflict(due_time) if due_time else None
        tid = lighthouse_core.add_task(content=content, due_time=due_time, notes=notes)

        # Calculate user-friendly today sequence count
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_tasks = lighthouse_core.get_tasks(today_str)
        today_seq = len(today_tasks)
        for i, tk in enumerate(today_tasks, 1):
            if tk[0] == tid:
                today_seq = i
                break

        if conflict:
            c_id, c_name, c_time = conflict
            await update.message.reply_text(
                f"⚠️ <b>Schedule Overlap Detected:</b>\n"
                f"You already have <i>'{html.escape(c_name)}'</i> scheduled around that time (<code>{html.escape(c_time)}</code>).\n\n"
                f"I've added <b>Task #{today_seq} for today</b> (ID: #{tid}): <i>'{html.escape(content)}'</i> for <code>{html.escape(due_time)}</code>.\n"
                f"Would you like me to move or adjust the earlier task?",
                parse_mode="HTML"
            )
        else:
            time_str = f" for <code>{html.escape(due_time)}</code>" if due_time else ""
            notes_str = f"\n↳ <i>Note synced: {html.escape(notes)}</i>" if notes else ""
            await update.message.reply_text(
                f"✅ Added <b>Task #{today_seq} for today</b>: <b>{html.escape(content)}</b>{time_str}{notes_str}",
                parse_mode="HTML"
            )

    # -----------------------------------------------------
    # INTENT: EDIT TASK
    # -----------------------------------------------------
    elif intent == "edit_task":
        raw_tid = intent_data.get("task_id")
        tid = resolve_task_id(raw_tid) if raw_tid else None
        if not tid:
            matches = lighthouse_core.find_tasks_by_query(intent_data.get("content") or raw_text)
            if matches:
                tid = matches[0][0]

        if tid:
            content = intent_data.get("content")
            due_time = intent_data.get("due_time")
            notes = intent_data.get("notes")
            if lighthouse_core.edit_task(tid, content=content, due_time=due_time, notes=notes):
                updated_t = lighthouse_core.get_task_by_id(tid)
                due_info = f" (due: <code>{html.escape(updated_t[2])}</code>)" if updated_t[2] else ""
                await update.message.reply_text(
                    f"✏️ Updated task <b>#{tid}</b>: <b>{html.escape(updated_t[1])}</b>{due_info}",
                    parse_mode="HTML"
                )
            else:
                await update.message.reply_text(f"Could not find task #{tid} to edit.")
        else:
            await update.message.reply_text("Which task would you like to edit? Please mention the task number or title.")

    # -----------------------------------------------------
    # INTENT: DELETE TASK (WITH CONFIRMATION)
    # -----------------------------------------------------
    elif intent == "delete_task":
        raw_tid = intent_data.get("task_id")
        tid = resolve_task_id(raw_tid) if raw_tid else None
        if not tid:
            matches = lighthouse_core.find_tasks_by_query(intent_data.get("content") or raw_text)
            if matches:
                tid = matches[0][0]

        if tid:
            target_task = lighthouse_core.get_task_by_id(tid)
            if target_task:
                desc = target_task[1]
                pending_confirmations[chat_id] = {
                    "action": "delete_task",
                    "id": tid,
                    "desc": desc
                }
                await update.message.reply_text(
                    f"⚠️ <b>Confirm Deletion</b>\n\n"
                    f"Are you sure you want to delete task <b>#{tid}</b>: <i>'{html.escape(desc)}'</i>?\n\n"
                    f"Reply <b>yes</b> to confirm or <b>no</b> to cancel.",
                    parse_mode="HTML"
                )
            else:
                await update.message.reply_text(f"Task #{tid} not found.")
        else:
            await update.message.reply_text("Please specify the task ID or number you want to delete.")

    # -----------------------------------------------------
    # INTENT: COMPLETE / UNCOMPLETE TASK
    # -----------------------------------------------------
    elif intent in ["complete_task", "done"]:
        raw_tid = intent_data.get("task_id")
        tid = resolve_task_id(raw_tid) if raw_tid else None
        if not tid:
            matches = lighthouse_core.find_tasks_by_query(intent_data.get("content") or raw_text)
            if matches:
                tid = matches[0][0]

        if tid:
            if lighthouse_core.mark_done(tid, 1):
                t_obj = lighthouse_core.get_task_by_id(tid)
                t_name = t_obj[1] if t_obj else f"Task #{tid}"
                await update.message.reply_text(f"✅ Completed <b>{html.escape(t_name)}</b>! Outstanding work. ⚡", parse_mode="HTML")
            else:
                await update.message.reply_text(f"Could not find active task #{tid}.")
        else:
            await update.message.reply_text(reply_text or "Which task would you like to complete? Provide task number or title.")

    elif intent == "uncomplete_task":
        raw_tid = intent_data.get("task_id")
        tid = resolve_task_id(raw_tid) if raw_tid else None
        if tid and lighthouse_core.mark_done(tid, 0):
            await update.message.reply_text(f"🔄 Reopened task <b>#{tid}</b> as pending.", parse_mode="HTML")
        else:
            await update.message.reply_text("Task ID not found.")

    # -----------------------------------------------------
    # INTENT: LIST TASKS / QUERY SCHEDULE
    # -----------------------------------------------------
    elif intent in ["list_tasks", "query_schedule"]:
        await list_cmd(update, context)

    # -----------------------------------------------------
    # INTENT: STICKY NOTES (ADD / EDIT / DELETE / LIST / SEARCH)
    # -----------------------------------------------------
    elif intent == "add_note":
        title = intent_data.get("note_title") or "Note"
        content = intent_data.get("note_content") or intent_data.get("content") or raw_text
        color = intent_data.get("note_color") or "amber"
        pinned = bool(intent_data.get("is_pinned"))

        nid = lighthouse_core.add_note(title=title, content=content, color=color, is_pinned=pinned)
        await update.message.reply_text(
            f"📝 Created Sticky Note <b>#{nid}</b>: <b>{html.escape(title)}</b>\n"
            f"<i>{html.escape(content)}</i>",
            parse_mode="HTML"
        )

    elif intent == "delete_note":
        nid = intent_data.get("note_id")
        if nid:
            note = lighthouse_core.get_note_by_id(nid)
            if note:
                desc = note.get("title") or note.get("content")[:30]
                pending_confirmations[chat_id] = {
                    "action": "delete_note",
                    "id": nid,
                    "desc": desc
                }
                await update.message.reply_text(
                    f"⚠️ <b>Confirm Note Deletion</b>\n\n"
                    f"Are you sure you want to delete note <b>#{nid}</b>: <i>'{html.escape(desc)}'</i>?\n\n"
                    f"Reply <b>yes</b> to confirm or <b>no</b> to cancel.",
                    parse_mode="HTML"
                )
            else:
                await update.message.reply_text(f"Note #{nid} not found.")
        else:
            await update.message.reply_text("Please specify the note number to delete.")

    elif intent in ["list_notes", "search_notes"]:
        search = intent_data.get("search_query") or ""
        notes = lighthouse_core.get_notes(query=search, include_archived=False)
        if not notes:
            await update.message.reply_text(f"No notes found{' matching ' + html.escape(search) if search else ''}.")
            return

        msg = f"📝 <b>{'Search Results' if search else 'Your Sticky Notes'}:</b>\n\n"
        for n in notes[:8]:
            pin_icon = "📌 " if n["is_pinned"] else "📄 "
            title_str = html.escape(n["title"]) if n["title"] else "Untitled Note"
            snippet = html.escape(n["content"][:80])
            msg += f"{pin_icon}<b>#{n['id']} {title_str}</b>\n   {snippet}\n\n"
        await update.message.reply_text(msg, parse_mode="HTML")

    # -----------------------------------------------------
    # INTENT: STUDY ROADMAP
    # -----------------------------------------------------
    elif intent == "generate_roadmap":
        exam_date = intent_data.get("exam_date") or (datetime.now() + timedelta(days=45)).strftime("%Y-%m-%d")
        hours = intent_data.get("study_hours") or "5"
        subjects = intent_data.get("subjects") or ["Core Subjects"]
        syllabus = intent_data.get("syllabus") or "Comprehensive Syllabus"

        await update.message.reply_text("⚙️ <i>Designing optimized multi-phase study roadmap...</i>", parse_mode="HTML")
        rm_data = lighthouse_core.generate_roadmap_ai(exam_date, hours, subjects, syllabus)
        lighthouse_core.save_roadmap(exam_date, float(hours), subjects, syllabus, rm_data.get("phases", []))

        phases = rm_data.get("phases", [])
        msg = f"🗺️ <b>Generated Exam Roadmap (Target: {html.escape(exam_date)})</b>\n\n"
        for p in phases:
            msg += f"<b>{html.escape(p.get('name', 'Phase'))}</b> ({html.escape(p.get('duration', ''))})\n"
            msg += f"• <i>Target:</i> {html.escape(p.get('dailyTarget', ''))}\n"
            msg += f"• <i>Milestone:</i> {html.escape(p.get('milestone', ''))}\n\n"
        msg += "<i>Want this as a PDF document? Reply: 'Send roadmap PDF'</i>"
        await update.message.reply_text(msg, parse_mode="HTML")

    elif intent == "get_roadmap":
        await roadmap_cmd(update, context)

    # -----------------------------------------------------
    # INTENT: RENAME TASK
    # -----------------------------------------------------
    elif intent == "rename_task":
        raw_tid = intent_data.get("task_id")
        tid = resolve_task_id(raw_tid) if raw_tid else None
        content = intent_data.get("content") or intent_data.get("reply")
        if not tid:
            matches = lighthouse_core.find_tasks_by_query(raw_text)
            if matches:
                tid = matches[0][0]
        if tid and content:
            if lighthouse_core.edit_task(tid, content=content):
                await update.message.reply_text(f"✏️ Renamed task <b>#{tid}</b> to: <b>{html.escape(content)}</b>", parse_mode="HTML")
            else:
                await update.message.reply_text(f"Task #{tid} not found.")
        else:
            await update.message.reply_text("Please specify the task number and new title (e.g. <i>'rename task 2 to Physics problem set'</i>).", parse_mode="HTML")

    # -----------------------------------------------------
    # INTENT: ESSENTIAL / DIRECT SCHEDULE CREATION
    # -----------------------------------------------------
    elif intent == "create_essential_schedule":
        text_input = intent_data.get("essential_text") or raw_text
        target_date = datetime.now().strftime("%Y-%m-%d")
        await update.message.reply_text("⚡ <i>Building essential schedule protocol...</i>", parse_mode="HTML")
        slots = lighthouse_core.parse_essential_schedule_text(text_input, target_date=target_date)
        t_id = lighthouse_core.save_timetable(
            title=intent_data.get("timetable_title") or f"Essential Routine ({datetime.now().strftime('%b %d')})",
            date_label=target_date,
            slots=slots,
            form_data={"mode": "essential", "text": text_input}
        )

        # Sync to today tasks automatically
        today_str = datetime.now().strftime("%Y-%m-%d")
        for s in slots:
            time_part = s.get("time", "").split("-")[0].strip()
            due = s.get("iso_time") or (f"{today_str}T{time_part.replace(' ', '')}:00" if time_part else None)
            lighthouse_core.add_task(content=s.get("activity", "Focus Block"), due_time=due, notes=s.get("note", "") or f"Category: {s.get('type', 'study')}", timetable_id=t_id)

        msg = f"⚡ <b>Created & Synced Essential Protocol #{t_id}</b>\n\n"
        for s in slots[:8]:
            msg += f"• <code>{html.escape(s.get('time', ''))}</code> — <b>{html.escape(s.get('activity', ''))}</b>\n"
        if len(slots) > 8:
            msg += f"\n<i>...and {len(slots) - 8} more slots.</i>"
        msg += "\n\n<i>✅ All slots synced to Today's dynamic dashboard!</i>"
        await update.message.reply_text(msg, parse_mode="HTML")

    # -----------------------------------------------------
    # INTENT: RENAME TIMETABLE / SCHEDULE
    # -----------------------------------------------------
    elif intent == "rename_timetable":
        t_id = intent_data.get("timetable_id")
        new_title = intent_data.get("timetable_title")
        tts = lighthouse_core.get_saved_timetables()
        if not t_id and tts:
            t_id = tts[0]["id"]
        if t_id and new_title:
            lighthouse_core.rename_timetable(t_id, new_title)
            await update.message.reply_text(f"🏷️ <b>Renamed Schedule #{t_id}</b> to: <b>{html.escape(new_title)}</b>", parse_mode="HTML")
        else:
            await update.message.reply_text("Please specify the schedule ID and new name (e.g. <i>'rename schedule 1 to Exam Sprint'</i>).", parse_mode="HTML")

    # -----------------------------------------------------
    # INTENT: DELETE TIMETABLE / SCHEDULE
    # -----------------------------------------------------
    elif intent == "delete_timetable":
        t_id = intent_data.get("timetable_id")
        tts = lighthouse_core.get_saved_timetables()
        if not t_id and tts:
            t_id = tts[0]["id"]
        if t_id:
            tt = lighthouse_core.get_timetable_by_id(t_id)
            title = tt.get("title", f"Protocol #{t_id}") if tt else f"Protocol #{t_id}"
            pending_confirmations[chat_id] = {
                "action": "delete_timetable",
                "id": t_id,
                "desc": title
            }
            await update.message.reply_text(
                f"⚠️ <b>Confirm Schedule Deletion</b>\n\n"
                f"Are you sure you want to delete schedule <b>#{t_id}</b> (<i>{html.escape(title)}</i>) and remove its synced tasks from Today's Schedule?\n\n"
                f"Reply <b>yes</b> to confirm or <b>no</b> to cancel.",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text("Please specify which schedule ID to delete (e.g. <i>'delete schedule 1'</i>).", parse_mode="HTML")

    # -----------------------------------------------------
    # INTENT: SYNC TIMETABLE TO TODAY
    # -----------------------------------------------------
    elif intent == "sync_timetable":
        t_id = intent_data.get("timetable_id")
        tts = lighthouse_core.get_saved_timetables()
        target_tt = None
        if t_id:
            target_tt = lighthouse_core.get_timetable_by_id(t_id)
        elif tts:
            target_tt = tts[0]

        if target_tt:
            today_str = datetime.now().strftime("%Y-%m-%d")
            slots = target_tt.get("slots", [])
            for s in slots:
                time_part = s.get("time", "").split("-")[0].strip()
                due = s.get("iso_time") or (f"{today_str}T{time_part.replace(' ', '')}:00" if time_part else None)
                note_str = s.get("note", "") or f"Protocol slot | Category: {s.get('type', 'study')}"
                lighthouse_core.add_task(content=s.get("activity", "Study Slot"), due_time=due, notes=note_str, timetable_id=target_tt["id"])
            await update.message.reply_text(
                f"⚡ <b>Synced {len(slots)} slots from '{html.escape(target_tt.get('title', 'Schedule'))}' to Today's Dashboard!</b>",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text("No schedule found to sync. Tell me to generate one first!")

    # -----------------------------------------------------
    # INTENT: TIMETABLES (GENERATE / LIST)
    # -----------------------------------------------------
    elif intent == "generate_timetable":
        wake_time = intent_data.get("wake_time") or "07:00"
        bed_time = intent_data.get("bed_time") or "23:00"
        hours = intent_data.get("study_hours") or "5"
        subjects = intent_data.get("subjects") or ["Physics", "Mathematics"]
        sync_to_tasks = intent_data.get("sync_to_tasks", False)

        await update.message.reply_text("⏱️ <i>Generating structured study timetable...</i>", parse_mode="HTML")
        tt_data = lighthouse_core.generate_timetable_ai(wake_time, bed_time, hours, subjects)
        t_id = lighthouse_core.save_timetable(
            title=f"Schedule ({datetime.now().strftime('%b %d')})",
            date_label=datetime.now().strftime("%Y-%m-%d"),
            slots=tt_data.get("slots", [])
        )

        # If user asked to sync slots into their daily tasks:
        synced_count = 0
        if sync_to_tasks or "sync" in lower_text:
            today_str = datetime.now().strftime("%Y-%m-%d")
            for s in tt_data.get("slots", []):
                time_part = s.get("time", "").split("-")[0].strip()
                due = s.get("iso_time") or (f"{today_str}T{time_part.replace(' ', '')}:00" if time_part else None)
                lighthouse_core.add_task(content=s.get("activity", "Study Block"), due_time=due, notes=s.get("note", "") or f"Category: {s.get('type', 'study')}", timetable_id=t_id)
                synced_count += 1

        slots = tt_data.get("slots", [])
        msg = f"⏱️ <b>Generated Timetable #{t_id}</b>\n\n"
        for s in slots[:8]:
            msg += f"• <code>{html.escape(s.get('time', ''))}</code> — {html.escape(s.get('activity', ''))}\n"

        if synced_count > 0:
            msg += f"\n⚡ <i>Synced {synced_count} focus blocks directly into your tasks list!</i>"
        msg += "\n\n<i>Want this as a PDF document? Reply: 'Send timetable PDF'</i>"
        await update.message.reply_text(msg, parse_mode="HTML")

    elif intent in ["list_timetables", "get_timetable"]:
        await timetable_cmd(update, context)


    # -----------------------------------------------------
    # INTENT: SEND PDF EXPORT
    # -----------------------------------------------------
    elif intent == "send_pdf":
        target = intent_data.get("pdf_target") or ("roadmap" if "roadmap" in lower_text else "timetable")
        context.args = [target]
        await pdf_cmd(update, context)

    # -----------------------------------------------------
    # INTENT: GENERAL CHAT & ACADEMIC MENTOR
    # -----------------------------------------------------
    else:
        if reply_text:
            await update.message.reply_text(reply_text)
        else:
            mentor_prompt = f"You are LighthouseAI, an intelligent academic advisor for Shlok. Provide a concise, actionable answer.\n\nUser: {raw_text}"
            reply = lighthouse_core.call_ai_model(mentor_prompt)
            await update.message.reply_text(reply or "Radhe Radhe, Shlok! How can I assist with your study goals today?")


# =========================================================
# MULTIMODAL PHOTO & IMAGE HANDLER
# =========================================================

@require_auth
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Multimodal photo handler for roadmaps, syllabi, study schedules, and notes.
    Downloads the image and uses Gemini Vision to extract and save structured plans.
    Falls back gracefully to generative plan synthesis if vision parsing is ambiguous.
    """
    chat_id = update.effective_chat.id
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")

    if not update.message.photo:
        return

    await update.message.reply_chat_action("upload_document")
    status_msg = await update.message.reply_text("🔍 <i>Analyzing image with Lighthouse Vision Engine...</i>", parse_mode="HTML")

    try:
        photo = update.message.photo[-1]
        photo_file = await context.bot.get_file(photo.file_id)
        image_bytes = await photo_file.download_as_bytearray()
        caption = update.message.caption or ""
        lower_cap = caption.lower()

        prompt = f"""You are LighthouseAI Multimodal Vision Engine. Analyze this study plan image (schedule, syllabus, roadmap, exam routine, or handwritten note).
User Caption: {caption}

Extract the structured details into STRICT JSON format:
{{
  "type": "roadmap" | "timetable" | "task_list" | "note",
  "title": "<title>",
  "summary": "<overview>",
  "exam_date": "<YYYY-MM-DD or 30 days from now>",
  "study_hours": 4,
  "subjects": ["<Subject 1>", "<Subject 2>"],
  "syllabus": "<extracted syllabus>",
  "phases": [
    {{
      "name": "Phase 1: Foundation",
      "type": "foundation",
      "days": [
        {{
          "day": 1,
          "date": "Day 1",
          "title": "<Mission title>",
          "type": "foundation",
          "tasks": ["<Task 1>", "<Task 2>"],
          "tip": "<Study advice>"
        }}
      ]
    }}
  ],
  "slots": [
    {{
      "time": "08:00 AM - 09:30 AM",
      "startTime": "08:00 AM",
      "activity": "<Subject / Task>",
      "type": "study",
      "priority": "high",
      "note": ""
    }}
  ],
  "tasks": [
    {{
      "content": "<task name>",
      "due_time": "<YYYY-MM-DD HH:MM or null>",
      "notes": "<notes>"
    }}
  ],
  "note_text": "<text>"
}}
Return ONLY valid JSON."""

        raw_json = lighthouse_core.call_ai_vision(bytes(image_bytes), prompt, mime_type="image/jpeg")
        parsed = {}
        if raw_json:
            try:
                parsed = json.loads(lighthouse_core.clean_json_str(raw_json))
            except Exception as pe:
                log.warning(f"Could not parse raw vision JSON: {pe}")

        # If vision returned empty but caption indicates roadmap or timetable, generate intelligent plan
        if not parsed or (not parsed.get("phases") and not parsed.get("slots") and not parsed.get("tasks") and not parsed.get("note_text")):
            if any(w in lower_cap for w in ["roadmap", "exam", "syllabus", "subject", "chapter", "study plan"]):
                exam_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
                rm_data = lighthouse_core.generate_roadmap_ai(exam_date=exam_date, study_hours="5", subjects=["Core Subject Revision"], syllabus=caption or "Syllabus from Image")
                parsed = {
                    "type": "roadmap",
                    "exam_date": exam_date,
                    "study_hours": 5,
                    "subjects": ["Core Subject Revision"],
                    "syllabus": caption or "Extracted Study Plan",
                    "phases": rm_data.get("phases", [])
                }
            elif any(w in lower_cap for w in ["schedule", "timetable", "routine", "slot", "daily"]):
                tt_data = lighthouse_core.generate_timetable_ai()
                parsed = {
                    "type": "timetable",
                    "title": caption or "Daily Focus Schedule",
                    "slots": tt_data.get("slots", [])
                }

        plan_type = parsed.get("type", "roadmap" if any(w in lower_cap for w in ["roadmap", "exam"]) else "note")

        if plan_type == "roadmap" or (parsed.get("phases") and len(parsed["phases"]) > 0):
            exam_date = parsed.get("exam_date") or (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
            hours = float(parsed.get("study_hours", 4))
            subs = parsed.get("subjects", ["Exam Preparation"])
            syl = parsed.get("syllabus", caption or "Extracted from Image")
            phases = parsed.get("phases", [])
            rid = lighthouse_core.save_roadmap(exam_date, hours, subs, syl, phases)

            phase_preview = "\n".join([f"• <b>{html.escape(p.get('name', 'Phase'))}</b> ({len(p.get('days', []))} daily missions)" for p in phases[:3]])
            await status_msg.edit_text(
                f"✅ <b>Study Roadmap Saved to Workspace!</b>\n\n"
                f"🗺️ <b>Exam Date:</b> <code>{exam_date}</code>\n"
                f"📚 <b>Subjects:</b> {html.escape(', '.join(subs))}\n"
                f"📊 <b>Phases ({len(phases)}):</b>\n{phase_preview}\n\n"
                f"<i>Access and export anytime in your Lighthouse Web Console under Study Roadmap or type <code>/roadmap</code>.</i>",
                parse_mode="HTML"
            )

        elif plan_type == "timetable" or (parsed.get("slots") and len(parsed["slots"]) > 0):
            slots = parsed.get("slots", [])
            title = parsed.get("title") or (caption if caption else "Extracted Schedule")
            tid = lighthouse_core.save_timetable(title=title, slots=slots)
            today = datetime.now().strftime("%Y-%m-%d")
            for s in slots:
                due_time = s.get("iso_time") or (f"{today}T{s.get('startTime', '08:00 AM').replace(' ', '')}:00" if s.get("startTime") else None)
                lighthouse_core.add_task(s.get("activity", "Study Slot"), due_time=due_time, notes=s.get("note", f"Category: {s.get('type', 'study')}"), timetable_id=tid)

            await status_msg.edit_text(
                f"✅ <b>Schedule Saved & Synced!</b>\n\n"
                f"⏱️ <b>Title:</b> {html.escape(title)}\n"
                f"📋 <b>Slots:</b> {len(slots)} slots saved and synced to Today's Dynamic Schedule in your Console.",
                parse_mode="HTML"
            )

        elif parsed.get("tasks") and len(parsed["tasks"]) > 0:
            tasks = parsed.get("tasks", [])
            for t in tasks:
                lighthouse_core.add_task(t.get("content", "Task"), t.get("due_time"), t.get("notes", ""))
            await status_msg.edit_text(
                f"✅ <b>Tasks Extracted!</b>\nAdded {len(tasks)} tasks from your image to Today's List.",
                parse_mode="HTML"
            )

        else:
            note_content = parsed.get("note_text") or parsed.get("summary") or caption or "Image scan notes"
            nid = lighthouse_core.add_note(f"Scan {datetime.now().strftime('%b %d')}", note_content, color="teal")
            await status_msg.edit_text(
                f"✅ <b>Sticky Note Saved!</b>\nCreated note #{nid} in your Sticky Notes board:\n<i>{html.escape(note_content[:100])}</i>",
                parse_mode="HTML"
            )

    except Exception as e:
        log.error(f"Error handling photo: {e}", exc_info=True)
        await status_msg.edit_text(f"⚠️ Error parsing image: {html.escape(str(e))}")


@require_auth
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Full Device & Workspace Project Scope Status."""
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    
    # 1. Device Battery & Power
    b_info = lighthouse_core.get_battery_info()
    percent = b_info.get("percent", 100)
    is_charging = bool(b_info.get("is_charging", False))
    
    if is_charging:
        batt_display = f"⚡ <b>{percent}% — CHARGING (Plugged In / AC Power)</b>"
    else:
        batt_icon = "🔋" if percent > 40 else "🪫"
        batt_display = f"{batt_icon} <b>{percent}% — On Battery (Discharging)</b>"

    # 2. Alarms
    alarms = lighthouse_core.get_active_alarms()
    alarm_str = f"{len(alarms)} active" if alarms else "None"
    if alarms:
        alarm_str += f" ({', '.join([a['time'] for a in alarms[:3]])})"

    # 3. Tasks / Today's Schedule
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_tasks = lighthouse_core.get_tasks(today_str)
    done_count = sum(1 for t in today_tasks if t[3])
    total_tasks = len(today_tasks)

    # 4. Study Roadmap
    rm = lighthouse_core.get_saved_roadmap()
    rm_str = f"Target: {rm.get('examDate')} ({len(rm.get('phases', []))} Phases)" if rm else "None active"

    # 5. Saved Schedules
    tts = lighthouse_core.get_saved_timetables()
    tt_str = f"{len(tts)} saved protocols" if tts else "None saved"

    # 6. Sticky Notes
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
        f"<i>All devices and project modules are connected and bidirectional.</i>"
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
        warning_text = "\n\n🚨 <b>CRITICAL WARNING:</b> Battery is below 40% and you have active alarms. Your device may turn off before your alarm rings! Please connect your charger."
    elif percent < 50 and not is_charging:
        warning_text = "\n\n⚠️ <b>Notice:</b> Battery is below 50%. Please connect your charger soon."
    elif is_charging:
        warning_text = "\n\n⚡ <i>Device is actively charging. Safe for all scheduled alarms and deep focus sessions.</i>"

    alarm_str = ""
    if active_alarms:
        alarm_str = "\n\n⏰ <b>Active Alarms:</b>\n" + "\n".join([f"• <b>{a['time']}</b> — {html.escape(a['label'])}" for a in active_alarms])
    else:
        alarm_str = "\n\n⏰ <b>Active Alarms:</b> None"

    await update.message.reply_text(
        f"{status_icon} <b>Device Battery & Power Status</b>\n\n"
        f"• <b>Battery Level:</b> {percent}%\n"
        f"• <b>Power State:</b> {charging_text}"
        f"{alarm_str}"
        f"{warning_text}",
        parse_mode="HTML"
    )


@require_auth
async def alarm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    if not context.args:
        alarms = lighthouse_core.get_alarms()
        if not alarms:
            await update.message.reply_text("⏰ No alarms set yet.\n\nUsage: <code>/alarm &lt;time&gt; &lt;label&gt;</code>\nExample: <code>/alarm 06:30 AM Morning Wake Up</code>", parse_mode="HTML")
            return
        lines = []
        for a in alarms:
            status = "🟢 Enabled" if a.get("enabled") else "⚪ Disabled"
            lines.append(f"• [#{a['id']}] <b>{a['time']}</b> — {html.escape(a['label'])} ({status})")
        await update.message.reply_text("⏰ <b>Saved Alarms:</b>\n\n" + "\n".join(lines), parse_mode="HTML")
        return

    time_arg = context.args[0]
    label_arg = " ".join(context.args[1:]) if len(context.args) > 1 else "Wake Up Alarm"
    aid = lighthouse_core.add_alarm(time_str=time_arg, label=label_arg)
    await update.message.reply_text(
        f"⏰ <b>Alarm Set!</b>\n\n"
        f"• <b>Time:</b> {html.escape(time_arg)}\n"
        f"• <b>Label:</b> {html.escape(label_arg)}\n"
        f"• <b>ID:</b> #{aid}\n\n"
        f"Synced to your LighthouseAI workspace and device clock triggers.",
        parse_mode="HTML"
    )


# =========================================================
# BACKGROUND REMINDER & BATTERY DAEMON
# =========================================================

async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    lighthouse_core.record_heartbeat(context.bot.username or "lighthouse_bot")
    if not ALLOWED_CHAT_ID:
        return

    # 1. Check Task Reminders
    due_items = lighthouse_core.get_due_unnotified()
    for task_id, content in due_items:
        try:
            await context.bot.send_message(
                chat_id=int(ALLOWED_CHAT_ID),
                text=f"⏰ <b>Scheduled Reminder</b>\n\n<b>#{task_id}</b>: {html.escape(content)}",
                parse_mode="HTML"
            )
            lighthouse_core.mark_notified(task_id)
        except Exception as e:
            log.warning(f"Failed to send reminder for task #{task_id}: {e}")

    # 2. Check Battery Alerts (<50% low battery & <40% with alarm)
    try:
        def send_tg_msg(text_msg):
            import asyncio
            asyncio.create_task(
                context.bot.send_message(
                    chat_id=int(ALLOWED_CHAT_ID),
                    text=text_msg,
                    parse_mode="HTML"
                )
            )

        lighthouse_core.check_and_send_battery_alerts(send_telegram_fn=send_tg_msg)
    except Exception as e:
        log.warning(f"Failed to check battery alerts in daemon: {e}")


async def telegram_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle uncaught errors and Telegram conflicts gracefully without crashing."""
    err = context.error
    if isinstance(err, telegram.error.Conflict):
        log.warning("⚠️ Telegram 409 Conflict: Another bot instance is currently active with this token! Terminate the other instance so this one can poll.")
        return
    if isinstance(err, (telegram.error.NetworkError, telegram.error.TimedOut)):
        log.warning(f"⚠️ Telegram network connection timeout: {err}")
        return
    log.error(f"Telegram Exception: {err}", exc_info=err)


# =========================================================
# APPLICATION ENTRYPOINT
# =========================================================

def main():
    lighthouse_core.init_db()
    if not TELEGRAM_TOKEN:
        log.warning("No TELEGRAM_TOKEN found in .env. Telegram bot daemon will not start.")
        return

    lighthouse_core.record_heartbeat("lighthouse_bot")
    app = Application.builder().token(TELEGRAM_TOKEN).connect_timeout(30).read_timeout(30).build()

    # Register Global Error Handler
    app.add_error_handler(telegram_error_handler)

    # Register Slash Commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("device", status_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("done", done_cmd))
    app.add_handler(CommandHandler("rename_task", rename_task_cmd))
    app.add_handler(CommandHandler("delete_task", delete_task_cmd))
    app.add_handler(CommandHandler("notes", notes_cmd))
    app.add_handler(CommandHandler("roadmap", roadmap_cmd))
    app.add_handler(CommandHandler("timetable", timetable_cmd))
    app.add_handler(CommandHandler("schedules", schedules_cmd))
    app.add_handler(CommandHandler("rename_schedule", rename_schedule_cmd))
    app.add_handler(CommandHandler("delete_schedule", delete_schedule_cmd))
    app.add_handler(CommandHandler("sync_schedule", sync_schedule_cmd))
    app.add_handler(CommandHandler("alarm", alarm_cmd))
    app.add_handler(CommandHandler("battery", battery_cmd))
    app.add_handler(CommandHandler("pdf", pdf_cmd))

    # Natural Language Conversational Message Handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Multimodal Photo & Image Handler
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Repeating Background Job for Reminders & Battery (every 15s)
    app.job_queue.run_repeating(check_reminders, interval=15, first=5)

    log.info("LighthouseAI Telegram Agent running with full workspace integration...")
    app.run_polling()


if __name__ == "__main__":
    main()


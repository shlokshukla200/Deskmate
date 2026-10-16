"""
LighthouseAI — Core Application Server.
Academic Command Center & Intelligent Daily Planner.
"""

import os
import json
import sqlite3
import re
from datetime import datetime, timedelta
import io
import requests
from flask import Flask, render_template, request, jsonify, send_file
import lighthouse_core

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "agent.db")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
ENV_PATH = os.path.join(BASE_DIR, ".env")


def load_env():
    """Load variables from .env if present without needing external packages."""
    if os.path.exists(ENV_PATH):
        try:
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ[k.strip()] = v.strip().strip('"').strip("'")
        except Exception:
            pass


load_env()

TEMPLATE_FOLDER = os.path.join(BASE_DIR, "templates") if os.path.exists(os.path.join(BASE_DIR, "templates")) else BASE_DIR
STATIC_FOLDER = os.path.join(BASE_DIR, "static")
app = Flask(__name__, template_folder=TEMPLATE_FOLDER, static_folder=STATIC_FOLDER)


@app.after_request
def add_header(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/favicon.ico")
def favicon():
    ico_path = os.path.join(STATIC_FOLDER, "favicon.ico")
    if os.path.exists(ico_path):
        return send_file(ico_path, mimetype="image/x-icon")
    return ("", 204)


def get_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            due_time TEXT,
            done INTEGER DEFAULT 0,
            notified INTEGER DEFAULT 0,
            created_at TEXT,
            notes TEXT DEFAULT '',
            pomodoro_count INTEGER DEFAULT 0
        )
    """)
    # Ensure notes & pomodoro columns exist
    c.execute("PRAGMA table_info(tasks)")
    task_cols = [row[1] for row in c.fetchall()]
    if 'notes' not in task_cols:
        try:
            c.execute("ALTER TABLE tasks ADD COLUMN notes TEXT DEFAULT ''")
        except Exception:
            pass
    if 'pomodoro_count' not in task_cols:
        try:
            c.execute("ALTER TABLE tasks ADD COLUMN pomodoro_count INTEGER DEFAULT 0")
        except Exception:
            pass
    if 'timetable_id' not in task_cols:
        try:
            c.execute("ALTER TABLE tasks ADD COLUMN timetable_id TEXT DEFAULT NULL")
        except Exception:
            pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS sticky_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT DEFAULT '',
            content TEXT NOT NULL,
            color TEXT DEFAULT 'default',
            bg_theme TEXT DEFAULT '',
            note_type TEXT DEFAULT 'text',
            checklist_items TEXT DEFAULT '[]',
            drawing_data TEXT DEFAULT '',
            task_id INTEGER DEFAULT NULL,
            is_pinned INTEGER DEFAULT 0,
            is_archived INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        )
    """)

    # Ensure all new columns exist on sticky_notes
    c.execute("PRAGMA table_info(sticky_notes)")
    note_cols = [row[1] for row in c.fetchall()]
    for col, col_def in [
        ('bg_theme', "TEXT DEFAULT ''"),
        ('note_type', "TEXT DEFAULT 'text'"),
        ('checklist_items', "TEXT DEFAULT '[]'"),
        ('drawing_data', "TEXT DEFAULT ''"),
        ('task_id', "INTEGER DEFAULT NULL"),
        ('is_archived', "INTEGER DEFAULT 0")
    ]:
        if col not in note_cols:
            try:
                c.execute(f"ALTER TABLE sticky_notes ADD COLUMN {col} {col_def}")
            except Exception:
                pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS saved_timetables (
            id TEXT PRIMARY KEY,
            title TEXT,
            date_label TEXT,
            slots_json TEXT,
            form_json TEXT,
            created_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS saved_roadmaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_date TEXT,
            study_hours REAL,
            subjects_json TEXT,
            syllabus TEXT,
            phases_json TEXT NOT NULL,
            created_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS bot_heartbeat (
            id INTEGER PRIMARY KEY,
            last_seen TEXT,
            bot_username TEXT,
            status TEXT
        )
    """)
    conn.commit()
    conn.close()


def get_tasks(date_str: str | None = None):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if date_str:
        c.execute(
            "SELECT id, content, due_time, done, created_at, COALESCE(notes, ''), COALESCE(pomodoro_count, 0), timetable_id FROM tasks "
            "WHERE (due_time LIKE ? OR (due_time IS NULL AND created_at LIKE ?)) "
            "ORDER BY done ASC, due_time IS NULL, due_time ASC",
            (f"{date_str}%", f"{date_str}%")
        )
    else:
        today = datetime.now().strftime("%Y-%m-%d")
        c.execute(
            "SELECT id, content, due_time, done, created_at, COALESCE(notes, ''), COALESCE(pomodoro_count, 0), timetable_id FROM tasks "
            "WHERE done = 0 OR due_time LIKE ? OR created_at LIKE ? "
            "ORDER BY done ASC, due_time IS NULL, due_time ASC",
            (f"{today}%", f"{today}%")
        )
    rows = c.fetchall()
    conn.close()
    return rows


def add_task(content: str, due_time: str | None = None, notes: str = "", timetable_id: str | None = None):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now_iso = datetime.now().isoformat()
    c.execute(
        "INSERT INTO tasks (content, due_time, done, notified, created_at, notes, pomodoro_count, timetable_id) VALUES (?, ?, 0, 0, ?, ?, 0, ?)",
        (content, due_time, now_iso, notes, timetable_id)
    )
    conn.commit()
    task_id = c.lastrowid

    # If notes were provided, also create a synced sticky note
    if notes.strip():
        c.execute(
            "INSERT INTO sticky_notes (title, content, color, bg_theme, note_type, checklist_items, drawing_data, task_id, is_pinned, is_archived, created_at, updated_at) "
            "VALUES (?, ?, 'amber', 'noodles', 'task', '[]', '', ?, 0, 0, ?, ?)",
            (f"Task: {content}", notes, task_id, now_iso, now_iso)
        )
        conn.commit()

    conn.close()
    return task_id


def mark_done(task_id: int, done_val: int = 1):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE tasks SET done = ? WHERE id = ?", (done_val, task_id))
    conn.commit()
    conn.close()
    return True


def delete_task(task_id: int):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    c.execute("DELETE FROM sticky_notes WHERE task_id = ?", (task_id,))
    conn.commit()
    conn.close()
    return True


def update_task_note(task_id: int, notes: str):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE tasks SET notes = ? WHERE id = ?", (notes, task_id))
    c.execute("SELECT content FROM tasks WHERE id = ?", (task_id,))
    row = c.fetchone()
    task_content = row[0] if row else "Task"

    now_iso = datetime.now().isoformat()
    # Check if a sticky note already exists for this task
    c.execute("SELECT id FROM sticky_notes WHERE task_id = ?", (task_id,))
    existing = c.fetchone()
    if existing:
        if notes.strip():
            c.execute("UPDATE sticky_notes SET content = ?, title = ?, updated_at = ? WHERE id = ?",
                      (notes, f"Task: {task_content}", now_iso, existing[0]))
        else:
            c.execute("DELETE FROM sticky_notes WHERE id = ?", (existing[0],))
    elif notes.strip():
        c.execute(
            "INSERT INTO sticky_notes (title, content, color, bg_theme, note_type, checklist_items, drawing_data, task_id, is_pinned, is_archived, created_at, updated_at) "
            "VALUES (?, ?, 'amber', 'noodles', 'task', '[]', '', ?, 0, 0, ?, ?)",
            (f"Task: {task_content}", notes, task_id, now_iso, now_iso)
        )
    conn.commit()
    conn.close()
    return True


# =========================================================
# AI MODEL ENGINE (GEMINI + GROQ)
# =========================================================

def call_ai_model(prompt: str, system_prompt: str = "", json_mode: bool = False, feature: str = "mentor") -> str | None:
    """Delegates to lighthouse_core.call_ai_model for multi-project key isolation, rotation, and fallback."""
    return lighthouse_core.call_ai_model(prompt=prompt, system_prompt=system_prompt, json_mode=json_mode, feature=feature)



def clean_json_str(raw: str) -> str:
    """Strip code block wrappers from JSON string."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


# =========================================================
# ROUTES
# =========================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    """Check live Telegram bot heartbeat, database status, and AI config."""
    init_db()
    load_env()
    config = get_config()
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT last_seen, bot_username, status FROM bot_heartbeat WHERE id = 1")
    hb = c.fetchone()
    conn.close()

    telegram_connected = False
    bot_username = None
    last_seen_str = None

    if hb:
        last_seen_str = hb[0]
        bot_username = hb[1]
        try:
            last_dt = datetime.fromisoformat(last_seen_str)
            # If heartbeat occurred within the last 45 seconds, the agent is actively polling!
            if (datetime.now() - last_dt).total_seconds() <= 45:
                telegram_connected = True
        except Exception:
            pass

    tasks = get_tasks()
    ai_available = bool(os.environ.get("GEMINI_API_KEY") or config.get("gemini_api_key") or config.get("groq_api_key"))

    return jsonify({
        "telegram_connected": telegram_connected,
        "bot_username": bot_username or "lighthouse_bot",
        "last_heartbeat": last_seen_str,
        "tasks_pending": len([t for t in tasks if not t[3]]),
        "ai_configured": ai_available
    })


@app.route("/api/tasks", methods=["GET", "POST"])
def api_tasks():
    if request.method == "POST":
        data = request.get_json() or {}
        content = data.get("content", "").strip()
        due_time = data.get("due_time")
        notes = data.get("notes", "").strip()
        if not content:
            return jsonify({"error": "Content required"}), 400
        task_id = add_task(content, due_time, notes)
        return jsonify({"success": True, "id": task_id})

    date_str = request.args.get("date")
    tasks = get_tasks(date_str)
    return jsonify([
        {
            "id": t[0],
            "content": t[1],
            "due_time": t[2],
            "done": bool(t[3]),
            "created_at": t[4],
            "notes": t[5] if len(t) > 5 else "",
            "pomodoro_count": t[6] if len(t) > 6 else 0
        } for t in tasks
    ])


@app.route("/api/tasks/<int:task_id>", methods=["PUT", "PATCH"])
@app.route("/api/tasks/<int:task_id>/edit", methods=["POST", "PUT"])
def api_edit_task(task_id):
    data = request.get_json() or {}
    content = data.get("content")
    due_time = data.get("due_time")
    notes = data.get("notes")
    success = lighthouse_core.edit_task(task_id, content=content, due_time=due_time, notes=notes)
    return jsonify({"success": success})


@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
@app.route("/api/tasks/<int:task_id>/delete", methods=["POST", "DELETE"])
def api_delete_task(task_id):
    success = delete_task(task_id)
    return jsonify({"success": success})


@app.route("/api/tasks/<int:task_id>/note", methods=["POST", "PUT"])
def api_update_task_note(task_id):
    data = request.get_json() or {}
    notes = data.get("notes", "")
    success = update_task_note(task_id, notes)
    return jsonify({"success": success})



@app.route("/api/tasks/<int:task_id>/pomodoro", methods=["POST"])
def api_task_pomodoro(task_id):
    """Increment the completed pomodoro session count on a task."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE tasks SET pomodoro_count = COALESCE(pomodoro_count, 0) + 1 WHERE id = ?", (task_id,))
    conn.commit()
    c.execute("SELECT pomodoro_count FROM tasks WHERE id = ?", (task_id,))
    row = c.fetchone()
    count = row[0] if row else 1
    conn.close()
    return jsonify({"success": True, "pomodoro_count": count})


@app.route("/api/tasks/batch", methods=["POST"])
def api_tasks_batch():
    """Add multiple tasks at once (used by Build My Schedule)."""
    data = request.get_json() or {}
    tasks = data.get("tasks", [])
    timetable_id = data.get("timetable_id")
    added_ids = []
    for item in tasks:
        content = item.get("content", "").strip()
        due_time = item.get("due_time")
        notes = item.get("notes", "").strip()
        tid_param = item.get("timetable_id") or timetable_id
        if content:
            tid = add_task(content, due_time, notes, tid_param)
            added_ids.append(tid)
    return jsonify({"success": True, "added_count": len(added_ids), "ids": added_ids})


@app.route("/api/tasks/<int:task_id>/done", methods=["POST"])
@app.route("/api/done/<int:task_id>", methods=["POST"])
def api_done(task_id):
    data = request.get_json(silent=True) or {}
    done_val = 0 if (data.get("undo") or data.get("done") is False) else 1
    success = mark_done(task_id, done_val)
    return jsonify({"success": success})


# =========================================================
# GOOGLE KEEP STYLE STICKY NOTES API (WITH THEMES, ARCHIVE & SEARCH)
# =========================================================

@app.route("/api/notes", methods=["GET", "POST"])
def api_notes():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if request.method == "POST":
        data = request.get_json() or {}
        title = data.get("title", "").strip()
        content = data.get("content", "").strip()
        color = data.get("color", "default")
        bg_theme = data.get("bg_theme", "")
        note_type = data.get("note_type", "text")
        checklist_items = json.dumps(data.get("checklist_items", []))
        drawing_data = data.get("drawing_data", "")
        is_pinned = 1 if data.get("is_pinned") else 0
        is_archived = 1 if data.get("is_archived") else 0

        if not content and not drawing_data and not data.get("checklist_items"):
            conn.close()
            return jsonify({"error": "Content is required"}), 400

        now_iso = datetime.now().isoformat()
        c.execute(
            "INSERT INTO sticky_notes (title, content, color, bg_theme, note_type, checklist_items, drawing_data, task_id, is_pinned, is_archived, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
            (title, content, color, bg_theme, note_type, checklist_items, drawing_data, is_pinned, is_archived, now_iso, now_iso)
        )
        conn.commit()
        note_id = c.lastrowid
        conn.close()
        return jsonify({"success": True, "id": note_id})

    # Ensure any tasks with notes that aren't yet in sticky_notes get synced
    c.execute("SELECT id, content, COALESCE(notes, '') FROM tasks WHERE notes != '' AND notes IS NOT NULL")
    task_notes = c.fetchall()
    for tid, tcontent, tnotes in task_notes:
        c.execute("SELECT id FROM sticky_notes WHERE task_id = ?", (tid,))
        if not c.fetchone() and tnotes.strip():
            now_iso = datetime.now().isoformat()
            c.execute(
                "INSERT INTO sticky_notes (title, content, color, bg_theme, note_type, checklist_items, drawing_data, task_id, is_pinned, is_archived, created_at, updated_at) "
                "VALUES (?, ?, 'amber', 'noodles', 'task', '[]', '', ?, 0, 0, ?, ?)",
                (f"Task: {tcontent}", tnotes, tid, now_iso, now_iso)
            )
            conn.commit()

    query = request.args.get("q", "").strip().lower()
    include_archived = request.args.get("archived") == "1"

    c.execute(
        "SELECT id, title, content, color, COALESCE(bg_theme, ''), COALESCE(note_type, 'text'), "
        "COALESCE(checklist_items, '[]'), COALESCE(drawing_data, ''), task_id, is_pinned, is_archived, created_at, updated_at "
        "FROM sticky_notes ORDER BY is_pinned DESC, id DESC"
    )
    rows = c.fetchall()
    conn.close()

    result = []
    for r in rows:
        is_archived = bool(r[10])
        if is_archived and not include_archived:
            continue

        try:
            cl_items = json.loads(r[6]) if r[6] else []
        except Exception:
            cl_items = []

        title = r[1]
        content = r[2]

        if query:
            if query not in title.lower() and query not in content.lower():
                # Check checklist items
                matches_item = any(query in item.get('text', '').lower() for item in cl_items)
                if not matches_item:
                    continue

        result.append({
            "id": r[0],
            "title": title,
            "content": content,
            "color": r[3],
            "bg_theme": r[4],
            "note_type": r[5],
            "checklist_items": cl_items,
            "drawing_data": r[7],
            "task_id": r[8],
            "is_pinned": bool(r[9]),
            "is_archived": is_archived,
            "created_at": r[11],
            "updated_at": r[12]
        })

    return jsonify(result)


@app.route("/api/notes/<int:note_id>", methods=["PUT", "DELETE"])
def api_note_detail(note_id):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if request.method == "DELETE":
        c.execute("SELECT task_id FROM sticky_notes WHERE id = ?", (note_id,))
        row = c.fetchone()
        if row and row[0]:
            # Clear note in tasks table
            c.execute("UPDATE tasks SET notes = '' WHERE id = ?", (row[0],))
        c.execute("DELETE FROM sticky_notes WHERE id = ?", (note_id,))
        conn.commit()
        conn.close()
        return jsonify({"success": True})

    data = request.get_json() or {}
    title = data.get("title", "").strip()
    content = data.get("content", "").strip()
    color = data.get("color", "default")
    bg_theme = data.get("bg_theme", "")
    note_type = data.get("note_type", "text")
    checklist_items = json.dumps(data.get("checklist_items", []))
    drawing_data = data.get("drawing_data", "")
    is_pinned = 1 if data.get("is_pinned") else 0
    is_archived = 1 if data.get("is_archived") else 0
    now_iso = datetime.now().isoformat()

    c.execute("SELECT task_id FROM sticky_notes WHERE id = ?", (note_id,))
    row = c.fetchone()
    if row and row[0]:
        c.execute("UPDATE tasks SET notes = ? WHERE id = ?", (content, row[0]))

    c.execute(
        "UPDATE sticky_notes SET title = ?, content = ?, color = ?, bg_theme = ?, note_type = ?, "
        "checklist_items = ?, drawing_data = ?, is_pinned = ?, is_archived = ?, updated_at = ? WHERE id = ?",
        (title, content, color, bg_theme, note_type, checklist_items, drawing_data, is_pinned, is_archived, now_iso, note_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True})


# =========================================================
# SAVED TIMETABLES API
# =========================================================

@app.route("/api/timetables", methods=["GET", "POST"])
def api_timetables():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if request.method == "POST":
        data = request.get_json() or {}
        t_id = str(data.get("id") or int(datetime.now().timestamp() * 1000))
        title = data.get("title") or f"Protocol {t_id[-4:]}"
        date_label = data.get("date_label") or datetime.now().strftime("%Y-%m-%d")
        slots = data.get("slots", [])
        form_data = data.get("formData") or data.get("form", {})

        now_iso = datetime.now().isoformat()
        c.execute(
            "INSERT OR REPLACE INTO saved_timetables (id, title, date_label, slots_json, form_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (t_id, title, date_label, json.dumps(slots), json.dumps(form_data), now_iso)
        )
        conn.commit()
        conn.close()
        return jsonify({"success": True, "id": t_id})

    c.execute("SELECT id, title, date_label, slots_json, form_json, created_at FROM saved_timetables ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()

    result = []
    for r in rows:
        try:
            slots = json.loads(r[3])
        except Exception:
            slots = []
        try:
            form_data = json.loads(r[4])
        except Exception:
            form_data = {}
        result.append({
            "id": r[0],
            "title": r[1],
            "date": r[2],
            "slots": slots,
            "formData": form_data,
            "created_at": r[5]
        })
    return jsonify(result)


@app.route("/api/timetables/<string:t_id>", methods=["GET", "PUT", "DELETE", "POST"])
@app.route("/api/timetables/<string:t_id>/delete", methods=["POST", "DELETE"])
def api_timetable_detail(t_id):
    if request.method in ["DELETE"] or request.path.endswith("/delete"):
        success = lighthouse_core.delete_timetable(t_id)
        return jsonify({"success": success})

    if request.method == "GET":
        tt = lighthouse_core.get_timetable_by_id(t_id)
        if not tt:
            return jsonify({"error": "Timetable not found"}), 404
        return jsonify(tt)

    data = request.get_json() or {}
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT title, date_label, slots_json, form_json FROM saved_timetables WHERE id = ?", (t_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Timetable not found"}), 404

    title = data.get("title", row[0])
    date_label = data.get("date_label", row[1])
    slots = data.get("slots", json.loads(row[2]) if row[2] else [])
    form_data = data.get("formData", json.loads(row[3]) if row[3] else {})

    c.execute(
        "UPDATE saved_timetables SET title = ?, date_label = ?, slots_json = ?, form_json = ? WHERE id = ?",
        (title, date_label, json.dumps(slots), json.dumps(form_data), t_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/timetables/<string:t_id>/rename", methods=["POST", "PUT"])
def api_rename_timetable(t_id):
    data = request.get_json() or {}
    new_title = data.get("title", "").strip()
    if not new_title:
        return jsonify({"error": "Title is required"}), 400
    success = lighthouse_core.rename_timetable(t_id, new_title)
    return jsonify({"success": success})


@app.route("/api/ai/essential-schedule", methods=["POST"])
def api_essential_schedule():
    """Build essential schedule directly from user prompt text or direct slots."""
    data = request.get_json() or {}
    raw_text = data.get("text", "").strip()
    direct_slots = data.get("slots", [])
    title = data.get("title") or "Essential Daily Schedule"
    target_date = data.get("targetDate") or datetime.now().strftime("%Y-%m-%d")

    if direct_slots and isinstance(direct_slots, list) and len(direct_slots) > 0:
        slots = direct_slots
    elif raw_text:
        slots = lighthouse_core.parse_essential_schedule_text(raw_text, target_date=target_date)
    else:
        return jsonify({"error": "Either text or slots is required"}), 400

    t_id = lighthouse_core.save_timetable(
        title=title,
        date_label=target_date,
        slots=slots,
        form_data={"mode": "essential", "text": raw_text}
    )
    return jsonify({
        "success": True,
        "id": t_id,
        "title": title,
        "slots": slots
    })



# =========================================================
# SAVED ROADMAPS API
# =========================================================

@app.route("/api/roadmaps", methods=["GET", "POST"])
def api_roadmaps():
    if request.method == "POST":
        data = request.get_json() or {}
        exam_date = data.get("examDate", "")
        study_hours = float(data.get("studyHours", 4))
        subjects = data.get("subjects", [])
        syllabus = data.get("syllabus", "")
        phases = data.get("phases", [])
        rid = lighthouse_core.save_roadmap(exam_date, study_hours, subjects, syllabus, phases)
        return jsonify({"success": True, "id": rid})

    return jsonify(lighthouse_core.get_all_roadmaps())


@app.route("/api/roadmaps/<int:rid>", methods=["GET", "DELETE"])
def api_roadmap_detail(rid):
    if request.method == "DELETE":
        success = lighthouse_core.delete_roadmap(rid)
        return jsonify({"success": success})
    rm = lighthouse_core.get_roadmap_by_id(rid)
    if not rm:
        return jsonify({"error": "Roadmap not found"}), 404
    return jsonify(rm)


@app.route("/api/pdf/timetable/<string:t_id>", methods=["GET"])
def api_pdf_timetable(t_id):
    tt = lighthouse_core.get_timetable_by_id(t_id)
    if not tt:
        return jsonify({"error": "Timetable not found"}), 404
    pdf_bytes = lighthouse_core.generate_timetable_pdf(tt)
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"timetable_{t_id}.pdf"
    )


@app.route("/api/pdf/roadmap", methods=["GET"])
def api_pdf_roadmap():
    rm = lighthouse_core.get_saved_roadmap()
    if not rm:
        rm = lighthouse_core.generate_roadmap_ai(exam_date=(datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"))
        lighthouse_core.save_roadmap(rm.get("examDate"), 5, ["Core Subjects"], "Comprehensive Syllabus", rm.get("phases", []))
    pdf_bytes = lighthouse_core.generate_roadmap_pdf(rm)
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name="exam_roadmap.pdf"
    )


# =========================================================
# MENTOR CHAT
# =========================================================

@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json() or {}
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"error": "Message cannot be empty"}), 400

    tasks = get_tasks()
    task_context = "\n".join([
        f"- #{t[0]}: {t[1]} (due: {t[2] or 'Anytime'}, {'DONE' if t[3] else 'PENDING'}{f' | Notes: {t[5]}' if len(t) > 5 and t[5] else ''})"
        for t in tasks
    ]) or "No tasks currently scheduled."

    now_str = datetime.now().strftime("%A, %B %d, %Y %I:%M %p")
    system_prompt = f"""You are LighthouseAI, an intelligent academic co-pilot and daily planner for Shlok.
Current Date/Time: {now_str}
Shlok's Active Schedule & Tasks:
{task_context}

Be direct, structured, motivating, and concise. Provide actionable advice for high-performance studying."""

    reply = call_ai_model(message, system_prompt=system_prompt, feature="mentor")
    if reply:
        return jsonify({"reply": reply, "model_used": "Lighthouse Engine"})

    return jsonify({
        "reply": "Radhe Radhe! I am ready to guide you. Please verify your GEMINI_API_KEY is configured in your .env file.",
        "model_used": "Lighthouse Offline"
    })


# =========================================================
# LIGHTHOUSEAI: STUDY ROADMAP GENERATOR
# =========================================================

@app.route("/api/ai/roadmap", methods=["POST"])
def api_roadmap():
    """Study & Exam Roadmap Generator (LighthouseAI)."""
    data = request.get_json() or {}
    exam_date = data.get("examDate", "")
    study_hours = data.get("studyHours", "4")
    subjects = data.get("subjects", [])
    if isinstance(subjects, str):
        subjects = [s.strip() for s in subjects.split(",") if s.strip()]
    syllabus = data.get("syllabus", "Comprehensive Core Syllabus")
    intensity = data.get("intensity", "balanced")

    if not exam_date:
        # Default 30 days from now
        exam_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")

    try:
        exam_dt = datetime.strptime(exam_date, "%Y-%m-%d")
        days_left = max(1, (exam_dt - datetime.now()).days)
    except Exception:
        days_left = 30

    sub_str = ", ".join(subjects) if subjects else "Core Academic Disciplines"

    prompt = f"""Create a day-by-day study roadmap for a student.
Exam date: {exam_date} ({days_left} days from today)
Subjects: {sub_str}
Daily study hours: {study_hours} hours
Intensity: {intensity}
Syllabus/topics: {syllabus}

Return ONLY valid JSON, no markdown:
{{
  "totalDays": {days_left},
  "phases": [
    {{
      "name": "Foundation Phase",
      "type": "foundation",
      "days": [
        {{
          "day": 1,
          "date": "Day 1 • Orientation",
          "title": "Core Foundations & Theory",
          "type": "foundation",
          "tasks": ["Study core formulas & concepts (1.5 hrs)", "Solve practice drills (1.5 hrs)", "Flashcard recall (1 hr)"],
          "tip": "Begin with high-confidence topics to establish flow state.",
          "hours": {study_hours}
        }}
      ]
    }}
  ]
}}
Make it realistic with at least 3-4 structured phases covering the syllabus. Every 6-7 days include a revision/rest checkpoint."""

    system_prompt = "You are an expert academic curriculum strategist (LighthouseAI). Return strictly clean JSON with the requested schema."
    raw = call_ai_model(prompt, system_prompt=system_prompt, json_mode=True, feature="roadmap")
    if raw:
        try:
            parsed = json.loads(clean_json_str(raw))
            if "phases" in parsed:
                try:
                    num_hours = float(study_hours) if str(study_hours).replace('.', '', 1).isdigit() else 4.0
                    rid = lighthouse_core.save_roadmap(exam_date, num_hours, subjects, syllabus, parsed["phases"])
                    parsed["id"] = rid
                except Exception:
                    pass
                return jsonify(parsed)
        except Exception:
            pass

    # High-quality deterministic fallback
    sub1 = subjects[0] if subjects else "Core Subject"
    sub2 = subjects[1] if len(subjects) > 1 else "Advanced Applications"
    num_hours = float(study_hours) if str(study_hours).replace('.', '', 1).isdigit() else 4.0
    fallback_phases = [
        {
            "name": "Foundation Phase",
            "type": "foundation",
            "days": [
                {
                    "day": 1,
                    "date": "Day 1",
                    "title": f"{sub1} - Fundamental Concepts",
                    "type": "foundation",
                    "tasks": [f"Deep study {sub1} core theory (1.5 hrs)", "Solve 4 standard problem sets (1.5 hrs)", "Formula sheet & flashcards (1 hr)"],
                    "tip": "Write formulas and core definitions on sticky notes for quick daily review.",
                    "hours": int(num_hours)
                },
                {
                    "day": 2,
                    "date": "Day 2",
                    "title": f"{sub2} - Practical Drills",
                    "type": "foundation",
                    "tasks": [f"Solve 5 practice problems in {sub2} (2 hrs)", "Identify weak spots & review mistakes (1.5 hrs)", "Summary synthesis (30 mins)"],
                    "tip": "Focus on understanding the underlying logic rather than memorization.",
                    "hours": int(num_hours)
                }
            ]
        },
        {
            "name": "Intensive Phase",
            "type": "intense",
            "days": [
                {
                    "day": 3,
                    "date": "Day 3",
                    "title": "Comprehensive Mock Test",
                    "type": "intense",
                    "tasks": ["Full-length timed mock test (2.5 hrs)", "Detailed error analysis (1.5 hrs)"],
                    "tip": "Simulate actual exam conditions without reference sheets.",
                    "hours": int(num_hours)
                }
            ]
        },
        {
            "name": "Revision Phase",
            "type": "revision",
            "days": [
                {
                    "day": 4,
                    "date": "Day 4",
                    "title": "Rest & Active Recall",
                    "type": "rest",
                    "tasks": ["Review all previous week's notes (1 hr)", "Spaced repetition flashcards (1 hr)", "Rest & mental recharge"],
                    "tip": "Rest is part of the plan — your brain consolidates neural connections during sleep.",
                    "hours": 2
                }
            ]
        }
    ]
    rid = lighthouse_core.save_roadmap(exam_date, num_hours, subjects, syllabus, fallback_phases)
    return jsonify({
        "id": rid,
        "totalDays": days_left,
        "phases": fallback_phases
    })


# =========================================================
# LIGHTHOUSEAI: BUILD MY SCHEDULE (TIMETABLE GENERATOR)
# =========================================================

@app.route("/api/ai/build-schedule", methods=["POST"])
def api_build_schedule():
    """Build My Schedule / Timetable Generator with 1-Click Sync (LighthouseAI)."""
    data = request.get_json() or {}
    wake_time = data.get("wakeTime", "06:00 AM")
    sleep_time = data.get("sleepTime", "10:00 PM")
    subjects = data.get("subjects", ["Mathematics", "Computer Science"])
    if isinstance(subjects, str):
        subjects = [s.strip() for s in subjects.split(",") if s.strip()]
    preferences = data.get("preferences", "")
    break_duration = data.get("breakDuration", "20 mins")
    study_session_max = data.get("studySessionMax", "90 mins")
    target_date = data.get("targetDate") or datetime.now().strftime("%Y-%m-%d")

    sub_list = ", ".join(subjects) if subjects else "Deep Focus Work & Projects"

    prompt = f"""Create a high-performance daily study timetable.
    
    STUDENT PROFILE:
    - Wake Time: {wake_time}
    - Sleep Time: {sleep_time}
    - Subjects / Focus Goals: {sub_list}
    - Constraints / Preferences: {preferences or "Prioritize deep work in the morning"}
    - Preferred Session Length: {study_session_max}
    - Preferred Break Length: {break_duration}
    - Target Date: {target_date}

    INTELLIGENT REQUIREMENTS:
    1. PRIORITIZE: Allocate prime morning slots to high-cognition subjects.
    2. DYNAMIC BREAKS: Suggest healthy breaks (stretching, hydration, power walk).
    3. REALISM: Include breakfast, lunch, and dinner routine.
    4. OUTPUT: Return ONLY a JSON object with:
       {{
         "summary": "<motivating 1-sentence schedule overview>",
         "slots": [
           {{
             "time": "07:00 AM - 08:30 AM",
             "startTime": "07:00 AM",
             "iso_time": "{target_date}T07:00:00",
             "activity": "<Subject / Task Title>",
             "type": "study" | "break" | "meal" | "routine" | "sleep",
             "priority": "high" | "medium" | "low"
           }}
         ]
       }}"""

    system_prompt = "You are a master academic scheduler (LighthouseAI). Return strictly clean JSON."
    raw = call_ai_model(prompt, system_prompt=system_prompt, json_mode=True, feature="schedule")
    if raw:
        try:
            parsed = json.loads(clean_json_str(raw))
            if "slots" in parsed and len(parsed["slots"]) > 0:
                return jsonify(parsed)
        except Exception:
            pass

    # Reliable fallback schedule
    s1 = subjects[0] if subjects else "Deep Work Focus"
    s2 = subjects[1] if len(subjects) > 1 else "Project Development"
    return jsonify({
        "summary": f"Optimal high-performance study timetable for {target_date}",
        "slots": [
            {
                "time": "06:30 AM - 07:15 AM",
                "startTime": "06:30 AM",
                "iso_time": f"{target_date}T06:30:00",
                "activity": "Morning Routine & Goal Setting",
                "type": "routine",
                "priority": "low"
            },
            {
                "time": "07:15 AM - 08:45 AM",
                "startTime": "07:15 AM",
                "iso_time": f"{target_date}T07:15:00",
                "activity": f"Deep Focus: {s1}",
                "type": "study",
                "priority": "high"
            },
            {
                "time": "08:45 AM - 09:15 AM",
                "startTime": "08:45 AM",
                "iso_time": f"{target_date}T08:45:00",
                "activity": "Breakfast & Quick Walk",
                "type": "meal",
                "priority": "medium"
            },
            {
                "time": "09:15 AM - 10:45 AM",
                "startTime": "09:15 AM",
                "iso_time": f"{target_date}T09:15:00",
                "activity": f"Problem Solving & Practice: {s2}",
                "type": "study",
                "priority": "high"
            },
            {
                "time": "12:30 PM - 01:30 PM",
                "startTime": "12:30 PM",
                "iso_time": f"{target_date}T12:30:00",
                "activity": "Lunch & Rest",
                "type": "meal",
                "priority": "low"
            },
            {
                "time": "03:00 PM - 04:30 PM",
                "startTime": "03:00 PM",
                "iso_time": f"{target_date}T15:00:00",
                "activity": f"Review & Revision: {s1} & {s2}",
                "type": "study",
                "priority": "medium"
            },
            {
                "time": "08:00 PM - 08:45 PM",
                "startTime": "08:00 PM",
                "iso_time": f"{target_date}T20:00:00",
                "activity": "Daily Reflection & Tomorrow's Plan",
                "type": "routine",
                "priority": "medium"
            }
        ]
    })


# =========================================================
# ALARMS & BATTERY ENDPOINTS
# =========================================================

@app.route("/api/alarms", methods=["GET", "POST"])
def api_alarms():
    if request.method == "POST":
        data = request.get_json() or {}
        time_str = data.get("time", "").strip()
        label = data.get("label", "Wake Up Alarm").strip()
        target_date = data.get("target_date", "")
        if not time_str:
            return jsonify({"error": "Time is required"}), 400
        aid = lighthouse_core.add_alarm(time_str=time_str, label=label, target_date=target_date)
        return jsonify({"success": True, "id": aid, "time": time_str, "label": label})
    alarms = lighthouse_core.get_alarms()
    return jsonify({"alarms": alarms})


@app.route("/api/alarms/<int:aid>/toggle", methods=["POST"])
def api_toggle_alarm(aid: int):
    data = request.get_json() or {}
    enabled = data.get("enabled")
    res = lighthouse_core.toggle_alarm(aid, enabled)
    return jsonify({"success": res})


@app.route("/api/alarms/<int:aid>", methods=["DELETE"])
def api_delete_alarm(aid: int):
    res = lighthouse_core.delete_alarm(aid)
    return jsonify({"success": res})


@app.route("/api/battery", methods=["GET"])
def api_get_battery():
    b_info = lighthouse_core.get_battery_info()
    active_alarms = lighthouse_core.get_active_alarms()
    percent = b_info.get("percent", 100)
    is_charging = b_info.get("is_charging", True)
    is_critical_alarm = (percent < 40 and not is_charging and len(active_alarms) > 0)
    is_low = (percent < 50 and not is_charging)
    return jsonify({
        "percent": percent,
        "is_charging": is_charging,
        "source": b_info.get("source", "system"),
        "active_alarms": active_alarms,
        "has_active_alarms": len(active_alarms) > 0,
        "is_low": is_low,
        "is_critical_alarm": is_critical_alarm
    })


@app.route("/api/battery/status", methods=["POST"])
def api_update_battery_status():
    data = request.get_json() or {}
    percent = data.get("percent")
    is_charging = data.get("is_charging", True)
    if percent is not None:
        try:
            percent_int = int(percent)
            lighthouse_core.update_device_battery_status(percent_int, bool(is_charging))
        except Exception as e:
            print(f"Error saving battery status: {e}")
    alert_info = lighthouse_core.check_and_send_battery_alerts()
    active_alarms = lighthouse_core.get_active_alarms()
    b_info = lighthouse_core.get_battery_info()
    p = b_info.get("percent", 100)
    c = b_info.get("is_charging", True)
    return jsonify({
        "success": True,
        "percent": p,
        "is_charging": c,
        "active_alarms": active_alarms,
        "is_critical_alarm": (p < 40 and not c and len(active_alarms) > 0),
        "is_low": (p < 50 and not c),
        "alert_info": alert_info
    })


# =========================================================
# MENTOR CHAT ENDPOINT
# =========================================================

@app.route("/api/ai/chat", methods=["POST"])
def api_ai_chat():
    """Chat endpoint for Lighthouse Mentor."""
    data = request.get_json() or {}
    user_msg = data.get("message", "").strip()
    if not user_msg:
        return jsonify({"reply": "Hello! How can I assist you with your schedule and studies today?"})

    tasks = get_tasks()
    pending = [t[1] for t in tasks if not t[3]]
    task_context = ", ".join(pending[:5]) if pending else "No pending tasks"

    system_prompt = f"""You are LighthouseAI, an academic co-pilot and daily planner for Shlok.
Keep your answers CONCISE (2-4 sentences max), direct, actionable, and encouraging.
Current active goals/tasks: {task_context}.
Recommend practical study strategies like active recall, spaced repetition, and focused Pomodoro sessions."""

    reply = call_ai_model(user_msg, system_prompt=system_prompt)
    if reply:
        return jsonify({"reply": reply.strip()})
    return jsonify({"reply": "I am here with you, Shlok! What goal are we tackling next?"})


# =========================================================
# VOICE ASSISTANT INTERACT ENDPOINT (GOOGLE ASSISTANT STYLE)
# =========================================================

@app.route("/api/voice/interact", methods=["POST"])
def api_voice_interact():
    """Voice Assistant endpoint for conversational Web Dashboard queries."""
    data = request.get_json() or {}
    query = data.get("query", "").strip()
    result = lighthouse_core.process_voice_query(query)
    return jsonify(result)


@app.route("/api/voice/tts", methods=["POST"])
def api_voice_tts():
    """Direct Text-to-Speech synthesis via Gemini Audio / Google TTS API."""
    data = request.get_json() or {}
    text = data.get("text", "").strip()
    voice = data.get("voice", "Puck")
    result = lighthouse_core.generate_gemini_tts(text, voice_name=voice)
    if result:
        return jsonify(result)
    return jsonify({"error": "TTS audio synthesis unavailable"}), 503


if __name__ == "__main__":
    init_db()
    print("\n* LighthouseAI Dashboard running on http://localhost:8420 (http://127.0.0.1:8420)")
    app.run(host="127.0.0.1", port=8420, debug=False)




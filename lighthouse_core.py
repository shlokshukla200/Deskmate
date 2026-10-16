"""
LighthouseAI — Core Application Engine & Shared Data Layer.
SQLite Database Manager, Gemini/Groq Fallback Orchestrator, & PDF Generators.
"""

import os
import json
import sqlite3
import re
import io
import html
from datetime import datetime, timedelta
import requests

from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, KeepTogether
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch

# =========================================================
# ENVIRONMENT & CONFIGURATION
# =========================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "agent.db")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
ENV_PATH = os.path.join(BASE_DIR, ".env")


def load_env():
    """Load variables from .env if present into os.environ."""
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


def get_config():
    """Load fallback config.json if present."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_env_var(key: str, value: str):
    """Safely update or add an environment variable in .env."""
    lines = []
    found = False
    if os.path.exists(ENV_PATH):
        try:
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            lines = []

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"#{key}="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)

    if not found:
        new_lines.append(f"{key}={value}\n")

    try:
        with open(ENV_PATH, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        os.environ[key] = str(value)
    except Exception as e:
        print(f"Error saving {key} to .env: {e}")


# =========================================================
# DATABASE SCHEMA & INITIALIZATION
# =========================================================

def init_db():
    """Initialize all SQLite tables with auto-migration for missing columns."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Tasks Table
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

    # Check & migrate tasks columns
    c.execute("PRAGMA table_info(tasks)")
    task_cols = [r[1] for r in c.fetchall()]
    if "notes" not in task_cols:
        try:
            c.execute("ALTER TABLE tasks ADD COLUMN notes TEXT DEFAULT ''")
        except Exception:
            pass
    if "pomodoro_count" not in task_cols:
        try:
            c.execute("ALTER TABLE tasks ADD COLUMN pomodoro_count INTEGER DEFAULT 0")
        except Exception:
            pass
    if "created_at" not in task_cols:
        try:
            c.execute("ALTER TABLE tasks ADD COLUMN created_at TEXT DEFAULT ''")
        except Exception:
            pass
    if "notified" not in task_cols:
        try:
            c.execute("ALTER TABLE tasks ADD COLUMN notified INTEGER DEFAULT 0")
        except Exception:
            pass
    if "timetable_id" not in task_cols:
        try:
            c.execute("ALTER TABLE tasks ADD COLUMN timetable_id TEXT DEFAULT NULL")
        except Exception:
            pass

    # Google Keep Style Sticky Notes Table
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

    # Check & migrate sticky_notes columns
    c.execute("PRAGMA table_info(sticky_notes)")
    note_cols = [r[1] for r in c.fetchall()]
    for col, col_def in [
        ('bg_theme', "TEXT DEFAULT ''"),
        ('note_type', "TEXT DEFAULT 'text'"),
        ('checklist_items', "TEXT DEFAULT '[]'"),
        ('drawing_data', "TEXT DEFAULT ''"),
        ('task_id', "INTEGER DEFAULT NULL"),
        ('is_pinned', "INTEGER DEFAULT 0"),
        ('is_archived', "INTEGER DEFAULT 0")
    ]:
        if col not in note_cols:
            try:
                c.execute(f"ALTER TABLE sticky_notes ADD COLUMN {col} {col_def}")
            except Exception:
                pass

    # Saved Timetables Table
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

    # Study Roadmaps Table
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

    # Alarms Table (Wake Up & Routine Alarms)
    c.execute("""
        CREATE TABLE IF NOT EXISTS alarms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time_str TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT 'Wake Up Alarm',
            enabled INTEGER DEFAULT 1,
            ring_days TEXT DEFAULT '[]',
            target_date TEXT,
            created_at TEXT
        )
    """)

    # Device Battery & Power Status Table
    c.execute("""
        CREATE TABLE IF NOT EXISTS device_status (
            id INTEGER PRIMARY KEY,
            battery_percent INTEGER,
            is_charging INTEGER,
            last_alerted_50 INTEGER DEFAULT 0,
            last_alerted_40 INTEGER DEFAULT 0,
            updated_at TEXT
        )
    """)

    # Bot Heartbeat Table
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



# =========================================================
# AI MODEL PIPELINE (GEMINI PRIMARY -> GROQ FALLBACK)
# =========================================================

def clean_json_str(raw: str) -> str:
    """Robustly sanitize markdown fenced JSON, backticks, and trailing commas."""
    if not raw:
        return ""
    text = re.sub(r"^```[a-zA-Z]*\n?", "", raw.strip(), flags=re.MULTILINE)
    text = re.sub(r"```$", "", text.strip(), flags=re.MULTILINE)
    text = text.strip()
    return text


def get_gemini_keys_for_feature(feature: str = None) -> list[str]:
    """
    Retrieve prioritized list of Gemini API keys for a specific feature.
    Supports feature-specific keys, comma-separated key pools, and master fallback.
    Feature options: 'voice', 'vision', 'schedule', 'roadmap', 'mentor', 'agent', or None.
    """
    load_env()
    config = get_config()
    keys = []

    # 1. Feature-specific key if set (e.g. GEMINI_API_KEY_VOICE)
    if feature:
        feat_env = os.environ.get(f"GEMINI_API_KEY_{feature.upper()}") or config.get(f"gemini_api_key_{feature.lower()}")
        if feat_env:
            for k in str(feat_env).split(","):
                k_clean = k.strip()
                if k_clean and k_clean not in keys:
                    keys.append(k_clean)

    # 2. General Key Pool (GEMINI_API_KEYS or GEMINI_API_KEY_POOL)
    pool_env = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY_POOL") or config.get("gemini_api_keys")
    if pool_env:
        for k in str(pool_env).split(","):
            k_clean = k.strip()
            if k_clean and k_clean not in keys:
                keys.append(k_clean)

    # 3. Master default fallback key (GEMINI_API_KEY)
    master_key = os.environ.get("GEMINI_API_KEY") or config.get("gemini_api_key")
    if master_key:
        for k in str(master_key).split(","):
            k_clean = k.strip()
            if k_clean and k_clean not in keys:
                keys.append(k_clean)

    return keys


def call_ai_model(prompt: str, system_prompt: str = "", json_mode: bool = False, feature: str = None) -> str | None:
    """
    Query Google Gemini with multi-key pool rotation & Groq fallback.
    Automatically rotates across feature keys and key pools on 429 quota exhaustion.
    """
    load_env()
    config = get_config()
    gemini_keys = get_gemini_keys_for_feature(feature)
    groq_key = os.environ.get("GROQ_API_KEY") or config.get("groq_api_key")

    full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt

    # 1. Primary: Google Gemini with Multi-Key Rotation
    if gemini_keys:
        gemini_models = ["gemini-flash-latest", "gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.7-flash"]
        for key_idx, g_key in enumerate(gemini_keys):
            for model in gemini_models:
                try:
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={g_key}"
                    payload = {
                        "contents": [{"parts": [{"text": full_prompt}]}],
                        "generationConfig": {
                            "temperature": 0.2 if json_mode else 0.4
                        }
                    }
                    res = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=8)
                    if res.status_code == 200:
                        data = res.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            if parts and "text" in parts[0]:
                                return parts[0]["text"].strip()
                    elif res.status_code == 429:
                        print(f"[Gemini Key #{key_idx+1} ({feature or 'general'}) 429 Quota] Rotating to next key/model...")
                        break  # Break model loop to try next API key in pool
                except Exception as e:
                    print(f"[Gemini-{model}] error: {e}")
                    continue

    # 2. Fallback: Groq LLM Engine
    if groq_key:
        groq_models = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]
        for model in groq_models:
            try:
                url = "https://api.groq.com/openai/v1/chat/completions"
                payload = {
                    "model": model,
                    "messages": [
                        *([{"role": "system", "content": system_prompt}] if system_prompt else []),
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.2 if json_mode else 0.4
                }
                if json_mode:
                    payload["response_format"] = {"type": "json_object"}

                res = requests.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
                    timeout=18
                )
                if res.status_code == 200:
                    data = res.json()
                    choices = data.get("choices", [])
                    if choices:
                        return choices[0]["message"]["content"].strip()
            except Exception as e:
                print(f"[Groq-{model}] error: {e}")
                continue

    return None


# =========================================================
# TASK DATABASE OPERATIONS
# =========================================================

def get_tasks(date_str: str | None = None):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if date_str:
        c.execute(
            "SELECT id, content, due_time, done, created_at, COALESCE(notes, ''), COALESCE(pomodoro_count, 0) FROM tasks "
            "WHERE (due_time LIKE ? OR (due_time IS NULL AND created_at LIKE ?)) "
            "ORDER BY done ASC, due_time IS NULL, due_time ASC",
            (f"{date_str}%", f"{date_str}%")
        )
    else:
        today = datetime.now().strftime("%Y-%m-%d")
        c.execute(
            "SELECT id, content, due_time, done, created_at, COALESCE(notes, ''), COALESCE(pomodoro_count, 0) FROM tasks "
            "WHERE done = 0 OR due_time LIKE ? OR created_at LIKE ? "
            "ORDER BY done ASC, due_time IS NULL, due_time ASC",
            (f"{today}%", f"{today}%")
        )
    rows = c.fetchall()
    conn.close()
    return rows


def get_all_tasks():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, content, due_time, done, created_at, COALESCE(notes, ''), COALESCE(pomodoro_count, 0) FROM tasks "
        "ORDER BY done ASC, due_time IS NULL, due_time ASC, id DESC"
    )
    rows = c.fetchall()
    conn.close()
    return rows


def get_pending_tasks():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, content, due_time, done, created_at, COALESCE(notes, ''), COALESCE(pomodoro_count, 0) FROM tasks "
        "WHERE done = 0 ORDER BY due_time IS NULL, due_time ASC, id ASC"
    )
    rows = c.fetchall()
    conn.close()
    return rows


def get_task_by_id(task_id: int):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, content, due_time, done, created_at, COALESCE(notes, ''), COALESCE(pomodoro_count, 0) FROM tasks WHERE id = ?",
        (task_id,)
    )
    row = c.fetchone()
    conn.close()
    return row


def add_task(content: str, due_time: str | None = None, notes: str = "", timetable_id: str | None = None):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now_iso = datetime.now().isoformat()
    c.execute(
        "INSERT INTO tasks (content, due_time, done, notified, created_at, notes, pomodoro_count, timetable_id) VALUES (?, ?, 0, 0, ?, ?, 0, ?)",
        (content.strip(), due_time.strip() if due_time else None, now_iso, notes.strip(), timetable_id)
    )
    conn.commit()
    task_id = c.lastrowid

    # If notes were provided, also create or sync a sticky note
    if notes.strip():
        c.execute(
            "INSERT INTO sticky_notes (title, content, color, bg_theme, note_type, checklist_items, drawing_data, task_id, is_pinned, is_archived, created_at, updated_at) "
            "VALUES (?, ?, 'amber', 'noodles', 'task', '[]', '', ?, 0, 0, ?, ?)",
            (f"Task: {content.strip()}", notes.strip(), task_id, now_iso, now_iso)
        )
        conn.commit()

    conn.close()
    return task_id


def edit_task(task_id: int, content: str = None, due_time: str = None, notes: str = None) -> bool:
    init_db()
    current = get_task_by_id(task_id)
    if not current:
        return False

    new_content = content.strip() if content is not None else current[1]
    new_due = due_time.strip() if due_time is not None else current[2]
    new_notes = notes.strip() if notes is not None else current[5]

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE tasks SET content = ?, due_time = ?, notes = ? WHERE id = ?",
        (new_content, new_due, new_notes, task_id)
    )
    # Sync sticky note if exists
    if notes is not None:
        c.execute("SELECT id FROM sticky_notes WHERE task_id = ?", (task_id,))
        existing = c.fetchone()
        now_iso = datetime.now().isoformat()
        if existing:
            if new_notes:
                c.execute("UPDATE sticky_notes SET content = ?, title = ?, updated_at = ? WHERE id = ?",
                          (new_notes, f"Task: {new_content}", now_iso, existing[0]))
            else:
                c.execute("DELETE FROM sticky_notes WHERE id = ?", (existing[0],))
        elif new_notes:
            c.execute(
                "INSERT INTO sticky_notes (title, content, color, bg_theme, note_type, checklist_items, drawing_data, task_id, is_pinned, is_archived, created_at, updated_at) "
                "VALUES (?, ?, 'amber', 'noodles', 'task', '[]', '', ?, 0, 0, ?, ?)",
                (f"Task: {new_content}", new_notes, task_id, now_iso, now_iso)
            )

    conn.commit()
    affected = c.rowcount
    conn.close()
    return affected > 0 or True


def mark_done(task_id: int, done_val: int = 1) -> bool:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE tasks SET done = ? WHERE id = ?", (done_val, task_id))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def delete_task(task_id: int) -> bool:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    c.execute("DELETE FROM sticky_notes WHERE task_id = ?", (task_id,))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def update_task_note(task_id: int, notes: str) -> bool:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE tasks SET notes = ? WHERE id = ?", (notes.strip(), task_id))
    c.execute("SELECT content FROM tasks WHERE id = ?", (task_id,))
    row = c.fetchone()
    task_content = row[0] if row else "Task"

    now_iso = datetime.now().isoformat()
    c.execute("SELECT id FROM sticky_notes WHERE task_id = ?", (task_id,))
    existing = c.fetchone()
    if existing:
        if notes.strip():
            c.execute("UPDATE sticky_notes SET content = ?, title = ?, updated_at = ? WHERE id = ?",
                      (notes.strip(), f"Task: {task_content}", now_iso, existing[0]))
        else:
            c.execute("DELETE FROM sticky_notes WHERE id = ?", (existing[0],))
    elif notes.strip():
        c.execute(
            "INSERT INTO sticky_notes (title, content, color, bg_theme, note_type, checklist_items, drawing_data, task_id, is_pinned, is_archived, created_at, updated_at) "
            "VALUES (?, ?, 'amber', 'noodles', 'task', '[]', '', ?, 0, 0, ?, ?)",
            (f"Task: {task_content}", notes.strip(), task_id, now_iso, now_iso)
        )
    conn.commit()
    conn.close()
    return True


def get_due_unnotified():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    c.execute(
        "SELECT id, content FROM tasks WHERE done = 0 AND notified = 0 AND due_time IS NOT NULL AND due_time != '' AND due_time <= ?",
        (now_str,)
    )
    rows = c.fetchall()
    conn.close()
    return rows


def mark_notified(task_id: int):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE tasks SET notified = 1 WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()


def find_tasks_by_query(query: str):
    """Fuzzy matching helper for conversational task searches."""
    tasks = get_all_tasks()
    query_lower = query.lower().strip()
    matches = []
    for t in tasks:
        content_lower = t[1].lower()
        if query_lower in content_lower or any(w in content_lower for w in query_lower.split() if len(w) > 3):
            matches.append(t)
    return matches


# =========================================================
# STICKY NOTES DATABASE OPERATIONS
# =========================================================

def get_notes(query: str = "", include_archived: bool = False):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute(
        "SELECT id, title, content, color, COALESCE(bg_theme, ''), COALESCE(note_type, 'text'), "
        "COALESCE(checklist_items, '[]'), COALESCE(drawing_data, ''), task_id, is_pinned, is_archived, created_at, updated_at "
        "FROM sticky_notes ORDER BY is_pinned DESC, id DESC"
    )
    rows = c.fetchall()
    conn.close()

    result = []
    q_lower = query.strip().lower()
    for r in rows:
        is_archived = bool(r[10])
        if is_archived and not include_archived:
            continue

        try:
            cl_items = json.loads(r[6]) if r[6] else []
        except Exception:
            cl_items = []

        title = r[1] or ""
        content = r[2] or ""

        if q_lower:
            if q_lower not in title.lower() and q_lower not in content.lower():
                matches_item = any(q_lower in item.get('text', '').lower() for item in cl_items)
                if not matches_item:
                    continue

        result.append({
            "id": r[0],
            "title": title,
            "content": content,
            "color": r[3] or "default",
            "bg_theme": r[4] or "",
            "note_type": r[5] or "text",
            "checklist_items": cl_items,
            "drawing_data": r[7] or "",
            "task_id": r[8],
            "is_pinned": bool(r[9]),
            "is_archived": is_archived,
            "created_at": r[11] or "",
            "updated_at": r[12] or ""
        })
    return result


def get_note_by_id(note_id: int):
    notes = get_notes(include_archived=True)
    for n in notes:
        if n["id"] == note_id:
            return n
    return None


def add_note(title: str = "", content: str = "", color: str = "default", bg_theme: str = "", note_type: str = "text", checklist_items: list = None, drawing_data: str = "", is_pinned: bool = False, is_archived: bool = False) -> int:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now_iso = datetime.now().isoformat()
    cl_json = json.dumps(checklist_items or [])

    c.execute(
        "INSERT INTO sticky_notes (title, content, color, bg_theme, note_type, checklist_items, drawing_data, task_id, is_pinned, is_archived, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
        (title.strip(), content.strip(), color, bg_theme, note_type, cl_json, drawing_data, 1 if is_pinned else 0, 1 if is_archived else 0, now_iso, now_iso)
    )
    nid = c.lastrowid
    conn.commit()
    conn.close()
    return nid


def update_note(note_id: int, **kwargs) -> bool:
    init_db()
    current = get_note_by_id(note_id)
    if not current:
        return False

    title = kwargs.get("title", current["title"])
    content = kwargs.get("content", current["content"])
    color = kwargs.get("color", current["color"])
    bg_theme = kwargs.get("bg_theme", current["bg_theme"])
    note_type = kwargs.get("note_type", current["note_type"])
    checklist_items = kwargs.get("checklist_items", current["checklist_items"])
    drawing_data = kwargs.get("drawing_data", current["drawing_data"])
    is_pinned = kwargs.get("is_pinned", current["is_pinned"])
    is_archived = kwargs.get("is_archived", current["is_archived"])
    now_iso = datetime.now().isoformat()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    if current["task_id"]:
        c.execute("UPDATE tasks SET notes = ? WHERE id = ?", (content, current["task_id"]))

    c.execute("""
        UPDATE sticky_notes SET
            title = ?, content = ?, color = ?, bg_theme = ?, note_type = ?,
            checklist_items = ?, drawing_data = ?, is_pinned = ?, is_archived = ?, updated_at = ?
        WHERE id = ?
    """, (
        title, content, color, bg_theme, note_type,
        json.dumps(checklist_items or []), drawing_data, 1 if is_pinned else 0, 1 if is_archived else 0, now_iso, note_id
    ))
    conn.commit()
    conn.close()
    return True


def delete_note(note_id: int) -> bool:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT task_id FROM sticky_notes WHERE id = ?", (note_id,))
    row = c.fetchone()
    if row and row[0]:
        c.execute("UPDATE tasks SET notes = '' WHERE id = ?", (row[0],))
    c.execute("DELETE FROM sticky_notes WHERE id = ?", (note_id,))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0


# =========================================================
# TIMETABLES & STUDY ROADMAP OPERATIONS
# =========================================================

def get_saved_timetables():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, title, date_label, slots_json, form_json, created_at FROM saved_timetables ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()

    result = []
    for r in rows:
        try:
            slots = json.loads(r[3]) if r[3] else []
        except Exception:
            slots = []
        try:
            form_data = json.loads(r[4]) if r[4] else {}
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
    return result


def get_timetable_by_id(tt_id: str):
    all_tts = get_saved_timetables()
    for t in all_tts:
        if str(t["id"]) == str(tt_id):
            return t
    return None


def save_timetable(t_id: str = None, title: str = "", date_label: str = "", slots: list = None, form_data: dict = None) -> str:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    target_id = str(t_id or int(datetime.now().timestamp() * 1000))
    final_title = title or f"Protocol {target_id[-4:]}"
    final_date = date_label or datetime.now().strftime("%Y-%m-%d")
    now_iso = datetime.now().isoformat()

    c.execute(
        "INSERT OR REPLACE INTO saved_timetables (id, title, date_label, slots_json, form_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (target_id, final_title, final_date, json.dumps(slots or []), json.dumps(form_data or {}), now_iso)
    )
    conn.commit()
    conn.close()
    return target_id


def rename_timetable(tt_id: str, new_title: str) -> bool:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE saved_timetables SET title = ? WHERE id = ?", (new_title.strip(), str(tt_id)))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def delete_timetable(tt_id: str) -> bool:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Find all tasks linked to this timetable (by timetable_id column or by notes pattern)
    c.execute("SELECT id FROM tasks WHERE timetable_id = ? OR notes LIKE ?", (str(tt_id), f"%[Timetable: {tt_id}]%"))
    linked_tasks = c.fetchall()
    for (tid,) in linked_tasks:
        c.execute("DELETE FROM sticky_notes WHERE task_id = ?", (tid,))
        c.execute("DELETE FROM tasks WHERE id = ?", (tid,))
    c.execute("DELETE FROM saved_timetables WHERE id = ?", (str(tt_id),))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return True


def parse_essential_schedule_text(raw_text: str, target_date: str = None) -> list:
    """Parses quick essential schedule blocks from text (e.g. 'Math at 8am-10am, DSA at 2pm-4pm, gym at 6pm')."""
    if not target_date:
        target_date = datetime.now().strftime("%Y-%m-%d")

    prompt = f"""Convert the following user schedule request into structured timetable slots.
Target Date: {target_date}
User Input: "{raw_text}"

Return ONLY a JSON array of slots with standard 12-hour AM/PM times:
[
  {{
    "time": "08:00 AM - 10:00 AM",
    "startTime": "08:00 AM",
    "iso_time": "{target_date}T08:00:00",
    "activity": "<Subject/Task>",
    "type": "study" | "meal" | "break" | "routine",
    "priority": "high" | "medium" | "low",
    "note": "<any specific note or instructions if provided>"
  }}
]"""
    system_prompt = "You are LighthouseAI Essential Schedule Builder. Return clean JSON array of slots only."
    raw = call_ai_model(prompt, system_prompt=system_prompt, json_mode=True)
    if raw:
        try:
            cleaned = clean_json_str(raw)
            parsed = json.loads(cleaned)
            if isinstance(parsed, list) and len(parsed) > 0:
                return parsed
            elif isinstance(parsed, dict) and "slots" in parsed:
                return parsed["slots"]
        except Exception:
            pass

    # Simple fallback parser if AI is unavailable
    slots = []
    lines = [l.strip() for l in re.split(r'[,;\n]', raw_text) if l.strip()]
    default_hour = 8
    for line in lines:
        start_t = f"{default_hour:02d}:00 AM" if default_hour < 12 else f"{(default_hour-12 or 12):02d}:00 PM"
        end_h = default_hour + 1
        end_t = f"{end_h:02d}:00 AM" if end_h < 12 else f"{(end_h-12 or 12):02d}:00 PM"
        slots.append({
            "time": f"{start_t} - {end_t}",
            "startTime": start_t,
            "iso_time": f"{target_date}T{default_hour:02d}:00:00",
            "activity": line,
            "type": "study",
            "priority": "high",
            "note": ""
        })
        default_hour += 2
    return slots



def get_all_roadmaps():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, exam_date, study_hours, subjects_json, syllabus, phases_json, created_at FROM saved_roadmaps ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    result = []
    for r in rows:
        try:
            subjects = json.loads(r[3]) if r[3] else []
        except Exception:
            subjects = []
        try:
            phases = json.loads(r[5]) if r[5] else []
        except Exception:
            phases = []
        result.append({
            "id": r[0],
            "examDate": r[1],
            "studyHours": r[2],
            "subjects": subjects,
            "syllabus": r[4],
            "phases": phases,
            "created_at": r[6]
        })
    return result


def get_roadmap_by_id(rid: int):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, exam_date, study_hours, subjects_json, syllabus, phases_json, created_at FROM saved_roadmaps WHERE id = ?", (int(rid),))
    r = c.fetchone()
    conn.close()
    if not r:
        return None
    try:
        subjects = json.loads(r[3]) if r[3] else []
    except Exception:
        subjects = []
    try:
        phases = json.loads(r[5]) if r[5] else []
    except Exception:
        phases = []
    return {
        "id": r[0],
        "examDate": r[1],
        "studyHours": r[2],
        "subjects": subjects,
        "syllabus": r[4],
        "phases": phases,
        "created_at": r[6]
    }


def delete_roadmap(rid: int) -> bool:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM saved_roadmaps WHERE id = ?", (int(rid),))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def get_saved_roadmap():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, exam_date, study_hours, subjects_json, syllabus, phases_json, created_at FROM saved_roadmaps ORDER BY id DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    try:
        subjects = json.loads(row[3]) if row[3] else []
    except Exception:
        subjects = []
    try:
        phases = json.loads(row[5]) if row[5] else []
    except Exception:
        phases = []
    return {
        "id": row[0],
        "examDate": row[1],
        "studyHours": row[2],
        "subjects": subjects,
        "syllabus": row[4],
        "phases": phases,
        "created_at": row[6]
    }


def save_roadmap(exam_date: str, study_hours: float, subjects: list, syllabus: str, phases: list) -> int:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now_iso = datetime.now().isoformat()
    c.execute("""
        INSERT INTO saved_roadmaps (exam_date, study_hours, subjects_json, syllabus, phases_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (exam_date, study_hours, json.dumps(subjects or []), syllabus, json.dumps(phases or []), now_iso))
    rid = c.lastrowid
    conn.commit()
    conn.close()
    return rid


# =========================================================
# ALARMS & BATTERY POWER MANAGEMENT (WINDOWS & ANDROID)
# =========================================================

def add_alarm(time_str: str, label: str = "Wake Up Alarm", enabled: int = 1, target_date: str = "") -> int:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now_iso = datetime.now().isoformat()
    t_date = target_date or datetime.now().strftime("%Y-%m-%d")
    c.execute(
        "INSERT INTO alarms (time_str, label, enabled, ring_days, target_date, created_at) VALUES (?, ?, ?, '[]', ?, ?)",
        (time_str.strip(), label.strip(), 1 if enabled else 0, t_date, now_iso)
    )
    conn.commit()
    aid = c.lastrowid
    conn.close()
    return aid


def get_alarms() -> list:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, time_str, label, enabled, ring_days, target_date, created_at FROM alarms ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    return [
        {
            "id": r[0],
            "time": r[1],
            "label": r[2],
            "enabled": bool(r[3]),
            "ring_days": json.loads(r[4]) if r[4] else [],
            "target_date": r[5],
            "created_at": r[6]
        }
        for r in rows
    ]


def get_active_alarms() -> list:
    return [a for a in get_alarms() if a.get("enabled")]


def toggle_alarm(alarm_id: int, enabled: bool = None) -> bool:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if enabled is None:
        c.execute("UPDATE alarms SET enabled = CASE WHEN enabled = 1 THEN 0 ELSE 1 END WHERE id = ?", (int(alarm_id),))
    else:
        c.execute("UPDATE alarms SET enabled = ? WHERE id = ?", (1 if enabled else 0, int(alarm_id)))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def delete_alarm(alarm_id: int) -> bool:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM alarms WHERE id = ?", (int(alarm_id),))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def get_battery_info() -> dict:
    """Detect system battery percentage and charging state via psutil, ctypes, or DB cache."""
    init_db()
    # 1. Check if client sync updated within last 60 seconds
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT battery_percent, is_charging, updated_at FROM device_status WHERE id = 1")
        row = c.fetchone()
        conn.close()
        if row and row[0] is not None and row[2]:
            try:
                up_dt = datetime.fromisoformat(row[2])
                if datetime.now() - up_dt < timedelta(seconds=60):
                    return {
                        "percent": int(row[0]),
                        "is_charging": bool(row[1]),
                        "source": "client_sync"
                    }
            except Exception:
                pass
    except Exception:
        pass

    # 2. psutil (Hardware battery query)
    try:
        import psutil
        b = psutil.sensors_battery()
        if b is not None:
            return {
                "percent": int(b.percent),
                "is_charging": bool(b.power_plugged),
                "source": "psutil"
            }
    except Exception:
        pass

    # 3. Windows ctypes kernel32 GetSystemPowerStatus
    try:
        import ctypes
        from ctypes import wintypes
        class SYSTEM_POWER_STATUS(ctypes.Structure):
            _fields_ = [
                ('ACLineStatus', wintypes.BYTE),
                ('BatteryFlag', wintypes.BYTE),
                ('BatteryLifePercent', wintypes.BYTE),
                ('SystemStatusFlag', wintypes.BYTE),
                ('BatteryLifeTime', wintypes.DWORD),
                ('BatteryFullLifeTime', wintypes.DWORD)
            ]
        sps = SYSTEM_POWER_STATUS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
            if sps.BatteryLifePercent <= 100:
                return {
                    "percent": int(sps.BatteryLifePercent),
                    "is_charging": sps.ACLineStatus == 1,
                    "source": "ctypes"
                }
    except Exception:
        pass

    # 4. Fallback to cached device_status if exists
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT battery_percent, is_charging, updated_at FROM device_status WHERE id = 1")
        row = c.fetchone()
        conn.close()
        if row and row[0] is not None:
            return {
                "percent": int(row[0]),
                "is_charging": bool(row[1]),
                "source": "client_sync_cached"
            }
    except Exception:
        pass

    return {"percent": 100, "is_charging": True, "source": "default"}


def update_device_battery_status(percent: int, is_charging: bool):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now_iso = datetime.now().isoformat()
    c.execute("SELECT id, last_alerted_50, last_alerted_40 FROM device_status WHERE id = 1")
    row = c.fetchone()
    if not row:
        c.execute(
            "INSERT INTO device_status (id, battery_percent, is_charging, last_alerted_50, last_alerted_40, updated_at) VALUES (1, ?, ?, 0, 0, ?)",
            (percent, 1 if is_charging else 0, now_iso)
        )
    else:
        a50 = row[1]
        a40 = row[2]
        if is_charging or percent >= 55:
            a50 = 0
            a40 = 0
        c.execute(
            "UPDATE device_status SET battery_percent = ?, is_charging = ?, last_alerted_50 = ?, last_alerted_40 = ?, updated_at = ? WHERE id = 1",
            (percent, 1 if is_charging else 0, a50, a40, now_iso)
        )
    conn.commit()
    conn.close()


def check_and_send_battery_alerts(send_telegram_fn=None) -> dict:
    """
    Checks battery state:
    - Below 50% and discharging: sends alert via Telegram.
    - Below 40% and alarm is set: sends critical warning via Telegram and marks alert.
    """
    init_db()
    b_info = get_battery_info()
    percent = b_info.get("percent", 100)
    is_charging = b_info.get("is_charging", True)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT last_alerted_50, last_alerted_40 FROM device_status WHERE id = 1")
    row = c.fetchone()
    a50 = row[0] if row else 0
    a40 = row[1] if row else 0

    active_alarms = get_active_alarms()
    has_alarm = len(active_alarms) > 0
    alarm_desc = ", ".join([f"{a['time']} ({a['label']})" for a in active_alarms]) if active_alarms else "None"

    alert_sent = None

    if not is_charging:
        # Check < 40% with Alarm Critical Warning
        if percent < 40 and has_alarm and not a40:
            msg = (
                f"🚨 <b>CRITICAL BATTERY WARNING: {percent}%</b>\n\n"
                f"Your device is on battery ({percent}%) and you have an alarm active:\n"
                f"⏰ <b>{html.escape(alarm_desc)}</b>\n\n"
                f"⚠️ <b>WARNING:</b> Battery is critically low! Your device may turn off before your alarm rings.\n"
                f"⚡ <i>Please plug in your charger immediately!</i>"
            )
            if send_telegram_fn:
                send_telegram_fn(msg)
            c.execute("UPDATE device_status SET last_alerted_40 = 1, last_alerted_50 = 1 WHERE id = 1")
            conn.commit()
            alert_sent = "critical_40_alarm"

        # Check < 50% Warning
        elif percent < 50 and not a50:
            msg = (
                f"⚠️ <b>Low Battery Alert: {percent}%</b>\n\n"
                f"Your device is unplugged and has dropped below 50%.\n"
                f"⚡ <i>Please connect your charger to stay powered up.</i>"
            )
            if send_telegram_fn:
                send_telegram_fn(msg)
            c.execute("UPDATE device_status SET last_alerted_50 = 1 WHERE id = 1")
            conn.commit()
            alert_sent = "low_50"

    conn.close()
    return {
        "percent": percent,
        "is_charging": is_charging,
        "has_alarm": has_alarm,
        "alarm_desc": alarm_desc,
        "alert_sent": alert_sent
    }



def call_ai_vision(image_bytes: bytes, prompt: str, system_prompt: str = "", mime_type: str = "image/jpeg", feature: str = "vision") -> str | None:
    """Query Google Gemini Multimodal Vision with image bytes with multi-key pool rotation."""
    import base64
    gemini_keys = get_gemini_keys_for_feature(feature)
    if not gemini_keys:
        return None
    b64_img = base64.b64encode(image_bytes).decode('utf-8')
    models_to_try = [
        "gemini-flash-latest",
        "gemini-3.8-flash",
        "gemini-3.5-flash",
        "gemini-3.7-flash",
        "gemini-pro-latest"
    ]
    for key_idx, g_key in enumerate(gemini_keys):
        for model in models_to_try:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={g_key}"
                payload = {
                    "contents": [
                        {
                            "parts": [
                                {"text": f"{system_prompt}\n\n{prompt}" if system_prompt else prompt},
                                {
                                    "inline_data": {
                                        "mime_type": mime_type,
                                        "data": b64_img
                                    }
                                }
                            ]
                        }
                    ],
                    "generationConfig": {
                        "temperature": 0.2,
                        "maxOutputTokens": 4096
                    }
                }
                resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=25)
                if resp.status_code == 200:
                    cand = resp.json().get("candidates", [{}])[0]
                    text = cand.get("content", {}).get("parts", [{}])[0].get("text", "")
                    if text:
                        return text
                elif resp.status_code == 429:
                    print(f"[Gemini Vision Key #{key_idx+1} 429 Quota] Rotating to next vision key...")
                    break
            except Exception as e:
                print(f"[Gemini Vision-{model}] error: {e}")
                continue
    return None


def generate_timetable_ai(wake_time: str = "07:00", bed_time: str = "23:00", study_hours: str = "4", subjects: list = None):
    """Generate structured study timetable slots with AI & fallback."""
    subjects = subjects or ["Core Subject A", "Core Subject B"]
    prompt = f"""Generate a high-performance daily timetable for a student who wakes at {wake_time} and sleeps at {bed_time}.
Target focused study hours: {study_hours} hours.
Subjects: {', '.join(subjects)}.

Rules:
1. Divide the day from {wake_time} to {bed_time} into continuous slots.
2. Slot types must be one of: 'study', 'revision', 'break', 'routine'.
3. Embed focused 45-60 min study blocks and short breaks.

Return STRICT JSON format:
{{
  "summary": "High performance routine with dedicated focus blocks",
  "slots": [
    {{ "time": "07:00 - 07:45", "activity": "Morning Routine & Planning", "type": "routine", "subject": "Routine" }},
    {{ "time": "07:45 - 09:30", "activity": "Deep Work Block 1", "type": "study", "subject": "{subjects[0]}" }}
  ]
}}"""

    system_prompt = "You are LighthouseAI, an expert academic planner. Output valid JSON only."
    raw = call_ai_model(prompt, system_prompt=system_prompt, json_mode=True, feature="schedule")
    if raw:
        try:
            parsed = json.loads(clean_json_str(raw))
            if "slots" in parsed and len(parsed["slots"]) > 0:
                return parsed
        except Exception:
            pass

    # Deterministic Fallback Schedule
    fallback_slots = [
        {"time": f"{wake_time} - 08:00", "activity": "Morning Routine & Focus Setup", "type": "routine", "subject": "Routine"},
        {"time": "08:00 - 10:00", "activity": f"Deep Focus: {subjects[0]}", "type": "study", "subject": subjects[0]},
        {"time": "10:00 - 10:30", "activity": "Active Break & Hydration", "type": "break", "subject": "Break"},
        {"time": "10:30 - 12:30", "activity": f"Core Study: {subjects[1] if len(subjects) > 1 else subjects[0]}", "type": "study", "subject": subjects[1] if len(subjects) > 1 else subjects[0]},
        {"time": "12:30 - 13:30", "activity": "Lunch & Rest", "type": "break", "subject": "Break"},
        {"time": "14:00 - 16:00", "activity": "Problem Solving & Active Recall", "type": "study", "subject": subjects[0]},
        {"time": "16:00 - 16:30", "activity": "Afternoon Walk & Tea", "type": "break", "subject": "Break"},
        {"time": "17:00 - 19:00", "activity": "Target Practice & Problem Sets", "type": "revision", "subject": subjects[-1]},
        {"time": "20:00 - 21:00", "activity": "Daily Review & Flashcards", "type": "revision", "subject": "Review"},
        {"time": f"21:30 - {bed_time}", "activity": "Wind Down & Sleep Prep", "type": "routine", "subject": "Sleep Prep"}
    ]
    return {"summary": "Balanced focus schedule with structured deep work intervals.", "slots": fallback_slots}


def generate_roadmap_ai(exam_date: str, study_hours: str = "4", subjects: list = None, syllabus: str = "Core Syllabus"):
    """Generate multi-phase exam roadmap with AI & fallback."""
    subjects = subjects or ["Mathematics", "Physics"]
    days_left = 30
    if exam_date:
        try:
            target = datetime.strptime(exam_date, "%Y-%m-%d")
            diff = (target - datetime.now()).days
            if diff > 0:
                days_left = diff
        except Exception:
            pass

    prompt = f"""Create a comprehensive study & revision roadmap for an exam on {exam_date} ({days_left} days remaining).
Daily Study Hours: {study_hours} hrs.
Subjects: {', '.join(subjects)}.
Syllabus / Focus: {syllabus}.

Structure into 3 distinct phases:
1. Foundation & Conceptual Mastery (Days 1 to {max(1, int(days_left*0.5))})
2. Problem Solving & High-Yield Applications (Days {max(2, int(days_left*0.5)+1)} to {max(2, int(days_left*0.8))})
3. Full Mocks & Active Recall (Days {max(3, int(days_left*0.8)+1)} to {days_left})

Return STRICT JSON format:
{{
  "totalDays": {days_left},
  "phases": [
    {{
      "phaseNumber": 1,
      "name": "Phase 1: Foundation & Core Concepts",
      "duration": "Days 1-{max(1, int(days_left*0.5))}",
      "dailyTarget": "3 modules / day + active recall",
      "milestone": "Master core theory across all subjects",
      "topics": ["{subjects[0]}: Fundamentals", "{subjects[1] if len(subjects)>1 else subjects[0]}: Core Principles"]
    }},
    {{
      "phaseNumber": 2,
      "name": "Phase 2: Practice & Problem Solving",
      "duration": "Days {max(2, int(days_left*0.5)+1)}-{max(2, int(days_left*0.8))}",
      "dailyTarget": "Solve 35 problems daily under timed conditions",
      "milestone": "Complete all past paper problem sets",
      "topics": ["High-yield derivations", "Advanced numerical problems"]
    }},
    {{
      "phaseNumber": 3,
      "name": "Phase 3: Final Mocks & High-Speed Revision",
      "duration": "Days {max(3, int(days_left*0.8)+1)}-{days_left}",
      "dailyTarget": "Full length mock test every second day",
      "milestone": "Score > 90% consistently on practice exams",
      "topics": ["Formula sheet memorization", "Full exam simulation"]
    }}
  ]
}}"""

    system_prompt = "You are LighthouseAI, a world-class academic advisor. Output valid JSON only."
    raw = call_ai_model(prompt, system_prompt=system_prompt, json_mode=True, feature="roadmap")
    if raw:
        try:
            parsed = json.loads(clean_json_str(raw))
            if "phases" in parsed and len(parsed["phases"]) > 0:
                return parsed
        except Exception:
            pass

    # High-quality deterministic fallback
    return {
        "totalDays": days_left,
        "phases": [
            {
                "phaseNumber": 1,
                "name": "Phase 1: Concept Mastery & Notes Consolidation",
                "duration": f"Days 1-{max(1, int(days_left*0.5))}",
                "dailyTarget": "Cover 2 chapters daily with active recall testing",
                "milestone": "Comprehensive summary formula sheets completed",
                "topics": [f"{s} Core Foundations" for s in subjects]
            },
            {
                "phaseNumber": 2,
                "name": "Phase 2: Intensive Problem Solving & Weak Areas",
                "duration": f"Days {max(2, int(days_left*0.5)+1)}-{max(2, int(days_left*0.8))}",
                "dailyTarget": "Timed problem solving blocks (45m sprint / 15m review)",
                "milestone": "100% of previous 5 years exam papers analyzed",
                "topics": [f"{s} Advanced Problem Sets" for s in subjects]
            },
            {
                "phaseNumber": 3,
                "name": "Phase 3: Mock Exams & Peak Performance Tuning",
                "duration": f"Days {max(3, int(days_left*0.8)+1)}-{days_left}",
                "dailyTarget": "1 Full mock exam every 48 hours under strict exam conditions",
                "milestone": "Exam strategy, pacing, and formula recall perfected",
                "topics": ["Full-length timed papers", "Rapid formula review"]
            }
        ]
    }


# =========================================================
# BOT HEARTBEAT
# =========================================================

def record_heartbeat(username: str = "lighthouse_bot"):
    try:
        init_db()
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO bot_heartbeat (id, last_seen, bot_username, status) VALUES (1, ?, ?, 'online')",
            (datetime.now().isoformat(), username)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Could not record heartbeat: {e}")


def get_bot_status():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT last_seen, bot_username, status FROM bot_heartbeat WHERE id = 1")
    row = c.fetchone()
    conn.close()

    telegram_connected = False
    last_seen_str = None
    bot_username = None

    if row and row[0]:
        last_seen_str = row[0]
        bot_username = row[1]
        try:
            last_dt = datetime.fromisoformat(last_seen_str)
            if datetime.now() - last_dt < timedelta(seconds=60):
                telegram_connected = True
        except Exception:
            pass

    return {
        "telegram_connected": telegram_connected,
        "bot_username": bot_username or "lighthouse_bot",
        "last_heartbeat": last_seen_str
    }


# =========================================================
# SERVER-SIDE PDF GENERATION (REPORTLAB)
# =========================================================

def generate_timetable_pdf(timetable_data: dict) -> bytes:
    """Generate a clean, dark-themed PDF of the timetable."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=18,
        textColor=colors.HexColor('#00f5ff'),
        spaceAfter=4,
        alignment=1
    )
    sub_style = ParagraphStyle(
        'DocSub',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        textColor=colors.HexColor('#a0aec0'),
        spaceAfter=14,
        alignment=1
    )
    cell_time = ParagraphStyle('CellTime', fontName='Helvetica-Bold', fontSize=9, textColor=colors.HexColor('#00f5ff'))
    cell_act = ParagraphStyle('CellAct', fontName='Helvetica', fontSize=9, textColor=colors.white)
    cell_type = ParagraphStyle('CellType', fontName='Helvetica-Bold', fontSize=8, textColor=colors.HexColor('#ffd700'))

    elements = []
    name = timetable_data.get("title") or timetable_data.get("name") or "Lighthouse Study Timetable"
    elements.append(Paragraph("LIGHTHOUSEAI", title_style))
    elements.append(Paragraph(f"{name} • Generated: {datetime.now().strftime('%b %d, %Y')}", sub_style))
    elements.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#00f5ff'), spaceAfter=14))

    table_data = [
        [
            Paragraph("<b>TIME</b>", cell_time),
            Paragraph("<b>ACTIVITY / MODULE</b>", cell_time),
            Paragraph("<b>TYPE</b>", cell_time),
            Paragraph("<b>SUBJECT</b>", cell_time)
        ]
    ]

    slots = timetable_data.get("slots", [])
    for s in slots:
        t_str = s.get("time", "")
        act_str = s.get("activity", "")
        type_str = s.get("type", "study").upper()
        subj_str = s.get("subject", "General")
        table_data.append([
            Paragraph(t_str, cell_time),
            Paragraph(act_str, cell_act),
            Paragraph(type_str, cell_type),
            Paragraph(subj_str, cell_act)
        ])

    table = Table(table_data, colWidths=[1.5 * inch, 3.2 * inch, 1.2 * inch, 1.6 * inch])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0a192f')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#00f5ff')),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#1e3a8a')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#020814'), colors.HexColor('#0b132b')])
    ]))

    elements.append(table)
    elements.append(Spacer(1, 16))
    summary = timetable_data.get("summary", "")
    if summary:
        sum_style = ParagraphStyle('Summary', fontName='Helvetica-Oblique', fontSize=9, textColor=colors.HexColor('#8892b0'))
        elements.append(Paragraph(f"Strategy Note: {summary}", sum_style))

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


def generate_roadmap_pdf(roadmap_data: dict) -> bytes:
    """Generate a clean, structured PDF of the study roadmap."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=18,
        textColor=colors.HexColor('#00f5ff'),
        spaceAfter=4,
        alignment=1
    )
    sub_style = ParagraphStyle(
        'DocSub',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        textColor=colors.HexColor('#a0aec0'),
        spaceAfter=14,
        alignment=1
    )
    phase_header = ParagraphStyle('PhaseHead', fontName='Helvetica-Bold', fontSize=12, textColor=colors.HexColor('#00f5ff'), spaceAfter=4)
    item_title = ParagraphStyle('ItemTitle', fontName='Helvetica-Bold', fontSize=9, textColor=colors.HexColor('#ffd700'))
    item_desc = ParagraphStyle('ItemDesc', fontName='Helvetica', fontSize=9, textColor=colors.white)

    elements = []
    exam_date = roadmap_data.get("examDate", "Upcoming Exam")
    elements.append(Paragraph("LIGHTHOUSEAI", title_style))
    elements.append(Paragraph(f"EXAM STUDY ROADMAP • Target Date: {exam_date}", sub_style))
    elements.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#00f5ff'), spaceAfter=14))

    phases = roadmap_data.get("phases", [])
    for p in phases:
        p_num = p.get("phaseNumber", 1)
        p_name = p.get("name", f"Phase {p_num}")
        p_dur = p.get("duration", "")
        p_target = p.get("dailyTarget", "")
        p_mile = p.get("milestone", "")
        topics = p.get("topics", [])

        phase_elements = [
            Paragraph(f"{p_name} ({p_dur})", phase_header),
            Paragraph(f"<b>Target:</b> {p_target}", item_desc),
            Paragraph(f"<b>Milestone:</b> {p_mile}", item_desc),
            Spacer(1, 4)
        ]

        if topics:
            topics_text = " • ".join(topics)
            phase_elements.append(Paragraph(f"<b>Core Topics:</b> {topics_text}", item_desc))

        phase_table = Table([[phase_elements]], colWidths=[7.4 * inch])
        phase_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#0b132b')),
            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#1e3a8a')),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('LEFTPADDING', (0, 0), (-1, -1), 10),
            ('RIGHTPADDING', (0, 0), (-1, -1), 10)
        ]))

        elements.append(KeepTogether([phase_table, Spacer(1, 10)]))

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


# =========================================================
# LIGHTHOUSE VOICE ASSISTANT ENGINE (GOOGLE ASSISTANT STYLE)
# =========================================================

def process_voice_query(query: str) -> dict:
    """
    Process conversational voice queries for Web Dashboard Voice Assistant.
    Answers generic questions, performs google searches, handles time, battery,
    alarms, tasks (add/edit/delete/done), sticky notes, schedules, and roadmaps
    with speech-optimized responses and Gemini TTS audio.
    """
    if not query or not query.strip():
        resp = "I'm listening. How can I assist with your workspace or studies?"
        return {"reply": resp, "spoken": resp, "action": "none"}

    clean_q = query.strip()
    lower_q = clean_q.lower()
    now_dt = datetime.now()
    now_time_str = now_dt.strftime("%I:%M %p").lstrip("0")
    now_date_str = now_dt.strftime("%A, %B %d, %Y")

    def _finalize_response(reply_text: str, spoken_text: str, action: str, **kwargs) -> dict:
        clean_spoken = re.sub(r'[*#_`]', '', spoken_text).strip()
        result = {
            "reply": reply_text,
            "spoken": clean_spoken,
            "action": action,
            **kwargs
        }
        # Generate Gemini TTS audio for direct audio playback
        try:
            tts_data = generate_gemini_tts(clean_spoken, voice_name="Puck", feature="voice")
            if tts_data and "audio_base64" in tts_data:
                result["audio_base64"] = tts_data["audio_base64"]
                result["audio_mime"] = tts_data.get("mime_type", "audio/wav")
        except Exception:
            pass
        return result

    # 1. Time query
    if any(phrase in lower_q for phrase in ["what time", "what is the time", "current time", "tell me the time", "what's the time"]):
        reply = f"The time is {now_time_str} on {now_dt.strftime('%A')}."
        return _finalize_response(reply, reply, "time")

    # 2. Date query
    if any(phrase in lower_q for phrase in ["what date", "what is the date", "what's the date", "what day is today", "what day is it"]):
        reply = f"Today is {now_date_str}."
        return _finalize_response(reply, reply, "date")

    # 3. Battery & Power query
    if any(phrase in lower_q for phrase in ["battery", "device battery", "how much charge", "is it charging", "is laptop charging", "is device charging", "battery percent", "battery status"]):
        b_info = get_battery_info()
        pct = b_info.get("percent", 100)
        charging = b_info.get("is_charging", False)
        state_str = "charging (plugged in)" if charging else "on battery (discharging)"
        reply = f"Your device battery is at {pct}% and is currently {state_str}."
        return _finalize_response(reply, reply, "battery", data=b_info)

    # 4. Google Search query
    google_match = re.search(r'(?:search\s+google\s+(?:for\s+)?|google\s+search\s+(?:for\s+)?|search\s+for\s+(.+?)\s+on\s+google|google\s+(.+)|search\s+(?:the\s+web\s+for\s+|online\s+for\s+)?)(.+)', lower_q)
    if ("google" in lower_q or lower_q.startswith("search ") or lower_q.startswith("lookup ") or lower_q.startswith("look up ")) and google_match:
        groups = [g for g in google_match.groups() if g]
        search_term = groups[-1] if groups else clean_q
        search_term = re.sub(r'^(for|about|on)\s+', '', search_term.strip())
        if not search_term:
            search_term = clean_q

        search_summary_prompt = f"Provide a direct, concise 1-2 sentence factual answer for someone searching for: '{search_term}'. Keep it conversational and natural for voice speaking."
        ai_summary = call_ai_model(search_summary_prompt, feature="mentor")
        google_url = f"https://www.google.com/search?q={requests.utils.quote(search_term)}"

        spoken_text = ai_summary if ai_summary else f"Searching Google for {search_term}."
        reply_md = f"{spoken_text}\n\n[Open Google Search: '{search_term}']({google_url})"
        return _finalize_response(reply_md, spoken_text, "google_search", search_term=search_term, url=google_url)

    # 5. Delete Task intent via voice
    del_match = re.search(r'(?:delete\s+task|remove\s+task|cancel\s+task)\s+(\d+)', clean_q, re.IGNORECASE)
    if del_match:
        num = int(del_match.group(1))
        today_tasks = get_tasks(now_dt.strftime("%Y-%m-%d"))
        if 1 <= num <= len(today_tasks):
            target_id = today_tasks[num - 1][0]
            target_name = today_tasks[num - 1][1]
            delete_task(target_id)
            reply = f"Deleted Task #{num}: {target_name} from your workspace."
            return _finalize_response(reply, reply, "task_deleted", task_id=target_id)
        elif delete_task(num):
            reply = f"Deleted Task #{num} from your workspace."
            return _finalize_response(reply, reply, "task_deleted", task_id=num)

    # 6. Rename / Edit Task intent via voice
    rename_match = re.search(r'(?:rename\s+task|change\s+task|edit\s+task)\s+(\d+)\s+(?:to\s+|as\s+)?(.+)', clean_q, re.IGNORECASE)
    if rename_match:
        num = int(rename_match.group(1))
        new_content = rename_match.group(2).strip()
        today_tasks = get_tasks(now_dt.strftime("%Y-%m-%d"))
        if 1 <= num <= len(today_tasks):
            target_id = today_tasks[num - 1][0]
            edit_task(target_id, content=new_content)
            reply = f"Renamed Task #{num} to: {new_content}."
            return _finalize_response(reply, reply, "task_edited", task_id=target_id)
        else:
            edit_task(num, content=new_content)
            reply = f"Updated Task #{num} to: {new_content}."
            return _finalize_response(reply, reply, "task_edited", task_id=num)

    # 7. Complete Task intent via voice
    done_match = re.search(r'(?:mark\s+task\s+(\d+)\s+(?:as\s+)?done|complete\s+task\s+(\d+)|finish\s+task\s+(\d+))', clean_q, re.IGNORECASE)
    if done_match:
        num = int(done_match.group(1) or done_match.group(2) or done_match.group(3))
        today_tasks = get_tasks(now_dt.strftime("%Y-%m-%d"))
        if 1 <= num <= len(today_tasks):
            target_id = today_tasks[num - 1][0]
            target_name = today_tasks[num - 1][1]
            mark_done(target_id, 1)
            reply = f"Great work! Completed task #{num}: {target_name}."
            return _finalize_response(reply, reply, "task_done", task_id=target_id)
        else:
            mark_done(num, 1)
            reply = f"Completed Task #{num}."
            return _finalize_response(reply, reply, "task_done", task_id=num)

    # 8. Add Task intent via voice
    add_match = re.search(r'(?:add\s+task|create\s+task|remind\s+me\s+to|schedule\s+task|schedule)\s+(.+)', clean_q, re.IGNORECASE)
    if add_match:
        task_text = add_match.group(1).strip()
        time_match = re.search(r'\b(?:at|for)\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b', task_text, re.IGNORECASE)
        due_str = None
        if time_match:
            time_raw = time_match.group(1)
            task_clean = task_text[:time_match.start()] + task_text[time_match.end():]
            task_clean = task_clean.strip()
            due_str = f"{now_dt.strftime('%Y-%m-%d')}T{time_raw}"
        else:
            task_clean = task_text

        tid = add_task(content=task_clean or task_text, due_time=due_str)
        today_tasks = get_tasks(now_dt.strftime("%Y-%m-%d"))
        seq = len(today_tasks)
        reply = f"Added Task #{seq}: {task_clean or task_text} to your today's schedule."
        return _finalize_response(reply, reply, "task_added", task_id=tid)

    # 9. Sticky Note intent via voice (Add note)
    note_add_match = re.search(r'(?:create\s+note|add\s+(?:sticky\s+)?note|save\s+note|take\s+a\s+note)\s+(.+)', clean_q, re.IGNORECASE)
    if note_add_match:
        note_text = note_add_match.group(1).strip()
        save_note(title=f"Note: {note_text[:25]}", content=note_text, color="amber")
        reply = f"Created sticky note: {note_text}."
        return _finalize_response(reply, reply, "note_added")

    # 10. Set alarm intent via voice
    alarm_set_match = re.search(r'set\s+(?:an\s+|a\s+)?(?:alarm|wake\s*up\s*alarm)\s+(?:for|at)\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)', clean_q, re.IGNORECASE)
    if alarm_set_match:
        alarm_time = alarm_set_match.group(1).upper()
        aid = add_alarm(time_str=alarm_time, label="Voice Wake Up Alarm")
        reply = f"Alarm set for {alarm_time}."
        return _finalize_response(reply, reply, "alarm_set", alarm_id=aid, time=alarm_time)

    # 11. Delete alarm intent via voice
    alarm_del_match = re.search(r'(?:delete\s+alarm|remove\s+alarm|turn\s+off\s+alarm|cancel\s+alarm)\s+(\d+)', clean_q, re.IGNORECASE)
    if alarm_del_match:
        aid = int(alarm_del_match.group(1))
        delete_alarm(aid)
        reply = f"Deleted Alarm #{aid}."
        return _finalize_response(reply, reply, "alarm_deleted", alarm_id=aid)

    # 12. Schedule / Tasks overview query
    if any(phrase in lower_q for phrase in ["my schedule", "my tasks", "what's on my schedule", "what do i have today", "tasks for today", "show schedule", "today's schedule"]):
        today_tasks = get_tasks(now_dt.strftime("%Y-%m-%d"))
        if not today_tasks:
            reply = "You have no tasks scheduled for today. Your schedule is clear!"
            return _finalize_response(reply, reply, "schedule", tasks=[])

        pending = [t for t in today_tasks if not t[3]]
        if pending:
            top_tasks = [f"{t[1]}" + (f" at {t[2].split('T')[-1][:5]}" if t[2] else "") for t in pending[:3]]
            reply = f"You have {len(today_tasks)} total tasks today with {len(pending)} pending. Upcoming: {', '.join(top_tasks)}."
        else:
            reply = f"All {len(today_tasks)} tasks for today are completed! Outstanding work."
        return _finalize_response(reply, reply, "schedule", tasks=today_tasks)

    # 13. Alarms query
    if any(phrase in lower_q for phrase in ["my alarms", "active alarms", "show alarms", "list alarms"]):
        alarms = get_active_alarms()
        if not alarms:
            reply = "You currently have no active alarms set."
        else:
            alarm_list = ", ".join([f"{a['time']} for {a['label']}" for a in alarms])
            reply = f"You have {len(alarms)} active alarm{'s' if len(alarms)>1 else ''}: {alarm_list}."
        return _finalize_response(reply, reply, "alarms", data=alarms)

    # 14. General Academic & AI Knowledge Question
    system_prompt = f"""You are Lighthouse Voice Assistant, an intelligent, helpful voice co-pilot for Shlok (like Google Assistant).
Current Date/Time: {now_date_str} {now_time_str}.
Keep your answers CONCISE, DIRECT, and NATURAL FOR SPEECH (maximum 2-3 sentences).
Do not use markdown formatting or bullet points since this will be spoken aloud to the user.
If it is a factual, scientific, math, or generic question, answer accurately and immediately."""

    ai_reply = call_ai_model(clean_q, system_prompt=system_prompt, feature="mentor")
    if ai_reply:
        clean_spoken = re.sub(r'[*#_`]', '', ai_reply).strip()
        return _finalize_response(clean_spoken, clean_spoken, "ai_response")

    fallback = "I heard you, Shlok! How can I assist with your tasks or study goals right now?"
    return _finalize_response(fallback, fallback, "fallback")



def generate_gemini_tts(text: str, voice_name: str = "Puck", feature: str = "voice") -> dict | None:
    """
    Generate natural voice audio using Gemini Speech / Google Cloud TTS.
    Iterates through dedicated voice keys and key pools with automatic rotation.
    """
    if not text or not text.strip():
        return None
    gemini_keys = get_gemini_keys_for_feature(feature)
    if not gemini_keys:
        return None

    clean_text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    clean_text = re.sub(r'[*#_`]', '', clean_text).strip()

    models_to_try = [
        "gemini-3.8-flash",
        "gemini-3.5-flash",
        "gemini-3.7-flash",
        "gemini-flash-latest"
    ]
    for key_idx, g_key in enumerate(gemini_keys):
        # 1. Try Gemini Audio Modality with active models
        for model in models_to_try:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={g_key}"
                payload = {
                    "contents": [{"parts": [{"text": clean_text}]}],
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {
                                    "voiceName": voice_name
                                }
                            }
                        }
                    }
                }
                resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=6)
                if resp.status_code == 200:
                    data = resp.json()
                    cand = data.get("candidates", [{}])[0]
                    parts = cand.get("content", {}).get("parts", [])
                    for p in parts:
                        if "inlineData" in p and p["inlineData"].get("data"):
                            return {
                                "audio_base64": p["inlineData"]["data"],
                                "mime_type": p["inlineData"].get("mimeType", "audio/wav")
                            }
                elif resp.status_code == 429:
                    print(f"[Gemini TTS Key #{key_idx+1} 429 Quota] Rotating to next voice key...")
                    break
            except Exception:
                continue

        # 2. Try Google Cloud TTS Synthesize endpoint with same API key
        try:
            url = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={g_key}"
            payload = {
                "input": {"text": clean_text},
                "voice": {"languageCode": "en-US", "name": "en-US-Neural2-F"},
                "audioConfig": {"audioEncoding": "MP3"}
            }
            resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=6)
            if resp.status_code == 200:
                audio_content = resp.json().get("audioContent")
                if audio_content:
                    return {
                        "audio_base64": audio_content,
                        "mime_type": "audio/mp3"
                    }
        except Exception:
            pass

    return None



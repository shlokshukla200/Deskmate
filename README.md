# LighthouseAI
> Academic Command Center, Intelligent Daily Scheduler & Study Workspace

LighthouseAI is a landscape study console and intelligent assistant built for high-performance student scheduling, exam preparation, and focus management.

---

## Key Features

1. **24-Hour Dynamic Schedule Timeline**
   - Full 24-hour vertical timeline showing all scheduled tasks, study blocks, and breaks.
   - Collapsible task sidebar with inline time picker and task notes.
   - Live real-time current time indicator line.

2. **Google Keep Style Sticky Notes**
   - Responsive masonry grid with 12 color palettes.
   - Pinned vs. Other notes separation.
   - Rich support for plain text, interactive checklists, and canvas sketches.

3. **Intelligent Timetable Generator**
   - Generates structured, conflict-free weekly timetables tailored to your wake time, subjects, and study targets.
   - One-click client-side and server-side PDF export.

4. **Structured Study Roadmap**
   - Multi-phase exam roadmap generator with daily milestones and revision cycles.
   - Export roadmap as high-resolution PDF document.

5. **Academic Mentor Chat**
   - Context-aware academic co-pilot that knows your tasks and schedule.
   - Live animated thinking state, timestamped messages, and thought duration metrics.

6. **Deep Focus Space Timer**
   - Fullscreen cosmic Pomodoro timer with ambient starfield animations.
   - Configurable focus, short break, and long break intervals.

7. **Telegram Real-time Sync**
   - Add tasks, query pending items, and receive reminders via Telegram.
   - Conflict detection when scheduling overlapping events.

---

## Quick Start

### 1. Configuration
Ensure your `.env` file is configured in the project root:
```ini
GEMINI_API_KEY=your_gemini_api_key_here
TELEGRAM_TOKEN=your_telegram_bot_token_here
ALLOWED_CHAT_ID=your_chat_id
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Launch Lighthouse Console
```bash
python dashboard.py
```
Open your browser and navigate to:
```
http://127.0.0.1:8420
```

### 4. (Optional) Run Telegram Bot Sync
In a separate terminal or background session:
```bash
python agent.py
```

---

## Architecture & Technology Stack
- **Backend**: Python 3, Flask, SQLite3
- **Frontend**: Vanilla JavaScript (ES6+), Modern CSS3, HTML5
- **Typography**: Orbitron, Inter, Cinzel, Exo 2, JetBrains Mono
- **PDF Generation**: jsPDF (client-side) & ReportLab (server-side)

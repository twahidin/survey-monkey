# Survey Chatbot Redesign — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Redesign the survey chatbot with agentic tool-use (images, videos, button options), glassmorphism UI, responsive media panel, and admin response analytics.

**Architecture:** Claude tool_use API for rich content (show_image, show_buttons, show_video). Unsplash/Pexels for media sourcing. Backend processes tool calls and streams results as typed SSE events. Frontend renders media panel (top on mobile, side on desktop) and interactive button components. Admin gets AI-generated insights (sentiment, themes, engagement) cached in a new DB table.

**Tech Stack:** FastAPI, SQLAlchemy, Anthropic tool_use API, Unsplash API, Pexels API, vanilla HTML/CSS/JS with glassmorphism.

---

### Task 1: Add SurveyInsight Model & Migration

**Files:**
- Modify: `models.py` (add new class after AnalysisMessage)
- Modify: `database.py` (add migration in init_db)

**Step 1: Add the SurveyInsight model to models.py**

Add after the `AnalysisMessage` class at bottom of `models.py`:

```python
class SurveyInsight(Base):
    __tablename__ = "survey_insights"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    survey_id = Column(UUID(as_uuid=True), ForeignKey("surveys.id"), nullable=False, index=True)
    insights_json = Column(Text, nullable=False)
    generated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
```

**Step 2: Add migration to database.py**

In `init_db()`, after the existing `ALTER TABLE` for facilitator_intro, add:

```python
conn.execute(text(
    "CREATE TABLE IF NOT EXISTS survey_insights ("
    "id UUID PRIMARY KEY DEFAULT gen_random_uuid(), "
    "survey_id UUID NOT NULL REFERENCES surveys(id), "
    "insights_json TEXT NOT NULL, "
    "generated_at TIMESTAMPTZ DEFAULT now())"
))
conn.execute(text(
    "CREATE INDEX IF NOT EXISTS ix_survey_insights_survey_id ON survey_insights(survey_id)"
))
conn.commit()
```

**Step 3: Add import of SurveyInsight to main.py**

In `main.py`, update the models import line:

```python
from models import (
    Survey, SurveyStatus, Participant, ParticipantStatus,
    ChatMessage, AdminUser, AnalysisMessage, SurveyInsight,
)
```

**Step 4: Test locally**

Run: `python -c "from models import SurveyInsight; print('OK')"`
Expected: `OK`

**Step 5: Commit**

```bash
git add models.py database.py main.py
git commit -m "feat: add SurveyInsight model and migration"
```

---

### Task 2: Add Tool-Use Constants & Media Fetching Helpers

**Files:**
- Modify: `main.py` (add constants and helper functions after existing helpers section)

**Step 1: Add tool definitions and media helpers**

After the `get_claude_client()` function in `main.py`, add:

```python
import httpx

# ──────────────────────────── Tool-Use ────────────────────────────

UNSPLASH_ACCESS_KEY = os.environ.get("UNSPLASH_ACCESS_KEY", "")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")

SURVEY_TOOLS = [
    {
        "name": "show_image",
        "description": "Show a relevant image to help the participant understand or engage with the topic. Use when visual context would enrich the conversation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query for finding a relevant image"},
                "caption": {"type": "string", "description": "Optional caption to display with the image"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "show_buttons",
        "description": "Present clickable option buttons instead of asking the participant to type. Use for questions with discrete answer choices (2-6 options).",
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question being asked"},
                "options": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["label", "value"],
                    },
                    "description": "2-6 options to present as buttons",
                },
                "allow_multiple": {
                    "type": "boolean",
                    "description": "If true, participant can select multiple options",
                    "default": False,
                },
            },
            "required": ["question", "options"],
        },
    },
    {
        "name": "show_video",
        "description": "Show a relevant short video clip. Use sparingly, only when video would significantly aid understanding.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query for finding a relevant video"},
                "caption": {"type": "string", "description": "Optional caption"},
            },
            "required": ["query"],
        },
    },
]

TOOL_USE_PROMPT = (
    "\n\n[TOOLS: You have tools available to enrich the conversation. "
    "Use show_buttons when asking questions with clear discrete choices (e.g., frequency, ratings, yes/no). "
    "Use show_image when a visual would help the participant understand or connect with the topic. "
    "Use show_video sparingly, only when a video clip would significantly help. "
    "You can combine text with tool calls — write your message text AND call a tool in the same turn.]"
)


async def fetch_unsplash_image(query: str) -> dict:
    """Fetch a relevant image from Unsplash. Returns {url, alt, caption} or empty dict."""
    if not UNSPLASH_ACCESS_KEY:
        return {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://api.unsplash.com/search/photos",
                params={"query": query, "per_page": 1, "orientation": "landscape"},
                headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"},
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("results"):
                    photo = data["results"][0]
                    return {
                        "url": photo["urls"]["regular"],
                        "alt": photo.get("alt_description", query),
                    }
    except Exception:
        pass
    return {}


async def fetch_pexels_video(query: str) -> dict:
    """Fetch a relevant video from Pexels. Returns {url, poster} or empty dict."""
    if not PEXELS_API_KEY:
        return {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://api.pexels.com/videos/search",
                params={"query": query, "per_page": 1},
                headers={"Authorization": PEXELS_API_KEY},
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("videos"):
                    video = data["videos"][0]
                    # Pick the smallest HD file
                    files = video.get("video_files", [])
                    mp4 = next((f for f in files if f.get("quality") == "hd" and "mp4" in f.get("file_type", "")), None)
                    if not mp4 and files:
                        mp4 = files[0]
                    if mp4:
                        return {
                            "url": mp4["link"],
                            "poster": video.get("image", ""),
                        }
    except Exception:
        pass
    return {}
```

**Step 2: Add httpx to requirements.txt**

Append to `requirements.txt`:

```
httpx==0.28.1
```

**Step 3: Test import**

Run: `python -c "from main import SURVEY_TOOLS; print(len(SURVEY_TOOLS), 'tools')"`
Expected: `3 tools`

**Step 4: Commit**

```bash
git add main.py requirements.txt
git commit -m "feat: add tool-use definitions and media fetching helpers"
```

---

### Task 3: Rewrite Chat Stream Endpoint with Tool-Use

**Files:**
- Modify: `main.py` (rewrite `_chat_stream_generator` and `survey_chat_stream`, update `join_survey`)

**Step 1: Rewrite the streaming generator to handle tool_use**

Replace the entire `_chat_stream_generator` function and `survey_chat_stream` endpoint with:

```python
async def _process_tool_call(tool_name: str, tool_input: dict) -> list:
    """Process a tool call and return SSE event dicts to send to the frontend."""
    events = []
    if tool_name == "show_image":
        media = await fetch_unsplash_image(tool_input["query"])
        if media:
            events.append({
                "t": "media",
                "type": "image",
                "url": media["url"],
                "alt": media.get("alt", ""),
                "caption": tool_input.get("caption", ""),
            })
    elif tool_name == "show_video":
        media = await fetch_pexels_video(tool_input["query"])
        if media:
            events.append({
                "t": "media",
                "type": "video",
                "url": media["url"],
                "poster": media.get("poster", ""),
                "caption": tool_input.get("caption", ""),
            })
    elif tool_name == "show_buttons":
        events.append({
            "t": "buttons",
            "question": tool_input.get("question", ""),
            "options": tool_input.get("options", []),
            "allow_multiple": tool_input.get("allow_multiple", False),
        })
    return events


async def _chat_stream_generator_v2(
    client, system: str, history: list, participant, db, near_limit: bool
):
    """Stream text + tool results as SSE events."""
    full_text = []
    tool_events = []

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=system,
            messages=history,
            tools=SURVEY_TOOLS,
        )

        # Process content blocks
        for block in response.content:
            if block.type == "text":
                # Send text in small chunks to simulate streaming
                text = block.text
                full_text.append(text)
                chunk_size = 12
                for i in range(0, len(text), chunk_size):
                    chunk = text[i:i + chunk_size]
                    yield f"data: {json.dumps({'t': 'chunk', 'v': chunk})}\n\n"
            elif block.type == "tool_use":
                events = await _process_tool_call(block.name, block.input)
                tool_events.extend(events)

        # Send tool events after text
        for event in tool_events:
            yield f"data: {json.dumps(event)}\n\n"

    except Exception as e:
        yield f"data: {json.dumps({'t': 'error', 'v': str(e)})}\n\n"
        return

    assistant_text = "".join(full_text)
    if not assistant_text and tool_events:
        # If Claude only used tools with no text, save a placeholder
        assistant_text = "(presented interactive content)"

    # Save assistant message
    db.add(ChatMessage(participant_id=participant.id, role="assistant", content=assistant_text))

    # Save tool events as metadata in the message content (for transcript replay)
    if tool_events:
        meta = json.dumps(tool_events)
        db.add(ChatMessage(
            participant_id=participant.id,
            role="assistant",
            content=f"[TOOL_EVENTS]{meta}",
        ))

    is_complete = near_limit
    if is_complete:
        now = datetime.now(timezone.utc)
        participant.status = ParticipantStatus.COMPLETED
        participant.completed_at = now
        participant.duration_seconds = (now - participant.started_at).total_seconds()

    db.commit()
    yield f"data: {json.dumps({'t': 'done', 'is_complete': is_complete})}\n\n"


@app.post("/api/survey/chat/stream")
async def survey_chat_stream(req: ChatRequest, db: Session = Depends(get_db)):
    """Stream the assistant reply as SSE with tool-use support."""
    participant = (
        db.query(Participant)
        .filter(Participant.session_token == req.session_token)
        .options(joinedload(Participant.messages), joinedload(Participant.survey))
        .first()
    )
    if not participant:
        raise HTTPException(status_code=404, detail="Session not found")
    if participant.status != ParticipantStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="This survey session has ended")
    if participant.survey.status != SurveyStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="This survey has been closed")

    db.add(ChatMessage(participant_id=participant.id, role="user", content=req.message))
    db.commit()

    msgs = sorted(participant.messages, key=lambda m: m.created_at)
    # Filter out tool event meta-messages from history sent to Claude
    history = [
        {"role": m.role, "content": m.content}
        for m in msgs
        if not m.content.startswith("[TOOL_EVENTS]")
    ]
    history.append({"role": "user", "content": req.message})

    user_message_count = sum(1 for m in history if m["role"] == "user")
    near_limit = user_message_count >= participant.survey.max_messages

    system = participant.survey.system_prompt + CONVERSATIONAL_PROMPT + TOOL_USE_PROMPT
    if near_limit:
        system += (
            "\n\n[SYSTEM NOTE: This is the participant's last allowed message. "
            "Thank them for their time, provide a brief summary of what you gathered, "
            "and end the conversation warmly. Do not use tools in this final message.]"
        )

    client = get_claude_client()
    gen = _chat_stream_generator_v2(
        client, system, history, participant, db, near_limit
    )
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

**Step 2: Update join_survey to include tool prompt**

In `join_survey()`, update the system prompt construction to include TOOL_USE_PROMPT:

```python
system += CONVERSATIONAL_PROMPT + TOOL_USE_PROMPT
```

(Replace the existing `system += CONVERSATIONAL_PROMPT` line.)

**Step 3: Keep the old non-streaming endpoint working**

The old `survey_chat` (non-streaming) endpoint can remain as-is for backwards compatibility — it just won't have tool support. This is fine.

**Step 4: Commit**

```bash
git add main.py
git commit -m "feat: rewrite chat stream with tool-use support"
```

---

### Task 4: Add Insights API Endpoints

**Files:**
- Modify: `main.py` (add two new endpoints after the analysis chat section)

**Step 1: Add insights endpoints**

Add after the `get_analysis_history` endpoint:

```python
# ══════════════════════════════════════════════════════════════════
#  ADMIN - SURVEY INSIGHTS (AI-generated analytics)
# ══════════════════════════════════════════════════════════════════

def _build_insights_prompt(survey, participants_data: list) -> str:
    """Build the prompt for generating survey insights."""
    convos = []
    for p in participants_data:
        if p["messages"]:
            msgs = "\n".join(f"  {m['role']}: {m['content']}" for m in p["messages"] if not m["content"].startswith("[TOOL_EVENTS]"))
            convos.append(f"[Participant {p['id'][:8]} | {p['status']} | {p['message_count']} msgs]\n{msgs}")

    return (
        f"Analyze these survey conversations and return a JSON object.\n\n"
        f"Survey: {survey.title}\nTopic: {survey.topic}\n"
        f"Total participants: {len(participants_data)}\n\n"
        f"--- CONVERSATIONS ---\n\n" + "\n\n".join(convos) + "\n\n"
        f"Return ONLY valid JSON with this exact structure:\n"
        f'{{\n'
        f'  "sentiment": {{"positive": <count>, "neutral": <count>, "negative": <count>}},\n'
        f'  "themes": [{{"name": "<theme>", "count": <mentions>}}, ...],\n'
        f'  "participants": [\n'
        f'    {{"id": "<first 8 chars>", "sentiment": "positive|neutral|negative", '
        f'"engagement": <1-10>, "themes": ["<theme>", ...]}},\n'
        f'    ...\n'
        f'  ]\n'
        f'}}'
    )


def _generate_insights(survey, db: Session) -> dict:
    """Generate fresh insights using Claude."""
    # Load all participants with messages
    participants_data = []
    for p in sorted(survey.participants, key=lambda x: x.started_at):
        msgs = sorted(p.messages, key=lambda m: m.created_at)
        participants_data.append({
            "id": str(p.id),
            "status": p.status.value,
            "message_count": len([m for m in msgs if not m.content.startswith("[TOOL_EVENTS]")]),
            "messages": [
                {"role": m.role, "content": m.content, "created_at": m.created_at.isoformat()}
                for m in msgs
            ],
        })

    if not participants_data:
        return {"sentiment": {"positive": 0, "neutral": 0, "negative": 0}, "themes": [], "participants": []}

    prompt = _build_insights_prompt(survey, participants_data)
    client = get_claude_client()
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system="You are a survey data analyst. Return ONLY valid JSON, no markdown fences, no explanation.",
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3].strip()
    if raw.startswith("json"):
        raw = raw[4:].strip()

    try:
        insights = json.loads(raw)
    except json.JSONDecodeError:
        insights = {"sentiment": {"positive": 0, "neutral": 0, "negative": 0}, "themes": [], "participants": [], "error": "Failed to parse insights"}

    # Cache in DB
    existing = db.query(SurveyInsight).filter(SurveyInsight.survey_id == survey.id).first()
    now = datetime.now(timezone.utc)
    if existing:
        existing.insights_json = json.dumps(insights)
        existing.generated_at = now
    else:
        db.add(SurveyInsight(
            survey_id=survey.id,
            insights_json=json.dumps(insights),
            generated_at=now,
        ))
    db.commit()
    return insights


@app.get("/api/surveys/{survey_id}/insights")
def get_survey_insights(
    survey_id: str,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    survey = (
        db.query(Survey)
        .filter(Survey.id == survey_id, Survey.admin_id == admin.id)
        .options(joinedload(Survey.participants).joinedload(Participant.messages))
        .first()
    )
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")

    # Check cache (stale after 5 minutes)
    cached = db.query(SurveyInsight).filter(SurveyInsight.survey_id == survey_id).first()
    if cached:
        age = (datetime.now(timezone.utc) - cached.generated_at).total_seconds()
        if age < 300:
            return {"insights": json.loads(cached.insights_json), "generated_at": cached.generated_at.isoformat(), "cached": True}

    insights = _generate_insights(survey, db)
    return {"insights": insights, "generated_at": datetime.now(timezone.utc).isoformat(), "cached": False}


@app.post("/api/surveys/{survey_id}/insights/regenerate")
def regenerate_survey_insights(
    survey_id: str,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    survey = (
        db.query(Survey)
        .filter(Survey.id == survey_id, Survey.admin_id == admin.id)
        .options(joinedload(Survey.participants).joinedload(Participant.messages))
        .first()
    )
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")
    insights = _generate_insights(survey, db)
    return {"insights": insights, "generated_at": datetime.now(timezone.utc).isoformat(), "cached": False}
```

**Step 2: Commit**

```bash
git add main.py
git commit -m "feat: add insights generation and caching endpoints"
```

---

### Task 5: Rewrite Survey Chatbot Frontend (survey.html)

**Files:**
- Rewrite: `templates/survey.html` (complete replacement)

**Step 1: Write the new survey.html**

This is a complete rewrite. The file contains the glassmorphism join screen, responsive chat layout with media panel, button/multi-select components, typing indicator, and completion screen. All in a single HTML file with embedded CSS and JS.

Key sections in the new file:

**CSS (~300 lines):**
- CSS custom properties for glassmorphism tokens
- Gradient background with animated floating orbs
- Glass card styles (backdrop-filter, borders)
- Responsive layout: mobile (single column, media on top) vs desktop (side-by-side)
- Chat message styles (glass bot messages, gradient user messages)
- Button option grid (glass pills, hover glow, selected state)
- Media panel (slide-in animation, dismiss button)
- Typing indicator (three-dot wave)
- Completion screen (animated checkmark, confetti)
- Progress bar in header

**HTML structure:**
```
#join-screen (glass card, code input, gradient orbs)
#chat-screen
  #media-panel (image/video container, dismiss button)
  #chat-area
    .chat-header (title, progress, end button)
    #messages (chat messages + button components)
    .chat-input-area (glass input bar)
#complete-screen (animated checkmark, thank you)
```

**JS (~200 lines):**
- `joinSurvey()` — join flow, same API
- `sendMessage()` — sends text, calls SSE stream endpoint
- `handleSSE()` — processes chunk/media/buttons/done events
- `renderMedia(data)` — shows image or video in media panel
- `renderButtons(data)` — renders button grid in chat, handles selection
- `dismissMedia()` — hides media panel
- `endSurvey()` — manual end
- Progress tracking (message count / max)

Write the full file. See the design doc for exact colors, layout, and behavior specs.

**Step 2: Test in browser**

Open `http://localhost:8000/` and verify:
- Join screen shows glass card on gradient background
- After joining, chat screen shows with glass header
- Messages appear with correct styling
- Typing indicator shows three dots
- Responsive: resize to mobile width, verify single-column layout

**Step 3: Commit**

```bash
git add templates/survey.html
git commit -m "feat: rewrite survey UI with glassmorphism and tool-use support"
```

---

### Task 6: Rewrite Admin Dashboard Frontend (admin.html)

**Files:**
- Rewrite: `templates/admin.html` (complete replacement)

**Step 1: Write the new admin.html**

Complete rewrite with glassmorphism style and new Insights tab. The file contains:

**CSS additions/changes:**
- Same glassmorphism design tokens as survey.html
- Gradient background
- Glass cards for all panels
- New Insights tab styles: sentiment bars, theme list, engagement histogram
- Enhanced participant table with color-coded sentiment/engagement
- Sortable table headers

**New tab: Insights** (inserted as first tab):
```html
<div class="tab-content" id="tab-insights">
  <div class="insights-grid">
    <!-- Sentiment Overview: horizontal bar chart -->
    <div class="glass-card">
      <h3>Sentiment Overview</h3>
      <div id="sentiment-chart"></div>
    </div>
    <!-- Top Themes: ranked list -->
    <div class="glass-card">
      <h3>Top Themes</h3>
      <div id="themes-list"></div>
    </div>
    <!-- Engagement Distribution: histogram -->
    <div class="glass-card">
      <h3>Engagement Distribution</h3>
      <div id="engagement-chart"></div>
    </div>
  </div>
  <!-- Enhanced participant table with sentiment & engagement -->
  <div class="glass-card" style="margin-top:20px">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <h3>Participant Details</h3>
      <button class="btn btn-sm btn-outline" onclick="regenerateInsights()">Regenerate Insights</button>
    </div>
    <table class="participant-table" id="insights-table"></table>
  </div>
</div>
```

**JS additions:**
- `loadInsights()` — calls `GET /api/surveys/{id}/insights`, renders charts
- `regenerateInsights()` — calls `POST /api/surveys/{id}/insights/regenerate`
- `renderSentimentChart(data)` — CSS horizontal bars with percentages
- `renderThemesList(themes)` — ranked list with count badges
- `renderEngagementHistogram(participants)` — CSS bar chart
- `renderInsightsTable(participants)` — sortable table with color-coded cells
- `sortTable(column)` — click header to sort

**Step 2: Test in browser**

Open `http://localhost:8000/admin` and verify:
- Login works, dashboard shows glass cards
- New Insights tab appears first
- Clicking Insights loads/generates analytics
- Sentiment bars, theme list, and engagement chart render
- Participant table shows sentiment and engagement columns
- All other tabs (Participants, Conversations, Analysis, Settings) still work

**Step 3: Commit**

```bash
git add templates/admin.html
git commit -m "feat: rewrite admin dashboard with glassmorphism and insights tab"
```

---

### Task 7: Delete Typing Monkey & Clean Up

**Files:**
- Delete: `templates/typing_monkey_chatbot.html`
- Modify: `main.py` (remove old non-tool `_chat_stream_generator` if still present)

**Step 1: Delete the typing monkey template**

```bash
rm templates/typing_monkey_chatbot.html
```

**Step 2: Clean up main.py**

Remove the old `_chat_stream_generator` function (the non-v2 version) if it still exists. Keep `_chat_stream_generator_v2` as the only streaming generator. Also remove the old non-streaming `survey_chat_stream` route (the one replaced in Task 3).

Keep the old `survey_chat` (non-streaming) endpoint as a fallback.

**Step 3: Commit**

```bash
git add -A
git commit -m "chore: remove typing monkey, clean up old streaming code"
```

---

### Task 8: Update CLAUDE.md and README

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`

**Step 1: Update CLAUDE.md**

Add new env vars (`UNSPLASH_ACCESS_KEY`, `PEXELS_API_KEY`) to the environment variables section. Update architecture to mention tool-use flow, media panel, insights. Note the `httpx` dependency. Mention new SSE event types.

**Step 2: Update README.md**

Add the two new env vars to the Railway deployment table. Mention the tool-use features and insights tab in the Features section.

**Step 3: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: update CLAUDE.md and README with new features and env vars"
```

---

### Task 9: Final Integration Test

**Step 1: Start local PostgreSQL and server**

```bash
docker run -d --name survey-pg -e POSTGRES_DB=survey_db -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/survey_db
export ANTHROPIC_API_KEY=sk-ant-...
export SECRET_KEY=dev-secret-key
# Optional: set UNSPLASH_ACCESS_KEY and PEXELS_API_KEY for media
pip install -r requirements.txt
python main.py
```

**Step 2: Test survey flow**

1. Go to `http://localhost:8000/admin`, log in, create a survey
2. Open `http://localhost:8000/` in mobile-width browser, enter survey code
3. Verify: glassmorphism UI, chat works, buttons appear when Claude decides to use them
4. If Unsplash key set: verify images appear in media panel
5. On mobile width: verify media shows on top, dismissable
6. On desktop width: verify side-by-side layout
7. Complete the survey, verify completion screen with animated checkmark

**Step 3: Test admin insights**

1. Go to admin, open the survey with completed responses
2. Click Insights tab
3. Verify sentiment chart, themes list, engagement histogram render
4. Verify participant table shows sentiment and engagement
5. Click "Regenerate Insights" — verify fresh data loads

**Step 4: Test without API keys**

Restart server without UNSPLASH_ACCESS_KEY and PEXELS_API_KEY. Verify chat still works — Claude may call show_image/show_video tools but no media renders (graceful fallback). Buttons should still work.

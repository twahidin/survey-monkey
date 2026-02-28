"""Main FastAPI application - Survey Chatbot with Admin Dashboard."""

import json
import os
import uuid
import secrets
import string
from datetime import datetime, timezone
from typing import Optional

import anthropic
import httpx
from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func

from database import get_db, init_db
from models import (
    Survey, SurveyStatus, Participant, ParticipantStatus,
    ChatMessage, AdminUser, AnalysisMessage, SurveyInsight,
)
from auth import (
    authenticate_admin, create_admin_user, create_access_token,
    decode_token, hash_password, update_admin_password,
)

app = FastAPI(title="Survey Chatbot", version="1.0.0")
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

# Appended to survey system prompt to keep tone conversational and elicit more reflection
CONVERSATIONAL_PROMPT = (
    "\n\n[STYLE: Be warm and conversational, not formal. "
    "Keep your replies relatively short so the participant does most of the talking. "
    "Often ask brief follow-ups to draw out more thoughts (e.g. 'What made you think that?', 'Can you say a bit more?', 'How did that feel?'). "
    "Reflect back what they share and invite elaboration. "
    "Your goal is to elicit genuine reflection and richer responses, not to rush through questions.]"
)


# ──────────────────────────── Startup ────────────────────────────

def _wait_for_db(max_attempts: int = 10, delay: float = 2.0):
    """Retry DB connection at startup (e.g. Postgres not ready yet on Railway)."""
    import time
    from sqlalchemy.exc import OperationalError
    for attempt in range(max_attempts):
        try:
            init_db()
            return
        except OperationalError as e:
            if attempt == max_attempts - 1:
                raise
            print(f"Database not ready (attempt {attempt + 1}/{max_attempts}), retrying in {delay}s...")
            time.sleep(delay)


@app.on_event("startup")
def on_startup():
    _wait_for_db()
    # Ensure default admin from env vars exists and password matches (so Railway vars always work)
    db = next(get_db())
    try:
        raw_user = os.environ.get("DEFAULT_ADMIN_USER") or "admin"
        raw_pass = os.environ.get("DEFAULT_ADMIN_PASS") or "admin123"
        default_user = raw_user.strip().strip("'\"").strip()
        default_pass = raw_pass.strip().strip("'\"").strip() or "admin123"
        if not default_user:
            default_user = "admin"
        existing = db.query(AdminUser).filter(AdminUser.username == default_user).first()
        if existing:
            update_admin_password(db, existing, default_pass)
            print(f"[Startup] Updated password for admin: {default_user!r}")
        elif db.query(AdminUser).count() == 0:
            create_admin_user(db, default_user, default_pass)
            print(f"[Startup] Created default admin: {default_user!r}")
        else:
            create_admin_user(db, default_user, default_pass)
            print(f"[Startup] Created admin from env: {default_user!r}")
    finally:
        db.close()


# ──────────────────────────── Helpers ────────────────────────────

def get_current_admin(request: Request, db: Session = Depends(get_db)) -> AdminUser:
    token = request.cookies.get("admin_token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    try:
        admin_id = uuid.UUID(payload["sub"])
    except (ValueError, TypeError, KeyError):
        raise HTTPException(status_code=401, detail="Invalid token")
    admin = db.query(AdminUser).filter(AdminUser.id == admin_id).first()
    if not admin:
        raise HTTPException(status_code=401, detail="Admin not found")
    return admin


def generate_survey_code(length: int = 6) -> str:
    chars = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def get_claude_client():
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


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
    """Fetch a relevant image from Unsplash. Returns {url, alt} or empty dict."""
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


# ──────────────────────────── Pydantic Schemas ────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str

class RegisterRequest(BaseModel):
    username: str
    password: str

class SurveyCreate(BaseModel):
    title: str
    topic: str
    system_prompt: str
    facilitator_intro: Optional[str] = None
    survey_code: Optional[str] = None
    max_messages: int = 20

class SurveyUpdate(BaseModel):
    title: Optional[str] = None
    topic: Optional[str] = None
    system_prompt: Optional[str] = None
    facilitator_intro: Optional[str] = None
    max_messages: Optional[int] = None

class JoinSurveyRequest(BaseModel):
    survey_code: str

class ChatRequest(BaseModel):
    session_token: str
    message: str

class AnalysisChatRequest(BaseModel):
    survey_id: str
    message: str


# ══════════════════════════════════════════════════════════════════
#  PAGE ROUTES
# ══════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
def serve_survey_page():
    return FileResponse("templates/survey.html")

@app.get("/admin", response_class=HTMLResponse)
def serve_admin_page():
    return FileResponse("templates/admin.html")


# ══════════════════════════════════════════════════════════════════
#  AUTH API
# ══════════════════════════════════════════════════════════════════

@app.post("/api/auth/login")
def login(req: LoginRequest, response: Response, db: Session = Depends(get_db)):
    username = (req.username or "").strip()
    password = (req.password or "").strip()
    admin = authenticate_admin(db, username, password)
    if not admin:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token({"sub": str(admin.id), "username": admin.username})
    response.set_cookie("admin_token", token, httponly=True, samesite="lax", max_age=86400)
    return {"token": token, "username": admin.username}

@app.post("/api/auth/register")
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    if db.query(AdminUser).filter(AdminUser.username == req.username).first():
        raise HTTPException(status_code=400, detail="Username already exists")
    admin = create_admin_user(db, req.username, req.password)
    token = create_access_token({"sub": str(admin.id), "username": admin.username})
    return {"token": token, "username": admin.username}

@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie("admin_token")
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
#  ADMIN - SURVEY MANAGEMENT
# ══════════════════════════════════════════════════════════════════

@app.get("/api/surveys")
def list_surveys(
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    surveys = (
        db.query(Survey)
        .filter(Survey.admin_id == admin.id)
        .options(joinedload(Survey.participants))
        .order_by(Survey.created_at.desc())
        .all()
    )
    return [
        {
            "id": str(s.id),
            "title": s.title,
            "topic": s.topic,
            "survey_code": s.survey_code,
            "status": s.status.value,
            "max_messages": s.max_messages,
            "facilitator_intro": s.facilitator_intro or "",
            "created_at": s.created_at.isoformat(),
            "closed_at": s.closed_at.isoformat() if s.closed_at else None,
            "active_participants": s.active_participants_count,
            "completed_participants": s.completed_participants_count,
            "total_participants": s.total_participants_count,
        }
        for s in surveys
    ]


@app.post("/api/surveys")
def create_survey(
    req: SurveyCreate,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    code = req.survey_code or generate_survey_code()
    if db.query(Survey).filter(Survey.survey_code == code).first():
        raise HTTPException(status_code=400, detail="Survey code already in use")
    survey = Survey(
        title=req.title,
        topic=req.topic,
        system_prompt=req.system_prompt,
        facilitator_intro=req.facilitator_intro or None,
        survey_code=code.upper(),
        max_messages=req.max_messages,
        admin_id=admin.id,
        status=SurveyStatus.ACTIVE,
    )
    db.add(survey)
    db.commit()
    db.refresh(survey)
    return {"id": str(survey.id), "survey_code": survey.survey_code}


@app.patch("/api/surveys/{survey_id}")
def update_survey(
    survey_id: str,
    req: SurveyUpdate,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    survey = db.query(Survey).filter(Survey.id == survey_id, Survey.admin_id == admin.id).first()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")
    for field, value in req.dict(exclude_unset=True).items():
        setattr(survey, field, value)
    db.commit()
    return {"ok": True}


@app.post("/api/surveys/{survey_id}/close")
def close_survey(
    survey_id: str,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    survey = db.query(Survey).filter(Survey.id == survey_id, Survey.admin_id == admin.id).first()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")
    survey.status = SurveyStatus.CLOSED
    survey.closed_at = datetime.now(timezone.utc)
    # Mark all active participants as abandoned
    for p in survey.participants:
        if p.status == ParticipantStatus.ACTIVE:
            p.status = ParticipantStatus.ABANDONED
            p.completed_at = datetime.now(timezone.utc)
            p.duration_seconds = (p.completed_at - p.started_at).total_seconds()
    db.commit()
    return {"ok": True}


@app.post("/api/surveys/{survey_id}/reopen")
def reopen_survey(
    survey_id: str,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    survey = db.query(Survey).filter(Survey.id == survey_id, Survey.admin_id == admin.id).first()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")
    survey.status = SurveyStatus.ACTIVE
    survey.closed_at = None
    db.commit()
    return {"ok": True}


@app.delete("/api/surveys/{survey_id}")
def delete_survey(
    survey_id: str,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    survey = db.query(Survey).filter(Survey.id == survey_id, Survey.admin_id == admin.id).first()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")
    db.query(AnalysisMessage).filter(AnalysisMessage.survey_id == survey_id).delete()
    db.delete(survey)
    db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
#  ADMIN - ANALYTICS
# ══════════════════════════════════════════════════════════════════

@app.get("/api/surveys/{survey_id}/results")
def get_survey_results(
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

    participants_data = []
    completion_times = []
    for p in sorted(survey.participants, key=lambda x: x.started_at):
        msgs = sorted(p.messages, key=lambda m: m.created_at)
        p_data = {
            "id": str(p.id),
            "status": p.status.value,
            "started_at": p.started_at.isoformat(),
            "completed_at": p.completed_at.isoformat() if p.completed_at else None,
            "duration_seconds": p.duration_seconds,
            "message_count": len(msgs),
            "messages": [
                {"role": m.role, "content": m.content, "created_at": m.created_at.isoformat()}
                for m in msgs
            ],
        }
        participants_data.append(p_data)
        if p.duration_seconds:
            completion_times.append(p.duration_seconds)

    avg_time = sum(completion_times) / len(completion_times) if completion_times else 0

    return {
        "survey": {
            "id": str(survey.id),
            "title": survey.title,
            "topic": survey.topic,
            "status": survey.status.value,
            "survey_code": survey.survey_code,
            "system_prompt": survey.system_prompt,
            "facilitator_intro": survey.facilitator_intro or "",
        },
        "stats": {
            "total_participants": len(survey.participants),
            "active_participants": survey.active_participants_count,
            "completed_participants": survey.completed_participants_count,
            "avg_completion_seconds": round(avg_time, 1),
        },
        "participants": participants_data,
    }


# ══════════════════════════════════════════════════════════════════
#  ADMIN - ANALYSIS CHATBOT (insights from survey data)
# ══════════════════════════════════════════════════════════════════

@app.post("/api/surveys/{survey_id}/analyze")
def analyze_survey(
    survey_id: str,
    req: AnalysisChatRequest,
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

    # Build survey data summary for context
    all_conversations = []
    for p in survey.participants:
        if p.messages:
            conv = "\n".join(f"  {m.role}: {m.content}" for m in sorted(p.messages, key=lambda x: x.created_at))
            status_label = p.status.value
            duration_label = f"{round(p.duration_seconds/60, 1)} min" if p.duration_seconds else "in progress"
            all_conversations.append(f"[Participant {str(p.id)[:8]} | {status_label} | {duration_label}]\n{conv}")

    survey_context = (
        f"Survey: {survey.title}\n"
        f"Topic: {survey.topic}\n"
        f"Total participants: {len(survey.participants)}\n"
        f"Completed: {survey.completed_participants_count}\n"
        f"Active: {survey.active_participants_count}\n\n"
        f"--- ALL CONVERSATIONS ---\n\n" +
        "\n\n".join(all_conversations) if all_conversations else "No conversations yet."
    )

    # Load prior analysis messages
    prior = (
        db.query(AnalysisMessage)
        .filter(AnalysisMessage.survey_id == survey_id, AnalysisMessage.admin_id == admin.id)
        .order_by(AnalysisMessage.created_at)
        .all()
    )
    history = [{"role": m.role, "content": m.content} for m in prior]
    history.append({"role": "user", "content": req.message})

    # Save user message
    db.add(AnalysisMessage(
        survey_id=survey_id, admin_id=admin.id, role="user", content=req.message
    ))
    db.commit()

    # Call Claude
    client = get_claude_client()
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        system=(
            "You are a survey data analyst. You have access to all the survey conversation data below. "
            "Provide insightful analysis, identify themes, summarize sentiment, and answer questions "
            "about the survey results. Be specific and cite participant responses when relevant.\n\n"
            f"{survey_context}"
        ),
        messages=history,
    )
    assistant_text = response.content[0].text

    # Save assistant message
    db.add(AnalysisMessage(
        survey_id=survey_id, admin_id=admin.id, role="assistant", content=assistant_text
    ))
    db.commit()

    return {"response": assistant_text}


@app.get("/api/surveys/{survey_id}/analysis-history")
def get_analysis_history(
    survey_id: str,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    messages = (
        db.query(AnalysisMessage)
        .filter(AnalysisMessage.survey_id == survey_id, AnalysisMessage.admin_id == admin.id)
        .order_by(AnalysisMessage.created_at)
        .all()
    )
    return [{"role": m.role, "content": m.content, "created_at": m.created_at.isoformat()} for m in messages]


# ══════════════════════════════════════════════════════════════════
#  PUBLIC - SURVEY CHATBOT
# ══════════════════════════════════════════════════════════════════

@app.post("/api/survey/join")
def join_survey(req: JoinSurveyRequest, db: Session = Depends(get_db)):
    survey = db.query(Survey).filter(Survey.survey_code == req.survey_code.upper()).first()
    if not survey:
        raise HTTPException(status_code=404, detail="Invalid survey code")
    if survey.status != SurveyStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="This survey is not currently active")

    session_token = secrets.token_urlsafe(32)
    participant = Participant(
        survey_id=survey.id,
        session_token=session_token,
        status=ParticipantStatus.ACTIVE,
    )
    db.add(participant)
    db.commit()

    # Build system prompt: include facilitator intro so the bot uses it when greeting
    system = survey.system_prompt
    if survey.facilitator_intro and survey.facilitator_intro.strip():
        system += (
            "\n\n[When you first greet the participant, use this introduction (say it naturally):\n"
            + survey.facilitator_intro.strip()
            + "\n]"
        )
    system += CONVERSATIONAL_PROMPT
    # Generate the opening message from Claude
    client = get_claude_client()
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": "(The participant has just joined the survey. Greet them and begin.)"}],
    )
    opening = response.content[0].text

    # Save the opening message
    db.add(ChatMessage(participant_id=participant.id, role="assistant", content=opening))
    db.commit()

    return {
        "session_token": session_token,
        "survey_title": survey.title,
        "opening_message": opening,
    }


@app.post("/api/survey/chat")
def survey_chat(req: ChatRequest, db: Session = Depends(get_db)):
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

    # Save user message
    db.add(ChatMessage(participant_id=participant.id, role="user", content=req.message))
    db.commit()

    # Build conversation history
    msgs = sorted(participant.messages, key=lambda m: m.created_at)
    history = [{"role": m.role, "content": m.content} for m in msgs]
    # Add the new message (since it's committed but not yet in the loaded relationship)
    history.append({"role": "user", "content": req.message})

    user_message_count = sum(1 for m in history if m["role"] == "user")
    near_limit = user_message_count >= participant.survey.max_messages

    system = participant.survey.system_prompt + CONVERSATIONAL_PROMPT
    if near_limit:
        system += (
            "\n\n[SYSTEM NOTE: This is the participant's last allowed message. "
            "Thank them for their time, provide a brief summary of what you gathered, "
            "and end the conversation warmly.]"
        )

    # Call Claude
    client = get_claude_client()
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=system,
        messages=history,
    )
    assistant_text = response.content[0].text

    # Save assistant response
    db.add(ChatMessage(participant_id=participant.id, role="assistant", content=assistant_text))

    # Check if we should auto-complete
    is_complete = near_limit
    if is_complete:
        now = datetime.now(timezone.utc)
        participant.status = ParticipantStatus.COMPLETED
        participant.completed_at = now
        participant.duration_seconds = (now - participant.started_at).total_seconds()

    db.commit()

    return {
        "response": assistant_text,
        "is_complete": is_complete,
    }


def _chat_stream_generator(
    client, system: str, history: list, participant, survey, db, near_limit: bool
):
    """Yield SSE events: chunk events then a done event. Saves message and updates participant when stream ends."""
    full_text = []
    try:
        with client.messages.stream(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=system,
            messages=history,
        ) as stream:
            for text in stream.text_stream:
                full_text.append(text)
                yield f"data: {json.dumps({'t': 'chunk', 'v': text})}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'t': 'error', 'v': str(e)})}\n\n"
        return
    assistant_text = "".join(full_text)
    # Save and possibly complete
    db.add(ChatMessage(participant_id=participant.id, role="assistant", content=assistant_text))
    is_complete = near_limit
    if is_complete:
        now = datetime.now(timezone.utc)
        participant.status = ParticipantStatus.COMPLETED
        participant.completed_at = now
        participant.duration_seconds = (now - participant.started_at).total_seconds()
    db.commit()
    yield f"data: {json.dumps({'t': 'done', 'is_complete': is_complete})}\n\n"


@app.post("/api/survey/chat/stream")
def survey_chat_stream(req: ChatRequest, db: Session = Depends(get_db)):
    """Stream the assistant reply as SSE; saves message and returns is_complete in final event."""
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
    history = [{"role": m.role, "content": m.content} for m in msgs]
    history.append({"role": "user", "content": req.message})

    user_message_count = sum(1 for m in history if m["role"] == "user")
    near_limit = user_message_count >= participant.survey.max_messages

    system = participant.survey.system_prompt + CONVERSATIONAL_PROMPT
    if near_limit:
        system += (
            "\n\n[SYSTEM NOTE: This is the participant's last allowed message. "
            "Thank them for their time, provide a brief summary of what you gathered, "
            "and end the conversation warmly.]"
        )

    client = get_claude_client()
    gen = _chat_stream_generator(
        client, system, history, participant, participant.survey, db, near_limit
    )
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/survey/complete")
def complete_survey_session(req: ChatRequest, db: Session = Depends(get_db)):
    """Allow participant to manually end their session."""
    participant = db.query(Participant).filter(Participant.session_token == req.session_token).first()
    if not participant:
        raise HTTPException(status_code=404, detail="Session not found")
    if participant.status == ParticipantStatus.ACTIVE:
        now = datetime.now(timezone.utc)
        participant.status = ParticipantStatus.COMPLETED
        participant.completed_at = now
        participant.duration_seconds = (now - participant.started_at).total_seconds()
        db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
#  HEALTH CHECK
# ══════════════════════════════════════════════════════════════════

@app.get("/api/health")
def health():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/api/admin-info")
def admin_info():
    """Return the admin username from env (so you can verify login). No password is ever returned."""
    raw = os.environ.get("DEFAULT_ADMIN_USER") or "admin"
    username = raw.strip().strip("'\"").strip() or "admin"
    return {"default_admin_username": username}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)

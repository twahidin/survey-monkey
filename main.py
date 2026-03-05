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
from sqlalchemy import case, func

from database import get_db, init_db
from models import (
    Survey, SurveyStatus, Participant, ParticipantStatus,
    ChatMessage, AdminUser, AnalysisMessage, SurveyInsight, InviteCode,
)
from auth import (
    authenticate_admin, create_admin_user, create_access_token,
    decode_token, hash_password, update_admin_password,
    encrypt_api_key, decrypt_api_key,
)

app = FastAPI(title="Survey Chatbot", version="1.0.0")
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_CHAT_MODEL = os.environ.get("CLAUDE_CHAT_MODEL", "claude-haiku-4-5-20251001")
CLAUDE_ANALYSIS_MODEL = os.environ.get("CLAUDE_ANALYSIS_MODEL", "claude-sonnet-4-6")

# Appended to survey system prompt to keep tone conversational and elicit more reflection
CONVERSATIONAL_PROMPT = (
    "\n\n[STYLE: Be warm and conversational, not formal. "
    "Keep your replies relatively short so the participant does most of the talking. "
    "Often ask brief follow-ups to draw out more thoughts (e.g. 'What made you think that?', 'Can you say a bit more?', 'How did that feel?'). "
    "Reflect back what they share and invite elaboration. "
    "Your goal is to elicit genuine reflection and richer responses, not to rush through questions.]"
)

SURVEY_TYPE_PROMPTS = {
    "general_sensing": "You are conducting a General Sensing survey — a quick pulse check. Ask each question, briefly clarify or follow up once, then move on. Keep it focused and efficient.",
    "categorising": "You are conducting a Categorising survey — classify participants into groups based on responses. Ask questions that help determine which category they belong to. At the end, reveal their category and provide a tailored response.",
    "depth_survey": "You are conducting a Depth Survey — a reflective conversation. Take your time with each topic. Ask probing follow-ups, explore underlying motivations, help participants reflect deeply. Prioritise depth over breadth.",
}


def compose_system_prompt(survey_type, questions, instructions):
    parts = []
    tp = SURVEY_TYPE_PROMPTS.get(survey_type)
    if tp:
        parts.append(tp)
    if questions and questions.strip():
        parts.append(f"[QUESTIONS TO ASK THE PARTICIPANT:\n{questions.strip()}\n]")
    if instructions and instructions.strip():
        parts.append(f"[ADDITIONAL INSTRUCTIONS:\n{instructions.strip()}\n]")
    return "\n\n".join(parts)


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


def get_claude_client(api_key: str = None):
    key = api_key or ANTHROPIC_API_KEY
    if not key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    return anthropic.Anthropic(api_key=key)


def get_visible_admin_ids(db: Session, admin: AdminUser) -> list:
    """Admin sees own + all teachers' surveys; teacher sees only own."""
    if admin.role == "admin":
        teacher_ids = [t.id for t in db.query(AdminUser).filter(
            AdminUser.parent_admin_id == admin.id
        ).all()]
        return [admin.id] + teacher_ids
    return [admin.id]


def resolve_api_key(db: Session, survey: Survey) -> str:
    """Resolve the API key for a survey: owner's key → parent admin's key → env var."""
    try:
        owner = db.query(AdminUser).filter(AdminUser.id == survey.admin_id).first()
        if owner and owner.encrypted_api_key:
            try:
                return decrypt_api_key(owner.encrypted_api_key)
            except Exception:
                pass
        if owner and owner.parent_admin_id:
            parent = db.query(AdminUser).filter(AdminUser.id == owner.parent_admin_id).first()
            if parent and parent.encrypted_api_key:
                try:
                    return decrypt_api_key(parent.encrypted_api_key)
                except Exception:
                    pass
    except Exception:
        pass
    return ANTHROPIC_API_KEY


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
    system_prompt: Optional[str] = None
    facilitator_intro: Optional[str] = None
    survey_code: Optional[str] = None
    max_messages: int = 20
    collect_name: bool = False
    collect_email: bool = False
    collect_phone: bool = False
    survey_type: Optional[str] = None
    questions: Optional[str] = None
    instructions: Optional[str] = None

class SurveyUpdate(BaseModel):
    title: Optional[str] = None
    topic: Optional[str] = None
    system_prompt: Optional[str] = None
    facilitator_intro: Optional[str] = None
    max_messages: Optional[int] = None
    collect_name: Optional[bool] = None
    collect_email: Optional[bool] = None
    collect_phone: Optional[bool] = None
    survey_type: Optional[str] = None
    questions: Optional[str] = None
    instructions: Optional[str] = None

class JoinSurveyRequest(BaseModel):
    survey_code: str

class ChatRequest(BaseModel):
    session_token: str
    message: str

class ContactInfoRequest(BaseModel):
    session_token: str
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None

class AnalysisChatRequest(BaseModel):
    survey_id: str
    message: str

class TeacherRegister(BaseModel):
    username: str
    password: str
    invite_code: str

class UpdateSettings(BaseModel):
    api_key: Optional[str] = None


# ══════════════════════════════════════════════════════════════════
#  PAGE ROUTES
# ══════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
def serve_survey_page():
    return FileResponse("templates/survey.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/admin", response_class=HTMLResponse)
def serve_admin_page():
    return FileResponse("templates/admin.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/register", response_class=HTMLResponse)
def serve_register_page():
    return FileResponse("templates/register.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


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
    return {"token": token, "username": admin.username, "role": admin.role}

@app.post("/api/auth/register")
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    if db.query(AdminUser).filter(AdminUser.username == req.username).first():
        raise HTTPException(status_code=400, detail="Username already exists")
    admin = create_admin_user(db, req.username, req.password)
    token = create_access_token({"sub": str(admin.id), "username": admin.username})
    return {"token": token, "username": admin.username, "role": admin.role}

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
    admin_ids = get_visible_admin_ids(db, admin)
    # Use subquery counts instead of loading all participants
    surveys = (
        db.query(
            Survey,
            func.count(Participant.id).label("total"),
            func.count(case((Participant.status == ParticipantStatus.ACTIVE, 1))).label("active"),
            func.count(case((Participant.status == ParticipantStatus.COMPLETED, 1))).label("completed"),
        )
        .outerjoin(Participant, Participant.survey_id == Survey.id)
        .filter(Survey.admin_id.in_(admin_ids))
        .group_by(Survey.id)
        .order_by(Survey.created_at.desc())
        .all()
    )
    # Build a map of admin_id → username for "created_by" labels
    admin_map = {}
    if admin.role == "admin":
        for a in db.query(AdminUser).filter(AdminUser.id.in_(admin_ids)).all():
            admin_map[str(a.id)] = a.username
    return [
        {
            "id": str(s.id),
            "title": s.title,
            "topic": s.topic,
            "survey_code": s.survey_code,
            "status": s.status.value,
            "max_messages": s.max_messages,
            "facilitator_intro": s.facilitator_intro or "",
            "collect_name": s.collect_name,
            "collect_email": s.collect_email,
            "collect_phone": s.collect_phone,
            "survey_type": s.survey_type or "",
            "questions": s.questions or "",
            "instructions": s.instructions or "",
            "created_at": s.created_at.isoformat(),
            "closed_at": s.closed_at.isoformat() if s.closed_at else None,
            "active_participants": active,
            "completed_participants": completed,
            "total_participants": total,
            "created_by": admin_map.get(str(s.admin_id), ""),
        }
        for s, total, active, completed in surveys
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
    # Compose system_prompt from wizard fields or use raw prompt
    if req.survey_type and req.questions:
        system_prompt = compose_system_prompt(req.survey_type, req.questions, req.instructions or "")
    elif req.system_prompt:
        system_prompt = req.system_prompt
    else:
        raise HTTPException(status_code=400, detail="Either survey type + questions or a system prompt is required")
    survey = Survey(
        title=req.title,
        topic=req.topic,
        system_prompt=system_prompt,
        facilitator_intro=req.facilitator_intro or None,
        survey_code=code.upper(),
        max_messages=req.max_messages,
        admin_id=admin.id,
        status=SurveyStatus.ACTIVE,
        collect_name=req.collect_name,
        collect_email=req.collect_email,
        collect_phone=req.collect_phone,
        survey_type=req.survey_type,
        questions=req.questions,
        instructions=req.instructions,
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
    admin_ids = get_visible_admin_ids(db, admin)
    survey = db.query(Survey).filter(Survey.id == survey_id, Survey.admin_id.in_(admin_ids)).first()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")
    for field, value in req.dict(exclude_unset=True).items():
        setattr(survey, field, value)
    # Recompose system_prompt if wizard fields are present
    effective_type = survey.survey_type
    effective_questions = survey.questions
    effective_instructions = survey.instructions
    if effective_type and effective_questions:
        survey.system_prompt = compose_system_prompt(effective_type, effective_questions, effective_instructions or "")
    db.commit()
    return {"ok": True}


@app.post("/api/surveys/{survey_id}/close")
def close_survey(
    survey_id: str,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    admin_ids = get_visible_admin_ids(db, admin)
    survey = db.query(Survey).filter(Survey.id == survey_id, Survey.admin_id.in_(admin_ids)).first()
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
    admin_ids = get_visible_admin_ids(db, admin)
    survey = db.query(Survey).filter(Survey.id == survey_id, Survey.admin_id.in_(admin_ids)).first()
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
    admin_ids = get_visible_admin_ids(db, admin)
    survey = db.query(Survey).filter(Survey.id == survey_id, Survey.admin_id.in_(admin_ids)).first()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")
    db.query(AnalysisMessage).filter(AnalysisMessage.survey_id == survey_id).delete()
    db.delete(survey)
    db.commit()
    return {"ok": True}


@app.delete("/api/surveys/{survey_id}/participants/{participant_id}")
def delete_participant(
    survey_id: str,
    participant_id: str,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """Delete a single participant and all their chat messages."""
    admin_ids = get_visible_admin_ids(db, admin)
    survey = db.query(Survey).filter(Survey.id == survey_id, Survey.admin_id.in_(admin_ids)).first()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")
    participant = db.query(Participant).filter(
        Participant.id == participant_id, Participant.survey_id == survey_id
    ).first()
    if not participant:
        raise HTTPException(status_code=404, detail="Participant not found")
    db.delete(participant)  # cascade deletes chat_messages
    db.commit()
    return {"ok": True}


class BulkDeleteParticipants(BaseModel):
    participant_ids: list[str]


@app.post("/api/surveys/{survey_id}/participants/bulk-delete")
def bulk_delete_participants(
    survey_id: str,
    req: BulkDeleteParticipants,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """Delete multiple participants and all their chat messages."""
    admin_ids = get_visible_admin_ids(db, admin)
    survey = db.query(Survey).filter(Survey.id == survey_id, Survey.admin_id.in_(admin_ids)).first()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")
    deleted = 0
    for pid in req.participant_ids:
        p = db.query(Participant).filter(
            Participant.id == pid, Participant.survey_id == survey_id
        ).first()
        if p:
            db.delete(p)
            deleted += 1
    db.commit()
    return {"ok": True, "deleted": deleted}


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
    admin_ids = get_visible_admin_ids(db, admin)
    survey = (
        db.query(Survey)
        .filter(Survey.id == survey_id, Survey.admin_id.in_(admin_ids))
        .first()
    )
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")

    # Efficient count queries instead of loading all participants
    counts = db.query(
        func.count(Participant.id).label("total"),
        func.count(case((Participant.status == ParticipantStatus.ACTIVE, 1))).label("active"),
        func.count(case((Participant.status == ParticipantStatus.COMPLETED, 1))).label("completed"),
        func.avg(Participant.duration_seconds).label("avg_duration"),
    ).filter(Participant.survey_id == survey_id).first()

    # Load participants with message counts (no message content yet)
    participants = (
        db.query(
            Participant,
            func.count(ChatMessage.id).label("msg_count"),
        )
        .outerjoin(ChatMessage, ChatMessage.participant_id == Participant.id)
        .filter(Participant.survey_id == survey_id)
        .group_by(Participant.id)
        .order_by(Participant.started_at)
        .all()
    )

    participants_data = []
    for p, msg_count in participants:
        # Load messages per participant (avoids one massive join)
        msgs = (
            db.query(ChatMessage)
            .filter(ChatMessage.participant_id == p.id)
            .order_by(ChatMessage.created_at)
            .all()
        )
        p_data = {
            "id": str(p.id),
            "status": p.status.value,
            "started_at": p.started_at.isoformat(),
            "completed_at": p.completed_at.isoformat() if p.completed_at else None,
            "duration_seconds": p.duration_seconds,
            "message_count": msg_count,
            "contact_name": p.contact_name or "",
            "contact_email": p.contact_email or "",
            "contact_phone": p.contact_phone or "",
            "messages": [
                {"role": m.role, "content": m.content, "created_at": m.created_at.isoformat()}
                for m in msgs
            ],
        }
        participants_data.append(p_data)

    return {
        "survey": {
            "id": str(survey.id),
            "title": survey.title,
            "topic": survey.topic,
            "status": survey.status.value,
            "survey_code": survey.survey_code,
            "system_prompt": survey.system_prompt,
            "facilitator_intro": survey.facilitator_intro or "",
            "survey_type": survey.survey_type or "",
            "questions": survey.questions or "",
            "instructions": survey.instructions or "",
            "max_messages": survey.max_messages,
            "collect_name": survey.collect_name,
            "collect_email": survey.collect_email,
            "collect_phone": survey.collect_phone,
        },
        "stats": {
            "total_participants": counts.total or 0,
            "active_participants": counts.active or 0,
            "completed_participants": counts.completed or 0,
            "avg_completion_seconds": round(counts.avg_duration or 0, 1),
        },
        "participants": participants_data,
    }


# ══════════════════════════════════════════════════════════════════
#  ADMIN - ANALYSIS CHATBOT (insights from survey data)
# ══════════════════════════════════════════════════════════════════

@app.post("/api/surveys/{survey_id}/analyze")
async def analyze_survey(
    survey_id: str,
    req: AnalysisChatRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    admin_ids = get_visible_admin_ids(db, admin)
    survey = (
        db.query(Survey)
        .filter(Survey.id == survey_id, Survey.admin_id.in_(admin_ids))
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
        f"System Prompt: {survey.system_prompt}\n"
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

    # Stream Claude response via SSE
    api_key = resolve_api_key(db, survey)
    client = get_claude_client(api_key)
    system_prompt = (
        "You are a survey data analyst. You have access to all the survey conversation data below. "
        "Provide insightful analysis, identify themes, summarize sentiment, and answer questions "
        "about the survey results. Be specific and cite participant responses when relevant.\n\n"
        "CHARTS: When presenting quantitative data, include interactive charts using fenced code blocks "
        "with the language tag `chart`. Each block must contain valid JSON with this schema:\n"
        '  For charts: {"type":"pie|bar|doughnut|horizontalBar","title":"Chart Title","labels":["A","B"],"data":[10,20]}\n'
        '  For tables: {"type":"table","title":"Table Title","headers":["Col1","Col2"],"rows":[["a","1"],["b","2"]]}\n\n'
        "Example:\n```chart\n"
        '{"type":"doughnut","title":"Sentiment Breakdown","labels":["Positive","Neutral","Negative"],"data":[12,5,3]}\n'
        "```\n\n"
        "Always accompany charts with a brief text interpretation. Use charts for sentiment distributions, "
        "theme frequency, engagement comparisons, and any numeric breakdowns. Use table type for detailed "
        "per-participant or multi-column data. You can include multiple charts in a single response.\n\n"
        f"{survey_context}"
    )

    async def analysis_stream():
        full_text = []
        try:
            with client.messages.stream(
                model=CLAUDE_ANALYSIS_MODEL,
                max_tokens=4096,
                system=system_prompt,
                messages=history,
            ) as stream:
                for text in stream.text_stream:
                    full_text.append(text)
                    yield f"data: {json.dumps({'t': 'chunk', 'v': text})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'t': 'error', 'v': str(e)})}\n\n"
            return

        assistant_text = "".join(full_text)
        db.add(AnalysisMessage(
            survey_id=survey_id, admin_id=admin.id, role="assistant", content=assistant_text
        ))
        db.commit()
        yield f"data: {json.dumps({'t': 'done'})}\n\n"

    return StreamingResponse(
        analysis_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
#  ADMIN - SURVEY INSIGHTS (AI-generated analytics)
# ══════════════════════════════════════════════════════════════════

def _build_insights_prompt(survey, participants_data: list) -> str:
    convos = []
    for p in participants_data:
        if p["messages"]:
            msgs = "\n".join(
                f"  {m['role']}: {m['content']}"
                for m in p["messages"]
                if not m["content"].startswith("[TOOL_EVENTS]")
            )
            convos.append(f"[Participant {p['id'][:8]} | {p['status']} | {p['message_count']} msgs]\n{msgs}")

    return (
        f"Analyze these survey conversations and return a JSON object.\n\n"
        f"Survey: {survey.title}\nTopic: {survey.topic}\n"
        f"System Prompt: {survey.system_prompt}\n"
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
    api_key = resolve_api_key(db, survey)
    client = get_claude_client(api_key)
    response = client.messages.create(
        model=CLAUDE_ANALYSIS_MODEL,
        max_tokens=4096,
        system="You are a survey data analyst. Return ONLY valid JSON, no markdown fences, no explanation.",
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
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
    admin_ids = get_visible_admin_ids(db, admin)
    survey = (
        db.query(Survey)
        .filter(Survey.id == survey_id, Survey.admin_id.in_(admin_ids))
        .options(joinedload(Survey.participants).joinedload(Participant.messages))
        .first()
    )
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")

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
    admin_ids = get_visible_admin_ids(db, admin)
    survey = (
        db.query(Survey)
        .filter(Survey.id == survey_id, Survey.admin_id.in_(admin_ids))
        .options(joinedload(Survey.participants).joinedload(Participant.messages))
        .first()
    )
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")
    insights = _generate_insights(survey, db)
    return {"insights": insights, "generated_at": datetime.now(timezone.utc).isoformat(), "cached": False}


# ══════════════════════════════════════════════════════════════════
#  PUBLIC - SURVEY CHATBOT
# ══════════════════════════════════════════════════════════════════

@app.post("/api/survey/join")
async def join_survey(req: JoinSurveyRequest, db: Session = Depends(get_db)):
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
    system += CONVERSATIONAL_PROMPT + TOOL_USE_PROMPT
    # Generate the opening message from Claude (with tool-use support)
    api_key = resolve_api_key(db, survey)
    client = get_claude_client(api_key)
    try:
        response = client.messages.create(
            model=CLAUDE_CHAT_MODEL,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": "(The participant has just joined the survey. Greet them warmly with a text message and begin. Always include a written greeting — do not rely solely on tools.)"}],
            tools=SURVEY_TOOLS,
        )
    except anthropic.AuthenticationError:
        raise HTTPException(status_code=500, detail="AI service authentication failed. Please check the API key configuration.")
    except anthropic.APIError as e:
        raise HTTPException(status_code=500, detail=f"AI service error: {e.message}")

    # Process content blocks — extract text and tool events
    text_parts = []
    tool_events = []
    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            events = await _process_tool_call(block.name, block.input)
            tool_events.extend(events)

    opening = "".join(text_parts)
    if not opening and tool_events:
        opening = "(presented interactive content)"

    # Save the synthetic user prompt and opening message so history is anchored
    db.add(ChatMessage(participant_id=participant.id, role="user", content="(The participant has just joined the survey.)"))
    db.add(ChatMessage(participant_id=participant.id, role="assistant", content=opening))
    if tool_events:
        db.add(ChatMessage(
            participant_id=participant.id, role="assistant",
            content=f"[TOOL_EVENTS]{json.dumps(tool_events)}",
        ))
    db.commit()

    return {
        "session_token": session_token,
        "survey_title": survey.title,
        "opening_message": opening,
        "opening_events": tool_events,
        "collect_name": survey.collect_name,
        "collect_email": survey.collect_email,
        "collect_phone": survey.collect_phone,
    }


async def _process_tool_call(tool_name: str, tool_input: dict) -> list:
    """Process a tool call and return SSE event dicts to send to the frontend."""
    events = []
    if tool_name == "show_image":
        media = await fetch_unsplash_image(tool_input["query"])
        if media:
            events.append({
                "t": "media", "type": "image",
                "url": media["url"], "alt": media.get("alt", ""),
                "caption": tool_input.get("caption", ""),
            })
    elif tool_name == "show_video":
        media = await fetch_pexels_video(tool_input["query"])
        if media:
            events.append({
                "t": "media", "type": "video",
                "url": media["url"], "poster": media.get("poster", ""),
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
            model=CLAUDE_CHAT_MODEL,
            max_tokens=1024,
            system=system,
            messages=history,
            tools=SURVEY_TOOLS,
        )

        for block in response.content:
            if block.type == "text":
                text = block.text
                full_text.append(text)
                chunk_size = 12
                for i in range(0, len(text), chunk_size):
                    chunk = text[i:i + chunk_size]
                    yield f"data: {json.dumps({'t': 'chunk', 'v': chunk})}\n\n"
            elif block.type == "tool_use":
                events = await _process_tool_call(block.name, block.input)
                tool_events.extend(events)

        for event in tool_events:
            yield f"data: {json.dumps(event)}\n\n"

    except Exception as e:
        yield f"data: {json.dumps({'t': 'error', 'v': str(e)})}\n\n"
        return

    assistant_text = "".join(full_text)
    if not assistant_text and tool_events:
        assistant_text = "(presented interactive content)"

    db.add(ChatMessage(participant_id=participant.id, role="assistant", content=assistant_text))

    if tool_events:
        db.add(ChatMessage(
            participant_id=participant.id, role="assistant",
            content=f"[TOOL_EVENTS]{json.dumps(tool_events)}",
        ))

    is_complete = near_limit
    if is_complete:
        now = datetime.now(timezone.utc)
        participant.status = ParticipantStatus.COMPLETED
        participant.completed_at = now
        participant.duration_seconds = (now - participant.started_at).total_seconds()

    db.commit()
    yield f"data: {json.dumps({'t': 'done', 'is_complete': is_complete})}\n\n"


class ResumeSessionRequest(BaseModel):
    session_token: str


@app.post("/api/survey/resume")
def resume_survey_session(req: ResumeSessionRequest, db: Session = Depends(get_db)):
    """Resume an existing survey session — returns conversation history."""
    participant = (
        db.query(Participant)
        .filter(Participant.session_token == req.session_token)
        .options(joinedload(Participant.messages), joinedload(Participant.survey))
        .first()
    )
    if not participant:
        raise HTTPException(status_code=404, detail="Session not found")
    if participant.status != ParticipantStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="Session already ended")
    if participant.survey.status != SurveyStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="Survey has been closed")

    msgs = sorted(participant.messages, key=lambda m: m.created_at)
    user_msg_count = sum(1 for m in msgs if m.role == "user")

    return {
        "session_token": participant.session_token,
        "survey_title": participant.survey.title,
        "max_messages": participant.survey.max_messages,
        "collect_name": participant.survey.collect_name,
        "collect_email": participant.survey.collect_email,
        "collect_phone": participant.survey.collect_phone,
        "user_message_count": user_msg_count,
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at.isoformat(),
            }
            for m in msgs
        ],
    }


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

    api_key = resolve_api_key(db, participant.survey)
    client = get_claude_client(api_key)
    gen = _chat_stream_generator_v2(
        client, system, history, participant, db, near_limit
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


@app.post("/api/survey/contact-info")
def submit_contact_info(req: ContactInfoRequest, db: Session = Depends(get_db)):
    """Save participant contact details after survey completion."""
    participant = db.query(Participant).filter(Participant.session_token == req.session_token).first()
    if not participant:
        raise HTTPException(status_code=404, detail="Session not found")
    if req.name:
        participant.contact_name = req.name.strip()
    if req.email:
        participant.contact_email = req.email.strip()
    if req.phone:
        participant.contact_phone = req.phone.strip()
    db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
#  ADMIN - TEACHER MANAGEMENT & SETTINGS
# ══════════════════════════════════════════════════════════════════

@app.get("/api/admin/me")
def admin_me(db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    return {
        "id": str(admin.id),
        "username": admin.username,
        "role": admin.role,
        "has_api_key": bool(admin.encrypted_api_key),
    }


@app.post("/api/admin/invite")
def create_invite(db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    if admin.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can invite teachers")
    code = secrets.token_urlsafe(16)
    invite = InviteCode(code=code, admin_id=admin.id)
    db.add(invite)
    db.commit()
    return {"code": code}


@app.post("/api/auth/register-teacher")
def register_teacher(req: TeacherRegister, db: Session = Depends(get_db)):
    invite = db.query(InviteCode).filter(InviteCode.code == req.invite_code, InviteCode.used_by_id.is_(None)).first()
    if not invite:
        raise HTTPException(status_code=400, detail="Invalid or already used invite code")
    if db.query(AdminUser).filter(AdminUser.username == req.username).first():
        raise HTTPException(status_code=400, detail="Username already exists")
    teacher = create_admin_user(db, req.username, req.password, role="teacher", parent_admin_id=invite.admin_id)
    invite.used_by_id = teacher.id
    invite.used_at = datetime.now(timezone.utc)
    db.commit()
    token = create_access_token({"sub": str(teacher.id), "username": teacher.username})
    return {"token": token, "username": teacher.username, "role": "teacher"}


@app.get("/api/admin/teachers")
def list_teachers(db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    if admin.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can view teachers")
    teachers = db.query(AdminUser).filter(AdminUser.parent_admin_id == admin.id).all()
    result = []
    for t in teachers:
        survey_count = db.query(Survey).filter(Survey.admin_id == t.id).count()
        result.append({
            "id": str(t.id),
            "username": t.username,
            "has_api_key": bool(t.encrypted_api_key),
            "survey_count": survey_count,
            "created_at": t.created_at.isoformat(),
        })
    return result


@app.delete("/api/admin/teachers/{teacher_id}")
def remove_teacher(teacher_id: str, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    if admin.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can remove teachers")
    teacher = db.query(AdminUser).filter(
        AdminUser.id == teacher_id, AdminUser.parent_admin_id == admin.id
    ).first()
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")
    # Reassign teacher's surveys to the admin
    db.query(Survey).filter(Survey.admin_id == teacher.id).update({"admin_id": admin.id})
    # Clean up analysis messages
    db.query(AnalysisMessage).filter(AnalysisMessage.admin_id == teacher.id).update({"admin_id": admin.id})
    # Delete invite codes used by this teacher
    db.query(InviteCode).filter(InviteCode.used_by_id == teacher.id).update({"used_by_id": None, "used_at": None})
    db.delete(teacher)
    db.commit()
    return {"ok": True}


@app.put("/api/admin/settings")
def update_settings(req: UpdateSettings, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    if req.api_key is not None:
        if req.api_key.strip():
            admin.encrypted_api_key = encrypt_api_key(req.api_key.strip())
        else:
            admin.encrypted_api_key = None
    db.commit()
    return {"ok": True, "has_api_key": bool(admin.encrypted_api_key)}


@app.put("/api/admin/teachers/{teacher_id}/api-key")
def update_teacher_api_key(teacher_id: str, req: UpdateSettings, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    if admin.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can update teacher API keys")
    teacher = db.query(AdminUser).filter(
        AdminUser.id == teacher_id, AdminUser.parent_admin_id == admin.id
    ).first()
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")
    if req.api_key is not None:
        if req.api_key.strip():
            teacher.encrypted_api_key = encrypt_api_key(req.api_key.strip())
        else:
            teacher.encrypted_api_key = None
    db.commit()
    return {"ok": True, "has_api_key": bool(teacher.encrypted_api_key)}


@app.get("/api/admin/invites")
def list_invites(db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    if admin.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can view invites")
    invites = db.query(InviteCode).filter(InviteCode.admin_id == admin.id).order_by(InviteCode.created_at.desc()).all()
    return [
        {
            "id": str(inv.id),
            "code": inv.code,
            "used": inv.used_by_id is not None,
            "created_at": inv.created_at.isoformat(),
        }
        for inv in invites
    ]


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

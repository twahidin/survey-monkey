"""Ponder — AI-facilitated conversational surveys and reflections for classrooms, teams and communities. FastAPI application."""

import asyncio
import json
import logging
import os
import uuid
import secrets
import string
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, Depends, HTTPException, Request, Response, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import case, func

from database import get_db, init_db
from models import (
    Survey, SurveyStatus, Participant, ParticipantStatus,
    ChatMessage, AdminUser, AnalysisMessage, SurveyInsight, InviteCode, MediaAsset,
)
from auth import (
    authenticate_admin, create_admin_user, create_access_token,
    decode_token, hash_password, update_admin_password,
    encrypt_api_key, decrypt_api_key,
)
from llm import (
    LLMConfig, LLMError, stream_chat, complete_chat, list_openrouter_models,
    extract_json_object, normalise_config, default_model, PROVIDERS,
    ANTHROPIC_API_KEY, OPENROUTER_API_KEY, CLAUDE_CHAT_MODEL, CLAUDE_ANALYSIS_MODEL,
)
from imagegen import generate_image, IMAGE_PROVIDERS
from slides import extract_slides, deck_context, SlideError
from report import (
    build_survey_report_html, build_survey_report_docx, build_participant_report_html,
)

logger = logging.getLogger(__name__)

APP_NAME = os.environ.get("APP_NAME", "Ponder")
app = FastAPI(title=APP_NAME, version="2.0.0")
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

UNSPLASH_ACCESS_KEY = os.environ.get("UNSPLASH_ACCESS_KEY", "")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "25")) * 1024 * 1024

# Appended to survey system prompt to keep tone conversational and elicit more reflection
CONVERSATIONAL_PROMPT = (
    "\n\n[STYLE: Be warm, encouraging and conversational, like a skilled facilitator leading a discussion. "
    "Keep your replies short so the participant does most of the talking. "
    "Ask one question at a time. Often ask brief follow-ups to draw out more thinking "
    "(e.g. 'What made you think that?', 'Can you give an example?', 'How did that feel?'). "
    "Reflect back what they share and invite elaboration. "
    "Your goal is to elicit genuine reflection and richer responses, not to rush through questions.]"
)

SURVEY_TYPE_PROMPTS = {
    "general_sensing": "You are conducting a General Sensing survey — a quick pulse check. Ask each question, briefly clarify or follow up once, then move on. Keep it focused and efficient.",
    "categorising": "You are conducting a Categorising survey — classify participants into groups based on responses. Ask questions that help determine which category they belong to. At the end, reveal their category and provide a tailored response.",
    "depth_survey": "You are conducting a Depth Survey — a reflective conversation. Take your time with each topic. Ask probing follow-ups, explore underlying motivations, help participants reflect deeply. Prioritise depth over breadth.",
    "formative_assessment": "You are running a Formative Assessment conversation. Work through the questions to surface what the participant understands and where misconceptions are. Probe reasoning with 'why' and 'how' follow-ups. Never lecture; hint at most once, then move on. Be encouraging and never make the participant feel judged.",
    "reflection": "You are guiding a Reflection conversation after a task or experience. Help the participant articulate what they did, what they learned, what was hard, and what they would do differently. Use open questions and give them space to think.",
    "guided_learning": (
        "You are running a Guided Learning walkthrough for a participant who may find the material difficult. "
        "Work through the briefing material in order, one small idea at a time. For each idea: first show the relevant slide "
        "(show_slide) or an illustration and explain it in two or three plain sentences; then check understanding with an "
        "interactive (show_interactive) — start with an easy multiple-choice or fill-in-the-blank, then ask a short written answer, "
        "and only then a longer 'explain in your own words' question. If an answer is wrong or shaky, do not move on: show the slide again, "
        "point at the exact part that answers it, give one hint, and ask again in a simpler way. Celebrate small wins, keep language simple, "
        "never make the participant feel judged, and keep a running sense of what they have mastered so the closing summary can list it."
    ),
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
    token = request.cookies.get("admin_token") or request.headers.get("Authorization", "").replace("Bearer ", "") or request.query_params.get("token", "")
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


def get_visible_admin_ids(db: Session, admin: AdminUser) -> list:
    """Admin sees own + all teachers' surveys; teacher sees only own."""
    if admin.role == "admin":
        teacher_ids = [t.id for t in db.query(AdminUser).filter(
            AdminUser.parent_admin_id == admin.id
        ).all()]
        return [admin.id] + teacher_ids
    return [admin.id]


def _dec(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return decrypt_api_key(value)
    except Exception:
        return ""


def _first(*values):
    for v in values:
        if v:
            return v
    return ""


def resolve_llm_config(db: Session, survey: Optional[Survey] = None, admin: Optional[AdminUser] = None) -> LLMConfig:
    """Resolve provider/model/keys: survey override → owner → parent admin → environment."""
    owner = admin
    if owner is None and survey is not None:
        owner = db.query(AdminUser).filter(AdminUser.id == survey.admin_id).first()
    parent = None
    if owner is not None and owner.parent_admin_id:
        parent = db.query(AdminUser).filter(AdminUser.id == owner.parent_admin_id).first()
    chain = [a for a in (owner, parent) if a is not None]

    # --- chat provider ---
    provider = _first(
        survey.llm_provider if survey else "",
        *[a.llm_provider for a in chain],
        "openrouter" if (not ANTHROPIC_API_KEY and OPENROUTER_API_KEY) else "anthropic",
    )
    if provider not in PROVIDERS:
        provider = "anthropic"

    model = ""
    if survey and survey.llm_provider == provider and survey.llm_model:
        model = survey.llm_model
    for a in chain:
        if not model and a.llm_provider == provider and a.llm_model:
            model = a.llm_model

    def key_for(a: AdminUser) -> str:
        return _dec(a.encrypted_openrouter_key if provider == "openrouter" else a.encrypted_api_key)

    api_key = ""
    if survey and survey.llm_provider == provider and survey.encrypted_llm_api_key:
        api_key = _dec(survey.encrypted_llm_api_key)
    for a in chain:
        if not api_key:
            api_key = key_for(a)

    # --- image generation ---
    image_mode = (survey.image_mode if survey else "") or "stock"
    image_provider = _first(survey.image_provider if survey else "", *[a.image_provider for a in chain], "pollinations")
    image_model = ""
    if survey and survey.image_provider == image_provider and survey.image_model:
        image_model = survey.image_model
    for a in chain:
        if not image_model and a.image_provider == image_provider and a.image_model:
            image_model = a.image_model
    image_base_url = _first(survey.image_base_url if survey else "", *[a.image_base_url for a in chain])
    image_key = ""
    if survey and survey.image_provider == image_provider and survey.encrypted_image_api_key:
        image_key = _dec(survey.encrypted_image_api_key)
    for a in chain:
        if not image_key and a.image_provider == image_provider:
            image_key = _dec(a.encrypted_image_api_key)
    if not image_key and image_provider == "openrouter":
        # Reuse the OpenRouter chat key if the image key was not set separately
        for a in chain:
            if not image_key:
                image_key = _dec(a.encrypted_openrouter_key)
        if not image_key and provider == "openrouter":
            image_key = api_key
        if not image_key:
            image_key = OPENROUTER_API_KEY
    if not image_key and image_provider == "pollinations":
        image_key = os.environ.get("POLLINATIONS_API_KEY", "")
    if not image_key and image_provider == "openai":
        image_key = os.environ.get("OPENAI_API_KEY", "")

    cfg = LLMConfig(
        provider=provider,
        model=model,
        api_key=api_key,
        image_mode=image_mode,
        image_provider=image_provider,
        image_model=image_model,
        image_base_url=image_base_url,
        image_api_key=image_key,
        image_style=(survey.image_style if survey else "") or "",
    )
    return normalise_config(cfg)


def resolve_api_key(db: Session, survey: Survey) -> str:
    """Kept for compatibility: the Anthropic key for a survey's owner chain."""
    return resolve_llm_config(db, survey).api_key


# ──────────────────────────── Tool-Use ────────────────────────────

SHOW_BUTTONS_TOOL = {
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
}

GENERATE_IMAGE_TOOL = {
    "name": "show_image",
    "description": (
        "Generate and display an illustrative image in the participant's visual panel. "
        "Use it when you introduce a new question, scenario or concept so the participant has something concrete to look at and react to. "
        "Describe a single clear scene in visual terms (subject, setting, mood). Do not ask for text or words in the image."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Visual description of the image to generate (one scene, 1-2 sentences)"},
            "caption": {"type": "string", "description": "Short caption shown under the image"},
        },
        "required": ["prompt"],
    },
}

STOCK_IMAGE_TOOL = {
    "name": "show_image",
    "description": "Show a relevant stock photo to help the participant understand or engage with the topic. Use when visual context would enrich the conversation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query for finding a relevant image"},
            "caption": {"type": "string", "description": "Optional caption to display with the image"},
        },
        "required": ["query"],
    },
}

SHOW_VIDEO_TOOL = {
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
}

SHOW_SLIDE_TOOL = {
    "name": "show_slide",
    "description": (
        "Display one slide/page of the briefing deck in the participant's visual panel. "
        "Use it whenever you discuss, explain or ask about a slide, and again when the participant struggles, so they can look at it while answering."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "1-based slide/page number from the DECK CONTENT list"},
            "caption": {"type": "string", "description": "Short caption pointing the participant to what to look at on this slide"},
        },
        "required": ["page"],
    },
}

INTERACTIVE_KINDS = ("mcq", "fill_blank", "order", "match", "scale")

SHOW_INTERACTIVE_TOOL = {
    "name": "show_interactive",
    "description": (
        "Show a small interactive check inside the chat that the participant completes by tapping or typing; the result comes back to you as their next message. "
        "Kinds: 'mcq' (one correct option; give options + correct index + explanation), "
        "'fill_blank' (a sentence with ___ blanks and the expected answers, in order; alternatives separated by |), "
        "'order' (items listed in the CORRECT order; they are shuffled for the participant), "
        "'match' (pairs of left/right items to match), "
        "'scale' (a confidence or agreement slider with min/max labels). "
        "Use these for quick understanding checks; always write a short message alongside the tool call."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(INTERACTIVE_KINDS)},
            "question": {"type": "string", "description": "The prompt shown above the interactive"},
            "options": {"type": "array", "items": {"type": "string"}, "description": "mcq: 2-5 answer options"},
            "correct": {"type": "integer", "description": "mcq: 0-based index of the correct option"},
            "text": {"type": "string", "description": "fill_blank: sentence with one or more ___ blanks"},
            "answers": {"type": "array", "items": {"type": "string"}, "description": "fill_blank: expected answer per blank, alternatives separated by |"},
            "items": {"type": "array", "items": {"type": "string"}, "description": "order: 3-6 items in the correct order"},
            "pairs": {
                "type": "array",
                "items": {"type": "object", "properties": {"left": {"type": "string"}, "right": {"type": "string"}}, "required": ["left", "right"]},
                "description": "match: 2-6 pairs",
            },
            "min_label": {"type": "string", "description": "scale: label at the low end (e.g. 'Not confident')"},
            "max_label": {"type": "string", "description": "scale: label at the high end (e.g. 'Very confident')"},
            "explanation": {"type": "string", "description": "Short explanation revealed after the participant answers"},
            "hint": {"type": "string", "description": "Optional hint the participant can reveal before answering"},
        },
        "required": ["kind", "question"],
    },
}

# Backwards-compatible name used elsewhere
SURVEY_TOOLS = [STOCK_IMAGE_TOOL, SHOW_BUTTONS_TOOL, SHOW_VIDEO_TOOL]


def build_survey_tools(cfg: LLMConfig, slide_count: int = 0) -> list:
    tools = [SHOW_BUTTONS_TOOL, SHOW_INTERACTIVE_TOOL]
    if slide_count:
        tools.append(SHOW_SLIDE_TOOL)
    if cfg.image_mode == "generate":
        tools.append(GENERATE_IMAGE_TOOL)
    elif cfg.image_mode == "stock" and UNSPLASH_ACCESS_KEY:
        tools.append(STOCK_IMAGE_TOOL)
    if PEXELS_API_KEY:
        tools.append(SHOW_VIDEO_TOOL)
    return tools


def build_tool_prompt(cfg: LLMConfig, slide_count: int = 0) -> str:
    lines = [
        "\n\n[TOOLS: You have tools to enrich the conversation. "
        "Use show_buttons when a question has clear discrete choices (frequency, ratings, yes/no, pick-one). "
        "Use show_interactive for quick understanding checks (mcq, fill_blank, order, match, scale); the participant's result "
        "arrives as a message starting with [Interactive] — respond to it (confirm, correct gently, or build on it) before moving on."
    ]
    if slide_count:
        lines.append(
            f"The briefing deck has {slide_count} slides and their text is listed under DECK CONTENT. "
            "Use show_slide(page) whenever you discuss a slide so it appears in the visual panel, and say which slide you are on. "
            "If the participant struggles, show the slide again and point to the exact part that answers the question."
        )
    if cfg.image_mode == "generate":
        lines.append(
            "Use show_image to generate an illustration whenever you introduce a new question, scenario or idea "
            "(roughly every one or two questions). The image appears in a visual panel next to the chat, "
            "so refer to it naturally ('Take a look at the picture — ...'). Describe a concrete scene, not abstract concepts."
        )
    elif cfg.image_mode == "stock" and UNSPLASH_ACCESS_KEY:
        lines.append("Use show_image when a photo would help the participant understand or connect with the topic.")
    if PEXELS_API_KEY:
        lines.append("Use show_video sparingly, only when a clip would significantly help.")
    lines.append("You can combine text with tool calls — write your message text AND call a tool in the same turn. Always include written text.]")
    return " ".join(lines)


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


async def _process_tool_call(tool_name: str, tool_input: dict, cfg: LLMConfig, db: Session,
                             survey_id=None, participant_id=None) -> list:
    """Process a tool call and return SSE event dicts to send to the frontend."""
    events = []
    if tool_name == "show_image":
        if cfg.image_mode == "generate":
            prompt = (tool_input.get("prompt") or tool_input.get("query") or "").strip()
            if not prompt:
                return events
            try:
                data, mime = await generate_image(cfg, prompt)
            except LLMError as e:
                logger.warning(f"Image generation failed ({cfg.image_provider}): {e.message}")
                return events
            except Exception as e:
                logger.warning(f"Image generation failed ({cfg.image_provider}): {e}")
                return events
            asset = MediaAsset(
                survey_id=survey_id, participant_id=participant_id, kind="generated",
                mime_type=mime, prompt=prompt, data=data, size_bytes=len(data),
            )
            db.add(asset)
            db.commit()
            db.refresh(asset)
            events.append({
                "t": "media", "type": "image",
                "url": f"/api/assets/{asset.id}", "alt": prompt,
                "caption": tool_input.get("caption", ""), "generated": True,
            })
        else:
            media = await fetch_unsplash_image(tool_input.get("query") or tool_input.get("prompt") or "")
            if media:
                events.append({
                    "t": "media", "type": "image",
                    "url": media["url"], "alt": media.get("alt", ""),
                    "caption": tool_input.get("caption", ""),
                })
    elif tool_name == "show_video":
        media = await fetch_pexels_video(tool_input.get("query", ""))
        if media:
            events.append({
                "t": "media", "type": "video",
                "url": media["url"], "poster": media.get("poster", ""),
                "caption": tool_input.get("caption", ""),
            })
    elif tool_name == "show_buttons":
        options = tool_input.get("options", [])
        if isinstance(options, list) and options:
            events.append({
                "t": "buttons",
                "question": tool_input.get("question", ""),
                "options": [o for o in options if isinstance(o, dict) and o.get("label")],
                "allow_multiple": bool(tool_input.get("allow_multiple", False)),
            })
    elif tool_name == "show_slide":
        try:
            page = int(tool_input.get("page"))
        except (TypeError, ValueError):
            return events
        if survey_id is None:
            return events
        asset = db.query(MediaAsset).filter(
            MediaAsset.survey_id == survey_id, MediaAsset.kind == "slide", MediaAsset.page_index == page
        ).first()
        if not asset:
            return events
        total = db.query(func.count(MediaAsset.id)).filter(MediaAsset.survey_id == survey_id, MediaAsset.kind == "slide").scalar() or 0
        events.append({
            "t": "media", "type": "image", "slide": page, "slide_total": total,
            "url": f"/api/assets/{asset.id}", "alt": f"Slide {page}",
            "caption": tool_input.get("caption", ""),
        })
    elif tool_name == "show_interactive":
        ev = _validate_interactive(tool_input)
        if ev:
            events.append(ev)
    return events


def _validate_interactive(inp: dict):
    """Normalise a show_interactive call into a safe, renderable event (or None)."""
    kind = (inp.get("kind") or "").strip()
    question = str(inp.get("question") or "").strip()
    if kind not in INTERACTIVE_KINDS or not question:
        return None
    strs = lambda v, n=8: [str(x).strip() for x in v if str(x).strip()][:n] if isinstance(v, list) else []
    ev = {"t": "interactive", "kind": kind, "question": question,
          "explanation": str(inp.get("explanation") or "").strip(), "hint": str(inp.get("hint") or "").strip()}
    if kind == "mcq":
        ev["options"] = strs(inp.get("options"), 6)
        if len(ev["options"]) < 2:
            return None
        try:
            ev["correct"] = int(inp.get("correct"))
        except (TypeError, ValueError):
            ev["correct"] = None
        if ev["correct"] is not None and not (0 <= ev["correct"] < len(ev["options"])):
            ev["correct"] = None
    elif kind == "fill_blank":
        ev["text"] = str(inp.get("text") or "").strip()
        ev["answers"] = strs(inp.get("answers"), 6)
        blanks = ev["text"].count("___")
        if not blanks or not ev["answers"]:
            return None
        ev["answers"] = ev["answers"][:blanks] if len(ev["answers"]) >= blanks else ev["answers"] + [""] * (blanks - len(ev["answers"]))
    elif kind == "order":
        ev["items"] = strs(inp.get("items"), 6)
        if len(ev["items"]) < 3:
            return None
    elif kind == "match":
        pairs = inp.get("pairs") if isinstance(inp.get("pairs"), list) else []
        ev["pairs"] = [{"left": str(p.get("left", "")).strip(), "right": str(p.get("right", "")).strip()}
                       for p in pairs if isinstance(p, dict) and p.get("left") and p.get("right")][:6]
        if len(ev["pairs"]) < 2:
            return None
    elif kind == "scale":
        ev["min_label"] = str(inp.get("min_label") or "Not at all").strip()
        ev["max_label"] = str(inp.get("max_label") or "Completely").strip()
    return ev


def _survey_slides(db: Session, survey_id) -> list:
    return (db.query(MediaAsset.id, MediaAsset.page_index, MediaAsset.text_content)
            .filter(MediaAsset.survey_id == survey_id, MediaAsset.kind == "slide")
            .order_by(MediaAsset.page_index).all())


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
    # briefing
    briefing_type: Optional[str] = None
    briefing_url: Optional[str] = None
    briefing_text: Optional[str] = None
    briefing_title: Optional[str] = None
    # visuals
    image_mode: Optional[str] = None
    image_provider: Optional[str] = None
    image_model: Optional[str] = None
    image_base_url: Optional[str] = None
    image_api_key: Optional[str] = None
    image_style: Optional[str] = None
    # llm override
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    llm_api_key: Optional[str] = None

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
    briefing_type: Optional[str] = None
    briefing_url: Optional[str] = None
    briefing_text: Optional[str] = None
    briefing_title: Optional[str] = None
    image_mode: Optional[str] = None
    image_provider: Optional[str] = None
    image_model: Optional[str] = None
    image_base_url: Optional[str] = None
    image_api_key: Optional[str] = None
    image_style: Optional[str] = None
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    llm_api_key: Optional[str] = None

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
    api_key: Optional[str] = None           # Anthropic key ("" clears)
    openrouter_key: Optional[str] = None    # OpenRouter key ("" clears)
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    image_provider: Optional[str] = None
    image_model: Optional[str] = None
    image_base_url: Optional[str] = None
    image_api_key: Optional[str] = None     # "" clears

class WizardRequest(BaseModel):
    goal: str
    audience: Optional[str] = None
    survey_type: Optional[str] = None
    num_questions: Optional[int] = 5
    tone: Optional[str] = None
    duration_minutes: Optional[int] = None
    extra: Optional[str] = None
    current: Optional[dict] = None
    feedback: Optional[str] = None
    survey_id: Optional[str] = None   # when refining an existing survey: lets the wizard read its slide deck

class TestImageRequest(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    style: Optional[str] = None
    prompt: Optional[str] = None


SURVEY_BRIEFING_TYPES = {"none", "slides", "video", "document", "image", "link"}
IMAGE_MODES = {"none", "stock", "generate"}


def _survey_public_dict(s: Survey, db: Session = None) -> dict:
    """Fields every admin-facing survey payload should expose."""
    slide_count = 0
    if db is not None:
        slide_count = db.query(func.count(MediaAsset.id)).filter(MediaAsset.survey_id == s.id, MediaAsset.kind == "slide").scalar() or 0
    return {
        "slide_count": int(slide_count),
        "briefing_type": s.briefing_type or "none",
        "briefing_url": s.briefing_url or "",
        "briefing_text": s.briefing_text or "",
        "briefing_title": s.briefing_title or "",
        "image_mode": s.image_mode or "stock",
        "image_provider": s.image_provider or "",
        "image_model": s.image_model or "",
        "image_base_url": s.image_base_url or "",
        "has_image_api_key": bool(s.encrypted_image_api_key),
        "image_style": s.image_style or "",
        "llm_provider": s.llm_provider or "",
        "llm_model": s.llm_model or "",
        "has_llm_api_key": bool(s.encrypted_llm_api_key),
    }


def _apply_survey_fields(survey: Survey, data: dict):
    """Apply create/update payload fields with validation and key encryption."""
    for field, value in data.items():
        if field in ("image_api_key", "llm_api_key"):
            continue
        if field == "briefing_type" and value is not None and value not in SURVEY_BRIEFING_TYPES:
            raise HTTPException(status_code=400, detail="Invalid briefing type")
        if field == "image_mode" and value is not None and value not in IMAGE_MODES:
            raise HTTPException(status_code=400, detail="Invalid image mode")
        if field == "image_provider" and value and value not in IMAGE_PROVIDERS:
            raise HTTPException(status_code=400, detail="Invalid image provider")
        if field == "llm_provider" and value and value not in PROVIDERS:
            raise HTTPException(status_code=400, detail="Invalid LLM provider")
        if field == "max_messages" and value is not None:
            value = max(1, min(int(value), 200))
        setattr(survey, field, value)
    if "image_api_key" in data:
        v = data["image_api_key"]
        if v is None:
            pass
        elif v.strip():
            survey.encrypted_image_api_key = encrypt_api_key(v.strip())
        else:
            survey.encrypted_image_api_key = None
    if "llm_api_key" in data:
        v = data["llm_api_key"]
        if v is None:
            pass
        elif v.strip():
            survey.encrypted_llm_api_key = encrypt_api_key(v.strip())
        else:
            survey.encrypted_llm_api_key = None


# ══════════════════════════════════════════════════════════════════
#  PAGE ROUTES
# ══════════════════════════════════════════════════════════════════

NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}

@app.get("/", response_class=HTMLResponse)
def serve_survey_page():
    return FileResponse("templates/survey.html", headers=NO_CACHE)

@app.get("/admin", response_class=HTMLResponse)
def serve_admin_page():
    return FileResponse("templates/admin.html", headers=NO_CACHE)

@app.get("/register", response_class=HTMLResponse)
def serve_register_page():
    return FileResponse("templates/register.html", headers=NO_CACHE)


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

def _validate_credentials(username: str, password: str):
    """Validate username (no spaces) and password (min 8 alphanumeric chars)."""
    if ' ' in username:
        raise HTTPException(status_code=400, detail="Username must not contain spaces")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        raise HTTPException(status_code=400, detail="Password must contain both letters and numbers")

@app.post("/api/auth/register")
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    _validate_credentials(req.username, req.password)
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
            **_survey_public_dict(s, db),
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
    code = (req.survey_code or generate_survey_code()).strip().upper()
    if db.query(Survey).filter(Survey.survey_code == code).first():
        raise HTTPException(status_code=400, detail="Survey code already in use")
    if req.survey_type and req.questions:
        system_prompt = compose_system_prompt(req.survey_type, req.questions, req.instructions or "")
    elif req.system_prompt:
        system_prompt = req.system_prompt
    else:
        raise HTTPException(status_code=400, detail="Either survey type + questions or a system prompt is required")
    survey = Survey(
        title=req.title.strip(),
        topic=req.topic.strip(),
        system_prompt=system_prompt,
        survey_code=code,
        admin_id=admin.id,
        status=SurveyStatus.ACTIVE,
    )
    data = req.dict(exclude_unset=True)
    for k in ("title", "topic", "system_prompt", "survey_code"):
        data.pop(k, None)
    _apply_survey_fields(survey, data)
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
    data = req.dict(exclude_unset=True)
    _apply_survey_fields(survey, data)
    if survey.survey_type and survey.questions:
        survey.system_prompt = compose_system_prompt(survey.survey_type, survey.questions, survey.instructions or "")
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
    db.query(SurveyInsight).filter(SurveyInsight.survey_id == survey_id).delete()
    db.query(AnalysisMessage).filter(AnalysisMessage.survey_id == survey_id).delete()
    db.query(MediaAsset).filter(MediaAsset.survey_id == survey_id).delete()
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
    db.query(MediaAsset).filter(MediaAsset.participant_id == participant.id).delete()
    db.delete(participant)
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
            db.query(MediaAsset).filter(MediaAsset.participant_id == p.id).delete()
            db.delete(p)
            deleted += 1
    db.commit()
    return {"ok": True, "deleted": deleted}


# ══════════════════════════════════════════════════════════════════
#  ADMIN - BRIEFING UPLOADS & MEDIA ASSETS
# ══════════════════════════════════════════════════════════════════

ALLOWED_UPLOADS = {
    "application/pdf": "document",
    "image/png": "image", "image/jpeg": "image", "image/webp": "image", "image/gif": "image",
    "video/mp4": "video", "video/webm": "video",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "slides",
    "application/vnd.ms-powerpoint": "slides",
}


@app.post("/api/surveys/{survey_id}/briefing/upload")
async def upload_briefing(
    survey_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    admin_ids = get_visible_admin_ids(db, admin)
    survey = db.query(Survey).filter(Survey.id == survey_id, Survey.admin_id.in_(admin_ids)).first()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")
    mime = (file.content_type or "").split(";")[0].strip().lower()
    kind = ALLOWED_UPLOADS.get(mime)
    if not kind:
        raise HTTPException(status_code=400, detail="Unsupported file type. Upload a PDF, PowerPoint, image, or MP4/WebM video.")
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)")
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    # Replace any previous briefing upload for this survey
    db.query(MediaAsset).filter(MediaAsset.survey_id == survey.id, MediaAsset.kind == "briefing").delete()
    asset = MediaAsset(
        survey_id=survey.id, kind="briefing", mime_type=mime, filename=file.filename,
        data=data, size_bytes=len(data),
    )
    db.add(asset)
    db.flush()
    survey.briefing_type = kind
    survey.briefing_url = f"/api/assets/{asset.id}"
    # Split decks into per-slide images + text so the chatbot can show and discuss each slide
    db.query(MediaAsset).filter(MediaAsset.survey_id == survey.id, MediaAsset.kind == "slide").delete()
    slide_count, slide_error = 0, ""
    if kind in ("slides", "document"):
        try:
            pages = await asyncio.to_thread(extract_slides, data, mime, file.filename or "")
            for pg in pages:
                db.add(MediaAsset(
                    survey_id=survey.id, kind="slide", mime_type=pg["mime"], filename=f"slide-{pg['page']}.jpg",
                    page_index=pg["page"], text_content=pg["text"], data=pg["image"], size_bytes=len(pg["image"]),
                ))
            slide_count = len(pages)
        except SlideError as e:
            slide_error = str(e)
        except Exception as e:  # never fail the upload because of slide splitting
            logger.warning(f"Slide extraction failed: {e}", exc_info=True)
            slide_error = "The file was saved, but it could not be split into slides."
    db.commit()
    return {"url": survey.briefing_url, "briefing_type": kind, "filename": file.filename, "size_bytes": len(data),
            "slide_count": slide_count, "slide_error": slide_error}


@app.get("/api/surveys/{survey_id}/slides")
def list_survey_slides(survey_id: str, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    admin_ids = get_visible_admin_ids(db, admin)
    survey = db.query(Survey).filter(Survey.id == survey_id, Survey.admin_id.in_(admin_ids)).first()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")
    return [{"page": r.page_index, "url": f"/api/assets/{r.id}", "text": (r.text_content or "")[:200]}
            for r in _survey_slides(db, survey.id)]


@app.get("/api/assets/{asset_id}")
def get_asset(asset_id: str, db: Session = Depends(get_db)):
    try:
        aid = uuid.UUID(asset_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")
    asset = db.query(MediaAsset).filter(MediaAsset.id == aid).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Not found")
    headers = {"Cache-Control": "public, max-age=31536000, immutable"}
    if asset.filename:
        safe = asset.filename.replace('"', "")
        headers["Content-Disposition"] = f'inline; filename="{safe}"'
    return Response(content=asset.data, media_type=asset.mime_type, headers=headers)


# ══════════════════════════════════════════════════════════════════
#  ADMIN - ANALYTICS
# ══════════════════════════════════════════════════════════════════

def _load_results(db: Session, survey: Survey) -> dict:
    counts = db.query(
        func.count(Participant.id).label("total"),
        func.count(case((Participant.status == ParticipantStatus.ACTIVE, 1))).label("active"),
        func.count(case((Participant.status == ParticipantStatus.COMPLETED, 1))).label("completed"),
        func.avg(Participant.duration_seconds).label("avg_duration"),
    ).filter(Participant.survey_id == survey.id).first()

    participants = (
        db.query(Participant, func.count(ChatMessage.id).label("msg_count"))
        .outerjoin(ChatMessage, ChatMessage.participant_id == Participant.id)
        .filter(Participant.survey_id == survey.id)
        .group_by(Participant.id)
        .order_by(Participant.started_at)
        .all()
    )
    participants_data = []
    for p, msg_count in participants:
        msgs = (
            db.query(ChatMessage)
            .filter(ChatMessage.participant_id == p.id)
            .order_by(ChatMessage.created_at)
            .all()
        )
        participants_data.append({
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
        })
    stats = {
        "total_participants": counts.total or 0,
        "active_participants": counts.active or 0,
        "completed_participants": counts.completed or 0,
        "avg_completion_seconds": round(counts.avg_duration or 0, 1),
    }
    return {"stats": stats, "participants": participants_data}


@app.get("/api/surveys/{survey_id}/results")
def get_survey_results(
    survey_id: str,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    admin_ids = get_visible_admin_ids(db, admin)
    survey = db.query(Survey).filter(Survey.id == survey_id, Survey.admin_id.in_(admin_ids)).first()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")
    results = _load_results(db, survey)
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
            **_survey_public_dict(survey, db),
        },
        "stats": results["stats"],
        "participants": results["participants"],
    }


@app.get("/api/surveys/{survey_id}/download-conversations")
def download_conversations(
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

    lines = []
    lines.append(f"Survey: {survey.title}")
    lines.append(f"Topic: {survey.topic}")
    lines.append(f"Status: {survey.status.value}")
    lines.append(f"Total participants: {len(survey.participants)}")
    lines.append(f"Exported: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("=" * 60)

    for p in sorted(survey.participants, key=lambda x: x.started_at):
        msgs = sorted(p.messages, key=lambda m: m.created_at)
        chat_msgs = [m for m in msgs if not m.content.startswith("[TOOL_EVENTS]")]
        if not chat_msgs:
            continue
        status_label = p.status.value
        duration = f"{round(p.duration_seconds/60, 1)} min" if p.duration_seconds else "in progress"
        lines.append("")
        lines.append(f"--- Participant {str(p.id)[:8]} | {status_label} | {duration} ---")
        if p.contact_name:
            lines.append(f"Name: {p.contact_name}")
        for m in chat_msgs:
            role_label = "Participant" if m.role == "user" else "Bot"
            lines.append(f"  [{role_label}]: {m.content}")

    content = "\n".join(lines)
    filename = f"{survey.title.replace(' ', '_')}_conversations.txt"
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _asset_loader(db: Session):
    def load(asset_id: str):
        try:
            return db.query(MediaAsset).filter(MediaAsset.id == uuid.UUID(asset_id)).first()
        except ValueError:
            return None
    return load


@app.get("/api/surveys/{survey_id}/report")
async def download_report(
    survey_id: str,
    request: Request,
    format: str = "html",
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """Full survey report: summary, AI insights, and every transcript with images."""
    admin_ids = get_visible_admin_ids(db, admin)
    survey = (
        db.query(Survey)
        .filter(Survey.id == survey_id, Survey.admin_id.in_(admin_ids))
        .options(joinedload(Survey.participants).joinedload(Participant.messages))
        .first()
    )
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")
    results = _load_results(db, survey)
    cached = db.query(SurveyInsight).filter(SurveyInsight.survey_id == survey_id).first()
    insights = None
    if cached:
        try:
            insights = json.loads(cached.insights_json)
        except Exception:
            insights = None
    elif results["participants"]:
        try:
            insights = await _generate_insights(survey, db)
        except Exception as e:
            logger.warning(f"Report insights generation failed: {e}")
    safe_title = "".join(c if c.isalnum() or c in "-_ " else "_" for c in survey.title).strip() or "survey"
    if format == "docx":
        data = build_survey_report_docx(survey, results["participants"], results["stats"], insights,
                                        asset_loader=_asset_loader(db), fetch_external=True)
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="{safe_title}_report.docx"'},
        )
    html = build_survey_report_html(survey, results["participants"], results["stats"], insights,
                                    asset_loader=_asset_loader(db), fetch_external=False)
    return HTMLResponse(html, headers=NO_CACHE)


# ══════════════════════════════════════════════════════════════════
#  ADMIN - ANALYSIS CHATBOT (insights from survey data)
# ══════════════════════════════════════════════════════════════════

ANALYST_SYSTEM = (
    "You are a survey data analyst for a school-sanctioned research platform. "
    "The data below contains anonymized survey responses collected with informed consent "
    "as part of an approved school initiative. Participant IDs are random hashes. "
    "Analyze the response patterns objectively. "
)


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

    all_conversations = []
    for p in survey.participants:
        if p.messages:
            chat_msgs = [m for m in sorted(p.messages, key=lambda x: x.created_at)
                         if not m.content.startswith("[TOOL_EVENTS]")]
            if not chat_msgs:
                continue
            conv = "\n".join(f"  {m.role}: {m.content}" for m in chat_msgs)
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

    prior = (
        db.query(AnalysisMessage)
        .filter(AnalysisMessage.survey_id == survey_id, AnalysisMessage.admin_id == admin.id)
        .order_by(AnalysisMessage.created_at)
        .all()
    )
    history = [{"role": m.role, "content": m.content} for m in prior if m.content and m.content.strip()]
    history.append({"role": "user", "content": req.message})
    deduped = []
    for msg in history:
        if deduped and deduped[-1]["role"] == msg["role"]:
            deduped[-1]["content"] += "\n\n" + msg["content"]
        else:
            deduped.append(msg)
    history = deduped

    db.add(AnalysisMessage(survey_id=survey_id, admin_id=admin.id, role="user", content=req.message))
    db.commit()

    cfg = resolve_llm_config(db, survey)
    system_prompt = (
        ANALYST_SYSTEM +
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

    logger.info(f"Analysis request: survey={survey_id}, provider={cfg.describe()}, history_len={len(history)}, context_chars={len(survey_context)}")

    async def analysis_stream():
        full_text = []
        yield f"data: {json.dumps({'t': 'status', 'v': 'Analyzing survey data...'})}\n\n"
        # Try the analysis model first, then the chat model as a fallback
        for analysis in (True, False):
            full_text = []
            try:
                async for ev in stream_chat(cfg, system_prompt, history, tools=None, max_tokens=4096, analysis=analysis):
                    if ev["type"] == "text":
                        full_text.append(ev["text"])
                        yield f"data: {json.dumps({'t': 'chunk', 'v': ev['text']})}\n\n"
                if full_text:
                    break
                logger.warning("Analysis model returned an empty response, trying fallback")
            except LLMError as e:
                logger.error(f"Analysis stream error ({cfg.describe()}, analysis={analysis}): {e.message}")
                if not analysis:
                    yield f"data: {json.dumps({'t': 'error', 'v': e.message})}\n\n"
                    return
            except Exception as e:
                logger.error(f"Analysis stream error: {e}", exc_info=True)
                if not analysis:
                    yield f"data: {json.dumps({'t': 'error', 'v': str(e)})}\n\n"
                    return

        assistant_text = "".join(full_text)
        if assistant_text.strip():
            try:
                db.add(AnalysisMessage(survey_id=survey_id, admin_id=admin.id, role="assistant", content=assistant_text))
                db.commit()
            except Exception as e:
                logger.error(f"Failed to save analysis message: {e}")
                db.rollback()
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
    return [{"id": str(m.id), "role": m.role, "content": m.content, "created_at": m.created_at.isoformat()} for m in messages]


class DeleteAnalysisRequest(BaseModel):
    message_ids: list[str]


@app.post("/api/surveys/{survey_id}/analysis-delete")
def delete_analysis_messages(
    survey_id: str,
    req: DeleteAnalysisRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    if not req.message_ids:
        return {"deleted": 0}
    count = (
        db.query(AnalysisMessage)
        .filter(
            AnalysisMessage.survey_id == survey_id,
            AnalysisMessage.admin_id == admin.id,
            AnalysisMessage.id.in_(req.message_ids),
        )
        .delete(synchronize_session=False)
    )
    db.commit()
    return {"deleted": count}


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


EMPTY_INSIGHTS = {"sentiment": {"positive": 0, "neutral": 0, "negative": 0}, "themes": [], "participants": []}


async def _generate_insights(survey, db: Session) -> dict:
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
        return dict(EMPTY_INSIGHTS)

    prompt = _build_insights_prompt(survey, participants_data)
    cfg = resolve_llm_config(db, survey)
    logger.info(f"Generating insights for survey {survey.id}: {len(participants_data)} participants, provider={cfg.describe()}, prompt length {len(prompt)} chars")
    insights_system = ANALYST_SYSTEM + "Return ONLY valid JSON, no markdown fences, no explanation."

    raw = ""
    for analysis in (True, False):
        try:
            result = await complete_chat(cfg, insights_system, [{"role": "user", "content": prompt}],
                                         max_tokens=4096, analysis=analysis)
        except LLMError as e:
            logger.error(f"Insights generation error ({cfg.describe()}, analysis={analysis}): {e.message}")
            continue
        except Exception as e:
            logger.error(f"Insights generation error: {e}", exc_info=True)
            continue
        raw = (result.get("text") or "").strip()
        if raw:
            break
        logger.warning("Empty insights response, trying fallback model")

    if not raw:
        return {**EMPTY_INSIGHTS, "error": "AI could not analyze the conversations — check the provider settings or try regenerating"}
    insights = extract_json_object(raw)
    if not isinstance(insights, dict):
        insights = {**EMPTY_INSIGHTS, "error": "Failed to parse insights"}

    existing = db.query(SurveyInsight).filter(SurveyInsight.survey_id == survey.id).first()
    now = datetime.now(timezone.utc)
    if existing:
        existing.insights_json = json.dumps(insights)
        existing.generated_at = now
    else:
        db.add(SurveyInsight(survey_id=survey.id, insights_json=json.dumps(insights), generated_at=now))
    db.commit()
    return insights


@app.get("/api/surveys/{survey_id}/insights")
async def get_survey_insights(
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

    insights = await _generate_insights(survey, db)
    return {"insights": insights, "generated_at": datetime.now(timezone.utc).isoformat(), "cached": False}


@app.post("/api/surveys/{survey_id}/insights/regenerate")
async def regenerate_survey_insights(
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
    insights = await _generate_insights(survey, db)
    return {"insights": insights, "generated_at": datetime.now(timezone.utc).isoformat(), "cached": False}


# ══════════════════════════════════════════════════════════════════
#  ADMIN - PROMPT WIZARD
# ══════════════════════════════════════════════════════════════════

WIZARD_SYSTEM = (
    "You are an expert instructional designer who writes prompts for AI facilitators that run "
    "conversational surveys, reflections and formative assessments. Participants may be school students, "
    "university students, teachers, employees or any adult group — match register and examples to the stated audience. "
    "Given the organiser's rough description, produce a complete, professional configuration.\n\n"
    "Return ONLY a JSON object with these keys (all strings unless noted):\n"
    '  "title": short survey title (max 8 words)\n'
    '  "topic": one-sentence description of what the survey is about\n'
    '  "survey_type": one of general_sensing | categorising | depth_survey | formative_assessment | reflection | guided_learning '
    '(guided_learning = walk a participant who finds the material hard through a slide deck: explain, then MCQ, then short answer, then longer answer, re-showing slides on mistakes)\n'
    '  "questions": the questions the facilitator must cover, numbered one per line, ordered from easy to demanding. '
    'Each question should be open, concrete and answerable by the target audience. Include a short note in brackets on what a strong answer contains where helpful. '
    'For guided_learning, group them by slide/section ("Slide 3: ...") and for each section give an MCQ (with options and the correct one), a short-answer prompt and a longer explain-it prompt. '
    'If DECK CONTENT is provided, base the questions on the actual slides and cite slide numbers.\n'
    '  "instructions": how the facilitator should behave — pacing, follow-up strategy, what to do with weak/strong answers, '
    'how to close, what NOT to do (e.g. never give away answers). 4-8 sentences.\n'
    '  "facilitator_intro": a friendly 1-2 sentence introduction the bot says at the start, in first person, naming the task\n'
    '  "briefing_text": 3-6 sentences the participant reads before starting, explaining the task, why it matters and what to expect\n'
    '  "image_style": a short visual style description for generated illustrations suited to the audience (e.g. "clean flat vector illustration, bright colours, no text")\n'
    '  "max_messages": integer, suggested number of participant replies for the whole conversation\n'
    "Write in clear, natural English appropriate for the audience's age. Never include markdown fences."
)


@app.post("/api/surveys/wizard")
async def survey_wizard(req: WizardRequest, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """AI assistant that drafts (or refines) the survey configuration from the organiser's brief."""
    goal = (req.goal or "").strip()
    if not goal and not (req.current and req.feedback):
        raise HTTPException(status_code=400, detail="Describe what you want the chatbot to find out")
    brief = [f"Organiser's goal: {goal}"]
    if req.audience:
        brief.append(f"Audience: {req.audience}")
    if req.survey_type:
        brief.append(f"Preferred survey type: {req.survey_type}")
    if req.num_questions:
        brief.append(f"Number of questions: about {req.num_questions}")
    if req.duration_minutes:
        brief.append(f"Target duration: about {req.duration_minutes} minutes")
    if req.tone:
        brief.append(f"Tone: {req.tone}")
    if req.extra:
        brief.append(f"Other notes: {req.extra}")
    if req.current:
        brief.append("\nCURRENT DRAFT (JSON):\n" + json.dumps(req.current, ensure_ascii=False, indent=1))
    if req.feedback:
        brief.append(f"\nORGANISER'S FEEDBACK — revise the draft accordingly, keeping what works:\n{req.feedback}")
    if req.survey_id:
        try:
            sid = uuid.UUID(req.survey_id)
            wiz_survey = db.query(Survey).filter(Survey.id == sid, Survey.admin_id.in_(get_visible_admin_ids(db, admin))).first()
        except ValueError:
            wiz_survey = None
        if wiz_survey:
            deck = _survey_slides(db, wiz_survey.id)
            if deck:
                brief.append(f"\nDECK CONTENT ({len(deck)} slides):\n" + deck_context(deck, max_chars=12000))
    cfg = resolve_llm_config(db, admin=admin)
    try:
        result = await complete_chat(cfg, WIZARD_SYSTEM, [{"role": "user", "content": "\n".join(brief)}],
                                     max_tokens=3000, analysis=True)
    except LLMError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    data = extract_json_object(result.get("text") or "")
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="The AI did not return a usable draft. Please try again.")
    if data.get("survey_type") not in SURVEY_TYPE_PROMPTS:
        data["survey_type"] = req.survey_type or "depth_survey"
    try:
        data["max_messages"] = max(3, min(int(data.get("max_messages") or 20), 100))
    except (TypeError, ValueError):
        data["max_messages"] = 20
    for k in ("title", "topic", "questions", "instructions", "facilitator_intro", "briefing_text", "image_style"):
        v = data.get(k)
        data[k] = (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) if v else "").strip()
    return {"draft": data, "provider": cfg.describe()}


# ══════════════════════════════════════════════════════════════════
#  PUBLIC - PARTICIPANT CHAT
# ══════════════════════════════════════════════════════════════════

def _briefing_payload(survey: Survey) -> dict:
    return {
        "type": survey.briefing_type or "none",
        "url": survey.briefing_url or "",
        "text": survey.briefing_text or "",
        "title": survey.briefing_title or "",
    }


def _build_chat_system(survey: Survey, cfg: LLMConfig, slides: list = None) -> str:
    system = survey.system_prompt
    if survey.briefing_text and survey.briefing_text.strip():
        system += (
            "\n\n[TASK BRIEFING — the participant has just read/watched this before the chat began. "
            "Refer to it where relevant and do not repeat it in full:\n"
            + survey.briefing_text.strip() + "\n]"
        )
    slides = slides or []
    if slides:
        system += (
            f"\n\n[DECK CONTENT — the briefing deck has {len(slides)} slides. Text of each slide:\n"
            + deck_context(slides) + "\n]"
        )
    return system + CONVERSATIONAL_PROMPT + build_tool_prompt(cfg, len(slides))


@app.post("/api/survey/join")
async def join_survey(req: JoinSurveyRequest, db: Session = Depends(get_db)):
    survey = db.query(Survey).filter(Survey.survey_code == req.survey_code.strip().upper()).first()
    if not survey:
        raise HTTPException(status_code=404, detail="Invalid survey code")
    if survey.status != SurveyStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="This survey is not currently active")

    session_token = secrets.token_urlsafe(32)
    participant = Participant(survey_id=survey.id, session_token=session_token, status=ParticipantStatus.ACTIVE)
    db.add(participant)
    db.commit()
    db.refresh(participant)

    cfg = resolve_llm_config(db, survey)
    slides = _survey_slides(db, survey.id)
    system = _build_chat_system(survey, cfg, slides)
    if survey.facilitator_intro and survey.facilitator_intro.strip():
        system += (
            "\n\n[When you first greet the participant, use this introduction (say it naturally):\n"
            + survey.facilitator_intro.strip()
            + "\n]"
        )
    tools = build_survey_tools(cfg, len(slides))
    try:
        result = await complete_chat(
            cfg, system,
            [{"role": "user", "content": "(The participant has just joined the survey. Greet them warmly with a text message and begin with the first question. Always include a written greeting — do not rely solely on tools.)"}],
            tools=tools, max_tokens=1024,
        )
    except LLMError as e:
        db.delete(participant)
        db.commit()
        raise HTTPException(status_code=e.status, detail=e.message)

    tool_events = []
    for call in result.get("tool_calls", []):
        tool_events.extend(await _process_tool_call(call["name"], call["input"], cfg, db, survey.id, participant.id))

    opening = result.get("text") or ""
    if not opening and tool_events:
        opening = "(presented interactive content)"
    if not opening:
        opening = "Hello, and welcome! Let's get started — whenever you're ready, tell me a little about yourself."

    db.add(ChatMessage(participant_id=participant.id, role="user", content="(The participant has just joined the survey.)"))
    db.add(ChatMessage(participant_id=participant.id, role="assistant", content=opening))
    if tool_events:
        db.add(ChatMessage(participant_id=participant.id, role="assistant", content=f"[TOOL_EVENTS]{json.dumps(tool_events)}"))
    db.commit()

    return {
        "session_token": session_token,
        "survey_title": survey.title,
        "survey_topic": survey.topic,
        "opening_message": opening,
        "opening_events": tool_events,
        "max_messages": survey.max_messages,
        "collect_name": survey.collect_name,
        "collect_email": survey.collect_email,
        "collect_phone": survey.collect_phone,
        "image_mode": cfg.image_mode,
        "briefing": _briefing_payload(survey),
    }


async def _chat_stream_generator(cfg: LLMConfig, system: str, history: list, participant, survey, db, near_limit: bool, slide_count: int = 0):
    """Stream text + tool results as SSE events."""
    full_text = []
    tool_events = []
    tools = build_survey_tools(cfg, slide_count)
    try:
        async for ev in stream_chat(cfg, system, history, tools=tools if not near_limit else None, max_tokens=1024):
            if ev["type"] == "text":
                full_text.append(ev["text"])
                yield f"data: {json.dumps({'t': 'chunk', 'v': ev['text']})}\n\n"
            elif ev["type"] == "tool_use":
                if ev["name"] == "show_image" and cfg.image_mode == "generate":
                    yield f"data: {json.dumps({'t': 'status', 'v': 'Creating an illustration…'})}\n\n"
                events = await _process_tool_call(ev["name"], ev["input"], cfg, db, survey.id, participant.id)
                tool_events.extend(events)
                for event in events:
                    yield f"data: {json.dumps(event)}\n\n"
        yield f"data: {json.dumps({'t': 'status', 'v': ''})}\n\n"
    except LLMError as e:
        yield f"data: {json.dumps({'t': 'error', 'v': e.message})}\n\n"
        return
    except Exception as e:
        logger.error(f"Chat stream error: {e}", exc_info=True)
        yield f"data: {json.dumps({'t': 'error', 'v': 'Something went wrong. Please try again.'})}\n\n"
        return

    assistant_text = "".join(full_text)
    if not assistant_text and tool_events:
        assistant_text = "(presented interactive content)"
    if not assistant_text:
        assistant_text = "Could you tell me a bit more about that?"
        yield f"data: {json.dumps({'t': 'chunk', 'v': assistant_text})}\n\n"

    db.add(ChatMessage(participant_id=participant.id, role="assistant", content=assistant_text))
    if tool_events:
        db.add(ChatMessage(participant_id=participant.id, role="assistant", content=f"[TOOL_EVENTS]{json.dumps(tool_events)}"))

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
    survey = participant.survey
    return {
        "session_token": participant.session_token,
        "survey_title": survey.title,
        "survey_topic": survey.topic,
        "max_messages": survey.max_messages,
        "collect_name": survey.collect_name,
        "collect_email": survey.collect_email,
        "collect_phone": survey.collect_phone,
        "user_message_count": user_msg_count,
        "image_mode": survey.image_mode or "stock",
        "briefing": _briefing_payload(survey),
        "messages": [
            {"role": m.role, "content": m.content, "created_at": m.created_at.isoformat()}
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
    message = (req.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is empty")

    db.add(ChatMessage(participant_id=participant.id, role="user", content=message))
    db.commit()

    msgs = sorted(participant.messages, key=lambda m: m.created_at)
    history = [
        {"role": m.role, "content": m.content}
        for m in msgs
        if not m.content.startswith("[TOOL_EVENTS]")
    ]
    history.append({"role": "user", "content": message})

    user_message_count = sum(1 for m in history if m["role"] == "user")
    near_limit = user_message_count >= participant.survey.max_messages

    survey = participant.survey
    cfg = resolve_llm_config(db, survey)
    slides = _survey_slides(db, survey.id)
    system = _build_chat_system(survey, cfg, slides)
    if near_limit:
        system += (
            "\n\n[SYSTEM NOTE: This is the participant's last allowed message. "
            "Thank them for their time, provide a brief summary of what you gathered, "
            "and end the conversation warmly. Do not use tools in this final message.]"
        )

    gen = _chat_stream_generator(cfg, system, history, participant, survey, db, near_limit, len(slides))
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
    """Save participant contact details."""
    participant = db.query(Participant).filter(Participant.session_token == req.session_token).first()
    if not participant:
        raise HTTPException(status_code=404, detail="Session not found")
    if req.name:
        participant.contact_name = req.name.strip()[:255]
    if req.email:
        participant.contact_email = req.email.strip()[:255]
    if req.phone:
        participant.contact_phone = req.phone.strip()[:100]
    db.commit()
    return {"ok": True}


@app.get("/api/survey/my-report")
def participant_report(session_token: str, db: Session = Depends(get_db)):
    """Printable copy of the participant's own conversation."""
    participant = (
        db.query(Participant)
        .filter(Participant.session_token == session_token)
        .options(joinedload(Participant.messages), joinedload(Participant.survey))
        .first()
    )
    if not participant:
        raise HTTPException(status_code=404, detail="Session not found")
    msgs = sorted(participant.messages, key=lambda m: m.created_at)
    html = build_participant_report_html(participant.survey, participant, msgs, asset_loader=_asset_loader(db))
    return HTMLResponse(html, headers=NO_CACHE)


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
        "has_openrouter_key": bool(admin.encrypted_openrouter_key),
        "llm_provider": admin.llm_provider or "",
        "llm_model": admin.llm_model or "",
    }


@app.get("/api/admin/settings")
def get_settings(db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    effective = resolve_llm_config(db, admin=admin)
    return {
        "llm_provider": admin.llm_provider or "",
        "llm_model": admin.llm_model or "",
        "has_api_key": bool(admin.encrypted_api_key),
        "has_openrouter_key": bool(admin.encrypted_openrouter_key),
        "image_provider": admin.image_provider or "",
        "image_model": admin.image_model or "",
        "image_base_url": admin.image_base_url or "",
        "has_image_api_key": bool(admin.encrypted_image_api_key),
        "effective": {
            "provider": effective.provider,
            "model": effective.model,
            "analysis_model": effective.analysis_model,
            "has_key": bool(effective.api_key),
            "image_provider": effective.image_provider,
            "image_model": effective.image_model,
            "has_image_key": bool(effective.image_api_key),
        },
        "env": {
            "anthropic_key": bool(ANTHROPIC_API_KEY),
            "openrouter_key": bool(OPENROUTER_API_KEY),
            "unsplash": bool(UNSPLASH_ACCESS_KEY),
            "pexels": bool(PEXELS_API_KEY),
            "openai_key": bool(os.environ.get("OPENAI_API_KEY")),
        },
        "defaults": {
            "anthropic": {"chat": CLAUDE_CHAT_MODEL, "analysis": CLAUDE_ANALYSIS_MODEL},
            "openrouter": {"chat": default_model("openrouter"), "analysis": default_model("openrouter", analysis=True)},
        },
        "image_providers": IMAGE_PROVIDERS,
    }


def _apply_settings(target: AdminUser, req: UpdateSettings):
    if req.api_key is not None:
        target.encrypted_api_key = encrypt_api_key(req.api_key.strip()) if req.api_key.strip() else None
    if req.openrouter_key is not None:
        target.encrypted_openrouter_key = encrypt_api_key(req.openrouter_key.strip()) if req.openrouter_key.strip() else None
    if req.llm_provider is not None:
        if req.llm_provider and req.llm_provider not in PROVIDERS:
            raise HTTPException(status_code=400, detail="Invalid LLM provider")
        target.llm_provider = req.llm_provider or None
    if req.llm_model is not None:
        target.llm_model = req.llm_model.strip() or None
    if req.image_provider is not None:
        if req.image_provider and req.image_provider not in IMAGE_PROVIDERS:
            raise HTTPException(status_code=400, detail="Invalid image provider")
        target.image_provider = req.image_provider or None
    if req.image_model is not None:
        target.image_model = req.image_model.strip() or None
    if req.image_base_url is not None:
        target.image_base_url = req.image_base_url.strip() or None
    if req.image_api_key is not None:
        target.encrypted_image_api_key = encrypt_api_key(req.image_api_key.strip()) if req.image_api_key.strip() else None


@app.put("/api/admin/settings")
def update_settings(req: UpdateSettings, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    _apply_settings(admin, req)
    db.commit()
    return {
        "ok": True,
        "has_api_key": bool(admin.encrypted_api_key),
        "has_openrouter_key": bool(admin.encrypted_openrouter_key),
        "has_image_api_key": bool(admin.encrypted_image_api_key),
    }


@app.get("/api/admin/models")
async def list_models(provider: str = "openrouter", db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Model catalogue for the settings/wizard dropdowns."""
    if provider == "anthropic":
        return {
            "chat": [
                {"id": CLAUDE_CHAT_MODEL, "name": f"{CLAUDE_CHAT_MODEL} (default chat)"},
                {"id": CLAUDE_ANALYSIS_MODEL, "name": f"{CLAUDE_ANALYSIS_MODEL} (default analysis)"},
                {"id": "claude-sonnet-5", "name": "Claude Sonnet 5 (recommended)"},
                {"id": "claude-opus-5", "name": "Claude Opus 5 (most capable Opus)"},
                {"id": "claude-fable-5-1", "name": "Claude Fable 5.1 (Anthropic's most capable model, premium price)"},
                {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5 (fastest, cheapest)"},
            ],
            "image": [],
        }
    key = _dec(admin.encrypted_openrouter_key) or OPENROUTER_API_KEY
    try:
        return await list_openrouter_models(key)
    except LLMError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


@app.post("/api/admin/test-image")
async def test_image(req: TestImageRequest, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Generate a sample image with the given (or stored) settings and return it inline."""
    base = resolve_llm_config(db, admin=admin)
    provider = req.provider or base.image_provider
    if provider not in IMAGE_PROVIDERS:
        raise HTTPException(status_code=400, detail="Invalid image provider")
    key = (req.api_key or "").strip()
    if not key:
        key = _dec(admin.encrypted_image_api_key) if admin.image_provider == provider else ""
        if not key and provider == "openrouter":
            key = _dec(admin.encrypted_openrouter_key) or OPENROUTER_API_KEY
        if not key and provider == "openai":
            key = os.environ.get("OPENAI_API_KEY", "")
        if not key and provider == "pollinations":
            key = os.environ.get("POLLINATIONS_API_KEY", "")
    cfg = LLMConfig(
        provider=base.provider, model=base.model, api_key=base.api_key,
        image_mode="generate", image_provider=provider,
        image_model=(req.model or "").strip() or (base.image_model if base.image_provider == provider else ""),
        image_base_url=(req.base_url or "").strip() or base.image_base_url,
        image_api_key=key, image_style=(req.style or "").strip(),
    )
    prompt = (req.prompt or "").strip() or "A small group of people discussing ideas around a table in a bright room"
    try:
        data, mime = await generate_image(cfg, prompt)
    except LLMError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Image generation failed: {e}")
    import base64 as _b64
    return {"data_url": f"data:{mime};base64,{_b64.b64encode(data).decode()}", "provider": provider, "model": cfg.image_model}


@app.post("/api/admin/invite")
def create_invite(db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    if admin.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can invite facilitators")
    code = secrets.token_urlsafe(16)
    invite = InviteCode(code=code, admin_id=admin.id)
    db.add(invite)
    db.commit()
    return {"code": code}


@app.post("/api/auth/register-teacher")
def register_teacher(req: TeacherRegister, db: Session = Depends(get_db)):
    _validate_credentials(req.username, req.password)
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
        raise HTTPException(status_code=403, detail="Only admins can view facilitators")
    teachers = db.query(AdminUser).filter(AdminUser.parent_admin_id == admin.id).all()
    result = []
    for t in teachers:
        survey_count = db.query(Survey).filter(Survey.admin_id == t.id).count()
        result.append({
            "id": str(t.id),
            "username": t.username,
            "has_api_key": bool(t.encrypted_api_key),
            "has_openrouter_key": bool(t.encrypted_openrouter_key),
            "llm_provider": t.llm_provider or "",
            "survey_count": survey_count,
            "created_at": t.created_at.isoformat(),
        })
    return result


@app.delete("/api/admin/teachers/{teacher_id}")
def remove_teacher(teacher_id: str, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    if admin.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can remove facilitators")
    teacher = db.query(AdminUser).filter(
        AdminUser.id == teacher_id, AdminUser.parent_admin_id == admin.id
    ).first()
    if not teacher:
        raise HTTPException(status_code=404, detail="Facilitator not found")
    db.query(Survey).filter(Survey.admin_id == teacher.id).update({"admin_id": admin.id})
    db.query(AnalysisMessage).filter(AnalysisMessage.admin_id == teacher.id).update({"admin_id": admin.id})
    db.query(InviteCode).filter(InviteCode.used_by_id == teacher.id).update({"used_by_id": None, "used_at": None})
    db.delete(teacher)
    db.commit()
    return {"ok": True}


@app.put("/api/admin/teachers/{teacher_id}/api-key")
def update_teacher_api_key(teacher_id: str, req: UpdateSettings, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    if admin.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can update facilitator API keys")
    teacher = db.query(AdminUser).filter(
        AdminUser.id == teacher_id, AdminUser.parent_admin_id == admin.id
    ).first()
    if not teacher:
        raise HTTPException(status_code=404, detail="Facilitator not found")
    _apply_settings(teacher, req)
    db.commit()
    return {"ok": True, "has_api_key": bool(teacher.encrypted_api_key), "has_openrouter_key": bool(teacher.encrypted_openrouter_key)}


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
    return {"status": "ok", "version": "2.0.0"}


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

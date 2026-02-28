# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI-powered survey chatbot with agentic tool-use. Participants join via a survey code and chat with a Claude-powered bot that can show images, videos, and interactive button options. Admins create surveys, monitor live participation, view AI-generated insights (sentiment, themes, engagement), read transcripts, and analyze responses via an AI chatbot.

## Commands

```bash
# Local development (requires PostgreSQL running)
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/survey_db
export ANTHROPIC_API_KEY=sk-ant-...
export SECRET_KEY=dev-secret-key
# Optional: for media in chat
export UNSPLASH_ACCESS_KEY=...
export PEXELS_API_KEY=...
pip install -r requirements.txt
python main.py                    # Runs on http://localhost:8000

# Start local PostgreSQL via Docker
docker run -d --name survey-pg -e POSTGRES_DB=survey_db -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16

# Deploy to Railway
railway up
```

No test suite, linter, or build step exists. The frontend is vanilla HTML/CSS/JS served from `templates/`.

## Architecture

**Four Python modules, no framework beyond FastAPI:**

- `main.py` — All API routes and the FastAPI app. Uses Anthropic tool_use API for agentic chat (show_image, show_buttons, show_video). Contains tool definitions (`SURVEY_TOOLS`), async media fetchers (Unsplash/Pexels), insights generation, and Pydantic schemas inline. SSE streaming at `/api/survey/chat/stream` with typed events: `chunk`, `media`, `buttons`, `done`, `error`.
- `models.py` — SQLAlchemy ORM models using PostgreSQL UUID primary keys. Six tables: `admin_users`, `surveys`, `participants`, `chat_messages`, `analysis_messages`, `survey_insights`. Enums: `SurveyStatus` (draft/active/closed), `ParticipantStatus` (active/completed/abandoned).
- `database.py` — Engine creation, session factory, `init_db()` with inline migrations. Auto-corrects Railway's `postgres://` to `postgresql://`. Pool sized for ~100 concurrent connections (25 + 75 overflow).
- `auth.py` — PBKDF2 password hashing, JWT tokens (PyJWT), 24-hour expiry. Admin auth via cookie (`admin_token`) or Bearer header.

**Frontend (no build step, glassmorphism design):**

- `templates/survey.html` — Participant chat with responsive media panel (side-by-side on desktop, top on mobile), interactive button options (single/multi-select), typing indicator, animated completion screen with confetti.
- `templates/admin.html` — Admin SPA with Insights tab (sentiment bars, theme rankings, engagement histogram, sortable analytics table), plus survey CRUD, transcript viewer, and analysis chatbot.

**Key flows:**

1. Participant joins → `POST /api/survey/join` → creates Participant, Claude generates opening message
2. Chat → `POST /api/survey/chat/stream` (SSE) → Claude may call tools (show_image/show_buttons/show_video), backend processes tool calls and streams typed events
3. Tool events stored as `[TOOL_EVENTS]` prefixed messages for transcript replay, filtered from Claude history
4. Insights → `GET /api/surveys/{id}/insights` → Claude analyzes all transcripts, returns structured JSON (sentiment, themes, engagement), cached in `survey_insights` table for 5 minutes
5. Analysis → `POST /api/surveys/{id}/analyze` → freeform AI analysis chatbot

**Environment variables:** `DATABASE_URL`, `ANTHROPIC_API_KEY`, `SECRET_KEY`, `DEFAULT_ADMIN_USER`, `DEFAULT_ADMIN_PASS`, `CLAUDE_MODEL` (default: `claude-haiku-4-5-20251001`), `PORT` (default: 8000), `UNSPLASH_ACCESS_KEY` (optional, for images), `PEXELS_API_KEY` (optional, for videos).

**Deployment:** Railway via Docker. Health check at `/api/health`. DB tables auto-created on startup with retry logic.

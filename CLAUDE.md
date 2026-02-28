# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI-powered survey chatbot where participants join via a survey code and chat with a Claude-powered bot. Admins create surveys, monitor live participation, read transcripts, and analyze responses via an AI analysis chatbot.

## Commands

```bash
# Local development (requires PostgreSQL running)
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/survey_db
export ANTHROPIC_API_KEY=sk-ant-...
export SECRET_KEY=dev-secret-key
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

- `main.py` — All API routes and the FastAPI app. Handles both public survey chatbot endpoints and admin dashboard endpoints. Contains Pydantic request schemas inline. Streaming SSE endpoint at `/api/survey/chat/stream`.
- `models.py` — SQLAlchemy ORM models using PostgreSQL UUID primary keys. Five tables: `admin_users`, `surveys`, `participants`, `chat_messages`, `analysis_messages`. Enums: `SurveyStatus` (draft/active/closed), `ParticipantStatus` (active/completed/abandoned).
- `database.py` — Engine creation, session factory, `init_db()` with inline migration (ALTER TABLE ADD COLUMN IF NOT EXISTS). Auto-corrects Railway's `postgres://` to `postgresql://`. Pool sized for ~100 concurrent connections (25 + 75 overflow).
- `auth.py` — PBKDF2 password hashing, JWT tokens (PyJWT), 24-hour expiry. Admin auth via cookie (`admin_token`) or Bearer header.

**Frontend (no build step):**

- `templates/survey.html` — Participant chat interface. Joined via survey code, uses SSE streaming.
- `templates/admin.html` — Full admin SPA (login, survey CRUD, live monitoring, transcript viewer, analysis chatbot).
- `templates/typing_monkey_chatbot.html` — Animated mascot component.

**Key flows:**

1. Participant joins → `POST /api/survey/join` → creates Participant, Claude generates opening message
2. Chat → `POST /api/survey/chat/stream` (SSE) → saves messages, auto-completes at `max_messages`
3. Analysis → `POST /api/surveys/{id}/analyze` → feeds all conversation transcripts as context to Claude

**Environment variables:** `DATABASE_URL`, `ANTHROPIC_API_KEY`, `SECRET_KEY`, `DEFAULT_ADMIN_USER`, `DEFAULT_ADMIN_PASS`, `CLAUDE_MODEL` (default: `claude-haiku-4-5-20251001`), `PORT` (default: 8000).

**Deployment:** Railway via Docker. Health check at `/api/health`. DB tables auto-created on startup with retry logic.

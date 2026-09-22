# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Ponder** (name configurable via `APP_NAME`) — AI-powered conversational survey / reflection / formative-assessment chatbot for classrooms, teams and communities (participants may be students, teachers or any adult group). Participants join via an access code, read a teacher-supplied briefing (slides, video or document), then chat with an AI facilitator in a two-panel UI (visual panel + chat) that can show AI-generated illustrations, stock images, videos and button options. Organisers (teachers, facilitators, HR, researchers) design surveys with an AI-assisted wizard, monitor participation, view insights, read transcripts and download reports (HTML/DOCX). The create wizard and Setup tab auto-save drafts to `localStorage` (`ponder_draft_new_<username>` / `ponder_draft_edit_<surveyId>`), excluding passwords/API keys and chosen files. The chat model can be Claude (Anthropic SDK) or any model on OpenRouter.

## Commands

```bash
# Local development (requires PostgreSQL running)
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/survey_db
export ANTHROPIC_API_KEY=sk-ant-...      # or OPENROUTER_API_KEY=sk-or-...
export SECRET_KEY=dev-secret-key
export ENCRYPTION_KEY=$(python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
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

**Python modules, no framework beyond FastAPI:**

- `main.py` — All API routes and the FastAPI app. Builds the tool list per survey (`build_survey_tools(cfg, slide_count)`: show_buttons and show_interactive always; show_slide when the survey has slides; show_image as AI generation or stock photo depending on `image_mode`; show_video when Pexels is configured). `show_interactive` kinds (`mcq`, `fill_blank`, `order`, `match`, `scale`) are validated by `_validate_interactive` and emitted as `interactive` SSE events; the participant's result comes back as a normal user message starting with `[Interactive`. Survey type `guided_learning` walks weaker participants through a deck slide by slide (explain → MCQ → short answer → longer answer, re-showing the slide on mistakes), resolves provider config (`resolve_llm_config`: survey override → owner → parent admin → env), processes tool calls (`_process_tool_call` persists generated images as `MediaAsset` rows), and serves the wizard (`POST /api/surveys/wizard`), briefing uploads, assets (`GET /api/assets/{id}`), settings, insights, analysis chat and reports. SSE streaming at `/api/survey/chat/stream` with typed events: `chunk`, `status`, `media` (images, videos and slides — slides carry `slide`/`slide_total`), `buttons`, `interactive`, `done`, `error`.
- `llm.py` — Provider abstraction. `stream_chat` / `complete_chat` take an `LLMConfig` and route to Anthropic (official SDK, `messages.stream`/`create` with tools) or OpenRouter (OpenAI-compatible chat completions over httpx, with tool-call delta accumulation). Tools are always defined in Anthropic `input_schema` form and converted for OpenRouter. `list_openrouter_models` feeds the model dropdowns. `LLMError` carries a user-safe message.
- `slides.py` — Splits an uploaded PDF/PowerPoint briefing into per-slide JPEG images + text (PyMuPDF; PowerPoint is first converted with LibreOffice `soffice`, installed in the Docker image). Slides are stored as `media_assets` rows with `kind="slide"`, `page_index`, `text_content`; `deck_context()` builds the DECK CONTENT block for the system prompt.
- `imagegen.py` — Image generation providers returning `(bytes, mime)`: `pollinations` (free, no key), `openrouter` (chat completions with `modalities: ["image","text"]`), `openai` (Images API, or any compatible endpoint via `image_base_url`).
- `report.py` — HTML (print-to-PDF) and DOCX report builders for the teacher's full survey report and the participant's own transcript; embeds stored images inline.
- `models.py` — SQLAlchemy ORM models using PostgreSQL UUID primary keys. Tables: `admin_users` (role, parent_admin_id, encrypted Anthropic/OpenRouter/image keys, provider defaults), `surveys` (wizard fields, briefing_*, image_*, llm_* overrides), `participants`, `chat_messages`, `analysis_messages`, `survey_insights`, `invite_codes`, `media_assets` (generated images, uploaded briefings and per-slide images as BYTEA; `page_index`/`text_content` for slides).
- `database.py` — Engine creation, session factory, `init_db()` with inline `ADD COLUMN IF NOT EXISTS` migrations. Auto-corrects Railway's `postgres://` to `postgresql://`.
- `auth.py` — PBKDF2 password hashing, JWT tokens (PyJWT), 24-hour expiry, Fernet encryption for stored API keys. Admin auth via cookie (`admin_token`), Bearer header, or `?token=` query (used for report/download links).

**Frontend (no build step, clean professional design, Inter font):**

- `templates/survey.html` — Participant flow: join → optional contact details → briefing (embeds YouTube/Vimeo/Google Slides/Drive/Canva/PDF/Office viewer/MP4/image via `buildEmbed`) → two-panel chat (visual panel left on desktop, on top and collapsible on mobile, with image/slide history thumbnails; `renderInteractive` draws MCQ, fill-in-the-blank, ordering, matching and scale cards in the chat with instant feedback) → completion with "Download my responses". Resumes sessions from localStorage.
- `templates/admin.html` — Teacher SPA: 5-step create wizard (Describe with AI draft → Conversation → Briefing → Visuals → Publish), Setup tab reusing the same form (`buildSurveyForm(prefix)` / `readForm` / `fillForm`), Insights, Participants, Conversations (renders generated images), Analysis chat with charts, report buttons, account Settings modal (provider/model/keys, image provider defaults).

**Key flows:**

1. Teacher creates a survey → wizard `POST /api/surveys/wizard` drafts JSON config with the analysis model (pass `survey_id` when refining to give it the deck text) → `POST /api/surveys` → optional `POST /api/surveys/{id}/briefing/upload` (decks are split into slides; `GET /api/surveys/{id}/slides` lists them)
2. Participant joins → `POST /api/survey/join` → creates Participant, opening message generated via `complete_chat` with tools; response includes `briefing` and `image_mode`
3. Chat → `POST /api/survey/chat/stream` (SSE) → `stream_chat` yields text and tool_use events; image tool calls emit a `status` event, generate the image, store it, and emit `media` with `/api/assets/{id}`
4. Tool events stored as `[TOOL_EVENTS]` prefixed messages for transcript replay and reports, filtered from model history
5. Insights → `GET /api/surveys/{id}/insights` → structured JSON cached 5 minutes; Analysis → `POST /api/surveys/{id}/analyze`
6. Reports → `GET /api/surveys/{id}/report?format=html|docx` (teacher), `GET /api/survey/my-report?session_token=` (participant)

**Environment variables:** `DATABASE_URL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_WORKSPACE_ID` (only for organisation-level keys that are not scoped to a workspace; sent as the `anthropic-workspace-id` header), `OPENROUTER_API_KEY` (if only this is set, OpenRouter becomes the default provider), `OPENROUTER_BASE_URL` (default `https://openrouter.ai/api/v1`; point at a mock for tests), `OPENROUTER_CHAT_MODEL` / `OPENROUTER_ANALYSIS_MODEL`, `OPENAI_API_KEY` / `POLLINATIONS_API_KEY` (server-wide image keys), `SECRET_KEY`, `ENCRYPTION_KEY` (Fernet key for stored API keys; auto-generated if unset, which invalidates stored keys on restart), `DEFAULT_ADMIN_USER`, `DEFAULT_ADMIN_PASS`, `CLAUDE_CHAT_MODEL` (default: `claude-sonnet-5`, used for student conversations), `CLAUDE_ANALYSIS_MODEL` (default: `claude-opus-5`, used for insights, analysis chat and the wizard), `APP_URL`, `MAX_UPLOAD_MB` (default 25), `MAX_SLIDES` (default 80), `PORT` (default: 8000), `UNSPLASH_ACCESS_KEY` (optional, stock photos), `PEXELS_API_KEY` (optional, videos).

**Testing locally without API keys:** run an OpenAI-compatible mock on a local port and set `OPENROUTER_BASE_URL=http://127.0.0.1:9999/v1 OPENROUTER_API_KEY=test-key` with no `ANTHROPIC_API_KEY`; the app then routes chat, wizard, insights and image generation (`modalities: ["image"]`) through the mock.

**Deployment:** Railway via Docker. Health check at `/api/health`. DB tables auto-created on startup with retry logic.

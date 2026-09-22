# Ponder — AI-facilitated conversational surveys and reflections

Ponder is an AI-facilitated survey, reflection and formative-assessment chatbot for classrooms, teams and communities — participants can be students, teachers or any adult group. Participants join with an **access code**, review the organiser's briefing, then talk with an AI facilitator that elicits their thinking (with illustrations for each question). Organisers design conversations with an AI-assisted wizard, monitor results live, and download reports.

Work in the survey builder is **auto-saved locally** as you type, so a closed tab or lost connection never loses a half-built survey.

## Features

### Participant experience (`/`)
- Students enter an access code (or open a `/?code=…` link)
- **Task briefing first**: the organiser's slide deck, video, PDF or document is shown before the chat (YouTube, Vimeo, Google Slides, Canva, Drive, PDF, uploaded PowerPoint/PDF/MP4)
- **Slide-aware chatbot**: uploaded PDF/PowerPoint decks are split into slides; the facilitator reads the slide text, shows any slide in the visual panel while discussing it, and re-shows it when a participant struggles
- **Interactive checks in the chat**: multiple choice, fill-in-the-blank, put-in-order, match-the-pairs and confidence scales with instant feedback; results flow back into the conversation and reports
- **Guided learning** survey type: walk weaker learners through a deck one idea at a time — explain, MCQ, short answer, then a longer answer
- **Two-panel chat**: a visual panel and the conversation — side by side on desktop, visual panel on top on mobile
- The facilitator bot asks one question at a time and probes for reasoning; it can show **AI-generated illustrations** for each question, **stock photos**, **videos** and **button options**
- Sessions resume after a refresh; students can **download their own responses** at the end

### Teacher / admin dashboard (`/admin`)
- **AI-assisted setup wizard**: describe the goal and audience; the assistant drafts the title, questions, facilitator instructions, opening line, student briefing and illustration style. Refine with feedback at any time.
- **Briefing step**: link or upload the material students see first
- **Visuals step**: choose no visuals, stock photos, or AI-generated illustrations with a pluggable image provider (free Pollinations, OpenRouter image models, OpenAI-compatible Images API) and a test button
- **AI insights**: sentiment, themes, engagement — auto-generated and cached
- **Conversation viewer** with generated images inline; **analysis chatbot** with charts
- **Reports**: print-ready HTML report and a Word (DOCX) download with summary, insights and every transcript (images included)
- **Provider settings**: run the chatbot on Claude (Anthropic) or any model on OpenRouter, per account or per survey

## Tech Stack
- **Backend**: FastAPI (Python)
- **Database**: PostgreSQL (Railway addon); generated images and uploaded briefings are stored in the database
- **AI**: Anthropic Claude API (default) or OpenRouter; image generation via Pollinations / OpenRouter / OpenAI-compatible
- **Frontend**: Vanilla HTML/CSS/JS (no build step)
- **Deployment**: Railway (Docker)

---

## Deploy to Railway

### 1. Create a Railway project
1. Go to [railway.app](https://railway.app) and create a new project
2. Connect your GitHub repo **or** use `railway up` from CLI

### 2. Add PostgreSQL
1. In your Railway project, click **+ New** → **Database** → **PostgreSQL**
2. Railway automatically sets `DATABASE_URL`

### 3. Set environment variables
In your Railway service settings, add:

| Variable | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key (teachers can also add their own in Settings) |
| `ANTHROPIC_WORKSPACE_ID` | *(Optional)* Only if your Anthropic key is an organisation-level key not scoped to a workspace; the workspace ID from Console → Settings → Workspaces |
| `OPENROUTER_API_KEY` | *(Optional)* OpenRouter key; used when a teacher selects OpenRouter without their own key. If no Anthropic key is set, OpenRouter becomes the default provider |
| `OPENROUTER_CHAT_MODEL` / `OPENROUTER_ANALYSIS_MODEL` | *(Optional)* default OpenRouter models (`anthropic/claude-haiku-4.5`, `anthropic/claude-sonnet-4.5`) |
| `OPENAI_API_KEY` / `POLLINATIONS_API_KEY` | *(Optional)* server-wide keys for image generation providers |
| `CLAUDE_CHAT_MODEL` / `CLAUDE_ANALYSIS_MODEL` | *(Optional)* Claude models for conversations and analysis (defaults `claude-sonnet-5` for conversations, `claude-opus-5` for analysis) |
| `SECRET_KEY` | A random string for JWT signing (e.g. `openssl rand -hex 32`) |
| `ENCRYPTION_KEY` | A Fernet key for encrypting stored API keys (`python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"`). Set it so keys survive restarts |
| `APP_URL` | *(Optional)* public URL of the app, sent to OpenRouter as the referer |
| `MAX_UPLOAD_MB` | *(Optional)* briefing upload limit (default 25) |
| `DEFAULT_ADMIN_USER` | Initial admin username (default: `admin`) |
| `DEFAULT_ADMIN_PASS` | Initial admin password (default: `admin123`) |
| `UNSPLASH_ACCESS_KEY` | *(Optional)* Unsplash API key for images in chat |
| `PEXELS_API_KEY` | *(Optional)* Pexels API key for videos in chat |

> `DATABASE_URL` is auto-provided by Railway's PostgreSQL addon.

### 4. Deploy
```bash
# Option A: Railway CLI
railway up

# Option B: Push to connected GitHub repo — auto-deploys
git push origin main
```

### 5. Access
- **Survey page**: `https://your-app.up.railway.app/`
- **Admin panel**: `https://your-app.up.railway.app/admin`

---

## Local Development

```bash
# 1. Start PostgreSQL (Docker)
docker run -d --name survey-pg -e POSTGRES_DB=survey_db -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16

# 2. Set env vars
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/survey_db
export ANTHROPIC_API_KEY=sk-ant-...
export SECRET_KEY=dev-secret-key

# 3. Install and run
pip install -r requirements.txt
python main.py
```

Visit `http://localhost:8000` (survey) and `http://localhost:8000/admin` (admin).

---

## How It Works

1. **Admin creates a survey** with a topic, system prompt, and survey code
2. **Participants** go to the main page, enter the code, and start chatting
3. Claude conducts the survey based on the admin's system prompt
4. After reaching the message limit, the survey auto-completes
5. **Admin monitors** active participants in real-time, reads transcripts, and uses the analysis chatbot to extract insights
6. Admin can **close** the survey when done

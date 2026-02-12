# Survey Chatbot — Railway Deployable

An AI-powered survey chatbot with an admin dashboard. Participants join via a **survey code**, chat with a Claude-powered bot that gathers insights, and admins can monitor results in real-time.

## Features

### Survey Chatbot (`/`)
- Participants enter a survey code to join
- AI-driven conversational survey using Claude
- Auto-completes after configurable message limit
- Clean, mobile-friendly chat interface

### Admin Dashboard (`/admin`)
- **Auth**: Username/password login with JWT tokens
- **Create surveys**: Set title, topic, system prompt, and survey code
- **Live monitoring**: See active participants, completed count, and average completion time
- **Conversation viewer**: Read every participant's full chat transcript
- **Analysis chatbot**: AI-powered analysis of survey responses — ask questions about themes, sentiment, and insights
- **Survey controls**: Close/reopen surveys, edit settings

## Tech Stack
- **Backend**: FastAPI (Python)
- **Database**: PostgreSQL (Railway addon)
- **AI**: Anthropic Claude API
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
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `SECRET_KEY` | A random string for JWT signing (e.g. `openssl rand -hex 32`) |
| `DEFAULT_ADMIN_USER` | Initial admin username (default: `admin`) |
| `DEFAULT_ADMIN_PASS` | Initial admin password (default: `admin123`) |

> `DATABASE_URL` is auto-provided by Railway's PostgreSQL addon.

### 4. Deploy
```bash
# Option A: Railway CLI
cd survey_app
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
cd survey_app
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

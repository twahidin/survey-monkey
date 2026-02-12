# Deploy to Railway — Quick Runbook

## 1. Log in (do this first in your terminal)

```bash
cd /Users/joetay/Desktop/CTSS/CTSS_SURVEY_MONKEY/survey_app
railway login
```

A browser window will open; sign in with GitHub or email.

---

## 2. Create a new project (first time only)

```bash
railway init
```

- Choose **Create new project**
- Enter a project name (e.g. `ctss-survey`)

---

## 3. Add PostgreSQL

```bash
railway add --database postgres
```

Or in the [Railway dashboard](https://railway.app/dashboard): open your project → **+ New** → **Database** → **PostgreSQL**. Railway will set `DATABASE_URL` automatically.

---

## 4. Set environment variables

In the dashboard: your project → **Variables** → add:

| Variable | Value |
|----------|--------|
| `ANTHROPIC_API_KEY` | Your Anthropic API key (e.g. `sk-ant-...`) |
| `SECRET_KEY` | Random string for JWT (run: `openssl rand -hex 32`) |
| `DEFAULT_ADMIN_USER` | Admin username (e.g. `admin`) |
| `DEFAULT_ADMIN_PASS` | Admin password (e.g. a strong password) |

Or via CLI (from `survey_app`):

```bash
railway variables set ANTHROPIC_API_KEY=sk-ant-your-key
railway variables set SECRET_KEY=$(openssl rand -hex 32)
railway variables set DEFAULT_ADMIN_USER=admin
railway variables set DEFAULT_ADMIN_PASS=your-secure-password
```

---

## 5. Deploy

From the `survey_app` directory:

```bash
railway up
```

This builds from the Dockerfile and deploys. When it finishes, Railway will show a URL like `https://your-app.up.railway.app`.

---

## 6. One-off: link service to existing project

If you already have a Railway project and want to deploy this app into it:

```bash
railway link
```

Pick the project and (if asked) the environment, then run **Add PostgreSQL** and **Set variables** as above, and finally `railway up`.

---

## URLs after deploy

- **Survey (participants):** `https://your-app.up.railway.app/`
- **Admin dashboard:** `https://your-app.up.railway.app/admin`

Health check is at `/api/health` (used by Railway for restarts).

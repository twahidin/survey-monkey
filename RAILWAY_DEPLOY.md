# Deploy to Railway — Quick Runbook

## 1. Log in (do this first in your terminal)

```bash
cd /Users/joetay/Desktop/CTSS/CTSS_SURVEY_MONKEY
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

**Important:** The app service must have `DATABASE_URL` set. Add Postgres in the same project and connect it to your app.

- **Dashboard:** Open your project → **+ New** → **Database** → **PostgreSQL**. Then open your **app service** → **Variables** → ensure `DATABASE_URL` is present (Railway often injects it when both services are in the same project). If not, click **Add variable** → **Add reference** and select the Postgres service’s `DATABASE_URL`.
- **CLI:** From the app directory, run `railway add --database postgres` (or add Postgres from the dashboard and redeploy so the app picks up the variable).

---

## 4. Set environment variables

In the dashboard: your project → **Variables** → add:

| Variable | Value |
|----------|--------|
| `ANTHROPIC_API_KEY` | Your Anthropic API key (e.g. `sk-ant-...`) |
| `SECRET_KEY` | Random string for JWT (run: `openssl rand -hex 32`) |
| `DEFAULT_ADMIN_USER` | Admin username (e.g. `admin`) |
| `DEFAULT_ADMIN_PASS` | Admin password (e.g. a strong password) |

Or via CLI (from repo root):

```bash
railway variables set ANTHROPIC_API_KEY=sk-ant-your-key
railway variables set SECRET_KEY=$(openssl rand -hex 32)
railway variables set DEFAULT_ADMIN_USER=admin
railway variables set DEFAULT_ADMIN_PASS=your-secure-password
```

---

## 5. Deploy

From the repo root:

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

---

## Troubleshooting: "Connection refused" to localhost:5432

This means the app is using the default database URL (localhost) because **`DATABASE_URL` is not set** for your app service.

1. In the [Railway dashboard](https://railway.app/dashboard), open your **project**.
2. Ensure you have a **PostgreSQL** service (if not, add one: **+ New** → **Database** → **PostgreSQL**).
3. Open your **web/app service** (the one that runs the FastAPI app).
4. Go to **Variables**. You should see `DATABASE_URL` (often added automatically when Postgres is in the same project). If it’s missing:
   - Click **+ New variable** → **Add reference**.
   - Choose the **PostgreSQL** service and the variable **DATABASE_URL**.
5. **Redeploy** the app (e.g. **Deploy** → **Redeploy** or push a new commit) so it starts with `DATABASE_URL` set.

---

## Troubleshooting: Admin login doesn't work

1. **Check which username the app is using**  
   Open `https://your-app.up.railway.app/api/admin-info` in a browser. It returns `{"default_admin_username": "..."}`. Use that **exact** username (case-sensitive) on the login page.

2. **Match Railway variables**  
   In Railway → your app service → **Variables**, ensure `DEFAULT_ADMIN_USER` and `DEFAULT_ADMIN_PASS` have no extra spaces or quotes in the value.

3. **Redeploy after changing variables** so the app restarts and syncs the admin password from env.

4. **Check logs** (Railway → Deployments → View Logs) for `[Startup] Updated password for admin: '...'` to confirm the username being synced.

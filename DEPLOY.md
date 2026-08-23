# Deployment

Split architecture — each piece runs where it is actually reliable:

```
┌──────────────────┐   Bearer JWT    ┌─────────────────────────────┐
│ Vercel (static)  │ ──────────────► │ Render/Railway (Docker)     │
│ React build      │                 │ FastAPI + scheduler + agents│
│ frontend/        │                 │ backend/Dockerfile          │
└──────────────────┘                 └──────────────┬──────────────┘
                                                    │
                                     ┌──────────────▼──────────────┐
                                     │ MongoDB Atlas (replica set) │
                                     └─────────────────────────────┘
```

Why not all-on-Vercel: the reconciliation scheduler is a background asyncio
loop, agent tool-calls routinely run past serverless timeouts, and the audit
hash-chain expects one persistent writer pool per instance. The API therefore
runs as a long-lived container; only the static UI lives on Vercel.

Auth is Bearer-token based (localStorage + refresh rotation), so frontend and
backend may live on different domains without third-party cookie issues.

## 1. Backend (Render, ~5 min)

1. Push this repo to GitHub (CI must be green).
2. Render dashboard → **New → Blueprint** → select this repo. `render.yaml`
   provisions `recon-control-api` from `backend/Dockerfile`.
3. Fill the prompted secrets:
   - `MONGO_URL` — Atlas SRV URI (M0 free tier works; use a replica set so
     transactions work: any Atlas tier qualifies)
   - `CORS_ORIGINS` — your Vercel URL (step 2), comma-separated if several
   - `CUSTOM_LLM_API_KEY` — OpenRouter key for the agent layer (optional)
4. Note the service URL, e.g. `https://recon-control-api.onrender.com`.
   Verify: `curl https://<api>/api/health` → `{"status":"ok",...}`.

Railway alternative: New Project → Deploy from GitHub → it auto-detects
`backend/Dockerfile`; set the same env vars in the service settings.

## 2. Frontend (Vercel, ~3 min)

> Redo note: delete any OLD project that was wired to this repo with the
> root-level `vercel.json` / `api/index.py` serverless config — those
> deployments failed on every push and the files no longer exist.

1. Vercel → **Add New → Project** → import the repo.
2. **Root Directory: `frontend`** (framework auto-detects as Create React App).
3. Environment variable (Production + Preview):
   - `REACT_APP_BACKEND_URL = https://<your-api-host>` (no trailing slash)
4. Deploy. SPA routing is handled by `frontend/vercel.json`.

## 3. Post-deploy checklist

- [ ] `curl https://<api>/api/health` returns ok
- [ ] Login page loads on the Vercel URL; demo login works (seeded users:
      `analyst@recon.io / analyst123` … `support@recon.io / support123`)
- [ ] Browser network tab shows API calls going to the backend host with
      `Authorization: Bearer …`
- [ ] Run a demo batch; scheduler visible under Admin → Schedules ticks
- [ ] Agent console answers (only when `CUSTOM_LLM_API_KEY` is set)

## Environment matrix

| Var | Frontend (Vercel) | Backend (Render/Railway) |
|---|---|---|
| REACT_APP_BACKEND_URL | ✅ build-time | — |
| MONGO_URL | — | ✅ |
| DB_NAME | — | ✅ (`recon_control_tower`) |
| JWT_SECRET | — | ✅ (32+ bytes hex) |
| COOKIE_SECURE | — | ✅ `true` behind HTTPS |
| CORS_ORIGINS | — | ✅ Vercel URL(s) |
| CUSTOM_LLM_BASE_URL / _MODEL / _API_KEY | — | optional (agents) |

## Local parity

`scripts/run_local.sh` boots Mongo + API (:8000, `COOKIE_SECURE=false`) +
production UI bundle (:3001). Same code paths as production.

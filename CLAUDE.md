# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

TalkProd-Chatbot ("Obsidian") is an AI shopping storefront for the Indian market (Amazon.in/Flipkart). A Flask backend serves a built React/Vite SPA and exposes JSON APIs for three features:

1. **Concierge search** (`gemini_service.get_top_products`) — grounded Google Search via Gemini returns 6 products; recovers real retailer URLs from grounding metadata and resolves `vertexaisearch` redirects. Each result carries a canonical `model` field (brand + model + tier, identical across colour/storage variants) used for RAG dedup.
2. **Talk to your product** (`product_knowledge.py`) — RAG over scraped Reddit discussions; answers grounded only on Reddit opinions. Embeddings via Gemini, stored/searched in Postgres/pgvector.
3. **Price-watch agent** (`agent_service.py`) — a real tool-calling agent decides whether to email a shopper and when to re-check. Driven by **Celery Beat**, which fans out one agent task per due watch.

## Commands

### Backend (Python, from repo root)
```bash
pip install -r requirements.txt
flask db upgrade                           # apply migrations (creates tables + pgvector extension)
python backend.py                          # runs Flask dev server on :5000 (FLASK_DEBUG=1 by default)
python product_knowledge.py "Sony WH-1000XM5"   # standalone: ingest a product + test retrieval
python agent_service.py                    # standalone: run one price-watch agent turn against a fake watch
```

### Background workers (Celery — required for talk ingestion + price watches)
```bash
python -m celery -A celery_app.celery worker --loglevel=info --pool=solo   # --pool=solo on Windows
python -m celery -A celery_app.celery beat   --loglevel=info               # periodic watch checks
```
`/talk/prepare` and the price-watch scheduler enqueue Celery tasks (`tasks.py`). **Without a running worker, talk-ingestion jobs stay stuck in "preparing"** and watches never get checked. RabbitMQ must be up (broker); Redis must be up (rate limiting only).

There is no test suite. Modules self-test via their `if __name__ == '__main__'` blocks (see `product_knowledge.py`, `agent_service.py`), each wrapped in a `create_app().app_context()`. `storefront-ui/e2e-talk.mjs` is a Playwright end-to-end script for the talk flow.

### Frontend (from `storefront-ui/`)
```bash
npm install
npm run dev        # Vite dev server on :5173, proxies /search /talk /watch to :5000 (see vite.config.js)
npm run build      # builds to storefront-ui/dist/ — Flask serves THIS, so rebuild after UI changes
```
Flask serves the built SPA from `storefront-ui/dist`. For UI changes to appear in the Flask-served app (not the Vite dev server), you must `npm run build`.

### Docker (full stack)
```bash
docker compose up --build   # db (pgvector), redis, rabbitmq, web (waitress), worker, beat
```
`web` runs `flask db upgrade && waitress-serve ... wsgi:app`. The multi-stage `Dockerfile` builds the SPA (node:20-slim) then the Python image (python:3.12-slim); there is **no apt layer** (deb repos are blocked on the build network — healthcheck uses Python `urllib`, not curl). Postgres 18's volume mounts at `/var/lib/postgresql` (not `/data`); worker/beat disable the web healthcheck.

### Required environment (`.env`, gitignored)
- **Required**: `GEMINI_API_KEY`, `RAPIDAPI_KEY`, `DATABASE_URL`, `SMTP_USER`, `SMTP_APP_PASSWORD`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `SECRET_KEY`, `JWT_SECRET`.
- **Optional**: `GEMINI_MODEL` (default `gemini-3.1-flash-lite`), `GEMINI_EMBED_MODEL` (default `gemini-embedding-001`), `RABBITMQ_URL` (default `amqp://guest:guest@localhost:5672//`), `REDIS_URL` (default `redis://localhost:6379/0`), `CACHE_TTL_DAYS` (7), `WATCH_INTERVAL_MINUTES` (30), `JWT_TTL_HOURS`, `LOG_LEVEL`, `PORT`, `SMTP_HOST`, `SMTP_PORT`.

`DATABASE_URL` selects the DB (Postgres w/ pgvector in prod; local dev uses a Docker pgvector Postgres, e.g. `postgresql://postgres:postgres@localhost:5433/obsidian`). Falls back to `sqlite:///obsidian.db`, but SQLite **cannot run the RAG feature** (no vector type). Legacy `postgres://` URLs are rewritten to `postgresql://`. Missing SMTP creds disable email gracefully; missing API keys raise on first use.

## Architecture

### App factory + blueprints
`backend.create_app(config=None)` builds and wires the app — nothing is created at import time, so waitress/gunicorn and tests each construct their own app. `wsgi.py` (`wsgi:app`) is the production entrypoint. Shared extensions/helpers live in `app/extensions.py` (importable by blueprints without a circular import back to the factory). Feature routes are blueprints under `app/routes/`: `main` (`/`), `search` (`/search`), `talk` (`/talk/*`), `auth` (`/auth/*`), `watch` (`/watch/*`), `health` (`/healthz` liveness, `/health` readiness — Postgres `SELECT 1` + Redis ping, 200/503).

### Persistence — everything is in Postgres now (`models.py`)
Flat-file/in-memory state was replaced by SQLAlchemy models (Flask-Migrate/Alembic manage schema):
- `.watches.json` → `User`, `Watch`, `PriceHistory`
- `.quota.json` → `ApiQuota` (incremented atomically so counts aren't lost across the web + worker processes)
- in-memory `_jobs` dict → `IngestionJob` (talk-ingestion status, keyed by product slug; survives restarts and is shared across processes)
- ChromaDB collections → `Chunk` (pgvector)

### Concurrency model (now multi-process — read before changing request handling)
Unlike the original single-process design, state is **shared through Postgres**, so multiple web workers + separate Celery worker/beat processes are safe:
- **Ingestion job status** lives in `IngestionJob` (`_set_job`/`_get_job` in `app/extensions.py`), not process memory.
- **"Talk" ingestion** is a real Celery task (`tasks.ingest_task`), not a `threading.Thread`. `/talk/prepare` enqueues it and returns; the React UI polls `/talk/status` (client-side `setInterval` in `TalkDrawer.jsx`) until `ready`. The UI keys ingestion/retrieval on the canonical `model` (`productKey`), so colour/storage variants of the same phone don't re-scrape.
- **Price checks** run in Celery Beat (`celery_app.beat_schedule` → `tasks.check_due_watches`, which fans out with `FOR UPDATE SKIP LOCKED`), not an in-process scheduler thread. Tasks are retry-safe: `task_acks_late` + `task_reject_on_worker_lost`, with idempotent task bodies.
- **No Celery result backend**: tasks write outcomes to Postgres and nothing reads per-task-id results, so Redis is used **only** for rate limiting (`Flask-Limiter`, `swallow_errors=True` → fail-open if Redis is down).

### RAG pipeline (`product_knowledge.py`)
Scrape → chunk → embed → store → cosine-retrieve:
- **Scrape** via reddit3 RapidAPI (free plan ~100 req/month, tracked in the `ApiQuota` row, hard-stops at 429). Comments fetched only for the 8 most-discussed posts. `FETCH_WORKERS=8`, `REQUEST_TIMEOUT=15` (tuned for speed).
- **Chunk** is rule-based: one chunk per post body + up to 5 top comments per commented post, junk-filtered, clipped to 1500 chars. ~40–70 chunks/product.
- **Embed** with Gemini `gemini-embedding-001`, truncated to 768 dims via `output_dimensionality` (Matryoshka); `task_type` RETRIEVAL_DOCUMENT for chunks, RETRIEVAL_QUERY for queries (`gemini_service.embed_documents` / `embed_query`, batched, with retry).
- **Store** as `Chunk` rows scoped by `_product_slug(product)` (a regex canonicalizer: strips storage/RAM/network/variant noise → `talk-<slug>`), stamped with `ingested_at`. `ingest()` deletes old chunks then bulk-inserts.
- **Reuse over re-scrape**: `_cache_status()` returns fresh/stale/missing from chunk count + max `ingested_at` vs `CACHE_TTL_DAYS`; empty = missing (self-healing).
- **Retrieve**: `Chunk.query.filter(product_slug=...).order_by(embedding.cosine_distance(qvec)).limit(top_k)`, dynamic top-k = `TOP_K_FRACTION` (0.35) of the collection, floored at `MIN_TOP_K` (5).

### Price-watch agent (`agent_service.py`)
A **LangChain `create_agent`** (LangGraph-backed ReAct loop) over `ChatGoogleGenerativeAI`. Given the `_AGENT_BRIEF` goal and 4 `@tool` closures over a per-run `state` dict: `get_current_price_tool`, `find_alternatives`, `notify_user`, `set_next_check_hours`. The model only *requests* tools; LangGraph dispatches to the Python fns and feeds results back. Loop bounded by `RECURSION_LIMIT = 2 * MAX_STEPS + 2`. Grounded price/alternative lookups live in `gemini_service` and are only *called* by the tools. **Note: prices are LLM-approximated (grounded Search), not a real price feed — unreliable.**

### Auth (`auth_service.py`)
Google OAuth (Authlib) → stateless JWT (HS256) in an httpOnly, SameSite=Lax cookie; `COOKIE_SECURE` on when `FLASK_DEBUG != 1`. `require_auth` decorator + `current_user()` gate the watch routes and key the rate limiter (`_rate_key`: user id if signed in, else client IP).

### Service boundaries
- `email_service.py` is standalone (stdlib Gmail SMTP over SSL) so `agent_service` and `watch_service` can send mail without a circular import; `send_email` never raises (returns False).
- `gemini_service.py` owns all grounded Google-Search calls, URL recovery, and the embedding functions; both `backend` and `agent_service` depend on it.
- `celery_app.py` is lightweight and side-effect-free to import (no Flask app / db.init); the Flask app context tasks run in is built lazily in `tasks.py`.

### Model conventions
When building AI features here, default to the latest capable models. The Gemini chat model is `MODEL` in `gemini_service.py`; the embedding model is `EMBED_MODEL` (both env-overridable). See the memory files under `.claude/` for project context and known gaps.

<div align="center">

# 🖤 Obsidian

**An AI shopping storefront for the Indian market — search grounded in Google, answers grounded in Reddit, and an agent that watches prices while you sleep.**

Flask · React + Vite · Postgres/pgvector · Celery · Gemini · LangChain

</div>

---

## What it is

Obsidian (repo: `TalkProd-Chatbot`) is a full-stack shopping assistant for Amazon.in / Flipkart shoppers. It is built around three ideas that most "AI storefronts" skip:

| | Feature | The idea |
|---|---|---|
| 🔎 | **Concierge search** | Describe what you want in plain English. Gemini answers with **grounded Google Search**, and real retailer URLs are recovered from the grounding metadata — not hallucinated links. |
| 💬 | **Talk to your product** | Ask "is the battery actually good?" and get an answer built **only** from scraped Reddit discussion, with the source threads cited. Retrieval-augmented, never free-styled. |
| 🔔 | **Price-watch agent** | A real tool-calling ReAct agent — not a cron job with an `if price < target` — decides *whether* to email you and *when* to look again. |

> **Honest caveat:** prices come from an LLM grounded in Google Search, not a retailer price feed. Treat them as approximate.

---

## Architecture at a glance

```
                       ┌──────────────────────────┐
   Browser  ──────────▶│  Flask (app factory)     │
   React/Vite SPA      │  blueprints:             │
   served from dist/   │  / /search /talk /watch  │
                       │  /auth /health           │
                       └───────┬──────────┬───────┘
                               │          │
                 enqueue tasks │          │ read/write
                               ▼          ▼
                        ┌───────────┐  ┌──────────────────────┐
                        │ RabbitMQ  │  │ Postgres + pgvector  │
                        └─────┬─────┘  │ users · watches      │
                              │        │ price_history        │
             ┌────────────────┴──┐     │ ingestion_jobs       │
             ▼                   ▼     │ chunks (vectors)     │
      ┌─────────────┐     ┌──────────┐ │ api_quota            │
      │ Celery      │     │ Celery   │ └──────────────────────┘
      │ worker      │◀────│ Beat     │
      │ ingest +    │ fan │ every N  │      ┌─────────┐
      │ watch agent │ out │ minutes  │      │  Redis  │  rate limits only
      └──────┬──────┘     └──────────┘      └─────────┘
             │
             ├──▶ Gemini (chat · grounded search · embeddings)
             ├──▶ reddit3 RapidAPI (discussion scrape)
             └──▶ Gmail SMTP (price-drop email)
```

**Everything shared lives in Postgres.** There is no in-process state, no flat-file JSON and no in-memory job dict — so multiple web workers, a Celery worker and Beat can all run at once without stepping on each other.

<details>
<summary><b>How it got here (the interesting refactor)</b></summary>

| Was | Now | Why |
|---|---|---|
| `.watches.json` on disk | `User` / `Watch` / `PriceHistory` tables | multi-process safe, queryable, per-user scoped |
| `.quota.json` | `ApiQuota` row, incremented atomically | web + worker were silently losing counts |
| in-memory `_jobs` dict | `IngestionJob` table | survives restarts, visible to every process |
| ChromaDB | `Chunk` with a pgvector column | one datastore, real SQL, cosine search in the DB |
| `threading.Thread` ingestion | Celery task | requests return instantly; work survives a restart |
| in-process scheduler thread | Celery Beat + `FOR UPDATE SKIP LOCKED` | no duplicate agent runs when scaled out |

Tasks are retry-safe (`task_acks_late` + `task_reject_on_worker_lost`, idempotent bodies). There is **no Celery result backend** — tasks write outcomes to Postgres, so Redis is needed only for rate limiting.

</details>

---

## Under the hood

### 🔎 Concierge search — `gemini_service.py`
Gemini with the Google Search tool returns 6 products. The service digs real retailer URLs out of the grounding metadata and resolves `vertexaisearch` redirects to their destinations. Every result carries a canonical **`model`** field (brand + model + tier, identical across colour and storage variants) — the key that stops a "Midnight 256 GB" and a "Starlight 512 GB" from being scraped and embedded twice.

### 💬 RAG pipeline — `product_knowledge.py`
```
scrape ──▶ chunk ──▶ embed ──▶ store ──▶ cosine-retrieve
```
- **Scrape** — reddit3 RapidAPI, 8 parallel workers, comments pulled only for the 8 most-discussed posts. The free plan is ~100 requests/month, tracked in `api_quota` and hard-stopped at 429.
- **Chunk** — rule-based: one chunk per post body, up to 5 top comments per commented post, junk-filtered, clipped to 1500 chars. Roughly 40–70 chunks per product.
- **Embed** — `gemini-embedding-001` truncated to **768 dims** via Matryoshka `output_dimensionality`; `RETRIEVAL_DOCUMENT` for chunks, `RETRIEVAL_QUERY` for questions.
- **Store** — `Chunk` rows keyed by a canonicalized slug (storage / RAM / network / variant noise stripped), stamped with `ingested_at`.
- **Reuse over re-scrape** — freshness comes from chunk count plus newest `ingested_at` versus `CACHE_TTL_DAYS`; an empty collection reads as *missing*, so the cache self-heals.
- **Retrieve** — `ORDER BY embedding <=> query_vec`, dynamic top-k of 35 % of the collection, floored at 5.

### 🔔 Price-watch agent — `agent_service.py`
A LangChain `create_agent` (LangGraph ReAct loop) over `ChatGoogleGenerativeAI`, given a goal brief and four tools closed over a per-run state dict:

| Tool | What the model can do with it |
|---|---|
| `get_current_price_tool` | look up today's grounded price |
| `find_alternatives` | search for a better-value substitute |
| `notify_user` | send the email — its call, not a threshold's |
| `set_next_check_hours` | decide its own next wake-up |

The model only *requests* tools; LangGraph dispatches to the Python functions and feeds the results back. The loop is bounded by `RECURSION_LIMIT = 2 * MAX_STEPS + 2`.

### 🔐 Auth — `auth_service.py`
Google OAuth (Authlib) → a stateless **HS256 JWT** in an httpOnly, `SameSite=Lax` cookie, `Secure` outside debug. `require_auth` gates every feature route and `current_user()` supplies the identity, so a caller can never pass someone else's email to touch their watches. The rate limiter keys on user id when signed in and client IP otherwise, and **fails open** if Redis is down.

---

## Quick start

### Option A — Docker (the whole stack)

```bash
docker compose up --build
```
Brings up `db` (pgvector), `redis`, `rabbitmq`, `web` (waitress), `worker` and `beat`. The web container runs `flask db upgrade` before serving. App on **http://localhost:5000**, RabbitMQ UI on **:15672**. Secrets come from `.env`; the compose file overrides the connection URLs so containers reach each other by service name.

### Option B — local dev

```bash
# 1. backend
pip install -r requirements.txt
flask db upgrade                 # creates tables + the pgvector extension
python backend.py                # http://localhost:5000

# 2. workers (own terminals) — required for talk ingestion + price watches
python -m celery -A celery_app.celery worker --loglevel=info --pool=solo   # --pool=solo on Windows
python -m celery -A celery_app.celery beat   --loglevel=info

# 3. frontend
cd storefront-ui
npm install
npm run dev        # http://localhost:5173, proxies /search /talk /watch to :5000
npm run build      # Flask serves storefront-ui/dist — rebuild after UI changes
```

> ⚠️ **No worker, no talk.** `/talk/prepare` and the price-watch scheduler both enqueue Celery tasks. Without a running worker, ingestion jobs sit in `preparing` forever and watches are never checked. RabbitMQ must be up (broker); Redis is needed only for rate limiting.

### Environment

Create a `.env` in the repo root (gitignored).

**Required**
```
GEMINI_API_KEY=       RAPIDAPI_KEY=          DATABASE_URL=
SMTP_USER=            SMTP_APP_PASSWORD=
GOOGLE_CLIENT_ID=     GOOGLE_CLIENT_SECRET=
SECRET_KEY=           JWT_SECRET=
```

**Optional** — `GEMINI_MODEL` (default `gemini-3.1-flash-lite`), `GEMINI_EMBED_MODEL` (default `gemini-embedding-001`), `RABBITMQ_URL`, `REDIS_URL`, `CACHE_TTL_DAYS` (7), `WATCH_INTERVAL_MINUTES` (30), `JWT_TTL_HOURS`, `LOG_LEVEL`, `PORT`, `SMTP_HOST`, `SMTP_PORT`.

`DATABASE_URL` picks the database — Postgres with pgvector in every real setup (locally, e.g. `postgresql://postgres:postgres@localhost:5433/obsidian`). It falls back to SQLite, but **SQLite cannot run the RAG feature** — there is no vector type. Legacy `postgres://` URLs are rewritten to `postgresql://`. Missing SMTP credentials disable email gracefully; missing API keys raise on first use.

---

## API

Every feature route requires the auth cookie.

| Method | Route | Body / query | Does |
|---|---|---|---|
| `GET` | `/auth/login` → `/auth/callback` | — | Google OAuth, sets the JWT cookie |
| `POST` `GET` | `/auth/logout` · `/auth/me` | — | clear session · current user |
| `POST` | `/search` | `{description}` | 6 grounded products · *20/hr* |
| `POST` | `/talk/prepare` | `{product}` | enqueue Reddit ingestion · *10/hr* |
| `GET` | `/talk/status` | `?product=` | `preparing \| ready \| error` + quota |
| `GET` | `/talk/quota` | — | RapidAPI budget left, for the UI meter |
| `POST` | `/talk/ask` | `{product, question, history}` | grounded answer + source threads |
| `POST` | `/watch/enable` | `{product, url, baseline_price, target_price}` | start watching · *30/hr* |
| `POST` | `/watch/disable` | `{id}` or `{product}` | stop (scoped to the caller) |
| `GET` | `/watch/list` | — | the caller's watches only |
| `POST` | `/watch/run-now` | `{id?}` | enqueue an agent run now (testing aid) |
| `GET` | `/healthz` · `/health` | — | liveness · readiness (Postgres + Redis, 200/503) |

---

## Layout

```
backend.py            create_app() factory — nothing built at import time
wsgi.py               production entrypoint (wsgi:app)
app/
  extensions.py       shared db / limiter / job helpers (no circular import)
  routes/             main · search · talk · auth · watch · health
gemini_service.py     grounded search, URL recovery, embeddings
product_knowledge.py  the RAG pipeline
agent_service.py      the LangChain price-watch agent
watch_service.py      watch CRUD
auth_service.py       OAuth + JWT + require_auth
email_service.py      standalone SMTP (never raises)
celery_app.py         Celery app + Beat schedule (side-effect-free import)
tasks.py              ingest_task · check_due_watches · run_watch_agent_task
models.py             SQLAlchemy models
migrations/           Alembic
storefront-ui/        React + Vite SPA (built into dist/, served by Flask)
```

### Testing

There is no test suite. Modules self-test through their `__main__` blocks, each wrapped in a `create_app().app_context()`:

```bash
python product_knowledge.py "Sony WH-1000XM5"   # ingest a product + test retrieval
python agent_service.py                         # one agent turn against a fake watch
node storefront-ui/e2e-talk.mjs                 # Playwright end-to-end for the talk flow
```

---

<div align="center">
<sub>Built by <a href="https://github.com/shubhchaudhury10">@shubhchaudhury10</a> · Gemini · LangChain · Postgres/pgvector · Celery</sub>
</div>

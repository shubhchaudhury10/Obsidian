"""Reddit-backed product knowledge for the "Talk to your product" feature.

Pipeline:
    1. Scrape: top 30 Reddit posts about the product via Reddit's public JSON API.
    2. Chunk:  rule-based — one chunk per post body + one per top comment (5/post).
    3. Store:  embed each chunk with Gemini gemini-embedding-001 (768-dim) and store the
               vectors in Postgres via pgvector (models.Chunk), scoped by product_slug.
    4. Retrieve: embed the user's question and cosine-search the product's chunks.

Reuse over re-scrape: before scraping, we check whether this product's chunks are already
in the DB and still fresh (within CACHE_TTL_DAYS). If so we reuse them and spend zero
RapidAPI requests; only missing or stale products are scraped. DB access needs a Flask app
context (request handlers and Celery tasks already have one).
Run standalone to test:  python product_knowledge.py "Samsung Galaxy S26 Ultra"
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import requests
from dotenv import load_dotenv
from sqlalchemy import func

from models import db, Chunk
from gemini_service import embed_documents, embed_query

load_dotenv()

POSTS_TO_FETCH = 30
COMMENTS_PER_POST = 5
# Fetching comments is one API call per post, so only pull them for the most-discussed
# posts (by comment count) to keep us well within RapidAPI's request quota. The other
# posts still contribute their title + body as chunks.
POSTS_WITH_COMMENTS = 8
# How many chunks to hand the LLM per question — computed dynamically from the size of
# the product's collection so a wide net is cast over small corpora without dumping the
# whole thing. retrieve() takes TOP_K_FRACTION of the stored chunks, floored at MIN_TOP_K
# (so sparse products still get usable context) and capped at the collection size.
TOP_K_FRACTION = 0.35  # share of a product's stored chunks to retrieve per question
MIN_TOP_K = 5          # ...but never fewer than this, even for tiny corpora

# Reuse previously-scraped Reddit knowledge for this many days before re-scraping. Reddit
# opinions drift over time, so we refresh a product after the TTL rather than caching it
# forever. Override with CACHE_TTL_DAYS in .env.
CACHE_TTL_DAYS = float(os.environ.get('CACHE_TTL_DAYS', '7'))

# Reddit blocks anonymous JSON access and gates its official API behind a builder
# registration, so we source posts through the reddit3 API (SteadyAPI) on RapidAPI.
# Set the RapidAPI key in .env.
RAPIDAPI_KEY = os.environ.get('RAPIDAPI_KEY')
RAPIDAPI_HOST = 'reddit3.p.rapidapi.com'

REQUEST_TIMEOUT = 15   # per-call cap; a slow thread fails fast (fetch returns [] on error)
# One parallel wave for all commented posts (== POSTS_WITH_COMMENTS) instead of two half
# waves, so the comment phase takes ~one call's time, not two. Same number of API calls.
FETCH_WORKERS = 8

# Latest RapidAPI quota seen on a response, so the UI can warn before exhaustion. The free
# tier is small (100/month). We persist the last-known value to the ApiQuota table so the
# meter shows immediately after a restart (it self-corrects on the next real API call).
# DB access needs a Flask app context; init_product_knowledge(app) wires it in. Run
# standalone (no app), quota stays in-memory only. The DB stores calls_used/calls_limit;
# RapidAPI reports remaining, so remaining = calls_limit - calls_used.
_QUOTA_PROVIDER = 'rapidapi_reddit'
_quota = {'remaining': None, 'limit': None}
_app = None


def init_product_knowledge(app):
    """Give this module the Flask app so quota persists to the DB. Call once at startup."""
    global _app
    _app = app
    _load_quota_from_db()


def _load_quota_from_db():
    if _app is None:
        return
    try:
        from models import ApiQuota
        with _app.app_context():
            row = ApiQuota.query.filter_by(provider=_QUOTA_PROVIDER).first()
            if row and row.calls_limit is not None:
                _quota['limit'] = row.calls_limit
                _quota['remaining'] = row.calls_limit - (row.calls_used or 0)
    except Exception:
        pass


def _save_quota():
    if _app is None:
        return
    try:
        from models import db, ApiQuota
        with _app.app_context():
            row = db.session.get(ApiQuota, _QUOTA_PROVIDER)
            if row is None:
                row = ApiQuota(provider=_QUOTA_PROVIDER)
                db.session.add(row)
            lim, rem = _quota.get('limit'), _quota.get('remaining')
            if lim is not None:
                row.calls_limit = lim
                if rem is not None:
                    row.calls_used = max(0, lim - rem)
            row.window_start = datetime.utcnow()
            db.session.commit()
    except Exception:
        pass


def get_quota():
    """Most recent {remaining, limit} from RapidAPI's rate-limit headers.

    Reads the shared ApiQuota row, not this process's in-memory copy: scraping runs in the
    Celery worker but the UI asks the web process, so the DB is the only place both see. In
    a request handler there's already an app context; outside one (standalone) we fall back
    to the in-memory value.
    """
    try:
        from models import ApiQuota
        row = ApiQuota.query.filter_by(provider=_QUOTA_PROVIDER).first()
        if row and row.calls_limit is not None:
            return {'limit': row.calls_limit, 'remaining': row.calls_limit - (row.calls_used or 0)}
    except Exception:
        pass
    return dict(_quota)


def _rapid_get(path, params):
    """GET against the reddit3 (SteadyAPI) RapidAPI host.

    Responses are shaped {meta: {status, cursor, ...}, body: ...}. Returns the
    parsed JSON; callers pull what they need out of `body`. Records remaining quota.
    """
    if not RAPIDAPI_KEY:
        raise RuntimeError(
            'RAPIDAPI_KEY is not set. Subscribe to the reddit3 API on RapidAPI and '
            'add your key to the .env file as RAPIDAPI_KEY=...'
        )
    resp = requests.get(
        f'https://{RAPIDAPI_HOST}{path}',
        params=params,
        headers={'x-rapidapi-host': RAPIDAPI_HOST, 'x-rapidapi-key': RAPIDAPI_KEY},
        timeout=REQUEST_TIMEOUT,
    )

    # Capture quota from headers regardless of status (429s carry them too).
    rem = resp.headers.get('x-ratelimit-requests-remaining')
    lim = resp.headers.get('x-ratelimit-requests-limit')
    if rem is not None:
        _quota['remaining'] = int(rem)
    if lim is not None:
        _quota['limit'] = int(lim)
    if rem is not None or lim is not None:
        _save_quota()

    if resp.status_code == 429:
        raise RuntimeError(
            'RapidAPI monthly request quota exhausted for the reddit3 free plan. '
            'Wait for the monthly reset or upgrade the plan.'
        )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------- scraping

def _clean_query(product_name):
    """Strip spec noise so the Reddit search matches real discussion.

    Product names from the catalog carry parentheticals and storage/color variants
    ("Galaxy S24 Ultra (Titanium Gray, 12GB RAM, 256GB Storage)") that make the search
    far too narrow. We keep the core brand + model that people actually post about.
    """
    name = re.sub(r'\([^)]*\)', '', product_name)   # drop "(Titanium Gray, 256GB...)"
    name = name.split(',')[0]                         # drop trailing ", 256GB Storage"
    name = re.sub(r'\b\d+\s?(GB|TB)\b', '', name, flags=re.I)  # drop "256GB", "12 GB"
    return re.sub(r'\s+', ' ', name).strip() or product_name


def _search_posts(product_name):
    """Return up to POSTS_TO_FETCH post records for the product, best-first.

    One search call returns ~25 posts (plenty), so we don't paginate — every call
    counts against a small monthly quota. Each record is a standard Reddit post dict.
    """
    payload = _rapid_get('/v1/reddit/search', {
        'search': _clean_query(product_name),
        'filter': 'posts',
        'timeFilter': 'all',
        'sortType': 'relevance',
    })
    posts = payload.get('body', []) or []
    return posts[:POSTS_TO_FETCH]


def _fetch_comments(post_url):
    """Top comments for one post. Returns [] on any failure — a missing thread
    shouldn't sink the whole ingestion."""
    try:
        payload = _rapid_get('/v1/reddit/post', {'url': post_url})
        body = payload.get('body', {}) or {}
        return body.get('post_comments', []) or []
    except Exception:
        return []


# ---------------------------------------------------------------- chunking

_EMOJI_ONLY = re.compile(r'^[\W_]+$')

def _is_junk(text, author=''):
    """Rule-based filter for comments/bodies that carry no product signal."""
    if not text:
        return True
    text = text.strip()
    if text in ('[deleted]', '[removed]'):
        return True
    if author in ('AutoModerator', '[deleted]'):
        return True
    if len(text.split()) < 10:           # "this 👆", "lol same", links-only
        return True
    if _EMOJI_ONLY.match(text):
        return True
    return False


def _clip(text, limit=1500):
    """Cap chunk length so one rambling essay doesn't dominate retrieval."""
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:limit]


def _post_url(post):
    """Full reddit.com URL for a post, from its `url` or `permalink` field."""
    if post.get('url'):
        return post['url']
    return f"https://www.reddit.com{post.get('permalink', '')}"


def build_chunks(product_name, on_progress=None):
    """Scrape Reddit and return a list of {text, metadata} chunks."""
    notify = on_progress or (lambda *_: None)

    notify('searching', 'Searching Reddit discussions...')
    posts = _search_posts(product_name)

    # Only the most-discussed posts get a comment fetch (one API call each); the rest
    # contribute their title + body. Keeps us within the request quota.
    ranked = sorted(posts, key=lambda p: p.get('num_comments', 0), reverse=True)
    with_comments = set(id(p) for p in ranked[:POSTS_WITH_COMMENTS])

    notify('reading', f'Reading {len(posts)} Reddit threads...')
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        comment_trees = list(pool.map(
            lambda p: _fetch_comments(_post_url(p)) if id(p) in with_comments else [],
            posts,
        ))

    chunks = []
    for post, comments in zip(posts, comment_trees):
        url = _post_url(post)
        title = post.get('title', '')

        # The post itself, when it has a real body (reviews, experience threads).
        body = post.get('selftext', '')
        if not _is_junk(body, post.get('author', '')):
            chunks.append({
                'text': _clip(f"{title}. {body}"),
                'metadata': {'source': url, 'type': 'post', 'score': post.get('score', 0)},
            })

        kept = 0
        for c in comments:
            if kept >= COMMENTS_PER_POST:
                break
            # reddit3 puts comment text in `content` (not `body`).
            text = c.get('content') or c.get('body') or ''
            if _is_junk(text, c.get('author', '')):
                continue
            # Prefix the thread title so a bare "battery is great" comment still
            # embeds with its product context.
            chunks.append({
                'text': _clip(f"[{title}] {text}"),
                'metadata': {'source': url, 'type': 'comment', 'score': c.get('score', 0)},
            })
            kept += 1

    return chunks


# ---------------------------------------------------------------- vector store

def _product_slug(product_name):
    """Canonical per-product cache key that groups a product's chunk rows.

    Collapses storage/RAM/color/connectivity variants of the SAME phone to one slug, so
    the concierge search returning the name differently each time doesn't cause redundant
    re-scrapes. e.g. "Galaxy S25 FE 5G (8GB RAM, 128GB Storage)", "Galaxy S25 FE (128 GB,
    Navy)" and "Galaxy S25 FE" all map to talk-samsung-galaxy-s25-fe — one scrape, reused
    across users. "S25 FE" vs "S24 FE" vs "S25 Ultra" stay distinct (model markers kept).
    """
    n = product_name.lower()
    n = n.replace('+', ' plus ')                       # keep S25+ distinct from S25
    n = re.sub(r'\([^)]*\)', ' ', n)                   # drop "(8GB RAM, 128GB Storage)"
    n = n.split(',')[0]                                 # drop trailing ", Navy"
    n = re.sub(r'\b\d+\s?(gb|tb)\b', ' ', n)            # storage sizes
    n = re.sub(r'\b(4g|5g|lte|wifi)\b', ' ', n)         # connectivity
    n = re.sub(r'\b(smartphone|mobile|phone|with)\b', ' ', n)  # filler words
    slug = re.sub(r'[^a-z0-9]+', '-', n).strip('-')[:50]
    return f'talk-{slug}' if slug else 'talk-product'


def _chunk_stats(slug):
    """(count, latest ingested_at) for a product's chunks, in one query."""
    return (
        db.session.query(func.count(Chunk.id), func.max(Chunk.ingested_at))
        .filter(Chunk.product_slug == slug)
        .one()
    )


def _cache_status(product_name):
    """Whether the DB already holds usable knowledge for this product.

    Returns one of:
      'fresh'   - present and within CACHE_TTL_DAYS  -> reuse it, no scrape needed.
      'stale'   - present but older than the TTL      -> re-scrape to refresh.
      'missing' - never ingested (or empty)           -> scrape for the first time.
    """
    count, latest = _chunk_stats(_product_slug(product_name))
    if not count or latest is None:
        return 'missing'
    age_days = (datetime.utcnow() - latest).total_seconds() / 86400
    return 'fresh' if age_days <= CACHE_TTL_DAYS else 'stale'


def ingest(product_name, on_progress=None, force=False):
    """Full pipeline: scrape -> chunk -> embed -> store. Returns chunk count.

    If the product is already in the DB and still fresh (within CACHE_TTL_DAYS), we reuse
    it and skip scraping entirely — this is what saves RapidAPI requests. Pass force=True
    to re-scrape regardless of what's cached. Needs a Flask app context (DB access).
    """
    notify = on_progress or (lambda *_: None)
    slug = _product_slug(product_name)

    if not force and _cache_status(product_name) == 'fresh':
        count, _ = _chunk_stats(slug)
        notify('cached', f'Found {count} saved Reddit opinions — no new scrape needed.')
        return count

    chunks = build_chunks(product_name, on_progress)
    if not chunks:
        raise RuntimeError(
            f'No usable Reddit discussions found for "{product_name}". '
            'Try a more common product name.'
        )

    notify('embedding', f'Organizing {len(chunks)} opinions...')
    vectors = embed_documents([c['text'] for c in chunks])

    # Replace any prior (missing/stale) chunks with the freshly-scraped set, stamped with
    # the ingest time so _cache_status can later judge freshness.
    now = datetime.utcnow()
    Chunk.query.filter(Chunk.product_slug == slug).delete(synchronize_session=False)
    db.session.bulk_save_objects([
        Chunk(
            product_slug=slug,
            text=c['text'],
            source=c['metadata'].get('source', ''),
            score=c['metadata'].get('score', 0),
            embedding=vec,
            ingested_at=now,
        )
        for c, vec in zip(chunks, vectors)
    ])
    db.session.commit()
    return len(chunks)


def retrieve(product_name, question):
    """Return the chunks most relevant to the question: [{text, source, score}, ...].
    Raises if the product was never ingested. Needs a Flask app context (DB access).

    How many chunks: TOP_K_FRACTION of the product's stored chunks, floored at MIN_TOP_K
    and capped at the total. Similarity is cosine distance (pgvector `<=>`).
    """
    slug = _product_slug(product_name)
    total, _ = _chunk_stats(slug)
    if not total:
        raise RuntimeError(f'"{product_name}" has not been ingested yet.')

    top_k = round(total * TOP_K_FRACTION)   # 35% of the product's chunks
    top_k = max(MIN_TOP_K, top_k)           # floor: never fewer than MIN_TOP_K
    top_k = min(top_k, total)               # cap: never more than exist

    qvec = embed_query(question)
    rows = (
        Chunk.query
        .filter(Chunk.product_slug == slug)
        .order_by(Chunk.embedding.cosine_distance(qvec))
        .limit(top_k)
        .all()
    )
    return [{'text': r.text, 'source': r.source or '', 'score': r.score or 0} for r in rows]


# ---------------------------------------------------------------- CLI test

if __name__ == '__main__':
    # DB access needs a Flask app context; build the app and push one for the self-test.
    from backend import create_app

    product = ' '.join(sys.argv[1:]) or 'Samsung Galaxy S24 Ultra'
    with create_app().app_context():
        started = time.time()
        count = ingest(product, on_progress=lambda stage, msg: print(f'[{stage}] {msg}'))
        print(f'\nIngested {count} chunks in {time.time() - started:.1f}s\n')

        for q in ('how is the battery life?', 'is the camera good in low light?'):
            print(f'Q: {q}')
            for hit in retrieve(product, q):
                print(f'  - ({hit["score"]} pts) {hit["text"][:140]}...')
            print()

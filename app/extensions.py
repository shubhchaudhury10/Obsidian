"""Shared extensions and helpers used across blueprints.

These live here (not in backend.py) so blueprint modules can import them without a
circular import back to the app factory. Extensions are created UNBOUND — create_app()
calls their .init_app(app) to attach them to a specific app.
"""

import os

from flask_migrate import Migrate
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from models import db, IngestionJob
from auth_service import current_user

# Path to the built React storefront (served by Flask). app/ is one level below the
# project root, so go up one directory to find storefront-ui/dist.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST_DIR = os.path.join(_PROJECT_ROOT, 'storefront-ui', 'dist')

# --- Extensions (unbound; bound to an app in create_app) -----------------------
migrate = Migrate()


def _rate_key():
    """Rate-limit bucket: the logged-in user (stable across IPs), else the client IP."""
    user = current_user()
    return f"user:{user['sub']}" if user and user.get('sub') else get_remote_address()


REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
# Counters live in Redis (REDIS_URL) — a real Redis is required; no in-memory fallback.
limiter = Limiter(key_func=_rate_key, storage_uri=REDIS_URL, strategy='fixed-window')


# --- "Talk to your product" ingestion job helpers ------------------------------
# Job status lives in the IngestionJob table (keyed by product name), so it survives
# restarts and is shareable across processes. Called from request handlers, which
# already have an app context, so db.session is usable directly.

def _set_job(product, **fields):
    """Upsert the ingestion job for a product. `chunks` maps to the chunk_count column."""
    job = (IngestionJob.query.filter_by(product_slug=product)
           .order_by(IngestionJob.id.desc()).first())
    if job is None:
        job = IngestionJob(product_slug=product, status=fields.get('status', 'preparing'))
        db.session.add(job)
    if 'chunks' in fields:
        job.chunk_count = fields.pop('chunks')
    for key, val in fields.items():
        if hasattr(job, key):
            setattr(job, key, val)
    db.session.commit()


def _get_job(product):
    """Latest ingestion job for a product as a dict, or None."""
    job = (IngestionJob.query.filter_by(product_slug=product)
           .order_by(IngestionJob.id.desc()).first())
    if job is None:
        return None
    return {
        'status': job.status,
        'stage': job.stage,
        'message': job.message,
        'chunks': job.chunk_count or 0,
    }

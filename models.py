"""SQLAlchemy models — the persistent data layer.

Replaces the app's flat-file / in-memory state:
  - .watches.json        -> Watch (+ User, PriceHistory)
  - .quota.json          -> ApiQuota
  - backend._jobs (dict) -> IngestionJob

One SQLAlchemy `db` instance, initialised on the Flask app in backend.py. Schema changes
are managed with Flask-Migrate (Alembic). The target database is chosen by DATABASE_URL
(Postgres in prod; SQLite by default for local dev) — the models are DB-agnostic.
"""

import datetime

from flask_sqlalchemy import SQLAlchemy
from pgvector.sqlalchemy import Vector

db = SQLAlchemy()

# Dimension of the Gemini embedding vectors we store — gemini-embedding-001 truncated to
# 768 via output_dimensionality (see gemini_service).
EMBED_DIM = 768


def _utcnow():
    return datetime.datetime.utcnow()


class User(db.Model):
    """A signed-in user (via Google OAuth). Identity comes from Google's stable `sub`."""
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    google_sub = db.Column(db.String(64), unique=True, nullable=False)   # Google's stable user id
    email = db.Column(db.String(255), unique=True, nullable=False)
    name = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

    watches = db.relationship('Watch', back_populates='user', cascade='all, delete-orphan')


class Watch(db.Model):
    """A price watch a user set on a product. Replaces a record in .watches.json."""
    __tablename__ = 'watches'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True
    )
    product = db.Column(db.String(500), nullable=False)
    product_url = db.Column(db.String(1000))
    baseline_price = db.Column(db.Numeric(10, 2))
    target_price = db.Column(db.Numeric(10, 2))
    last_price = db.Column(db.Numeric(10, 2))
    last_price_text = db.Column(db.String(120))
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    notified = db.Column(db.Boolean, default=False, nullable=False)
    next_check_at = db.Column(db.DateTime, index=True)   # the scheduler's "due" query
    last_checked = db.Column(db.DateTime)
    last_decision = db.Column(db.Text)                    # agent's one-line summary
    last_error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

    user = db.relationship('User', back_populates='watches')
    prices = db.relationship('PriceHistory', back_populates='watch', cascade='all, delete-orphan')


class PriceHistory(db.Model):
    """Every price the agent observed for a watch — enables trend/sparkline. New (was nowhere)."""
    __tablename__ = 'price_history'

    id = db.Column(db.Integer, primary_key=True)
    watch_id = db.Column(
        db.Integer, db.ForeignKey('watches.id', ondelete='CASCADE'), nullable=False, index=True
    )
    price = db.Column(db.Numeric(10, 2), nullable=False)
    checked_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

    watch = db.relationship('Watch', back_populates='prices')


class IngestionJob(db.Model):
    """Status of one product's Reddit ingestion. Replaces the in-memory backend._jobs dict."""
    __tablename__ = 'ingestion_jobs'

    id = db.Column(db.Integer, primary_key=True)
    product_slug = db.Column(db.String(255), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False)    # preparing | ready | error
    stage = db.Column(db.String(40))
    message = db.Column(db.String(500))
    chunk_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)


class Chunk(db.Model):
    """One embedded Reddit chunk for the "Talk to your product" RAG (pgvector).

    Replaces the per-product ChromaDB collection: one row per chunk, scoped by
    product_slug and similarity-searched on `embedding` (cosine). Requires Postgres with
    the `vector` extension. Embeddings come from Gemini gemini-embedding-001 (768-dim).
    """
    __tablename__ = 'chunks'

    id = db.Column(db.Integer, primary_key=True)
    product_slug = db.Column(db.String(255), nullable=False, index=True)   # groups a product's chunks
    text = db.Column(db.Text, nullable=False)
    source = db.Column(db.String(1000))                    # the Reddit thread URL
    score = db.Column(db.Integer, default=0)               # Reddit upvotes (for display)
    embedding = db.Column(Vector(EMBED_DIM), nullable=False)
    ingested_at = db.Column(db.DateTime, default=_utcnow, nullable=False, index=True)  # freshness


class ApiQuota(db.Model):
    """RapidAPI request-quota accounting. Replaces .quota.json.

    Increment atomically to avoid lost counts across workers:
        UPDATE api_quota SET calls_used = calls_used + 1
        WHERE provider = :p AND calls_used < calls_limit RETURNING calls_used;
    """
    __tablename__ = 'api_quota'

    provider = db.Column(db.String(50), primary_key=True)   # e.g. 'rapidapi_reddit'
    calls_used = db.Column(db.Integer, default=0, nullable=False)
    calls_limit = db.Column(db.Integer)
    window_start = db.Column(db.DateTime, default=_utcnow, nullable=False)

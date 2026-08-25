"""Application entry point: the Flask app factory.

Routes live in feature blueprints under app/routes/; shared extensions and helpers in
app/extensions.py. This file only builds and wires the app together.

Run the dev server with `python backend.py`. The Flask CLI (e.g. `flask db upgrade`)
auto-detects the create_app factory.
"""

import logging
import os

from flask import Flask

from models import db
import product_knowledge as pk
import watch_service as watch
from auth_service import init_auth
from app.extensions import migrate, limiter, DIST_DIR
from app.routes.main import main_bp
from app.routes.search import search_bp
from app.routes.talk import talk_bp
from app.routes.auth import auth_bp
from app.routes.watch import watch_bp
from app.routes.health import health_bp


def _configure_logging():
    """Basic structured-ish logging: timestamp, level, logger name, message.

    Configured once; harmless if called again (logging ignores re-config unless forced).
    Swap the format for a JSON formatter later if shipping logs to an aggregator.
    """
    logging.basicConfig(
        level=os.environ.get('LOG_LEVEL', 'INFO'),
        format='%(asctime)s %(levelname)-7s [%(name)s] %(message)s',
    )


def create_app(config=None):
    """Application factory: build and return a configured Flask app.

    Nothing is created at import time — call create_app() to get an app. This lets a
    production WSGI server (waitress/gunicorn) and tests each construct their own app,
    and lets tests pass a `config` override (e.g. a throwaway SQLite DB).
    """
    _configure_logging()
    app = Flask(__name__, static_folder=DIST_DIR, static_url_path='')

    # Flask session cookie signing (used by Authlib to hold OAuth state during the
    # Google redirect). Must be stable across restarts/workers.
    app.secret_key = os.environ.get('SECRET_KEY', 'dev-insecure-change-me')
    # Secure cookies require HTTPS; disable in local dev (http) so the cookie still sets.
    app.config['COOKIE_SECURE'] = os.environ.get('FLASK_DEBUG', '1') != '1'

    # Database: DATABASE_URL selects the backend (Postgres in prod, SQLite by default).
    # Some hosts hand out legacy postgres:// URLs — SQLAlchemy needs postgresql://.
    _db_url = os.environ.get('DATABASE_URL', 'sqlite:///obsidian.db')
    if _db_url.startswith('postgres://'):
        _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # Test/other overrides win over the env-derived defaults above.
    if config:
        app.config.update(config)

    # Bind extensions and services to this app.
    init_auth(app)
    db.init_app(app)
    migrate.init_app(app, db)
    limiter.init_app(app)
    watch.init_watch_service(app)   # give the watch layer the app for DB contexts
    pk.init_product_knowledge(app)  # give product_knowledge the app so quota persists

    # Register feature blueprints (routes keep their full paths, e.g. /watch/enable).
    app.register_blueprint(main_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(talk_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(watch_bp)
    app.register_blueprint(health_bp)

    return app


if __name__ == '__main__':
    app = create_app()
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', '1') == '1'
    # Price checks are driven by Celery Beat (see celery_app.py), run as a separate
    # process — no in-process scheduler thread here anymore.
    app.run(host='0.0.0.0', port=port, debug=debug)

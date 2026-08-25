"""Health blueprint: liveness and readiness probes.

Two distinct checks, the standard split used by orchestrators (Docker/K8s/load balancers):
  /healthz  -> LIVENESS: is the process up and serving? No dependency checks. If this
               fails, the container should be restarted.
  /health   -> READINESS: are our dependencies (Postgres, Redis) reachable? If this
               fails (503), the app is alive but not ready to serve traffic yet.

Both are public (no auth, no rate limit) — probes must always be reachable.
"""

import redis
from flask import Blueprint, jsonify
from sqlalchemy import text

from models import db
from app.extensions import REDIS_URL

health_bp = Blueprint('health', __name__)


@health_bp.route('/healthz')
def liveness():
    """Liveness: the process is up. Cheap and dependency-free."""
    return jsonify({'status': 'alive'}), 200


@health_bp.route('/health')
def readiness():
    """Readiness: check each backing service; 200 only if all are reachable, else 503."""
    checks = {}
    healthy = True

    # Postgres — a trivial round-trip proves the connection + pool work.
    try:
        db.session.execute(text('SELECT 1'))
        checks['database'] = 'ok'
    except Exception as exc:
        checks['database'] = f'error: {type(exc).__name__}'
        healthy = False

    # Redis — broker + rate-limit store; PING proves it's answering.
    try:
        redis.from_url(REDIS_URL, socket_connect_timeout=2).ping()
        checks['redis'] = 'ok'
    except Exception as exc:
        checks['redis'] = f'error: {type(exc).__name__}'
        healthy = False

    status_code = 200 if healthy else 503
    return jsonify({'status': 'healthy' if healthy else 'degraded', 'checks': checks}), status_code

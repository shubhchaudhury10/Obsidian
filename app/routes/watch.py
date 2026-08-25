"""Watch blueprint: price-watch CRUD for the logged-in user."""

from flask import Blueprint, jsonify, request

import watch_service as watch
from auth_service import require_auth, current_user
from app.extensions import limiter

watch_bp = Blueprint('watch', __name__)


@watch_bp.route('/watch/enable', methods=['POST'])
@limiter.limit('30 per hour')
@require_auth
def watch_enable():
    """Start watching a product's price for the logged-in user. Returns the watch."""
    payload = request.get_json(silent=True) or {}
    try:
        result = watch.enable_watch(
            product=payload.get('product', ''),
            url=payload.get('url', ''),
            email=current_user()['email'],   # from the session, never client-supplied
            baseline_price=payload.get('baseline_price'),
            target_price=payload.get('target_price'),
        )
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'watch': result})


@watch_bp.route('/watch/disable', methods=['POST'])
@require_auth
def watch_disable():
    payload = request.get_json(silent=True) or {}
    # Scope to the caller's own email so one user can't disable another's watch by id.
    ok = watch.disable_watch(
        watch_id=payload.get('id'),
        product=payload.get('product'),
        email=current_user()['email'],
    )
    if not ok:
        return jsonify({'error': 'No matching watch found.'}), 404
    return jsonify({'status': 'disabled'})


@watch_bp.route('/watch/list', methods=['GET'])
@require_auth
def watch_list():
    """Only the logged-in user's watches — the email comes from the session, not a param."""
    return jsonify({'watches': watch.list_watches(email=current_user()['email'])})


@watch_bp.route('/watch/run-now', methods=['POST'])
@require_auth
def watch_run_now():
    """Enqueue an immediate agent run for the caller's watches (testing aid). Optional
    body: {"id": <watch id>}. With no id, enqueues every enabled watch the caller owns."""
    from tasks import run_watch_agent_task
    payload = request.get_json(silent=True) or {}
    wid = payload.get('id')
    watches = watch.list_watches(email=current_user()['email'])
    targets = [w for w in watches if w['enabled'] and (wid is None or w['id'] == wid)]
    for w in targets:
        run_watch_agent_task.delay(w['id'])
    return jsonify({'enqueued': [w['id'] for w in targets]})

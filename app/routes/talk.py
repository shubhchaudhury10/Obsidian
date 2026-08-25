"""Talk-to-your-product blueprint: RAG ingestion (async via Celery) + Q&A."""

from flask import Blueprint, jsonify, request

import product_knowledge as pk
from gemini_service import answer_product_question
from auth_service import require_auth
from app.extensions import limiter, _set_job, _get_job

talk_bp = Blueprint('talk', __name__)


@talk_bp.route('/talk/prepare', methods=['POST'])
@limiter.limit('10 per hour')     # each prepare can spend up to ~9 RapidAPI requests
@require_auth
def talk_prepare():
    """Kick off Reddit ingestion for a product in the background (Celery)."""
    payload = request.get_json(silent=True) or {}
    product = payload.get('product', '').strip()
    if not product:
        return jsonify({'error': 'Product is required.'}), 400
    # Set an initial status synchronously (so the first poll sees it), then enqueue the
    # slow scrape/embed as a Celery task for a worker to run in the background.
    _set_job(product, status='preparing', stage='starting',
             message='Getting ready...', chunks=0)
    from tasks import ingest_task
    ingest_task.delay(product)
    return jsonify({'status': 'preparing'})


@talk_bp.route('/talk/status', methods=['GET'])
@require_auth
def talk_status():
    product = request.args.get('product', '').strip()
    job = _get_job(product)
    if not job:
        return jsonify({'status': 'unknown'})
    return jsonify({**job, 'quota': pk.get_quota()})


@talk_bp.route('/talk/quota', methods=['GET'])
@require_auth
def talk_quota():
    """Remaining RapidAPI requests on the reddit3 free plan, for the UI meter."""
    return jsonify(pk.get_quota())


@talk_bp.route('/talk/ask', methods=['POST'])
@require_auth
def talk_ask():
    payload = request.get_json(silent=True) or {}
    product = payload.get('product', '').strip()
    question = payload.get('question', '').strip()
    history = payload.get('history', [])
    if not product or not question:
        return jsonify({'error': 'Product and question are required.'}), 400
    job = _get_job(product)
    if not job or job.get('status') != 'ready':
        return jsonify({'error': 'This product is not ready yet. Please wait a moment.'}), 409
    try:
        chunks = pk.retrieve(product, question)
        answer = answer_product_question(product, question, chunks, history)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500
    sources = sorted({c['source'] for c in chunks if c.get('source')})
    return jsonify({'answer': answer, 'sources': sources})

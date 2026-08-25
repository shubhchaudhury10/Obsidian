"""Search blueprint: the concierge product search (grounded Gemini)."""

from flask import Blueprint, jsonify, request

from gemini_service import get_top_products
from auth_service import require_auth
from app.extensions import limiter

search_bp = Blueprint('search', __name__)


@search_bp.route('/search', methods=['POST'])
@limiter.limit('20 per hour')     # Gemini grounded search — costs API calls
@require_auth
def search():
    payload = request.get_json(silent=True) or {}
    description = payload.get('description', '').strip()
    if not description:
        return jsonify({'error': 'Description is required.'}), 400
    try:
        results = get_top_products(description)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500
    return jsonify({'results': results})

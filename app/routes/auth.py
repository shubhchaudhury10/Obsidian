"""Auth blueprint: Google OAuth + JWT session cookie."""

from flask import (
    Blueprint, jsonify, redirect, url_for, make_response, current_app,
)

from models import db, User
from auth_service import (
    google_client, google_configured, mint_jwt, current_user,
    set_session_cookie, clear_session_cookie,
)

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/auth/login')
def auth_login():
    """Kick off the Google OAuth flow (full-page redirect to Google)."""
    if not google_configured():
        return jsonify({'error': 'Google login is not configured on the server.'}), 503
    # Endpoint is namespaced by the blueprint: 'auth.auth_callback' -> /auth/callback.
    redirect_uri = url_for('auth.auth_callback', _external=True)
    return google_client().authorize_redirect(redirect_uri)


@auth_bp.route('/auth/callback')
def auth_callback():
    """Google redirects here with the code; exchange it, mint our JWT, set the cookie."""
    if not google_configured():
        return jsonify({'error': 'Google login is not configured on the server.'}), 503
    try:
        token = google_client().authorize_access_token()
    except Exception as exc:
        return jsonify({'error': f'Google sign-in failed: {exc}'}), 400
    info = token.get('userinfo') or {}
    sub, email = info.get('sub'), info.get('email')
    if not sub or not email:
        return jsonify({'error': 'Google did not return a verified email.'}), 400

    # Upsert the user row (identity keyed on Google's stable `sub`).
    user = User.query.filter_by(google_sub=sub).first()
    if user is None:
        user = User(google_sub=sub, email=email, name=info.get('name', ''))
        db.session.add(user)
    else:
        user.email = email
        user.name = info.get('name', '')
    db.session.commit()

    session_jwt = mint_jwt(sub, email, info.get('name', ''))
    resp = make_response(redirect('/'))
    return set_session_cookie(resp, session_jwt, current_app.config['COOKIE_SECURE'])


@auth_bp.route('/auth/logout', methods=['POST'])
def auth_logout():
    resp = make_response(jsonify({'status': 'logged_out'}))
    return clear_session_cookie(resp)


@auth_bp.route('/auth/me')
def auth_me():
    """Who is logged in, for the UI. Never 401s — returns {authenticated: false} if not."""
    user = current_user()
    if not user:
        return jsonify({'authenticated': False})
    return jsonify({'authenticated': True, 'email': user.get('email'), 'name': user.get('name', '')})

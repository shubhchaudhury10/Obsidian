"""Main blueprint: serves the built React SPA at the root."""

from flask import Blueprint, send_from_directory

from app.extensions import DIST_DIR

main_bp = Blueprint('main', __name__)


@main_bp.route('/')
def index():
    return send_from_directory(DIST_DIR, 'index.html')

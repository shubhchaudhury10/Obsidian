"""WSGI entrypoint for production servers.

A production WSGI server (waitress locally/in Docker, or gunicorn on Linux) imports this
module and serves the `app` object below — it does NOT run backend.py's `__main__` block
(that's only Flask's dev server). Examples:

    waitress-serve --listen=*:5000 wsgi:app
    gunicorn --bind 0.0.0.0:5000 wsgi:app        # Linux only

`app` is built once here via the factory, so the server has a ready WSGI callable.
"""

from backend import create_app

app = create_app()

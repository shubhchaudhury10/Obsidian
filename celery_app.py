"""Celery application — the background task queue for Obsidian.

RabbitMQ is the broker (the task queue); Redis stays as the result backend. This module
is intentionally lightweight and safe to import from anywhere (e.g. backend.py, to enqueue
tasks): it
creates only the Celery instance and the Beat schedule — NO Flask app, NO db.init, NO
service init. The Flask app context that tasks run in is built lazily in tasks.py, so
importing this from the web process has no side effects.

Run the worker and the scheduler as separate processes:
    celery -A celery_app.celery worker --loglevel=info --pool=solo   # --pool=solo on Windows
    celery -A celery_app.celery beat   --loglevel=info
"""

import os

from celery import Celery
from dotenv import load_dotenv

load_dotenv()

RABBITMQ_URL = os.environ.get('RABBITMQ_URL', 'amqp://guest:guest@localhost:5672//')
_WATCH_INTERVAL_MIN = float(os.environ.get('WATCH_INTERVAL_MINUTES', '30'))

celery = Celery(
    'obsidian',
    broker=RABBITMQ_URL,   # RabbitMQ carries the task messages (the queue)
    include=['tasks'],     # the worker imports tasks.py to register the task functions
)
# NO result backend: tasks write their real outcomes to Postgres (IngestionJob / Watch /
# PriceHistory) and nothing reads Celery's per-task-id results, so we skip the backend
# entirely — Redis is now used only for rate limiting.

# Retry-safety: ack a message only AFTER its task finishes, and redeliver it if the worker
# dies mid-task. With idempotent task bodies (ingest replaces a product's chunks; the watch
# agent overwrites the watch row) a crashed worker's job is safely re-run, not lost. Normal
# task exceptions still ack (only a worker crash redelivers), so there's no poison loop.
celery.conf.task_acks_late = True
celery.conf.task_reject_on_worker_lost = True

# Beat fires this periodically; the task fans out one agent task per due watch.
celery.conf.beat_schedule = {
    'check-due-watches': {
        'task': 'tasks.check_due_watches',
        'schedule': _WATCH_INTERVAL_MIN * 60,   # seconds
    },
}
celery.conf.timezone = 'UTC'

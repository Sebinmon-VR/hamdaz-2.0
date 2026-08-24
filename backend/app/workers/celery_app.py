"""Celery application — background work, in its own process.

Root cause #4: the legacy system runs three ``while True`` + ``sleep()`` threads *inside* the
web process, so every gunicorn worker runs its own copy. That is why SharePoint writes and
supplier emails were duplicated. Here the workers are separate processes, the schedule is
owned by a single beat instance, and every run is recorded in ``job_runs`` for the developer
panel to render.
"""

from __future__ import annotations

from typing import Any

from celery import Celery
from celery.signals import setup_logging

from app.core.compat import configure_event_loop_policy
from app.core.config import get_settings
from app.core.logging import bind_correlation_id, configure_logging

configure_event_loop_policy()
settings = get_settings()

celery_app = Celery(
    "hamdaz",
    broker=str(settings.redis_url),
    backend=str(settings.redis_url),
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Dubai",
    enable_utc=True,
    # Ack after the task finishes so a worker crash re-queues rather than drops. Requires
    # tasks to be idempotent, which every connector sync is by design.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    task_time_limit=600,
    task_soft_time_limit=540,
    result_expires=60 * 60 * 24,
    broker_connection_retry_on_startup=True,
)

# The beat schedule. Nothing here writes to SharePoint — C2.
celery_app.conf.beat_schedule = {
    "sharepoint-delta-sync": {
        "task": "app.workers.tasks.sync_sharepoint_proposals",
        "schedule": 300.0,  # 5 min; the legacy 60 s loop existed only because it was in-process
        "options": {"expires": 240},
    },
    "expire-labels": {
        "task": "app.workers.tasks.expire_labels",
        "schedule": 3600.0,
    },
}


@setup_logging.connect
def _configure_worker_logging(**_: Any) -> None:
    """Use our structlog config rather than Celery's, so worker logs match the API's."""
    configure_logging(settings)


def start_task_context(task_id: str | None = None) -> str:
    """Bind a correlation ID at the start of a task.

    Called from the task prologue so a job's log lines join the same trace as the request
    that enqueued it.
    """
    return bind_correlation_id(task_id)

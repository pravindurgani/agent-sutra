from __future__ import annotations

import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

import config

logger = logging.getLogger(__name__)

# SQLite-backed job store so scheduled tasks survive reboots
# Uses a SEPARATE database from aiosqlite (config.DB_PATH) to avoid lock contention
_scheduler_db = config.BASE_DIR / "storage" / "scheduler.db"
_job_store_url = f"sqlite:///{_scheduler_db}"
scheduler = AsyncIOScheduler(
    jobstores={"default": SQLAlchemyJobStore(url=_job_store_url)},
)


def start_scheduler():
    """Start the APScheduler instance."""
    if not scheduler.running:
        scheduler.start()
        jobs = scheduler.get_jobs()
        logger.info("Scheduler started (%d persisted jobs loaded)", len(jobs))


def stop_scheduler():
    """Shut down the scheduler gracefully."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


def add_interval_job(func, hours: int = 0, minutes: int = 0, job_id: str = "", **kwargs):
    """Add a recurring job at a fixed interval."""
    scheduler.add_job(
        func,
        "interval",
        hours=hours,
        minutes=minutes,
        id=job_id or None,
        replace_existing=True,
        kwargs=kwargs,
    )
    logger.info("Added interval job %s: every %dh %dm", job_id[:8] if job_id else "auto", hours, minutes)


def list_jobs() -> list[dict]:
    """Return summary of all scheduled jobs."""
    return [
        {"id": job.id, "next_run": str(job.next_run_time), "name": job.name}
        for job in scheduler.get_jobs()
    ]


def remove_job(job_id: str):
    """Remove a scheduled job by ID. Accepts partial ID (prefix match)."""
    # Support partial ID matching (user sees first 8 chars)
    for job in scheduler.get_jobs():
        if job.id.startswith(job_id):
            scheduler.remove_job(job.id)
            logger.info("Removed job: %s", job.id)
            return
    raise ValueError(f"No job found matching: {job_id}")

"""Queue a company's roundup and run it without blocking the browser.

On a laptop the run starts in a background thread. On Azure Functions the
button only queues the job; the minute timer starts it, because an HTTP
request would be closed before Claude finished.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone

from pipeline.config import settings
from pipeline.registry import Registry, Tenant, default_registry
from pipeline.runner import PipelineResult

logger = logging.getLogger("earned-media.jobs")

_inflight: set[str] = set()
_inflight_lock = threading.Lock()


class RunInProgress(Exception):
    """This company already has a roundup queued or running."""


def execution_mode() -> str:
    """thread (local), inline (tests), or defer (Azure HTTP queues for the timer)."""
    forced = os.getenv("DASHBOARD_EXECUTION", "").strip().lower()
    if forced in {"inline", "thread", "defer"}:
        return forced
    if os.getenv("FUNCTIONS_WORKER_RUNTIME"):
        return "defer"
    return "thread"


def enqueue_run(
    registry: Registry,
    tenant: Tenant,
    *,
    cadence: str,
    trigger: str,
    triggered_by: str,
) -> str:
    if registry.active_job(tenant.slug):
        raise RunInProgress(tenant.name)
    job = registry.create_job(
        tenant.slug,
        cadence=cadence,
        trigger=trigger,
        triggered_by=triggered_by,
    )
    mode = execution_mode()
    if mode == "thread":
        spawn(job.id)
    elif mode == "inline":
        execute_job(job.id, registry)
    return job.id


def spawn(job_id: str) -> None:
    with _inflight_lock:
        if job_id in _inflight:
            return
        _inflight.add(job_id)

    def _run() -> None:
        try:
            execute_job(job_id)
        finally:
            with _inflight_lock:
                _inflight.discard(job_id)

    threading.Thread(target=_run, name=f"emo-{job_id}", daemon=True).start()


def execute_job(job_id: str, registry: Registry | None = None) -> None:
    registry = registry or default_registry()
    job = registry.claim_job(job_id)
    if job is None:
        return
    tenant = registry.get_tenant(job.tenant_slug)
    try:
        if tenant is None:
            raise RuntimeError("That company is no longer on the list.")
        # A failed attempt used to be saved and repeated every morning.
        # Open the existing company tables instead of creating a storage account.
        if not tenant.provisioned_at or not tenant.storage_account:
            from pipeline.provision import provision_tenant

            tenant = provision_tenant(registry, tenant)
        elif tenant.provision_error:
            tenant.provision_error = ""
            registry.save_tenant(tenant)
        from pipeline.runner import run_for_tenant

        result = run_for_tenant(tenant, dry_run=settings.dry_run_research)
        error = "; ".join(
            f"{item.person.name}: {item.error}" for item in result.errors if item.error
        )
        status = "error" if error and not result.delivered else "done"
        if not result.results and not result.delivered:
            status = "done"
        registry.finish_job(
            job_id,
            status=status,
            summary=summarize(result),
            error=error,
        )
        fresh = registry.get_tenant(tenant.slug)
        if fresh is not None:
            fresh.last_run_at = job.started_at or fresh.last_run_at
            registry.save_tenant(fresh)
    except Exception as exc:  # noqa: BLE001 — the dashboard has to show the failure
        logger.exception("Roundup failed for job %s", job_id)
        registry.finish_job(
            job_id,
            status="error",
            summary="The run did not finish.",
            error=str(exc),
        )


def dispatch_due(registry: Registry | None = None, *, now: datetime | None = None) -> list[str]:
    """Queue companies whose saved schedule is due, then start queued work."""
    registry = registry or default_registry()
    moment = now or datetime.now(timezone.utc)
    started: list[str] = []
    for tenant in registry.due_tenants(moment):
        if registry.active_job(tenant.slug):
            continue
        if not (tenant.blast_to or "").strip():
            continue
        from pipeline.schedule import next_run_at, stamp

        upcoming = next_run_at(
            cadence=tenant.cadence,
            hour=tenant.hour,
            minute=tenant.minute,
            weekday=tenant.weekday,
            interval_days=tenant.interval_days,
            tz_name=tenant.timezone,
            after=moment,
        )
        tenant.next_run_at = stamp(upcoming)
        registry.save_tenant(tenant)
        job = registry.create_job(
            tenant.slug,
            cadence=tenant.cadence,
            trigger="schedule",
            triggered_by="schedule",
        )
        started.append(job.id)
    mode = execution_mode()
    for job in registry.queued_jobs():
        if mode == "thread":
            spawn(job.id)
        else:
            execute_job(job.id, registry)
        if job.id not in started:
            started.append(job.id)
    return started


def summarize(result: PipelineResult) -> str:
    if not result.results:
        return "No speakers yet. Add speakers, then run again."
    found = sum(item.new for item in result.results)
    failed = len(result.errors)
    if result.delivered:
        lead = f"Roundup sent with {found} new opportunities"
    else:
        lead = (
            f"Research finished with {found} new opportunities, "
            "but the roundup was not sent"
        )
    if result.urgent:
        lead += f" ({result.urgent} time-sensitive)"
    if failed:
        noun = "speaker" if failed == 1 else "speakers"
        lead += f". {failed} {noun} could not be researched"
    return lead + "."

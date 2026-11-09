"""Azure Functions host: morning Excel job, per-company schedules, and the dashboard."""

from __future__ import annotations

import logging

import azure.functions as func

from pipeline.dashboard import handle_request

app = func.FunctionApp()
logger = logging.getLogger("earned-media")


@app.timer_trigger(schedule="0 0 7 * * *", arg_name="timer", run_on_startup=False)
def morning_roundup(timer: func.TimerRequest) -> None:
    """One pass of research, scoring, and the team blast. Does not retry on a partial failure."""
    if timer.past_due:
        logger.warning("Morning roundup timer is past due")
    logger.info("Earned media morning roundup starting")
    from pipeline.runner import run_daily

    result = run_daily()
    logger.info(
        "Roundup finished synced=%s processed=%s urgent=%s errors=%s path=%s",
        result.synced,
        len(result.results),
        result.urgent,
        len(result.errors),
        result.outbox_path,
    )
    for item in result.errors:
        logger.error("Research failed for %s: %s", item.person.name, item.error)


@app.timer_trigger(schedule="0 */1 * * * *", arg_name="timer", run_on_startup=False)
def portco_schedules(timer: func.TimerRequest) -> None:
    """Start queued dashboard runs and any company whose saved cadence is due."""
    if timer.past_due:
        logger.warning("Portfolio schedule timer is past due")
    from pipeline.jobs import dispatch_due

    started = dispatch_due()
    if started:
        logger.info("Portfolio runs started: %s", ", ".join(started))
    try:
        from pipeline.followup import process_pending_followups

        process_pending_followups()
    except Exception:
        logger.exception("Follow-up processing failed")


@app.route(route="{*path}", methods=["GET", "POST"], auth_level=func.AuthLevel.ANONYMOUS)
def site(req: func.HttpRequest) -> func.HttpResponse:
    return _site(req)


@app.route(route="/", methods=["GET", "POST"], auth_level=func.AuthLevel.ANONYMOUS)
def site_root(req: func.HttpRequest) -> func.HttpResponse:
    return _site(req)


def _site(req: func.HttpRequest) -> func.HttpResponse:
    headers = {key: value for key, value in req.headers.items()}
    result = handle_request(req.method, req.url, headers, req.get_body() or b"")
    return func.HttpResponse(
        result.body or None,
        status_code=result.status,
        headers={key: value for key, value in result.headers},
        mimetype="text/html",
    )

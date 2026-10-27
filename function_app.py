"""Azure Functions host: morning timer, LinkedIn drafts, and the feedback pages."""

from __future__ import annotations

import logging

import azure.functions as func

from pipeline.feedback import handle_feedback_request, parse_form_body
from pipeline.storage import open_store

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


@app.timer_trigger(schedule="0 * * * * *", arg_name="timer", run_on_startup=False)
def linkedin_drafts(timer: func.TimerRequest) -> None:
    """Send LinkedIn drafts queued by Make it LinkedIn. The click only queues."""
    del timer
    from pipeline.followup import process_pending_followups

    process_pending_followups()


@app.route(
    route="feedback/{token}/{action}",
    methods=["GET", "POST"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def feedback_page(req: func.HttpRequest) -> func.HttpResponse:
    form = parse_form_body(req.get_body()) if req.method.upper() == "POST" else None
    result = handle_feedback_request(open_store(), req.method, req.url, form)
    headers = {"Location": result.location} if result.location else {}
    return func.HttpResponse(
        result.body or None,
        status_code=result.status,
        headers=headers,
        mimetype="text/html",
    )

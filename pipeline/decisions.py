"""Batch keys, decline replies, and what a button is allowed to overwrite.

A roundup email is one batch. Each opportunity in that email is numbered
1, 2, 3 in the order it appears. A decline reply is one line per item:

    2. pay-to-play webinar, wrong audience

The batch key in the subject (emo- plus 8 hex characters) says which email
those numbers belong to. The reason is required because the next research
run reads it. A bare "no" only blocks one URL; the reason is what generalizes
to the outlet, the format, or the fit.
"""

from __future__ import annotations

import hashlib
import re
from html import unescape

_BATCH_KEY = re.compile(r"\b(emo-[0-9a-f]{8})\b", re.I)
_NUMBERED = re.compile(r"^\s*(\d+)\s*\.\s*(\S.*?)\s*$")
_REPLY_SUBJECT = re.compile(r"^\s*(re|aw|sv|antw)\s*:", re.I)
_QUOTE = re.compile(
    r"(?im)^(?:"
    r"-{5,}\s*original message\s*-{5,}|"
    r"_{8,}|"
    r"from:\s+\S|"
    r"on .+ wrote:\s*$|"
    r">"
    r")"
)
_VERDICTS = {"yes", "no", "go", "linkedin"}


def batch_key_for(generated_on: str, fingerprints_in_order: list[str]) -> str:
    """Stable id for one email. Same date and same items, same key."""
    if not fingerprints_in_order:
        return ""
    raw = generated_on + "|" + "|".join(fingerprints_in_order)
    digest = hashlib.sha256(raw.encode()).hexdigest()[:8]
    return f"emo-{digest}"


def stored_decision(
    current_verdict: str,
    current_reason: str,
    verdict: str,
    reason: str,
) -> tuple[str, str] | None:
    """Return the verdict and reason to store.

    None means leave the row unchanged. Make it LinkedIn does not erase a
    No-Go: a post draft is not a decision to start suggesting that kind of
    opportunity again. Go does replace a No-Go, because they want the pitch.
    """
    if verdict not in _VERDICTS:
        raise ValueError(f"verdict must be yes, no, go, or linkedin, got {verdict!r}")
    if verdict == "linkedin" and (current_verdict or "") == "no":
        return None
    if verdict == "no":
        return "no", reason.strip()
    if verdict == "linkedin":
        return "linkedin", current_reason or ""
    return verdict, ""


def parse_numbered_lines(text: str) -> list[tuple[int, str]]:
    """Lines shaped like `2. the reason`. Shorter than three characters is skipped."""
    found: list[tuple[int, str]] = []
    for line in (text or "").splitlines():
        match = _NUMBERED.match(line)
        if not match:
            continue
        number = int(match.group(1))
        reason = match.group(2).strip()
        if number < 1 or len(reason) < 3:
            continue
        found.append((number, reason))
    return found


def parse_decline_reply(
    subject: str,
    body: str,
    *,
    require_reply_subject: bool = False,
) -> tuple[str, list[tuple[int, str]]]:
    """Pull a batch key and numbered reasons out of a reply.

    Inbox polling sets require_reply_subject so the roundup itself, sitting
    in a mailbox, is not read as a list of declines. The text below a quoted
    original is ignored, because that quote contains the numbered items.
    """
    subject = subject or ""
    if require_reply_subject and not _REPLY_SUBJECT.match(subject):
        return "", []
    fresh = unquoted(html_to_text(body or ""))
    key_match = _BATCH_KEY.search(subject) or _BATCH_KEY.search(fresh)
    if not key_match:
        return "", []
    return key_match.group(1).lower(), parse_numbered_lines(fresh)


def is_echo(reason: str, title: str) -> bool:
    """True when the line is the roundup item, not a reason someone typed."""
    title_norm = " ".join((title or "").split()).lower()
    text = " ".join((reason or "").split()).lower()
    if len(title_norm) < 8 or not text:
        return False
    if text == title_norm:
        return True
    if not text.startswith(title_norm):
        return False
    rest = text[len(title_norm) :].strip()
    return not rest or rest.startswith(("(", "—", "-"))


def apply_decline_reply(
    store: object,
    subject: str,
    body: str,
    *,
    require_reply_subject: bool = False,
) -> list:
    """Save each numbered reason onto that item. Unknown numbers are skipped."""
    key, lines = parse_decline_reply(
        subject, body, require_reply_subject=require_reply_subject
    )
    if not key or not lines:
        return []
    saved = []
    for number, reason in lines:
        record = store.get_batch_item(key, number)  # type: ignore[attr-defined]
        if record is None or is_echo(reason, record.title):
            continue
        row = store.record_feedback(record.token, "no", reason)  # type: ignore[attr-defined]
        if row is not None and row.verdict == "no":
            saved.append(row)
    return saved


def decline_from_form(store: object, record: object, text: str) -> tuple[list, str]:
    """Save a No-Go typed on the page.

    A plain sentence applies to the item they opened. Numbered lines apply
    across the batch, same shape as a reply email.
    """
    lines = parse_numbered_lines(text or "")
    if not lines:
        reason = (text or "").strip()
        if len(reason) < 3:
            return [], (
                "Say why in a few words so the next roundup can avoid "
                "opportunities like this."
            )
        saved = store.record_feedback(record.token, "no", reason)  # type: ignore[attr-defined]
        return ([saved] if saved is not None else []), ""

    saved = []
    for number, reason in lines:
        target = None
        batch_key = getattr(record, "batch_key", "") or ""
        item_number = int(getattr(record, "item_number", 0) or 0)
        if batch_key:
            target = store.get_batch_item(batch_key, number)  # type: ignore[attr-defined]
        if target is None and (number == item_number or len(lines) == 1):
            target = record
        if target is None or is_echo(reason, getattr(target, "title", "")):
            continue
        row = store.record_feedback(target.token, "no", reason)  # type: ignore[attr-defined]
        if row is not None and row.verdict == "no":
            saved.append(row)
    if not saved:
        example = item_number_example(record)
        return [], (
            "Use the item number from the email, then a period, then the reason. "
            f"For example: {example}"
        )
    return saved, ""


def item_number_example(record: object) -> str:
    number = int(getattr(record, "item_number", 0) or 0) or 1
    return f"{number}. pay-to-play, wrong audience"


def html_to_text(value: str) -> str:
    if "<" not in value or ">" not in value:
        return value
    text = re.sub(r"(?is)<(script|style)\b.*?>.*?</\1>", " ", value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"(?i)</div\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text)


def unquoted(body: str) -> str:
    match = _QUOTE.search(body or "")
    if not match:
        return body or ""
    return body[: match.start()]

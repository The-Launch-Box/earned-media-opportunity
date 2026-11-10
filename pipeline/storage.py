"""SQLite stand-in for Azure Table Storage (roster + sent fingerprints)."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from pipeline.config import settings
from pipeline.models import (
    Opportunity,
    PodcastAudience,
    RadarMetrics,
    SiteMetrics,
    Spokesperson,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS roster (
    person_id     TEXT PRIMARY KEY,
    firm          TEXT NOT NULL,
    name          TEXT NOT NULL,
    title         TEXT,
    linkedin      TEXT,
    topics        TEXT,
    prior_work    TEXT,
    bio           TEXT,
    email         TEXT,
    synced_at     TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sent_opportunities (
    person_id   TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    url         TEXT NOT NULL,
    title       TEXT,
    outlet      TEXT,
    sent_at     TEXT NOT NULL,
    PRIMARY KEY (person_id, fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_sent_person ON sent_opportunities(person_id);
CREATE INDEX IF NOT EXISTS idx_sent_fingerprint ON sent_opportunities(fingerprint);

CREATE TABLE IF NOT EXISTS semrush_domains (
    domain     TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS radar_domains (
    domain     TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS podcast_shows (
    query      TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS opportunity_feedback (
    token            TEXT PRIMARY KEY,
    person_id        TEXT NOT NULL,
    person_name      TEXT NOT NULL,
    fingerprint      TEXT NOT NULL,
    url              TEXT NOT NULL,
    title            TEXT,
    outlet           TEXT,
    opportunity_type TEXT,
    why_relevant     TEXT,
    suggested_angle  TEXT,
    verdict          TEXT,
    reason           TEXT,
    created_at       TEXT NOT NULL,
    decided_at       TEXT,
    batch_key        TEXT,
    item_number      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_feedback_fingerprint
    ON opportunity_feedback(fingerprint);

CREATE TABLE IF NOT EXISTS roundup_batches (
    batch_key  TEXT PRIMARY KEY,
    subject    TEXT NOT NULL,
    reply_to   TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_jobs (
    job_id      TEXT PRIMARY KEY,
    token       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,
    error       TEXT,
    created_at  TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_action_token ON action_jobs(token, created_at);

CREATE TABLE IF NOT EXISTS ingested_messages (
    message_id  TEXT PRIMARY KEY,
    ingested_at TEXT NOT NULL
);
"""


@dataclass
class FeedbackRecord:
    token: str
    person_id: str
    person_name: str
    fingerprint: str
    url: str
    title: str
    outlet: str
    opportunity_type: str
    why_relevant: str
    suggested_angle: str
    verdict: str
    reason: str
    created_at: str
    decided_at: str
    batch_key: str = ""
    item_number: int = 0


@dataclass
class ActionJob:
    job_id: str
    token: str
    kind: str
    status: str
    error: str
    created_at: str
    finished_at: str


class LocalStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or settings.db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            self._ensure_column(conn, "roster", "first_seen_at", "TEXT")
            self._ensure_column(conn, "opportunity_feedback", "batch_key", "TEXT")
            self._ensure_column(conn, "opportunity_feedback", "item_number", "INTEGER")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_feedback_batch
                ON opportunity_feedback(batch_key, item_number)
                """
            )

    def _ensure_column(
        self, conn: sqlite3.Connection, table: str, column: str, decl: str
    ) -> None:
        cols = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column in cols:
            return
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        if column == "first_seen_at":
            conn.execute(
                "UPDATE roster SET first_seen_at = synced_at "
                "WHERE first_seen_at IS NULL OR first_seen_at = ''"
            )

    def roster_ids(self) -> set[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT person_id FROM roster").fetchall()
        return {r["person_id"] for r in rows}

    def sync_roster(self, people: list[Spokesperson]) -> tuple[int, list[Spokesperson]]:
        """Upsert the current Excel roster. Returns (count, newly added people)."""
        now = datetime.now(timezone.utc).isoformat()
        existing = self.roster_ids()
        newcomers = [p for p in people if p.person_id not in existing]
        incoming_ids = [p.person_id for p in people]

        with self._conn() as conn:
            conn.executemany(
                """
                INSERT INTO roster
                (person_id, firm, name, title, linkedin, topics, prior_work,
                 bio, email, synced_at, first_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(person_id) DO UPDATE SET
                    firm = excluded.firm,
                    name = excluded.name,
                    title = excluded.title,
                    linkedin = excluded.linkedin,
                    topics = excluded.topics,
                    prior_work = excluded.prior_work,
                    bio = excluded.bio,
                    email = excluded.email,
                    synced_at = excluded.synced_at
                """,
                [
                    (
                        p.person_id,
                        p.firm,
                        p.name,
                        p.title,
                        p.linkedin,
                        p.topics,
                        p.prior_work,
                        p.bio,
                        p.email,
                        now,
                        now,
                    )
                    for p in people
                ],
            )
            if incoming_ids:
                placeholders = ",".join("?" for _ in incoming_ids)
                conn.execute(
                    f"DELETE FROM roster WHERE person_id NOT IN ({placeholders})",
                    incoming_ids,
                )
            else:
                conn.execute("DELETE FROM roster")
        return len(people), newcomers

    def list_roster(self) -> list[Spokesperson]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM roster ORDER BY firm, name"
            ).fetchall()
        return [_spokesperson_row(r) for r in rows]

    def get_person(self, person_id: str) -> Spokesperson | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM roster WHERE person_id = ?",
                (person_id,),
            ).fetchone()
        if row is None:
            return None
        return _spokesperson_row(row)

    def save_person(self, person: Spokesperson) -> None:
        """Insert or update one speaker. Does not remove anyone else."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT first_seen_at FROM roster WHERE person_id = ?",
                (person.person_id,),
            ).fetchone()
            first_seen = (
                existing["first_seen_at"]
                if existing and existing["first_seen_at"]
                else now
            )
            conn.execute(
                """
                INSERT INTO roster
                (person_id, firm, name, title, linkedin, topics, prior_work,
                 bio, email, synced_at, first_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(person_id) DO UPDATE SET
                    firm = excluded.firm,
                    name = excluded.name,
                    title = excluded.title,
                    linkedin = excluded.linkedin,
                    topics = excluded.topics,
                    prior_work = excluded.prior_work,
                    bio = excluded.bio,
                    email = excluded.email,
                    synced_at = excluded.synced_at
                """,
                (
                    person.person_id,
                    person.firm,
                    person.name,
                    person.title,
                    person.linkedin,
                    person.topics,
                    person.prior_work,
                    person.bio,
                    person.email,
                    now,
                    first_seen,
                ),
            )

    def delete_person(self, person_id: str) -> bool:
        """Remove one speaker and the links already sent for them."""
        with self._conn() as conn:
            deleted = conn.execute(
                "DELETE FROM roster WHERE person_id = ?",
                (person_id,),
            ).rowcount
            conn.execute(
                "DELETE FROM sent_opportunities WHERE person_id = ?",
                (person_id,),
            )
        return deleted > 0

    def already_sent(self, person_id: str, fingerprint: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM sent_opportunities
                WHERE person_id = ? AND fingerprint = ?
                """,
                (person_id, fingerprint),
            ).fetchone()
        return row is not None

    def filter_new(
        self, person_id: str, opportunities: list[Opportunity]
    ) -> list[Opportunity]:
        """Drop URLs already filed for this person in sent_opportunities."""
        seen: set[str] = set()
        fresh: list[Opportunity] = []
        for opp in opportunities:
            if not opp.fingerprint:
                continue
            if opp.fingerprint in seen:
                continue
            if self.already_sent(person_id, opp.fingerprint):
                continue
            seen.add(opp.fingerprint)
            fresh.append(opp)
        return fresh

    def record_sent(
        self, person_id: str, opportunities: list[Opportunity]
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO sent_opportunities
                (person_id, fingerprint, url, title, outlet, sent_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        person_id,
                        o.fingerprint,
                        o.url,
                        o.title,
                        o.outlet,
                        now,
                    )
                    for o in opportunities
                    if o.fingerprint
                ],
            )

    def sent_count(self, person_id: str | None = None) -> int:
        with self._conn() as conn:
            if person_id:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM sent_opportunities WHERE person_id = ?",
                    (person_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM sent_opportunities"
                ).fetchone()
        return int(row["n"])

    def get_semrush_cache(
        self,
        domain: str,
        max_age_days: int,
        *,
        now: datetime | None = None,
    ) -> SiteMetrics | None:
        """Return a still-fresh Semrush snapshot, or None if missing/stale."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload, fetched_at FROM semrush_domains WHERE domain = ?",
                (domain,),
            ).fetchone()
        if row is None:
            return None
        fetched_at = row["fetched_at"] or ""
        try:
            fetched = datetime.fromisoformat(fetched_at)
        except ValueError:
            return None
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        age = (now or datetime.now(timezone.utc)) - fetched
        if age.days > max_age_days:
            return None
        try:
            data = json.loads(row["payload"])
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        return SiteMetrics.model_validate(data)

    def put_semrush_cache(self, metrics: SiteMetrics) -> None:
        domain = (metrics.domain or "").strip().lower()
        if not domain:
            return
        fetched_at = metrics.fetched_at or datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO semrush_domains (domain, payload, fetched_at)
                VALUES (?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    payload = excluded.payload,
                    fetched_at = excluded.fetched_at
                """,
                (domain, metrics.model_dump_json(), fetched_at),
            )

    def get_radar_cache(
        self,
        domain: str,
        max_age_days: int,
        *,
        now: datetime | None = None,
    ) -> RadarMetrics | None:
        """Return a still-fresh Radar ranking, or None if missing/stale."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload, fetched_at FROM radar_domains WHERE domain = ?",
                (domain,),
            ).fetchone()
        if row is None:
            return None
        data = _fresh_payload(row["payload"], row["fetched_at"], max_age_days, now=now)
        return RadarMetrics.model_validate(data) if data is not None else None

    def put_radar_cache(self, metrics: RadarMetrics) -> None:
        domain = (metrics.domain or "").strip().lower()
        if not domain:
            return
        fetched_at = metrics.fetched_at or datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO radar_domains (domain, payload, fetched_at)
                VALUES (?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    payload = excluded.payload,
                    fetched_at = excluded.fetched_at
                """,
                (domain, metrics.model_dump_json(), fetched_at),
            )

    def get_podcast_cache(
        self,
        query: str,
        max_age_days: int,
        *,
        now: datetime | None = None,
    ) -> PodcastAudience | None:
        """Return a still-fresh podcast lookup, or None if missing or stale."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload, fetched_at FROM podcast_shows WHERE query = ?",
                (query,),
            ).fetchone()
        return _cached_podcast(row, max_age_days, now=now)

    def put_podcast_cache(self, audience: PodcastAudience) -> None:
        query = (audience.query or "").strip()
        if not query:
            return
        fetched_at = audience.fetched_at or datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO podcast_shows (query, payload, fetched_at)
                VALUES (?, ?, ?)
                ON CONFLICT(query) DO UPDATE SET
                    payload = excluded.payload,
                    fetched_at = excluded.fetched_at
                """,
                (query, audience.model_dump_json(), fetched_at),
            )

    def export_debug(self) -> str:
        with self._conn() as conn:
            roster = [dict(r) for r in conn.execute("SELECT * FROM roster")]
            sent = [
                dict(r) for r in conn.execute("SELECT * FROM sent_opportunities")
            ]
            semrush = [
                dict(r) for r in conn.execute("SELECT * FROM semrush_domains")
            ]
            radar = [
                dict(r) for r in conn.execute("SELECT * FROM radar_domains")
            ]
            podcasts = [
                dict(r) for r in conn.execute("SELECT * FROM podcast_shows")
            ]
            feedback = [
                dict(r) for r in conn.execute("SELECT * FROM opportunity_feedback")
            ]
        return json.dumps(
            {
                "roster": roster,
                "sent": sent,
                "semrush": semrush,
                "radar": radar,
                "podcasts": podcasts,
                "feedback": feedback,
            },
            indent=2,
        )

    def issue_feedback(
        self, person: Spokesperson, opportunities: list[Opportunity]
    ) -> None:
        """Attach a stable Yes/No token to each opportunity and store a pending row."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            for opp in opportunities:
                if not opp.fingerprint:
                    continue
                existing = conn.execute(
                    """
                    SELECT token FROM opportunity_feedback
                    WHERE person_id = ? AND fingerprint = ? AND verdict IS NULL
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (person.person_id, opp.fingerprint),
                ).fetchone()
                if existing:
                    opp.feedback_token = existing["token"]
                    continue
                token = secrets.token_urlsafe(18)
                conn.execute(
                    """
                    INSERT INTO opportunity_feedback
                    (token, person_id, person_name, fingerprint, url, title,
                     outlet, opportunity_type, why_relevant, suggested_angle,
                     verdict, reason, created_at, decided_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '', ?, NULL)
                    """,
                    (
                        token,
                        person.person_id,
                        person.name,
                        opp.fingerprint,
                        opp.url,
                        opp.title,
                        opp.outlet,
                        opp.opportunity_type,
                        opp.why_relevant,
                        opp.suggested_angle,
                        now,
                    ),
                )
                opp.feedback_token = token

    def get_feedback(self, token: str) -> FeedbackRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM opportunity_feedback WHERE token = ?",
                (token,),
            ).fetchone()
        if row is None:
            return None
        return _feedback_from_row(row)

    def record_feedback(self, token: str, verdict: str, reason: str = "") -> FeedbackRecord | None:
        """Save a decision for this link. A later yes or go on the same URL wins."""
        from pipeline.decisions import stored_decision

        current = self.get_feedback(token)
        if current is None:
            return None
        decided = stored_decision(current.verdict, current.reason, verdict, reason)
        if decided is None:
            return current
        new_verdict, new_reason = decided
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE opportunity_feedback
                SET verdict = ?, reason = ?, decided_at = ?
                WHERE token = ?
                """,
                (new_verdict, new_reason, now, token),
            )
        return self.get_feedback(token)

    def save_batch(
        self,
        batch_key: str,
        subject: str,
        reply_to: str,
        opportunities: list[Opportunity],
    ) -> None:
        """Remember which email item 1, 2, 3 belong to, and who gets the follow-up."""
        if not _BATCH_KEY.match(batch_key or ""):
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO roundup_batches (batch_key, subject, reply_to, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(batch_key) DO UPDATE SET
                    subject = excluded.subject,
                    reply_to = excluded.reply_to
                """,
                (batch_key, subject, reply_to or "", now),
            )
            for opp in opportunities:
                if not opp.feedback_token or not opp.item_number:
                    continue
                conn.execute(
                    """
                    UPDATE opportunity_feedback
                    SET batch_key = ?, item_number = ?
                    WHERE token = ?
                    """,
                    (batch_key, opp.item_number, opp.feedback_token),
                )

    def get_batch_item(self, batch_key: str, item_number: int) -> FeedbackRecord | None:
        if not _BATCH_KEY.match(batch_key or "") or item_number < 1:
            return None
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM opportunity_feedback
                WHERE batch_key = ? AND item_number = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (batch_key, item_number),
            ).fetchone()
        if row is None:
            return None
        return _feedback_from_row(row)

    def batch_reply_to(self, batch_key: str) -> str:
        if not _BATCH_KEY.match(batch_key or ""):
            return ""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT reply_to FROM roundup_batches WHERE batch_key = ?",
                (batch_key,),
            ).fetchone()
        if row is None:
            return ""
        return row["reply_to"] or ""

    def enqueue_action(self, token: str, kind: str) -> str:
        if kind not in {"go", "linkedin"}:
            raise ValueError(f"kind must be go or linkedin, got {kind!r}")
        now = datetime.now(timezone.utc).isoformat()
        cutoff = _stale_cutoff()
        with self._conn() as conn:
            existing = conn.execute(
                """
                SELECT job_id, status, created_at FROM action_jobs
                WHERE token = ? AND kind = ? AND status IN ('queued', 'running')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (token, kind),
            ).fetchone()
            if existing:
                fresh = existing["status"] == "queued" or (existing["created_at"] or "") >= cutoff
                if fresh:
                    return existing["job_id"]
                conn.execute(
                    """
                    UPDATE action_jobs
                    SET status = 'queued', error = '', finished_at = NULL
                    WHERE job_id = ?
                    """,
                    (existing["job_id"],),
                )
                return existing["job_id"]
            job_id = secrets.token_urlsafe(12)
            conn.execute(
                """
                INSERT INTO action_jobs
                (job_id, token, kind, status, error, created_at, finished_at)
                VALUES (?, ?, ?, 'queued', '', ?, NULL)
                """,
                (job_id, token, kind, now),
            )
        return job_id

    def claim_action(self, job_id: str) -> ActionJob | None:
        cutoff = _stale_cutoff()
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE action_jobs
                SET status = 'running'
                WHERE job_id = ?
                  AND (
                    status = 'queued'
                    OR (status = 'running' AND created_at < ?)
                  )
                """,
                (job_id, cutoff),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM action_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return _action_from_row(row)

    def queued_actions(self) -> list[ActionJob]:
        cutoff = _stale_cutoff()
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM action_jobs
                WHERE status = 'queued'
                   OR (status = 'running' AND created_at < ?)
                ORDER BY created_at
                """,
                (cutoff,),
            ).fetchall()
        return [_action_from_row(row) for row in rows]

    def finish_action(self, job_id: str, *, status: str, error: str = "") -> None:
        if status not in {"sent", "error"}:
            raise ValueError(f"status must be sent or error, got {status!r}")
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE action_jobs
                SET status = ?, error = ?, finished_at = ?
                WHERE job_id = ?
                """,
                (status, (error or "")[:2000], now, job_id),
            )

    def latest_action(self, token: str) -> ActionJob | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM action_jobs
                WHERE token = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (token,),
            ).fetchone()
        if row is None:
            return None
        return _action_from_row(row)

    def message_seen(self, message_id: str) -> bool:
        if not message_id:
            return False
        with self._conn() as conn:
            row = conn.execute(
                "SELECT message_id FROM ingested_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        return row is not None

    def remember_message(self, message_id: str) -> bool:
        if not message_id:
            return False
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO ingested_messages (message_id, ingested_at)
                VALUES (?, ?)
                ON CONFLICT(message_id) DO NOTHING
                """,
                (message_id, now),
            )
            inserted = cur.rowcount == 1
        return inserted

    def latest_decisions(self) -> dict[str, FeedbackRecord]:
        """Newest decision per URL. LinkedIn drafts do not count as a decision."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM opportunity_feedback
                WHERE verdict IN ('yes', 'no', 'go')
                ORDER BY decided_at DESC, created_at DESC
                """
            ).fetchall()
        latest: dict[str, FeedbackRecord] = {}
        for row in rows:
            record = _feedback_from_row(row)
            if record.fingerprint and record.fingerprint not in latest:
                latest[record.fingerprint] = record
        return latest

    def rejected_fingerprints(self) -> set[str]:
        return {
            fp
            for fp, record in self.latest_decisions().items()
            if record.verdict == "no"
        }

    def rejections(self, *, limit: int = 25) -> list[FeedbackRecord]:
        """Latest No-Go decisions, newest first, for the research prompt."""
        rejected = [
            record
            for record in self.latest_decisions().values()
            if record.verdict == "no" and record.reason.strip()
        ]
        rejected.sort(key=lambda record: record.decided_at or "", reverse=True)
        return rejected[:limit]


def _cached_podcast(
    row: sqlite3.Row | None,
    max_age_days: int,
    *,
    now: datetime | None = None,
) -> PodcastAudience | None:
    if row is None:
        return None
    fetched_at = row["fetched_at"] or ""
    try:
        fetched = datetime.fromisoformat(fetched_at)
    except ValueError:
        return None
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    age = (now or datetime.now(timezone.utc)) - fetched
    if age.days > max_age_days:
        return None
    try:
        data = json.loads(row["payload"])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return PodcastAudience.model_validate(data)


def _fresh_payload(
    payload: str | None,
    fetched_at: str | None,
    max_age_days: int,
    *,
    now: datetime | None = None,
) -> dict | None:
    """Decoded JSON cache payload, or None when it is stale or unreadable."""
    try:
        fetched = datetime.fromisoformat(fetched_at or "")
    except ValueError:
        return None
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    age = (now or datetime.now(timezone.utc)) - fetched
    if age.days > max_age_days:
        return None
    try:
        data = json.loads(payload or "")
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def open_store() -> LocalStore:
    """Local SQLite, or Azure Table Storage when STORAGE_BACKEND=tables."""
    if settings.storage_backend in {"tables", "table", "azure"}:
        from pipeline.table_store import TableStore

        return TableStore()  # type: ignore[return-value]
    return LocalStore()


def _spokesperson_row(row: sqlite3.Row) -> Spokesperson:
    return Spokesperson(
        person_id=row["person_id"],
        firm=row["firm"],
        name=row["name"],
        title=row["title"] or "",
        linkedin=row["linkedin"] or "",
        topics=row["topics"] or "",
        prior_work=row["prior_work"] or "",
        bio=row["bio"] or "",
        email=row["email"],
    )


def _feedback_from_row(row: sqlite3.Row) -> FeedbackRecord:
    return FeedbackRecord(
        token=row["token"],
        person_id=row["person_id"],
        person_name=row["person_name"] or "",
        fingerprint=row["fingerprint"] or "",
        url=row["url"] or "",
        title=row["title"] or "",
        outlet=row["outlet"] or "",
        opportunity_type=row["opportunity_type"] or "",
        why_relevant=row["why_relevant"] or "",
        suggested_angle=row["suggested_angle"] or "",
        verdict=row["verdict"] or "",
        reason=row["reason"] or "",
        created_at=row["created_at"] or "",
        decided_at=row["decided_at"] or "",
        batch_key=_row_text(row, "batch_key"),
        item_number=_row_int(row, "item_number"),
    )


def _row_text(row: sqlite3.Row, name: str) -> str:
    if name not in row.keys() or row[name] is None:
        return ""
    return str(row[name])


def _row_int(row: sqlite3.Row, name: str) -> int:
    if name not in row.keys() or row[name] is None:
        return 0
    try:
        return int(row[name])
    except (TypeError, ValueError):
        return 0


def _action_from_row(row: sqlite3.Row) -> ActionJob:
    return ActionJob(
        job_id=row["job_id"],
        token=row["token"],
        kind=row["kind"],
        status=row["status"] or "",
        error=row["error"] or "",
        created_at=row["created_at"] or "",
        finished_at=row["finished_at"] or "",
    )


_BATCH_KEY = re.compile(r"^emo-[0-9a-f]{8}$")
_STALE_AFTER = timedelta(minutes=30)


def _stale_cutoff() -> str:
    return (datetime.now(timezone.utc) - _STALE_AFTER).isoformat()

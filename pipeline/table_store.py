"""Azure Table Storage adapter with the same methods as LocalStore."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipeline.config import settings
from pipeline.models import (
    Opportunity,
    PodcastAudience,
    RadarMetrics,
    SiteMetrics,
    Spokesperson,
)
from pipeline.storage import ActionJob, FeedbackRecord, _fresh_payload

_BATCH_KEY = re.compile(r"^emo-[0-9a-f]{8}$")
_STALE_AFTER = timedelta(minutes=30)


def _q(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _table_prefix(prefix: str) -> str:
    """Azure table names are alphanumeric and at most 63 characters."""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", prefix or "")
    if not cleaned:
        return ""
    if not cleaned[0].isalpha():
        cleaned = "t" + cleaned
    return cleaned[:40]


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value)


class TableStore:
    def __init__(self, account: str | None = None, *, prefix: str = "") -> None:
        account_name = (account or settings.azure_storage_account).strip()
        if not account_name:
            raise RuntimeError(
                "AZURE_STORAGE_ACCOUNT is required when STORAGE_BACKEND=tables"
            )
        from azure.data.tables import TableServiceClient

        self.account = account_name
        self.prefix = _table_prefix(prefix)
        self.db_path = Path(f"https://{account_name}.table.core.windows.net")
        connection = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "").strip()
        platform = (settings.azure_storage_account or "").strip()
        # A dedicated per-company account is opened with the managed identity.
        # The shared platform account, including per-company tables on it,
        # uses the connection string the Function App already has.
        dedicated = bool((account or "").strip()) and account_name != platform
        if dedicated:
            from azure.identity import DefaultAzureCredential

            service = TableServiceClient(
                endpoint=f"https://{account_name}.table.core.windows.net",
                credential=DefaultAzureCredential(),
            )
        elif connection:
            service = TableServiceClient.from_connection_string(connection)
        else:
            from azure.identity import ManagedIdentityCredential

            service = TableServiceClient(
                endpoint=f"https://{account_name}.table.core.windows.net",
                credential=ManagedIdentityCredential(),
            )

        def table(name: str) -> str:
            return f"{self.prefix}{name}" if self.prefix else name

        self._roster = service.create_table_if_not_exists(table("Roster"))
        self._sent = service.create_table_if_not_exists(table("SentOpportunities"))
        self._semrush = service.create_table_if_not_exists(table("SemrushDomains"))
        self._radar = service.create_table_if_not_exists(table("RadarDomains"))
        self._podcasts = service.create_table_if_not_exists(table("PodcastShows"))
        self._feedback = service.create_table_if_not_exists(table("OpportunityFeedback"))
        self._batches = service.create_table_if_not_exists(table("RoundupBatches"))
        self._actions = service.create_table_if_not_exists(table("ActionJobs"))
        self._ingested = service.create_table_if_not_exists(table("IngestedMessages"))

    def roster_ids(self) -> set[str]:
        return {entity["RowKey"] for entity in self._roster.list_entities()}

    def sync_roster(self, people: list[Spokesperson]) -> tuple[int, list[Spokesperson]]:
        now = datetime.now(timezone.utc).isoformat()
        existing = self.roster_ids()
        newcomers = [person for person in people if person.person_id not in existing]
        incoming_ids = {person.person_id for person in people}

        for person in people:
            first_seen = now
            try:
                current = self._roster.get_entity("roster", person.person_id)
                first_seen = _text(current.get("first_seen_at")) or now
            except _not_found():
                first_seen = now
            self._roster.upsert_entity(
                {
                    "PartitionKey": "roster",
                    "RowKey": person.person_id,
                    "firm": person.firm,
                    "name": person.name,
                    "title": person.title,
                    "linkedin": person.linkedin,
                    "topics": person.topics,
                    "prior_work": person.prior_work,
                    "bio": person.bio,
                    "email": person.email or "",
                    "synced_at": now,
                    "first_seen_at": first_seen,
                },
                mode=_replace(),
            )

        for entity in list(self._roster.list_entities()):
            if entity["RowKey"] not in incoming_ids:
                self._roster.delete_entity(entity["PartitionKey"], entity["RowKey"])
        return len(people), newcomers

    def list_roster(self) -> list[Spokesperson]:
        people = [_spokesperson(entity) for entity in self._roster.list_entities()]
        people.sort(key=lambda person: (person.firm.casefold(), person.name.casefold()))
        return people

    def get_person(self, person_id: str) -> Spokesperson | None:
        try:
            entity = self._roster.get_entity("roster", person_id)
        except _not_found():
            return None
        return _spokesperson(entity)

    def save_person(self, person: Spokesperson) -> None:
        """Insert or update one speaker. Does not remove anyone else."""
        now = datetime.now(timezone.utc).isoformat()
        first_seen = now
        try:
            current = self._roster.get_entity("roster", person.person_id)
            first_seen = _text(current.get("first_seen_at")) or now
        except _not_found():
            first_seen = now
        self._roster.upsert_entity(
            {
                "PartitionKey": "roster",
                "RowKey": person.person_id,
                "firm": person.firm,
                "name": person.name,
                "title": person.title,
                "linkedin": person.linkedin,
                "topics": person.topics,
                "prior_work": person.prior_work,
                "bio": person.bio,
                "email": person.email or "",
                "synced_at": now,
                "first_seen_at": first_seen,
            },
            mode=_replace(),
        )

    def delete_person(self, person_id: str) -> bool:
        """Remove one speaker and the links already sent for them."""
        try:
            self._roster.delete_entity("roster", person_id)
        except _not_found():
            return False
        sent = list(
            self._sent.query_entities(query_filter=f"PartitionKey eq {_q(person_id)}")
        )
        for entity in sent:
            self._sent.delete_entity(entity["PartitionKey"], entity["RowKey"])
        return True

    def already_sent(self, person_id: str, fingerprint: str) -> bool:
        try:
            self._sent.get_entity(person_id, fingerprint)
        except _not_found():
            return False
        return True

    def filter_new(
        self, person_id: str, opportunities: list[Opportunity]
    ) -> list[Opportunity]:
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

    def record_sent(self, person_id: str, opportunities: list[Opportunity]) -> None:
        from azure.core.exceptions import ResourceExistsError

        now = datetime.now(timezone.utc).isoformat()
        for opp in opportunities:
            if not opp.fingerprint:
                continue
            try:
                self._sent.create_entity(
                    {
                        "PartitionKey": person_id,
                        "RowKey": opp.fingerprint,
                        "url": opp.url,
                        "title": opp.title,
                        "outlet": opp.outlet,
                        "sent_at": now,
                    }
                )
            except ResourceExistsError:
                continue

    def sent_count(self, person_id: str | None = None) -> int:
        if person_id:
            rows = self._sent.query_entities(
                query_filter=f"PartitionKey eq {_q(person_id)}"
            )
        else:
            rows = self._sent.list_entities()
        return sum(1 for _ in rows)

    def get_semrush_cache(
        self,
        domain: str,
        max_age_days: int,
        *,
        now: datetime | None = None,
    ) -> SiteMetrics | None:
        try:
            row = self._semrush.get_entity("domain", domain)
        except _not_found():
            return None
        fetched_at = _text(row.get("fetched_at"))
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
            data = json.loads(_text(row.get("payload")))
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
        self._semrush.upsert_entity(
            {
                "PartitionKey": "domain",
                "RowKey": domain,
                "payload": metrics.model_dump_json(),
                "fetched_at": fetched_at,
            },
            mode=_replace(),
        )

    def get_radar_cache(
        self,
        domain: str,
        max_age_days: int,
        *,
        now: datetime | None = None,
    ) -> RadarMetrics | None:
        try:
            row = self._radar.get_entity("domain", domain)
        except _not_found():
            return None
        data = _fresh_payload(
            _text(row.get("payload")),
            _text(row.get("fetched_at")),
            max_age_days,
            now=now,
        )
        return RadarMetrics.model_validate(data) if data is not None else None

    def put_radar_cache(self, metrics: RadarMetrics) -> None:
        domain = (metrics.domain or "").strip().lower()
        if not domain:
            return
        fetched_at = metrics.fetched_at or datetime.now(timezone.utc).isoformat()
        self._radar.upsert_entity(
            {
                "PartitionKey": "domain",
                "RowKey": domain,
                "payload": metrics.model_dump_json(),
                "fetched_at": fetched_at,
            },
            mode=_replace(),
        )

    def get_podcast_cache(
        self,
        query: str,
        max_age_days: int,
        *,
        now: datetime | None = None,
    ) -> PodcastAudience | None:
        try:
            row = self._podcasts.get_entity("show", query)
        except _not_found():
            return None
        fetched_at = _text(row.get("fetched_at"))
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
            data = json.loads(_text(row.get("payload")))
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        return PodcastAudience.model_validate(data)

    def put_podcast_cache(self, audience: PodcastAudience) -> None:
        query = (audience.query or "").strip()
        if not query:
            return
        fetched_at = audience.fetched_at or datetime.now(timezone.utc).isoformat()
        self._podcasts.upsert_entity(
            {
                "PartitionKey": "show",
                "RowKey": query,
                "payload": audience.model_dump_json(),
                "fetched_at": fetched_at,
            },
            mode=_replace(),
        )

    def export_debug(self) -> str:
        return json.dumps(
            {
                "roster": [_plain(entity) for entity in self._roster.list_entities()],
                "sent": [_plain(entity) for entity in self._sent.list_entities()],
                "semrush": [_plain(entity) for entity in self._semrush.list_entities()],
                "radar": [_plain(entity) for entity in self._radar.list_entities()],
                "podcasts": [_plain(entity) for entity in self._podcasts.list_entities()],
                "feedback": [
                    _plain(entity) for entity in self._feedback.list_entities()
                ],
            },
            indent=2,
        )

    def issue_feedback(
        self, person: Spokesperson, opportunities: list[Opportunity]
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        for opp in opportunities:
            if not opp.fingerprint:
                continue
            pending = self._pending_feedback(person.person_id, opp.fingerprint)
            if pending is not None:
                opp.feedback_token = _text(pending.get("RowKey"))
                continue
            token = secrets.token_urlsafe(18)
            self._feedback.create_entity(
                {
                    "PartitionKey": "feedback",
                    "RowKey": token,
                    "person_id": person.person_id,
                    "person_name": person.name,
                    "fingerprint": opp.fingerprint,
                    "url": opp.url,
                    "title": opp.title,
                    "outlet": opp.outlet,
                    "opportunity_type": opp.opportunity_type,
                    "why_relevant": opp.why_relevant,
                    "suggested_angle": opp.suggested_angle,
                    "verdict": "",
                    "reason": "",
                    "created_at": now,
                    "decided_at": "",
                }
            )
            opp.feedback_token = token

    def get_feedback(self, token: str) -> FeedbackRecord | None:
        try:
            row = self._feedback.get_entity("feedback", token)
        except _not_found():
            return None
        return _feedback_from_entity(row)

    def record_feedback(
        self, token: str, verdict: str, reason: str = ""
    ) -> FeedbackRecord | None:
        from pipeline.decisions import stored_decision

        current = self.get_feedback(token)
        if current is None:
            return None
        decided = stored_decision(current.verdict, current.reason, verdict, reason)
        if decided is None:
            return current
        new_verdict, new_reason = decided
        now = datetime.now(timezone.utc).isoformat()
        try:
            entity = dict(self._feedback.get_entity("feedback", token))
        except _not_found():
            return None
        entity["verdict"] = new_verdict
        entity["reason"] = new_reason
        entity["decided_at"] = now
        self._feedback.update_entity(entity, mode=_replace())
        return self.get_feedback(token)

    def save_batch(
        self,
        batch_key: str,
        subject: str,
        reply_to: str,
        opportunities: list[Opportunity],
    ) -> None:
        if not _BATCH_KEY.match(batch_key or ""):
            return
        now = datetime.now(timezone.utc).isoformat()
        self._batches.upsert_entity(
            {
                "PartitionKey": "batch",
                "RowKey": batch_key,
                "subject": subject,
                "reply_to": reply_to or "",
                "created_at": now,
            },
            mode=_replace(),
        )
        for opp in opportunities:
            if not opp.feedback_token or not opp.item_number:
                continue
            try:
                entity = dict(self._feedback.get_entity("feedback", opp.feedback_token))
            except _not_found():
                continue
            entity["batch_key"] = batch_key
            entity["item_number"] = int(opp.item_number)
            self._feedback.update_entity(entity, mode=_replace())

    def get_batch_item(self, batch_key: str, item_number: int) -> FeedbackRecord | None:
        if not _BATCH_KEY.match(batch_key or "") or item_number < 1:
            return None
        rows = list(
            self._feedback.query_entities(
                query_filter=(
                    "PartitionKey eq 'feedback' "
                    f"and batch_key eq {_q(batch_key)} "
                    f"and item_number eq {int(item_number)}"
                )
            )
        )
        if not rows:
            return None
        rows.sort(key=lambda row: _text(row.get("created_at")), reverse=True)
        return _feedback_from_entity(rows[0])

    def batch_reply_to(self, batch_key: str) -> str:
        if not _BATCH_KEY.match(batch_key or ""):
            return ""
        try:
            row = self._batches.get_entity("batch", batch_key)
        except _not_found():
            return ""
        return _text(row.get("reply_to"))

    def enqueue_action(self, token: str, kind: str) -> str:
        if kind not in {"go", "linkedin"}:
            raise ValueError(f"kind must be go or linkedin, got {kind!r}")
        cutoff = _stale_cutoff()
        existing = self._open_action(token, kind)
        if existing is not None:
            created = _text(existing.get("created_at"))
            status = _text(existing.get("status"))
            if status == "queued" or created >= cutoff:
                return _text(existing.get("RowKey"))
            existing["status"] = "queued"
            existing["error"] = ""
            existing["finished_at"] = ""
            self._actions.update_entity(existing, mode=_replace())
            return _text(existing.get("RowKey"))
        job_id = secrets.token_urlsafe(12)
        now = datetime.now(timezone.utc).isoformat()
        self._actions.create_entity(
            {
                "PartitionKey": "action",
                "RowKey": job_id,
                "token": token,
                "kind": kind,
                "status": "queued",
                "error": "",
                "created_at": now,
                "finished_at": "",
            }
        )
        return job_id

    def claim_action(self, job_id: str) -> ActionJob | None:
        try:
            entity = dict(self._actions.get_entity("action", job_id))
        except _not_found():
            return None
        status = _text(entity.get("status"))
        created = _text(entity.get("created_at"))
        if status == "running" and created >= _stale_cutoff():
            return None
        if status not in {"queued", "running"}:
            return None
        entity["status"] = "running"
        self._actions.update_entity(entity, mode=_replace())
        return _action_from_entity(entity)

    def queued_actions(self) -> list[ActionJob]:
        cutoff = _stale_cutoff()
        jobs: list[ActionJob] = []
        for entity in self._actions.query_entities(
            query_filter="PartitionKey eq 'action'"
        ):
            job = _action_from_entity(entity)
            if job.status == "queued" or (
                job.status == "running" and job.created_at < cutoff
            ):
                jobs.append(job)
        jobs.sort(key=lambda job: job.created_at)
        return jobs

    def finish_action(self, job_id: str, *, status: str, error: str = "") -> None:
        if status not in {"sent", "error"}:
            raise ValueError(f"status must be sent or error, got {status!r}")
        try:
            entity = dict(self._actions.get_entity("action", job_id))
        except _not_found():
            return
        entity["status"] = status
        entity["error"] = (error or "")[:2000]
        entity["finished_at"] = datetime.now(timezone.utc).isoformat()
        self._actions.update_entity(entity, mode=_replace())

    def latest_action(self, token: str) -> ActionJob | None:
        rows = self._actions_for_token(token)
        if not rows:
            return None
        rows.sort(key=lambda row: _text(row.get("created_at")), reverse=True)
        return _action_from_entity(rows[0])

    def message_seen(self, message_id: str) -> bool:
        if not message_id:
            return False
        try:
            self._ingested.get_entity("mail", _mail_key(message_id))
        except _not_found():
            return False
        return True

    def remember_message(self, message_id: str) -> bool:
        if not message_id:
            return False
        try:
            self._ingested.create_entity(
                {
                    "PartitionKey": "mail",
                    "RowKey": _mail_key(message_id),
                    "ingested_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        except Exception:
            return False
        return True

    def latest_decisions(self) -> dict[str, FeedbackRecord]:
        records = [
            record
            for record in (
                _feedback_from_entity(entity)
                for entity in self._feedback.list_entities()
            )
            if record.verdict in {"yes", "no", "go"}
        ]
        records.sort(
            key=lambda record: (record.decided_at or "", record.created_at or ""),
            reverse=True,
        )
        latest: dict[str, FeedbackRecord] = {}
        for record in records:
            if record.fingerprint and record.fingerprint not in latest:
                latest[record.fingerprint] = record
        return latest

    def rejected_fingerprints(self) -> set[str]:
        return {
            fingerprint
            for fingerprint, record in self.latest_decisions().items()
            if record.verdict == "no"
        }

    def rejections(self, *, limit: int = 25) -> list[FeedbackRecord]:
        rejected = [
            record
            for record in self.latest_decisions().values()
            if record.verdict == "no" and record.reason.strip()
        ]
        rejected.sort(key=lambda record: record.decided_at or "", reverse=True)
        return rejected[:limit]

    def _pending_feedback(self, person_id: str, fingerprint: str):
        rows = list(
            self._feedback.query_entities(
                query_filter=(
                    "PartitionKey eq 'feedback' "
                    f"and person_id eq {_q(person_id)} "
                    f"and fingerprint eq {_q(fingerprint)} "
                    "and verdict eq ''"
                )
            )
        )
        if not rows:
            return None
        rows.sort(key=lambda row: _text(row.get("created_at")), reverse=True)
        return rows[0]

    def _open_action(self, token: str, kind: str):
        rows = [
            row
            for row in self._actions_for_token(token)
            if _text(row.get("kind")) == kind and _text(row.get("status")) in {"queued", "running"}
        ]
        if not rows:
            return None
        rows.sort(key=lambda row: _text(row.get("created_at")), reverse=True)
        return dict(rows[0])

    def _actions_for_token(self, token: str) -> list:
        return list(
            self._actions.query_entities(
                query_filter=(
                    "PartitionKey eq 'action' "
                    f"and token eq {_q(token)}"
                )
            )
        )


def _not_found() -> type[Exception]:
    from azure.core.exceptions import ResourceNotFoundError

    return ResourceNotFoundError


def _replace():
    from azure.data.tables import UpdateMode

    return UpdateMode.REPLACE


def _spokesperson(entity: dict) -> Spokesperson:
    email = _text(entity.get("email"))
    return Spokesperson(
        person_id=_text(entity.get("RowKey")),
        firm=_text(entity.get("firm")),
        name=_text(entity.get("name")),
        title=_text(entity.get("title")),
        linkedin=_text(entity.get("linkedin")),
        topics=_text(entity.get("topics")),
        prior_work=_text(entity.get("prior_work")),
        bio=_text(entity.get("bio")),
        email=email or None,
    )


def _feedback_from_entity(entity: dict) -> FeedbackRecord:
    return FeedbackRecord(
        token=_text(entity.get("RowKey")),
        person_id=_text(entity.get("person_id")),
        person_name=_text(entity.get("person_name")),
        fingerprint=_text(entity.get("fingerprint")),
        url=_text(entity.get("url")),
        title=_text(entity.get("title")),
        outlet=_text(entity.get("outlet")),
        opportunity_type=_text(entity.get("opportunity_type")),
        why_relevant=_text(entity.get("why_relevant")),
        suggested_angle=_text(entity.get("suggested_angle")),
        verdict=_text(entity.get("verdict")),
        reason=_text(entity.get("reason")),
        created_at=_text(entity.get("created_at")),
        decided_at=_text(entity.get("decided_at")),
        batch_key=_text(entity.get("batch_key")),
        item_number=_as_int(entity.get("item_number")),
    )


def _action_from_entity(entity: dict) -> ActionJob:
    return ActionJob(
        job_id=_text(entity.get("RowKey")),
        token=_text(entity.get("token")),
        kind=_text(entity.get("kind")),
        status=_text(entity.get("status")),
        error=_text(entity.get("error")),
        created_at=_text(entity.get("created_at")),
        finished_at=_text(entity.get("finished_at")),
    )


def _as_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _mail_key(message_id: str) -> str:
    return hashlib.sha256(message_id.encode()).hexdigest()


def _stale_cutoff() -> str:
    return (datetime.now(timezone.utc) - _STALE_AFTER).isoformat()


def _plain(entity: dict) -> dict[str, str]:
    skip = {"etag", "Timestamp"}
    return {key: _text(value) for key, value in entity.items() if key not in skip}

"""Azure Table Storage adapter with the same methods as LocalStore."""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from pipeline.config import settings
from pipeline.models import Opportunity, SiteMetrics, Spokesperson
from pipeline.storage import FeedbackRecord


def _q(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value)


class TableStore:
    def __init__(self, account: str | None = None) -> None:
        account_name = (account or settings.azure_storage_account).strip()
        if not account_name:
            raise RuntimeError(
                "AZURE_STORAGE_ACCOUNT is required when STORAGE_BACKEND=tables"
            )
        from azure.data.tables import TableServiceClient

        self.account = account_name
        self.db_path = Path(f"https://{account_name}.table.core.windows.net")
        connection = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "").strip()
        if connection:
            service = TableServiceClient.from_connection_string(connection)
        else:
            from azure.identity import ManagedIdentityCredential

            service = TableServiceClient(
                endpoint=f"https://{account_name}.table.core.windows.net",
                credential=ManagedIdentityCredential(),
            )
        self._roster = service.create_table_if_not_exists("Roster")
        self._sent = service.create_table_if_not_exists("SentOpportunities")
        self._semrush = service.create_table_if_not_exists("SemrushDomains")
        self._feedback = service.create_table_if_not_exists("OpportunityFeedback")

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

    def export_debug(self) -> str:
        return json.dumps(
            {
                "roster": [_plain(entity) for entity in self._roster.list_entities()],
                "sent": [_plain(entity) for entity in self._sent.list_entities()],
                "semrush": [_plain(entity) for entity in self._semrush.list_entities()],
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
        if verdict not in {"yes", "no"}:
            raise ValueError(f"verdict must be yes or no, got {verdict!r}")
        current = self.get_feedback(token)
        if current is None:
            return None
        cleaned = reason.strip() if verdict == "no" else ""
        now = datetime.now(timezone.utc).isoformat()
        try:
            entity = dict(self._feedback.get_entity("feedback", token))
        except _not_found():
            return None
        entity["verdict"] = verdict
        entity["reason"] = cleaned
        entity["decided_at"] = now
        self._feedback.update_entity(entity, mode=_replace())
        return self.get_feedback(token)

    def latest_decisions(self) -> dict[str, FeedbackRecord]:
        records = [
            record
            for record in (
                _feedback_from_entity(entity)
                for entity in self._feedback.list_entities()
            )
            if record.verdict in {"yes", "no"}
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
    )


def _plain(entity: dict) -> dict[str, str]:
    skip = {"etag", "Timestamp"}
    return {key: _text(value) for key, value in entity.items() if key not in skip}

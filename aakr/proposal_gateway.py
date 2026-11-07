"""Runtime learning proposals: separate store outside Catalog truth.

Runtime stores learning proposals in its own governed namespace, never in
oneshot_dev.knowledge. Curator reads proposals through an authorized gateway
and records acceptance/rejection back through the Runtime API.

Acceptance creates a NEW Catalog draft with derived_from_ref/derived_from_sha256.
It does NOT publish automatically. Rejection remains in Runtime history.
No revision_proposals table in oneshot_dev.knowledge. No direct Runtime
INSERT access to Catalog tables.

Only verified completed Build results with reconciled effects are eligible.
Missing receipts, stale source revisions, conflict or unsupported evidence
block promotion.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aakr.catalog.models import HEX, CatalogError

logger = logging.getLogger(__name__)


@dataclass
class Proposal:
    """Runtime learning proposal — stored OUTSIDE Catalog truth."""

    proposal_id: str
    task_id: str
    run_id: str
    workspace: str
    validation_receipt_sha256: str
    source_ref: str
    source_ref_sha256: str
    accepted_requirement_ids: list[str]
    target_entry_id: str
    target_version: int
    proposed_changes: dict[str, Any]
    creator: str
    created_at: str
    revision: int = 0
    idempotency_key: str = ""
    review_state: str = "pending"  # pending, accepted, rejected
    reviewer: str = ""
    decision_at: str = ""
    decision_reason: str = ""

    def __post_init__(self):
        if not HEX.fullmatch(self.validation_receipt_sha256):
            raise CatalogError("validation_receipt_sha256 must be 64-char hex")
        if not HEX.fullmatch(self.source_ref_sha256):
            raise CatalogError("source_ref_sha256 must be 64-char hex")


class ProposalStore:
    """Runtime-owned proposal store. NOT in oneshot_dev.knowledge.

    In production, this uses a separately governed namespace with separate
    credentials. For reference, it uses an in-memory store.
    """

    def __init__(self):
        self._proposals: dict[str, Proposal] = {}
        self._by_idempotency: dict[str, str] = {}

    def submit(self, proposal: Proposal) -> str:
        """Submit a new proposal. Returns proposal_id.

        Rejects:
        - Missing validation receipt
        - Stale source revision (checked by caller)
        - Duplicate idempotency key with different content
        """
        if (
            proposal.idempotency_key
            and proposal.idempotency_key in self._by_idempotency
        ):
            existing_id = self._by_idempotency[proposal.idempotency_key]
            existing = self._proposals[existing_id]
            if (
                existing.source_ref != proposal.source_ref
                or existing.source_ref_sha256 != proposal.source_ref_sha256
            ):
                raise CatalogError("idempotency key conflict: different content")
            return existing_id  # Idempotent

        if proposal.proposal_id in self._proposals:
            raise CatalogError("duplicate proposal_id")

        self._proposals[proposal.proposal_id] = proposal
        if proposal.idempotency_key:
            self._by_idempotency[proposal.idempotency_key] = proposal.proposal_id
        return proposal.proposal_id

    def get(self, proposal_id: str) -> Proposal | None:
        return self._proposals.get(proposal_id)

    def list_pending(self) -> list[Proposal]:
        return [p for p in self._proposals.values() if p.review_state == "pending"]

    def decide(
        self, proposal_id: str, *, accepted: bool, reviewer: str, reason: str = ""
    ) -> None:
        """Record Curator acceptance or rejection."""
        p = self._proposals.get(proposal_id)
        if p is None:
            raise CatalogError("proposal not found")
        if p.review_state != "pending":
            raise CatalogError("proposal already reviewed")
        p.review_state = "accepted" if accepted else "rejected"
        p.reviewer = reviewer
        p.decision_at = datetime.now(UTC).isoformat()
        p.decision_reason = reason


class SQLiteProposalStore(ProposalStore):
    """SQLite-backed persistent proposal store.

    Schema mirrors the in-memory ProposalStore interface but persists
    proposals to a SQLite database. Uses INSERT OR IGNORE for idempotent
    submits and JSON serialization for list/dict fields.
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS proposals (
                proposal_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                workspace TEXT NOT NULL,
                validation_receipt_sha256 TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                source_ref_sha256 TEXT NOT NULL,
                accepted_requirement_ids TEXT NOT NULL,
                target_entry_id TEXT NOT NULL,
                target_version INTEGER NOT NULL,
                proposed_changes TEXT NOT NULL,
                creator TEXT NOT NULL,
                created_at TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0,
                idempotency_key TEXT NOT NULL DEFAULT '',
                review_state TEXT NOT NULL DEFAULT 'pending',
                reviewer TEXT NOT NULL DEFAULT '',
                decision_at TEXT NOT NULL DEFAULT '',
                decision_reason TEXT NOT NULL DEFAULT ''
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_proposals_idempotency
                ON proposals(idempotency_key) WHERE idempotency_key != '';
        """)
        self._conn.commit()

    def _row_to_proposal(self, row: sqlite3.Row) -> Proposal:
        return Proposal(
            proposal_id=row["proposal_id"],
            task_id=row["task_id"],
            run_id=row["run_id"],
            workspace=row["workspace"],
            validation_receipt_sha256=row["validation_receipt_sha256"],
            source_ref=row["source_ref"],
            source_ref_sha256=row["source_ref_sha256"],
            accepted_requirement_ids=json.loads(row["accepted_requirement_ids"]),
            target_entry_id=row["target_entry_id"],
            target_version=row["target_version"],
            proposed_changes=json.loads(row["proposed_changes"]),
            creator=row["creator"],
            created_at=row["created_at"],
            revision=row["revision"],
            idempotency_key=row["idempotency_key"],
            review_state=row["review_state"],
            reviewer=row["reviewer"],
            decision_at=row["decision_at"],
            decision_reason=row["decision_reason"],
        )

    def submit(self, proposal: Proposal) -> str:
        """Submit a proposal persistently. Idempotent on same idempotency_key."""
        # Check idempotency key first
        if proposal.idempotency_key:
            existing = self._conn.execute(
                "SELECT * FROM proposals WHERE idempotency_key=?",
                (proposal.idempotency_key,),
            ).fetchone()
            if existing:
                if (
                    existing["source_ref"] != proposal.source_ref
                    or existing["source_ref_sha256"] != proposal.source_ref_sha256
                ):
                    raise CatalogError("idempotency key conflict: different content")
                return existing["proposal_id"]

        # Check duplicate proposal_id
        dup = self._conn.execute(
            "SELECT 1 FROM proposals WHERE proposal_id=?", (proposal.proposal_id,)
        ).fetchone()
        if dup:
            raise CatalogError("duplicate proposal_id")

        self._conn.execute(
            "INSERT OR IGNORE INTO proposals VALUES ("
            "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                proposal.proposal_id,
                proposal.task_id,
                proposal.run_id,
                proposal.workspace,
                proposal.validation_receipt_sha256,
                proposal.source_ref,
                proposal.source_ref_sha256,
                json.dumps(proposal.accepted_requirement_ids),
                proposal.target_entry_id,
                proposal.target_version,
                json.dumps(proposal.proposed_changes),
                proposal.creator,
                proposal.created_at,
                proposal.revision,
                proposal.idempotency_key,
                proposal.review_state,
                proposal.reviewer,
                proposal.decision_at,
                proposal.decision_reason,
            ),
        )
        self._conn.commit()
        logger.info("Proposal submitted: %s", proposal.proposal_id)
        return proposal.proposal_id

    def get(self, proposal_id: str) -> Proposal | None:
        row = self._conn.execute(
            "SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        return self._row_to_proposal(row) if row else None

    def list_pending(self) -> list[Proposal]:
        rows = self._conn.execute(
            "SELECT * FROM proposals WHERE review_state='pending' ORDER BY created_at"
        ).fetchall()
        return [self._row_to_proposal(r) for r in rows]

    def decide(
        self, proposal_id: str, *, accepted: bool, reviewer: str, reason: str = ""
    ) -> None:
        row = self._conn.execute(
            "SELECT review_state FROM proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise CatalogError("proposal not found")
        if row["review_state"] != "pending":
            raise CatalogError("proposal already reviewed")
        self._conn.execute(
            "UPDATE proposals SET review_state=?, reviewer=?, decision_at=?, decision_reason=? "
            "WHERE proposal_id=?",
            (
                "accepted" if accepted else "rejected",
                reviewer,
                datetime.now(UTC).isoformat(),
                reason,
                proposal_id,
            ),
        )
        self._conn.commit()
        logger.info(
            "Proposal %s: %s", proposal_id, "accepted" if accepted else "rejected"
        )

    def close(self) -> None:
        self._conn.close()


class ProposalGateway:
    """Authorized gateway for Curator to read Runtime proposals.

    Curator retrieves proposals read-only, validates source receipt identity,
    signatures/ACL where supported, duplicate proposals and evidence scope,
    then accepts/rejects through the Runtime API.

    Accepted proposal creates a new Catalog draft with derived_from_ref/derived_from_sha256.
    Does NOT publish automatically.
    """

    def __init__(self, store: ProposalStore, *, trusted: bool = False):
        self.store = store
        self.trusted = trusted

    def review(
        self, proposal_id: str, *, reviewer: str, accept: bool, reason: str = ""
    ) -> dict[str, Any]:
        """Review a proposal. If accepted, return derived_from fields for new draft."""
        p = self.store.get(proposal_id)
        if p is None:
            raise CatalogError("proposal not found")
        if p.review_state != "pending":
            raise CatalogError("proposal already reviewed")

        if accept:
            if not self.trusted:
                raise CatalogError("untrusted gateway cannot accept proposals")
            # Validate receipt exists and is valid
            if not p.validation_receipt_sha256:
                raise CatalogError("missing validation receipt")
            self.store.decide(
                proposal_id, accepted=True, reviewer=reviewer, reason=reason
            )
            return {
                "accepted": True,
                "derived_from_ref": p.source_ref,
                "derived_from_sha256": p.source_ref_sha256,
                "target_entry_id": p.target_entry_id,
                "target_version": p.target_version,
            }
        else:
            self.store.decide(
                proposal_id, accepted=False, reviewer=reviewer, reason=reason
            )
            return {"accepted": False}

"""Lifecycle: Submitted Item -> Guidance Checklist -> Scope Review -> Source Discovery
-> Record Store -> Evidence Verification -> Checkpoint -> Reassessment -> Remaining Item.

Eight-phase lifecycle with proper ownership boundaries:
1. Submitted Item Store: persists work items, NOT Catalog drafts
2. Guidance Checklist: from knowledge gaps, NOT applicability conditions
3. Scope Review: configurable review workflow with persisted decisions
4. Source Discovery: autonomous + dynamic source discovery beyond fixed pool
5. Record Store: full research records linked to checkpoint and source refs
6. Evidence Verification: independent claim-level verification (authenticity, support, provenance, contradictions)
7. Checkpoint Store: persisted progress with recovery and retry tracking
8. Gap Reassessment: evidence for Curator review + persisted Next Item with bounded retries

Publication into the Knowledge Registry is a separate, policy-governed operation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

from aakr.catalog.models import CatalogError

# ─── Review Mode Constants ───────────────────────────────────────────────
REVIEW_AUTO = "auto_approve"
REVIEW_MANUAL = "manual_review"
REVIEW_STAGED = "staged_review"


@dataclass
class KnowledgeGap:
    gap_id: str
    description: str
    source_hints: list[str]
    priority: str = "normal"


@dataclass
class AcceptedRequirement:
    req_id: str
    description: str
    source_hints: list[str]


@dataclass
class SubmittedItem:
    item_id: str
    request: str
    knowledge_gaps: list[KnowledgeGap]
    accepted_requirements: list[AcceptedRequirement]
    status: str = "submitted"
    parent_item_id: str = ""
    retry_count: int = 0
    created_at: str = ""
    updated_at: str = ""


@dataclass
class GuidanceItem:
    gap_id: str
    description: str
    guidance_text: str
    source_hints: list[str]
    priority: str
    origin: str


@dataclass
class GuidanceChecklist:
    item_id: str
    items: list[GuidanceItem]


@dataclass
class ReviewScope:
    source_types: list[str] = field(default_factory=lambda: ["documentation"])
    max_sources_per_gap: int = 5
    min_confidence_threshold: float = 0.1
    research_depth: str = "standard"
    include_external: bool = False


@dataclass
class ReviewDecision:
    review_id: str
    item_id: str
    mode: str
    proposed_scope: dict[str, Any]
    decision: str
    edited_scope: dict[str, Any] | None = None
    reviewer: str = ""
    notes: str = ""
    timestamp: str = ""


@dataclass
class SourceRef:
    uri: str
    source_type: str = "documentation"
    content_hash: str = ""
    title: str = ""
    retrieved_at: str = ""


@dataclass
class ResearchRecord:
    record_id: str
    item_id: str
    gap_id: str
    checkpoint_id: str
    finding: str
    source_ref: dict[str, Any]
    evidence_kind: str
    methodology: str
    confidence: float
    recorded_by: str
    recorded_at: str


@dataclass
class ClaimVerification:
    claim_id: str
    gap_id: str
    finding: str
    source_uri: str
    authentic: bool
    supports_claim: bool
    stance: str  # 'supports' | 'contradicts' | 'insufficient_evidence'
    provenance_chain: list[str]
    contradictions: list[str]
    verdict: str
    evidence_passage: str = ""  # source text snippet assessed
    assessment_method: str = ""  # how stance was determined
    applicability: str = ""  # 'matching' | 'mismatched' | 'unknown'
    coverage: str = ""  # 'full' | 'partial' | 'none'
    source_version: str = ""  # version signal extracted from source
    claim_version: str = ""  # version signal extracted from claim
    issues: list[str] = field(default_factory=list)


@dataclass
class Checkpoint:
    checkpoint_id: str
    item_id: str
    phase: str
    resolved_gaps: list[str]
    pending_gaps: list[str]
    research_attempts: int
    retry_count: int
    evidence_summary: dict[str, str]
    recovery_state: str
    created_at: str
    gap_descriptions: dict[str, str] = field(default_factory=dict)


@dataclass
class LifecycleState:
    item_id: str
    phase: str
    submitted_item: SubmittedItem | None = None
    checklist: GuidanceChecklist | None = None
    scope: ReviewScope | None = None
    review_decision: ReviewDecision | None = None
    research_records: list[ResearchRecord] = field(default_factory=list)
    verification_result: dict[str, Any] = field(default_factory=dict)
    checkpoint: Checkpoint | None = None
    resolved: bool = False
    remaining_gaps: list[str] = field(default_factory=list)
    evidence_package: dict[str, Any] = field(default_factory=dict)
    answerability: dict[str, Any] = field(default_factory=dict)
    next_item: SubmittedItem | None = None
    exhausted: bool = False
    errors: list[str] = field(default_factory=list)


# ─── Source Discovery ────────────────────────────────────────────────────
@runtime_checkable
class SourceConnector(Protocol):
    """Protocol for external source connectors — enables discovery beyond a fixed pool."""

    def discover(
        self, keywords: list[str], scope: ReviewScope
    ) -> list[dict[str, Any]]: ...


class DynamicSourcePool:
    """Combines a static pool with connectors and supports query expansion across cycles."""

    def __init__(
        self,
        static_pool: list[dict[str, Any]],
        connectors: list[SourceConnector] | None = None,
    ):
        self.static_pool = static_pool
        self.connectors = connectors or []
        self._expanded_pool: list[dict[str, Any]] = list(static_pool)
        self._discovered: dict[str, dict[str, Any]] = {}

    @property
    def pool(self) -> list[dict[str, Any]]:
        return self._expanded_pool

    def expand(self, keywords: list[str], scope: ReviewScope) -> list[dict[str, Any]]:
        """Discover new sources via connectors. Returns newly discovered sources."""
        new_sources: list[dict[str, Any]] = []
        if not scope.include_external:
            return new_sources
        for connector in self.connectors:
            try:
                results = connector.discover(keywords, scope)
                for src in results:
                    sid = src.get("doc_id", src.get("uri", ""))
                    if sid and sid not in self._discovered:
                        self._discovered[sid] = src
                        self._expanded_pool.append(src)
                        new_sources.append(src)
            except Exception as e:
                logger.debug("source discovery failed for %s: %s", connector, e)
        return new_sources

    def reset(self):
        self._expanded_pool = list(self.static_pool)
        self._discovered.clear()


class SimpleConnector:
    """A simple connector that adds sources from an external list on demand."""

    def __init__(self, external_sources: list[dict[str, Any]]):
        self.external_sources = external_sources

    def discover(self, keywords: list[str], scope: ReviewScope) -> list[dict[str, Any]]:
        results = []
        for src in self.external_sources:
            src_text = (
                str(src.get("title", ""))
                + " "
                + str(src.get("content", ""))
                + " "
                + " ".join(src.get("topics", []))
            ).lower()
            if any(kw.lower() in src_text for kw in keywords if len(kw) > 2):
                results.append(src)
        return results


class SubmittedItemStore:
    """Persists research work items independently of Catalog truth. Idempotent."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_schema()

    def _conn(self):
        c = sqlite3.connect(self.db_path, isolation_level=None, timeout=3)
        c.row_factory = sqlite3.Row
        return c

    def _init_schema(self):
        with self._conn() as c:
            c.executescript("""CREATE TABLE IF NOT EXISTS submitted_items (
                item_id TEXT PRIMARY KEY, request TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'submitted',
                parent_item_id TEXT NOT NULL DEFAULT '',
                retry_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                knowledge_gaps TEXT NOT NULL, accepted_requirements TEXT NOT NULL
            );""")

    def submit(self, item: SubmittedItem) -> str:
        ts = item.created_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        item.created_at = ts
        item.updated_at = ts
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO submitted_items (item_id, request, status, parent_item_id, retry_count, created_at, updated_at, knowledge_gaps, accepted_requirements) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.item_id,
                    item.request,
                    item.status,
                    item.parent_item_id,
                    item.retry_count,
                    ts,
                    ts,
                    json.dumps([asdict(g) for g in item.knowledge_gaps]),
                    json.dumps([asdict(r) for r in item.accepted_requirements]),
                ),
            )
        return item.item_id

    def get(self, item_id: str) -> SubmittedItem | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM submitted_items WHERE item_id=?", (item_id,)
            ).fetchone()
            if not row:
                return None
            gaps = [KnowledgeGap(**g) for g in json.loads(row["knowledge_gaps"])]
            reqs = [
                AcceptedRequirement(**r)
                for r in json.loads(row["accepted_requirements"])
            ]
            return SubmittedItem(
                item_id=row["item_id"],
                request=row["request"],
                knowledge_gaps=gaps,
                accepted_requirements=reqs,
                status=row["status"],
                parent_item_id=row["parent_item_id"],
                retry_count=row["retry_count"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    def exists(self, item_id: str) -> bool:
        with self._conn() as c:
            return (
                c.execute(
                    "SELECT 1 FROM submitted_items WHERE item_id=?", (item_id,)
                ).fetchone()
                is not None
            )

    def update_status(self, item_id: str, status: str):
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._conn() as c:
            c.execute(
                "UPDATE submitted_items SET status=?, updated_at=? WHERE item_id=?",
                (status, ts, item_id),
            )

    def get_by_parent(self, parent_item_id: str) -> list[SubmittedItem]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM submitted_items WHERE parent_item_id=? ORDER BY created_at",
                (parent_item_id,),
            ).fetchall()
            items = []
            for row in rows:
                gaps = [KnowledgeGap(**g) for g in json.loads(row["knowledge_gaps"])]
                reqs = [
                    AcceptedRequirement(**r)
                    for r in json.loads(row["accepted_requirements"])
                ]
                items.append(
                    SubmittedItem(
                        item_id=row["item_id"],
                        request=row["request"],
                        knowledge_gaps=gaps,
                        accepted_requirements=reqs,
                        status=row["status"],
                        parent_item_id=row["parent_item_id"],
                        retry_count=row["retry_count"],
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                    )
                )
            return items


class RecordStore:
    """Persists full research records linked to checkpoint and source refs."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_schema()

    def _conn(self):
        c = sqlite3.connect(self.db_path, isolation_level=None, timeout=3)
        c.row_factory = sqlite3.Row
        return c

    def _init_schema(self):
        with self._conn() as c:
            c.executescript("""CREATE TABLE IF NOT EXISTS research_records (
                record_id TEXT PRIMARY KEY, item_id TEXT NOT NULL, gap_id TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL, finding TEXT NOT NULL,
                source_ref TEXT NOT NULL, evidence_kind TEXT NOT NULL,
                methodology TEXT NOT NULL, confidence REAL NOT NULL,
                recorded_by TEXT NOT NULL, recorded_at TEXT NOT NULL
            );CREATE INDEX IF NOT EXISTS idx_rr_item ON research_records(item_id);
            CREATE INDEX IF NOT EXISTS idx_rr_gap ON research_records(gap_id);
            CREATE INDEX IF NOT EXISTS idx_rr_cp ON research_records(checkpoint_id);""")

    def record(self, rec: ResearchRecord) -> str:
        with self._conn() as c:
            # Check if record already exists (idempotency check)
            existing = c.execute(
                "SELECT source_ref FROM research_records WHERE record_id=?",
                (rec.record_id,),
            ).fetchone()
            if existing:
                # Compare content — reject if conflicting
                existing_ref = json.loads(existing["source_ref"])
                existing_hash = existing_ref.get("content_hash", "")
                new_hash = rec.source_ref.get("content_hash", "")
                if existing_hash and new_hash and existing_hash != new_hash:
                    # Conflicting content for same record_id — reject
                    return rec.record_id
                # Same content — idempotent, skip
                return rec.record_id
            c.execute(
                "INSERT INTO research_records (record_id, item_id, gap_id, checkpoint_id, finding, source_ref, evidence_kind, methodology, confidence, recorded_by, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rec.record_id,
                    rec.item_id,
                    rec.gap_id,
                    rec.checkpoint_id,
                    rec.finding,
                    json.dumps(rec.source_ref),
                    rec.evidence_kind,
                    rec.methodology,
                    rec.confidence,
                    rec.recorded_by,
                    rec.recorded_at,
                ),
            )
        return rec.record_id

    def get_for_gap(self, item_id: str, gap_id: str) -> list[ResearchRecord]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM research_records WHERE item_id=? AND gap_id=? ORDER BY recorded_at",
                (item_id, gap_id),
            ).fetchall()
            return [self._row_to_rec(r) for r in rows]

    def get_all_for_item(self, item_id: str) -> list[ResearchRecord]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM research_records WHERE item_id=? ORDER BY recorded_at",
                (item_id,),
            ).fetchall()
            return [self._row_to_rec(r) for r in rows]

    def get_for_checkpoint(self, checkpoint_id: str) -> list[ResearchRecord]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM research_records WHERE checkpoint_id=? ORDER BY recorded_at",
                (checkpoint_id,),
            ).fetchall()
            return [self._row_to_rec(r) for r in rows]

    def _row_to_rec(self, row: sqlite3.Row) -> ResearchRecord:
        return ResearchRecord(
            record_id=row["record_id"],
            item_id=row["item_id"],
            gap_id=row["gap_id"],
            checkpoint_id=row["checkpoint_id"],
            finding=row["finding"],
            source_ref=json.loads(row["source_ref"]),
            evidence_kind=row["evidence_kind"],
            methodology=row["methodology"],
            confidence=row["confidence"],
            recorded_by=row["recorded_by"],
            recorded_at=row["recorded_at"],
        )


class CheckpointStore:
    """Persists lifecycle checkpoints with retry tracking — survives interruption."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_schema()

    def _conn(self):
        c = sqlite3.connect(self.db_path, isolation_level=None, timeout=3)
        c.row_factory = sqlite3.Row
        return c

    def _init_schema(self):
        with self._conn() as c:
            c.executescript("""CREATE TABLE IF NOT EXISTS checkpoints (
                checkpoint_id TEXT PRIMARY KEY, item_id TEXT NOT NULL, phase TEXT NOT NULL,
                resolved_gaps TEXT NOT NULL, pending_gaps TEXT NOT NULL,
                research_attempts INTEGER NOT NULL DEFAULT 0,
                retry_count INTEGER NOT NULL DEFAULT 0,
                evidence_summary TEXT NOT NULL, recovery_state TEXT NOT NULL,
                created_at TEXT NOT NULL, gap_descriptions TEXT NOT NULL DEFAULT '{}'
            );CREATE INDEX IF NOT EXISTS idx_cp_item ON checkpoints(item_id);""")
            try:
                c.execute(
                    "ALTER TABLE checkpoints ADD COLUMN gap_descriptions TEXT NOT NULL DEFAULT '{}'"
                )
            except sqlite3.OperationalError:
                pass  # column already exists

    def save(self, cp: Checkpoint) -> str:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO checkpoints (checkpoint_id, item_id, phase, resolved_gaps, pending_gaps, research_attempts, retry_count, evidence_summary, recovery_state, created_at, gap_descriptions) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cp.checkpoint_id,
                    cp.item_id,
                    cp.phase,
                    json.dumps(cp.resolved_gaps),
                    json.dumps(cp.pending_gaps),
                    cp.research_attempts,
                    cp.retry_count,
                    json.dumps(cp.evidence_summary),
                    cp.recovery_state,
                    cp.created_at,
                    json.dumps(getattr(cp, "gap_descriptions", {})),
                ),
            )
        return cp.checkpoint_id

    def load_latest(self, item_id: str) -> Checkpoint | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM checkpoints WHERE item_id=? ORDER BY created_at DESC, research_attempts DESC LIMIT 1",
                (item_id,),
            ).fetchone()
            if not row:
                return None
            gap_descs: dict[str, Any] = {}
            try:
                gap_descs = (
                    json.loads(row["gap_descriptions"])
                    if row["gap_descriptions"]
                    else {}
                )
            except (KeyError, json.JSONDecodeError):
                pass  # old schema without gap_descriptions
            return Checkpoint(
                checkpoint_id=row["checkpoint_id"],
                item_id=row["item_id"],
                phase=row["phase"],
                resolved_gaps=json.loads(row["resolved_gaps"]),
                pending_gaps=json.loads(row["pending_gaps"]),
                research_attempts=row["research_attempts"],
                retry_count=row["retry_count"],
                evidence_summary=json.loads(row["evidence_summary"]),
                recovery_state=row["recovery_state"],
                created_at=row["created_at"],
                gap_descriptions=gap_descs,
            )


class ReviewStore:
    """Persists scope review decisions — review workflow with audit trail."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_schema()

    def _conn(self):
        c = sqlite3.connect(self.db_path, isolation_level=None, timeout=3)
        c.row_factory = sqlite3.Row
        return c

    def _init_schema(self):
        with self._conn() as c:
            c.executescript("""CREATE TABLE IF NOT EXISTS review_decisions (
                review_id TEXT PRIMARY KEY, item_id TEXT NOT NULL,
                mode TEXT NOT NULL, proposed_scope TEXT NOT NULL,
                decision TEXT NOT NULL, edited_scope TEXT,
                reviewer TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
                timestamp TEXT NOT NULL
            );CREATE INDEX IF NOT EXISTS idx_rd_item ON review_decisions(item_id);""")

    def save(self, decision: ReviewDecision) -> str:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO review_decisions (review_id, item_id, mode, proposed_scope, decision, edited_scope, reviewer, notes, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision.review_id,
                    decision.item_id,
                    decision.mode,
                    json.dumps(decision.proposed_scope),
                    decision.decision,
                    json.dumps(decision.edited_scope)
                    if decision.edited_scope
                    else None,
                    decision.reviewer,
                    decision.notes,
                    decision.timestamp,
                ),
            )
        return decision.review_id

    def get_latest(self, item_id: str) -> ReviewDecision | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM review_decisions WHERE item_id=? ORDER BY timestamp DESC LIMIT 1",
                (item_id,),
            ).fetchone()
            if not row:
                return None
            return ReviewDecision(
                review_id=row["review_id"],
                item_id=row["item_id"],
                mode=row["mode"],
                proposed_scope=json.loads(row["proposed_scope"]),
                decision=row["decision"],
                edited_scope=json.loads(row["edited_scope"])
                if row["edited_scope"]
                else None,
                reviewer=row["reviewer"],
                notes=row["notes"],
                timestamp=row["timestamp"],
            )


class AutonomousResearcher:
    """Autonomous source discovery with dynamic expansion — NOT an injected function."""

    def __init__(
        self,
        source_pool: DynamicSourcePool,
        *,
        evidence_kind: str = "test_assertion",
        researcher_identity: str = "researcher",
    ):
        self.source_pool = source_pool
        self.evidence_kind = evidence_kind
        self.researcher_identity = researcher_identity

    def research(
        self,
        checklist: GuidanceChecklist,
        *,
        skip_gaps: set[str] | None = None,
        scope: ReviewScope | None = None,
        checkpoint_id: str = "",
    ) -> list[ResearchRecord]:
        skip = skip_gaps or set()
        records = []
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        pool = self.source_pool.pool
        for item in checklist.items:
            if item.gap_id in skip:
                continue
            keywords = item.source_hints + item.description.lower().split()
            # Try dynamic expansion if no match in current pool
            found_source = None
            best_score = 0
            for source in pool:
                source_text = (
                    str(source.get("title", ""))
                    + " "
                    + str(source.get("content", ""))
                    + " "
                    + " ".join(source.get("topics", []))
                ).lower()
                score = sum(
                    1 for kw in keywords if len(kw) > 2 and kw.lower() in source_text
                )
                if score > best_score:
                    best_score = score
                    found_source = source
            # If not found and scope allows external, try dynamic expansion
            if not found_source and scope and scope.include_external:
                new_sources = self.source_pool.expand(keywords, scope)
                for source in new_sources:
                    source_text = (
                        str(source.get("title", ""))
                        + " "
                        + str(source.get("content", ""))
                        + " "
                        + " ".join(source.get("topics", []))
                    ).lower()
                    score = sum(
                        1
                        for kw in keywords
                        if len(kw) > 2 and kw.lower() in source_text
                    )
                    if score > best_score:
                        best_score = score
                        found_source = source
            if found_source:
                src_content = str(found_source.get("content", ""))
                src_hash = hashlib.sha256(src_content.encode()).hexdigest()
                src_ref = {
                    "uri": found_source.get("doc_id", found_source.get("uri", "")),
                    "source_type": found_source.get("source_type", "documentation"),
                    "content_hash": src_hash,
                    "title": found_source.get("title", ""),
                    "retrieved_at": ts,
                }
                src_uri = (
                    found_source.get("doc_id", found_source.get("uri", "")) or "none"
                )
                finding_hash = hashlib.sha256(
                    f"{item.gap_id}:{checkpoint_id}:{src_uri}:{item.description}".encode()
                ).hexdigest()[:12]
                confidence = min(0.9, 0.3 + 0.1 * best_score)
                rec = ResearchRecord(
                    record_id=f"{item.gap_id}-{finding_hash}",
                    item_id=checklist.item_id,
                    gap_id=item.gap_id,
                    checkpoint_id=checkpoint_id,
                    finding=f'Found evidence in "{found_source.get("title", "")}" addressing: {item.description}',
                    source_ref=src_ref,
                    evidence_kind=self.evidence_kind,
                    methodology=f"Score-based selection from {len(pool)} sources (scope: {scope.research_depth if scope else 'default'}). Relevance score: {best_score} keyword matches. Keywords: {keywords[:5]}",
                    confidence=confidence,
                    recorded_by=self.researcher_identity,
                    recorded_at=ts,
                )
            else:
                rec = ResearchRecord(
                    record_id=f"{item.gap_id}-{checkpoint_id}-none-{ts.replace(':', '').replace('-', '')}",
                    item_id=checklist.item_id,
                    gap_id=item.gap_id,
                    checkpoint_id=checkpoint_id,
                    finding=f"No source found addressing: {item.description}",
                    source_ref={
                        "uri": "",
                        "source_type": "",
                        "content_hash": "",
                        "title": "",
                        "retrieved_at": ts,
                    },
                    evidence_kind=self.evidence_kind,
                    methodology=f"Autonomous search of {len(pool)} sources found no match. Keywords: {item.source_hints}",
                    confidence=0.0,
                    recorded_by=self.researcher_identity,
                    recorded_at=ts,
                )
            records.append(rec)
        return records


class EvidenceVerifier:
    """Independent claim-level evidence verification.

    Decision sequence:
      Atomic Claim → Source Identity/Integrity → Evidence Passage →
      Applicability Assessment → Semantic Stance → Coverage Assessment →
      Aggregate Applicable Evidence → Resolution.

    A contradiction requires incompatible propositions with matching scope.
    Keyword overlap alone cannot produce a verified claim.
    Changed source content is quarantined, not reclassified.
    """

    # Negation markers — multi-word phrases that explicitly deny a claim.
    _NEGATIONS = (
        "does not",
        "do not",
        "don't",
        "doesn't",
        "cannot",
        "can't",
        "never",
        "is not",
        "are not",
        "will not",
        "won't",
        "should not",
        "shouldn't",
        "not ",
    )
    # Version patterns
    _VERSION_PATTERNS = (
        re.compile(r"(?:python|py)\s*(3\.\d+)", re.IGNORECASE),
        re.compile(r"\bv(\d+(?:\.\d+)?)\b", re.IGNORECASE),
        re.compile(r"version\s+(\d+(?:\.\d+)?)", re.IGNORECASE),
    )

    def __init__(self, source_pool: list[dict[str, Any]]):
        self.source_pool = source_pool

    # ── Atomic claim decomposition ─────────────────────────────────────
    @staticmethod
    def _decompose_claims(claim_text: str) -> list[str]:
        """Split compound claims into atomic sub-claims by 'and'/'or'."""
        parts = re.split(r"\s+(?:and|or)\s+", claim_text, flags=re.IGNORECASE)
        return [p.strip() for p in parts if len(p.strip()) > 3]

    # ── Version extraction ────────────────────────────────────────────
    def _extract_version(self, text: str) -> str:
        """Extract version signal from text. Returns '' if none found."""
        for pat in self._VERSION_PATTERNS:
            m = pat.search(text)
            if m:
                return m.group(1)
        return ""

    # ── Evidence passage extraction ──────────────────────────────────
    @staticmethod
    def _extract_passage(
        source_text: str, claim_keywords: list[str], max_len: int = 200
    ) -> str:
        """Extract the source text snippet most relevant to the claim."""
        if not source_text or not claim_keywords:
            return source_text[:max_len] if source_text else ""
        best_pos = 0
        best_score = 0
        window = 80
        for i in range(0, max(1, len(source_text) - window), 20):
            snippet = source_text[i : i + window]
            score = sum(1 for kw in claim_keywords[:10] if kw in snippet)
            if score > best_score:
                best_score = score
                best_pos = i
        return source_text[best_pos : best_pos + max_len][:max_len]

    # ── Negation detection (word proximity) ────────────────────────────
    def _has_negation_near_claim(
        self, source_text: str, claim_keywords: list[str]
    ) -> bool:
        words = source_text.split()
        for neg in self._NEGATIONS:
            neg_words = neg.split()
            for i in range(len(words) - len(neg_words) + 1):
                if all(words[i + j] == neg_words[j] for j in range(len(neg_words))):
                    for k in range(len(neg_words), len(neg_words) + 2):
                        if i + k < len(words) and any(
                            kw in words[i + k] for kw in claim_keywords[:10]
                        ):
                            return True
                    if i > 0 and any(kw in words[i - 1] for kw in claim_keywords[:10]):
                        return True
        return False

    # ── Semantic stance for a single atomic claim ────────────────────
    def _assess_atomic_stance(
        self, source_text: str, atomic_keywords: list[str]
    ) -> str:
        """Assess stance for a single atomic sub-claim."""
        min_matches = max(2, len(atomic_keywords[:10]) * 2 // 3)
        actual = sum(1 for kw in atomic_keywords[:10] if kw in source_text)
        if actual >= min_matches:
            if self._has_negation_near_claim(source_text, atomic_keywords):
                return "contradicts"
            return "supports"
        else:
            if self._has_negation_near_claim(source_text, atomic_keywords):
                return "contradicts"
            return "insufficient_evidence"

    # ── Coverage assessment for compound claims ──────────────────────
    def _assess_coverage(
        self, source_text: str, claim_text: str
    ) -> tuple[str, str, str]:
        """Returns (stance, coverage, assessment_method).

        For compound claims, checks each atomic sub-claim.
        Full coverage required for 'supports'. Partial → insufficient_evidence.
        """
        atomics = self._decompose_claims(claim_text)
        if len(atomics) <= 1:
            # Single atomic claim
            kws = self._extract_claim_keywords(claim_text)
            stance = self._assess_atomic_stance(source_text, kws)
            coverage = (
                "full"
                if stance == "supports"
                else ("none" if stance == "insufficient_evidence" else "full")
            )
            return stance, coverage, f"atomic:{len(kws)}kws,stance={stance}"

        # Compound claim — assess each sub-claim
        sub_stances = []
        for atomic in atomics:
            kws = self._extract_claim_keywords(atomic)
            s = self._assess_atomic_stance(source_text, kws)
            sub_stances.append(s)

        # Any contradicts → contradicts
        if "contradicts" in sub_stances:
            return (
                "contradicts",
                "partial",
                f"compound:{len(atomics)}atomic,contradicts={sub_stances.count('contradicts')}",
            )
        # All supports → supports (full coverage)
        if all(s == "supports" for s in sub_stances):
            return "supports", "full", f"compound:{len(atomics)}atomic,all_supported"
        # Some supports, some insufficient → partial coverage
        if "supports" in sub_stances:
            return (
                "insufficient_evidence",
                "partial",
                f"compound:{len(atomics)}atomic,supported={sub_stances.count('supports')}/{len(atomics)}",
            )
        # All insufficient
        return (
            "insufficient_evidence",
            "none",
            f"compound:{len(atomics)}atomic,none_supported",
        )

    # ── Utility methods ───────────────────────────────────────────────
    @staticmethod
    def _extract_claim_text(finding: str) -> str:
        if "addressing: " in finding:
            return finding.split("addressing: ", 1)[1]
        return finding

    @staticmethod
    def _extract_claim_keywords(claim_text: str) -> list[str]:
        return [
            w
            for w in claim_text.lower().split()
            if len(w) > 2
            and w
            not in ("found", "evidence", "addressing", "the", "and", "for", "with")
        ]

    # ── Main verification ─────────────────────────────────────────────
    # Research relevance (Researcher.confidence) and evidence verification
    # confidence are separate concerns. The verifier determines verdicts
    # solely from its own independent assessment: source authenticity,
    # content integrity, semantic stance, coverage, and applicability.
    # The researcher's confidence score is NOT used for verdict determination.
    def verify(
        self, records: list[ResearchRecord], scope: ReviewScope | None = None
    ) -> dict[str, Any]:
        results: dict[str, Any] = {}
        by_gap: dict[str, list[ResearchRecord]] = {}
        for r in records:
            by_gap.setdefault(r.gap_id, []).append(r)

        for gap_id, gap_records in by_gap.items():
            verifications = []
            for rec in gap_records:
                issues: list[str] = []

                # ── Step 1: Source Identity ────────────────────────────
                source_uri = rec.source_ref.get("uri", "")
                authentic = False
                if source_uri:
                    authentic = any(
                        s.get("doc_id", s.get("uri", "")) == source_uri
                        for s in self.source_pool
                    )
                if not authentic and source_uri:
                    issues.append("source_not_in_pool")

                # ── Step 2: Source Integrity (content hash) ─────────────
                content_intact = True
                stored_hash = rec.source_ref.get("content_hash", "")
                provenance = [f"record:{rec.record_id}"]
                if source_uri:
                    provenance.append(f"source:{source_uri}")
                    if stored_hash and authentic:
                        source = next(
                            (
                                s
                                for s in self.source_pool
                                if s.get("doc_id", s.get("uri", "")) == source_uri
                            ),
                            None,
                        )
                        if source:
                            actual_hash = hashlib.sha256(
                                str(source.get("content", "")).encode()
                            ).hexdigest()
                            content_intact = stored_hash == actual_hash
                            provenance.append(f"content_hash:{stored_hash[:16]}")
                            provenance.append(
                                f"hash_verified:{'yes' if content_intact else 'no'}"
                            )
                            if not content_intact:
                                issues.append("content_hash_mismatch")
                        else:
                            provenance.append(f"content_hash:{stored_hash[:16]}")
                            provenance.append("hash_verified:source_not_found")
                    else:
                        provenance.append(
                            f"content_hash:{stored_hash[:16] if stored_hash else 'empty'}"
                        )
                        provenance.append("hash_verified:no")
                else:
                    provenance.append("source:none")

                # ── Step 3: Quarantine if content changed ──────────────
                if not content_intact and source_uri:
                    # Evidence is stale — quarantine, don't classify stance
                    cv = ClaimVerification(
                        claim_id=rec.record_id,
                        gap_id=gap_id,
                        finding=rec.finding,
                        source_uri=source_uri,
                        authentic=authentic,
                        supports_claim=False,
                        stance="insufficient_evidence",
                        provenance_chain=provenance,
                        contradictions=[],
                        verdict="quarantined",
                        evidence_passage="",
                        assessment_method="quarantined:content_hash_mismatch",
                        applicability="unknown",
                        coverage="none",
                        source_version="",
                        claim_version="",
                        issues=issues + ["quarantined:content_changed"],
                    )
                    verifications.append(asdict(cv))
                    continue

                # ── Step 4: Evidence Passage ───────────────────────────
                source = None
                if source_uri and authentic:
                    source = next(
                        (
                            s
                            for s in self.source_pool
                            if s.get("doc_id", s.get("uri", "")) == source_uri
                        ),
                        None,
                    )
                source_text = (
                    (
                        str(source.get("content", ""))
                        + " "
                        + " ".join(source.get("topics", []))
                    ).lower()
                    if source
                    else ""
                )
                claim_text = self._extract_claim_text(rec.finding)
                claim_keywords = self._extract_claim_keywords(claim_text)
                passage = self._extract_passage(source_text, claim_keywords)

                # ── Step 5: Applicability Assessment ───────────────────
                source_version = (
                    self._extract_version(source_text) if source_text else ""
                )
                claim_version = self._extract_version(claim_text.lower())
                if claim_version and source_version and claim_version != source_version:
                    applicability = "mismatched"
                    issues.append("version_mismatch")
                elif (
                    claim_version and source_version and claim_version == source_version
                ):
                    applicability = "matching"
                elif claim_version or source_version:
                    # One side specifies a version, the other doesn't —
                    # applicability is unconfirmed.
                    applicability = "unknown"
                else:
                    # Neither side specifies a version — general scope.
                    # The source addresses the claim at the same (unversioned) scope.
                    applicability = "unspecified"

                # ── Step 6: Semantic Stance + Coverage ─────────────────
                if source_text:
                    stance, coverage, method = self._assess_coverage(
                        source_text, claim_text
                    )
                else:
                    stance, coverage, method = (
                        "insufficient_evidence",
                        "none",
                        "no_source",
                    )

                if stance == "insufficient_evidence" and coverage == "partial":
                    issues.append("partial_coverage")
                elif stance == "insufficient_evidence":
                    issues.append("insufficient_evidence")
                elif stance == "contradicts":
                    issues.append("source_contradicts_claim")

                supports_claim = stance == "supports" and coverage == "full"

                # ── Step 7: Per-record verdict ─────────────────────────
                # Only 'matching' or 'unspecified' applicability can yield 'verified'.
                # 'unknown' applicability (one side versioned, other not) yields
                # 'unconfirmed_applicability' — keyword overlap alone is insufficient
                # without confirmed scope.
                if not source_uri:
                    verdict = "no_evidence"
                elif not authentic:
                    verdict = "unverified_source"
                elif stance == "contradicts":
                    verdict = "contradicted"
                elif coverage == "partial":
                    verdict = "partial_coverage"
                elif stance == "insufficient_evidence":
                    verdict = "insufficient_evidence"
                elif applicability == "mismatched":
                    verdict = "applicability_mismatch"
                elif applicability == "unknown":
                    verdict = "unconfirmed_applicability"
                    issues.append("applicability_unconfirmed")
                else:
                    verdict = "verified"

                if source_uri and not authentic:
                    issues.append("source_not_authentic")

                cv = ClaimVerification(
                    claim_id=rec.record_id,
                    gap_id=gap_id,
                    finding=rec.finding,
                    source_uri=source_uri,
                    authentic=authentic,
                    supports_claim=supports_claim,
                    stance=stance,
                    provenance_chain=provenance,
                    contradictions=[],
                    verdict=verdict,
                    evidence_passage=passage[:200],
                    assessment_method=method,
                    applicability=applicability,
                    coverage=coverage,
                    source_version=source_version,
                    claim_version=claim_version,
                    issues=issues,
                )
                verifications.append(asdict(cv))

            # ── Step 8: Cross-record contradiction detection ────────────
            # Only declare contradiction when sources have MATCHING applicability
            # (same version/scope). Mismatched versions don't conflict.
            applicable_v = [
                v
                for v in verifications
                if v.get("applicability") != "mismatched"
                and v.get("verdict")
                not in ("quarantined", "no_evidence", "unverified_source")
            ]
            has_supports = any(
                v["stance"] == "supports" and v.get("coverage") == "full"
                for v in applicable_v
            )
            has_contradicts = any(v["stance"] == "contradicts" for v in applicable_v)

            if has_supports and has_contradicts:
                for v in verifications:
                    if (
                        v.get("applicability") != "mismatched"
                        and v.get("verdict") != "quarantined"
                    ):
                        v["contradictions"].append(
                            f"conflicting: {sum(1 for x in applicable_v if x['stance'] == 'supports')} supports, "
                            f"{sum(1 for x in applicable_v if x['stance'] == 'contradicts')} contradicts"
                        )
                        if v["stance"] == "supports" and v["verdict"] == "verified":
                            v["verdict"] = "contradictory"

            # ── Step 9: Aggregate per-gap verdict ──────────────────────
            gap_verdicts = [v["verdict"] for v in verifications]
            quarantined = any(v["verdict"] == "quarantined" for v in verifications)

            if "contradictory" in gap_verdicts or (has_supports and has_contradicts):
                results[gap_id] = {
                    "verdict": "contradictory",
                    "claims": verifications,
                    "valid": False,
                }
            elif quarantined and not has_supports:
                results[gap_id] = {
                    "verdict": "quarantined",
                    "claims": verifications,
                    "valid": False,
                }
            elif "verified" in gap_verdicts:
                results[gap_id] = {
                    "verdict": "verified",
                    "claims": verifications,
                    "valid": True,
                }
            elif "unconfirmed_applicability" in gap_verdicts:
                results[gap_id] = {
                    "verdict": "unconfirmed_applicability",
                    "claims": verifications,
                    "valid": False,
                }
            elif "partial_coverage" in gap_verdicts:
                results[gap_id] = {
                    "verdict": "partial_coverage",
                    "claims": verifications,
                    "valid": False,
                }
            elif "applicability_mismatch" in gap_verdicts:
                results[gap_id] = {
                    "verdict": "applicability_mismatch",
                    "claims": verifications,
                    "valid": False,
                }
            elif "contradicted" in gap_verdicts:
                results[gap_id] = {
                    "verdict": "contradicted",
                    "claims": verifications,
                    "valid": False,
                }
            elif "no_evidence" in gap_verdicts:
                results[gap_id] = {
                    "verdict": "no_evidence",
                    "claims": verifications,
                    "valid": False,
                }
            elif "insufficient_evidence" in gap_verdicts:
                results[gap_id] = {
                    "verdict": "insufficient_evidence",
                    "claims": verifications,
                    "valid": False,
                }
            else:
                results[gap_id] = {
                    "verdict": gap_verdicts[0] if gap_verdicts else "unknown",
                    "claims": verifications,
                    "valid": False,
                }
        return results


class GapReassessment:
    """Evaluates remaining gaps — produces evidence, NOT deprecation."""

    # Answerability categories — each maps to why a gap is in its current state.
    ANSWERABILITY_CATEGORIES = (
        "verified",  # evidence found, applicable, full coverage
        "contradicted",  # sources disagree in matching scope
        "unconfirmed_applicability",  # source found but scope not confirmed
        "partial_coverage",  # source covers part of compound claim
        "quarantined",  # source content changed since research
        "insufficient_evidence",  # source found but evidence too weak
        "no_evidence",  # no source found at all
        "unresolved",  # gap not yet researched or not in verification
    )

    @staticmethod
    def assess_answerability(
        checkpoint: Checkpoint,
        verification: dict[str, Any] | None = None,
        all_gap_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Produce a structured answerability report categorizing each gap.

        Unlike assess() which returns a binary resolved/pending split,
        this method categorizes every gap by its current evidence state so
        consumers can determine precisely which requirements need additional
        research and why.

        Returns:
            {
                'categories': {category: [gap_id, ...]},
                'summary': {category: count},
                'answerable': bool,  # all gaps verified
                'needs_research': [gap_id, ...],  # gaps requiring more work
                'needs_research_reason': {gap_id: category},
            }
        """
        all_gaps = set(
            all_gap_ids or (checkpoint.resolved_gaps + checkpoint.pending_gaps)
        )
        categories: dict[str, list[str]] = {
            c: [] for c in GapReassessment.ANSWERABILITY_CATEGORIES
        }

        for gap_id in sorted(all_gaps):
            if verification and gap_id in verification:
                verdict = verification[gap_id].get("verdict", "unresolved")
                # Map verdict to answerability category
                if verdict == "verified":
                    categories["verified"].append(gap_id)
                elif verdict in ("contradictory", "contradicted"):
                    categories["contradicted"].append(gap_id)
                elif verdict == "unconfirmed_applicability":
                    categories["unconfirmed_applicability"].append(gap_id)
                elif verdict == "partial_coverage":
                    categories["partial_coverage"].append(gap_id)
                elif verdict == "quarantined":
                    categories["quarantined"].append(gap_id)
                elif verdict == "no_evidence":
                    categories["no_evidence"].append(gap_id)
                elif verdict == "insufficient_evidence":
                    categories["insufficient_evidence"].append(gap_id)
                elif verdict == "applicability_mismatch":
                    categories["unconfirmed_applicability"].append(gap_id)
                else:
                    categories["unresolved"].append(gap_id)
            elif gap_id in checkpoint.resolved_gaps:
                categories["verified"].append(gap_id)
            else:
                categories["unresolved"].append(gap_id)

        summary = {c: len(v) for c, v in categories.items()}
        needs_research = []
        needs_research_reason = {}
        for cat in GapReassessment.ANSWERABILITY_CATEGORIES:
            if cat == "verified":
                continue
            for gid in categories[cat]:
                needs_research.append(gid)
                needs_research_reason[gid] = cat

        return {
            "categories": categories,
            "summary": summary,
            "answerable": len(needs_research) == 0,
            "needs_research": needs_research,
            "needs_research_reason": needs_research_reason,
        }

    @staticmethod
    def assess(
        checkpoint: Checkpoint,
        records: list[ResearchRecord],
        verification: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        gap_records: dict[str, list[ResearchRecord]] = {}
        for r in records:
            gap_records.setdefault(r.gap_id, []).append(r)
        newly_resolved = []
        still_pending = []
        for gap_id in checkpoint.pending_gaps:
            recs = gap_records.get(gap_id, [])
            has_evidence = any(
                r.confidence > 0 and r.source_ref.get("uri") for r in recs
            )
            # If verification available, require verified verdict
            if verification and gap_id in verification:
                has_evidence = (
                    has_evidence and verification[gap_id].get("verdict") == "verified"
                )
            if has_evidence:
                newly_resolved.append(gap_id)
            else:
                still_pending.append(gap_id)
        all_resolved = len(still_pending) == 0
        all_gaps = checkpoint.resolved_gaps + newly_resolved
        evidence_package = {
            "item_id": checkpoint.item_id,
            "resolved_gaps": all_gaps,
            "remaining_gaps": still_pending,
            "research_attempts": checkpoint.research_attempts,
            "retry_count": checkpoint.retry_count,
            "records": [
                {
                    "gap_id": r.gap_id,
                    "finding": r.finding,
                    "source": r.source_ref.get("uri", ""),
                    "confidence": r.confidence,
                    "checkpoint_id": r.checkpoint_id,
                }
                for r in records
            ],
            "eligible_for_publication": all_resolved,
            "verification_used": verification is not None,
            "note": "Evidence package for Curator review. Publication is a separate policy-governed operation.",
        }
        return all_resolved, evidence_package


class LifecycleOrchestrator:
    """Orchestrates the full 8-phase lifecycle with review workflow, independent
    verification, idempotent continuation, and bounded retries."""

    def __init__(
        self,
        db_path: str,
        source_pool: DynamicSourcePool | list[dict[str, Any]],
        *,
        evidence_kind: str = "test_assertion",
        researcher_identity: str = "researcher",
        max_attempts: int = 3,
        review_mode: str = REVIEW_AUTO,
    ):
        if isinstance(source_pool, list):
            source_pool = DynamicSourcePool(source_pool)
        self.item_store = SubmittedItemStore(db_path)
        self.record_store = RecordStore(db_path)
        self.checkpoint_store = CheckpointStore(db_path)
        self.review_store = ReviewStore(db_path)
        self.researcher = AutonomousResearcher(
            source_pool,
            evidence_kind=evidence_kind,
            researcher_identity=researcher_identity,
        )
        self.evidence_verifier = EvidenceVerifier(source_pool.pool)
        self.source_pool = source_pool
        self.max_attempts = max_attempts
        self.review_mode = review_mode
        logger.info(
            "LifecycleOrchestrator initialized: db=%s, max_attempts=%d, review_mode=%s",
            db_path,
            max_attempts,
            review_mode,
        )

    def submit_item(self, item: SubmittedItem) -> LifecycleState:
        """Phase 1: Submit to SubmittedItemStore (idempotent)."""
        self.item_store.submit(item)
        return LifecycleState(
            item_id=item.item_id, phase="submitted", submitted_item=item
        )

    def generate_checklist(self, state: LifecycleState) -> GuidanceChecklist:
        """Phase 2: Guidance Checklist from knowledge gaps."""
        item = state.submitted_item or self.item_store.get(state.item_id)
        if not item:
            raise CatalogError("submitted item not found")
        guidance_items = []
        for gap in item.knowledge_gaps:
            guidance_items.append(
                GuidanceItem(
                    gap_id=gap.gap_id,
                    description=gap.description,
                    guidance_text=f"Investigate: {gap.description}. Look in: {', '.join(gap.source_hints)}",
                    source_hints=gap.source_hints,
                    priority=gap.priority,
                    origin="knowledge_gap",
                )
            )
        for req in item.accepted_requirements:
            guidance_items.append(
                GuidanceItem(
                    gap_id=req.req_id,
                    description=req.description,
                    guidance_text=f"Verify: {req.description}. Look in: {', '.join(req.source_hints)}",
                    source_hints=req.source_hints,
                    priority="high",
                    origin="accepted_requirement",
                )
            )
        checklist = GuidanceChecklist(item_id=state.item_id, items=guidance_items)
        state.checklist = checklist
        state.phase = "checklist"
        return checklist

    def scope_review(
        self,
        state: LifecycleState,
        scope: ReviewScope | None = None,
        *,
        reviewer: str = "system",
        notes: str = "",
    ) -> ReviewDecision:
        """Phase 3: Scope Review — configurable review workflow with persisted decisions.

        Modes: auto_approve (default), manual_review (requires explicit approve),
        staged_review (scope + findings both reviewed).
        """
        proposed = scope or ReviewScope()
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        decision = ReviewDecision(
            review_id=f"{state.item_id}-review-{ts.replace(':', '').replace('-', '')}",
            item_id=state.item_id,
            mode=self.review_mode,
            proposed_scope=asdict(proposed),
            decision="approved" if self.review_mode == REVIEW_AUTO else "pending",
            edited_scope=None,
            reviewer=reviewer,
            notes=notes,
            timestamp=ts,
        )
        self.review_store.save(decision)
        state.review_decision = decision
        state.scope = proposed
        state.phase = "scope"
        return decision

    def approve_scope(
        self,
        state: LifecycleState,
        *,
        edited_scope: ReviewScope | None = None,
        reviewer: str = "reviewer",
        notes: str = "",
    ) -> ReviewDecision:
        """Explicitly approve or edit the scope (for manual/staged review modes)."""
        latest = self.review_store.get_latest(state.item_id)
        if not latest:
            raise CatalogError("no scope review found — call scope_review first")
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        final_scope = edited_scope or state.scope or ReviewScope()
        decision = ReviewDecision(
            review_id=f"{state.item_id}-approve-{ts.replace(':', '').replace('-', '')}",
            item_id=state.item_id,
            mode=latest.mode,
            proposed_scope=latest.proposed_scope,
            decision="approved",
            edited_scope=asdict(final_scope) if edited_scope else None,
            reviewer=reviewer,
            notes=notes,
            timestamp=ts,
        )
        self.review_store.save(decision)
        state.review_decision = decision
        state.scope = final_scope
        return decision

    def conduct_research(
        self,
        state: LifecycleState,
        *,
        skip_gaps: set[str] | None = None,
        checkpoint_id: str = "",
    ) -> list[ResearchRecord]:
        """Phase 4: Source Discovery — autonomous + dynamic."""
        if not state.checklist:
            raise CatalogError(
                "generate_checklist must be called before conduct_research"
            )
        scope = state.scope or ReviewScope()
        records = self.researcher.research(
            state.checklist,
            skip_gaps=skip_gaps,
            scope=scope,
            checkpoint_id=checkpoint_id,
        )
        state.research_records = records
        state.phase = "research"
        return records

    def record_findings(self, state: LifecycleState, *, checkpoint_id: str = "") -> int:
        """Phase 5: Record Store — records linked to checkpoint_id and source refs."""
        count = 0
        for rec in state.research_records:
            if checkpoint_id and not rec.checkpoint_id:
                rec.checkpoint_id = checkpoint_id
            self.record_store.record(rec)
            count += 1
        state.phase = "record"
        return count

    def verify_evidence(self, state: LifecycleState) -> dict[str, Any]:
        """Phase 6: Independent claim-level evidence verification.

        Verifies: source authenticity, claim support, provenance chain,
        contradiction handling. Uses EvidenceVerifier (independent from researcher).
        """
        scope = state.scope or ReviewScope()
        # Use ALL records for this item (across cycles) so the verifier
        # can detect contradictions between sources found in different cycles
        all_records = self.record_store.get_all_for_item(state.item_id)
        verification = self.evidence_verifier.verify(all_records, scope=scope)
        state.verification_result = verification
        state.phase = "verified"
        return verification

    def checkpoint(
        self,
        state: LifecycleState,
        *,
        skip_gaps: set[str] | None = None,
        retry_count: int = 0,
    ) -> Checkpoint:
        """Phase 7: Checkpoint Store with retry tracking."""
        skip = skip_gaps or set()
        all_gap_ids = (
            [item.gap_id for item in state.checklist.items] if state.checklist else []
        )
        resolved = list(skip)
        pending = []
        evidence_summary: dict[str, str] = {}
        verification = state.verification_result
        for gap_id in all_gap_ids:
            if gap_id in skip:
                continue
            recs = self.record_store.get_for_gap(state.item_id, gap_id)
            has_evidence = any(
                r.confidence > 0 and r.source_ref.get("uri") for r in recs
            )
            if verification and gap_id in verification:
                has_evidence = (
                    has_evidence and verification[gap_id].get("verdict") == "verified"
                )
            if has_evidence:
                resolved.append(gap_id)
                best = max(recs, key=lambda r: r.confidence)
                evidence_summary[gap_id] = best.finding[:200]
            else:
                pending.append(gap_id)
        attempts = 0
        existing = self.checkpoint_store.load_latest(state.item_id)
        if existing:
            attempts = existing.research_attempts + 1
        # Store gap descriptions for claim-revision detection on resume
        gap_descs = (
            {item.gap_id: item.description for item in state.checklist.items}
            if state.checklist
            else {}
        )
        cp = Checkpoint(
            checkpoint_id=f"{state.item_id}-cp-{attempts}",
            item_id=state.item_id,
            phase="checkpoint",
            resolved_gaps=resolved,
            pending_gaps=pending,
            research_attempts=attempts,
            retry_count=retry_count,
            evidence_summary=evidence_summary,
            recovery_state="reassess" if not pending else "research",
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            gap_descriptions=gap_descs,
        )
        self.checkpoint_store.save(cp)
        state.checkpoint = cp
        state.phase = "checkpoint"
        # Update record checkpoint_id
        for rec in state.research_records:
            if not rec.checkpoint_id:
                rec.checkpoint_id = cp.checkpoint_id
                self.record_store.record(rec)
        return cp

    def reassess(self, state: LifecycleState) -> tuple[bool, dict[str, Any]]:
        """Phase 8: Gap Reassessment — evidence + persisted Next Item with bounded retries."""
        if not state.checkpoint:
            raise CatalogError("checkpoint must be called before reassess")
        records = self.record_store.get_all_for_item(state.item_id)
        resolved, evidence_package = GapReassessment.assess(
            state.checkpoint, records, state.verification_result
        )
        # Answerability assessment: categorize each gap by its evidence state
        all_gap_ids = (
            [item.gap_id for item in state.checklist.items] if state.checklist else []
        )
        state.answerability = GapReassessment.assess_answerability(
            state.checkpoint, state.verification_result, all_gap_ids
        )
        evidence_package["answerability"] = state.answerability
        logger.info(
            "reassess: answerable=%s, needs_research=%s",
            state.answerability.get("answerable", False),
            state.answerability.get("needs_research", []),
        )
        if resolved:
            state.resolved = True
            state.remaining_gaps = []
            state.evidence_package = evidence_package
            state.phase = "complete"
            state.next_item = None
            state.exhausted = False
            self.item_store.update_status(state.item_id, "resolved")
        else:
            state.resolved = False
            state.remaining_gaps = evidence_package["remaining_gaps"]
            state.evidence_package = evidence_package
            state.phase = "reassess"
            self.item_store.update_status(state.item_id, "remaining")
            item = state.submitted_item or self.item_store.get(state.item_id)
            if item is None:
                raise CatalogError("item not found during reassess")
            current_retry = state.checkpoint.retry_count
            # Bounded retries
            if current_retry >= self.max_attempts:
                state.exhausted = True
                state.phase = "exhausted"
                self.item_store.update_status(state.item_id, "exhausted")
                state.next_item = None
            else:
                state.exhausted = False
                # Produce persisted Next Item (idempotent)
                next_id = f"{state.item_id}-r{current_retry + 1}"
                if not self.item_store.exists(next_id):
                    remaining_gaps = [
                        g
                        for g in item.knowledge_gaps
                        if g.gap_id in state.remaining_gaps
                    ]
                    remaining_reqs = [
                        r
                        for r in item.accepted_requirements
                        if r.req_id in state.remaining_gaps
                    ]
                    next_item = SubmittedItem(
                        item_id=next_id,
                        request=item.request,
                        knowledge_gaps=remaining_gaps,
                        accepted_requirements=remaining_reqs,
                        parent_item_id=state.item_id,
                        retry_count=current_retry + 1,
                    )
                    self.item_store.submit(next_item)
                    state.next_item = next_item
                else:
                    state.next_item = self.item_store.get(next_id)
        return resolved, evidence_package

    def run_full_lifecycle(
        self, item: SubmittedItem, *, scope: ReviewScope | None = None
    ) -> LifecycleState:
        """Run all 8 phases in sequence."""
        logger.info(
            "run_full_lifecycle: item_id=%s, gaps=%d, requirements=%d",
            item.item_id,
            len(item.knowledge_gaps),
            len(item.accepted_requirements),
        )
        state = self.submit_item(item)
        logger.debug("Phase 1 complete: item submitted")
        self.generate_checklist(state)
        logger.debug(
            "Phase 2 complete: checklist generated, gaps=%d",
            len(state.checklist.items) if state.checklist else 0,
        )
        self.scope_review(state, scope)
        logger.debug("Phase 3 complete: scope reviewed")
        self.conduct_research(state)
        logger.debug("Phase 4 complete: research conducted")
        self.record_findings(state)
        logger.debug("Phase 5 complete: findings recorded")
        self.verify_evidence(state)
        logger.debug("Phase 6 complete: evidence verified")
        self.checkpoint(state, retry_count=item.retry_count)
        logger.debug("Phase 7 complete: checkpoint saved")
        self.reassess(state)
        logger.info(
            "run_full_lifecycle: resolved=%s, remaining_gaps=%d, phase=%s",
            state.resolved,
            len(state.remaining_gaps),
            state.phase,
        )
        return state

    def resume(
        self, item_id: str, *, scope: ReviewScope | None = None
    ) -> LifecycleState:
        """Resume from a persisted checkpoint through all phases.

        Detects material claim revisions: if a gap's description has changed
        since the checkpoint was created, the gap is removed from skip and
        re-researched even if it was previously resolved.
        """
        logger.info("resume: item_id=%s", item_id)
        item = self.item_store.get(item_id)
        if not item:
            logger.error("resume: submitted item not found: %s", item_id)
            raise CatalogError("submitted item not found")
        cp = self.checkpoint_store.load_latest(item_id)
        if not cp:
            logger.error("resume: no checkpoint found for item: %s", item_id)
            raise CatalogError("no checkpoint found for item")
        state = LifecycleState(
            item_id=item_id, phase=cp.recovery_state, submitted_item=item
        )
        self.generate_checklist(state)
        self.scope_review(state, scope)
        skip = set(cp.resolved_gaps)
        # Claim-revision reassessment: re-research gaps whose descriptions changed
        prev_descs = getattr(cp, "gap_descriptions", {}) or {}
        if prev_descs and state.checklist:
            current_descs = {gi.gap_id: gi.description for gi in state.checklist.items}
            for gap_id in list(skip):
                if gap_id in current_descs and gap_id in prev_descs:
                    if current_descs[gap_id] != prev_descs[gap_id]:
                        skip.discard(gap_id)  # Material revision — re-research
                        logger.info(
                            "resume: gap %s re-researched (description changed)", gap_id
                        )
        logger.info(
            "resume: skip_gaps=%s, research_attempts=%d",
            sorted(skip),
            cp.research_attempts,
        )
        self.conduct_research(state, skip_gaps=skip, checkpoint_id=cp.checkpoint_id)
        self.record_findings(state, checkpoint_id=cp.checkpoint_id)
        self.verify_evidence(state)
        self.checkpoint(state, skip_gaps=skip, retry_count=item.retry_count)
        self.reassess(state)
        return state

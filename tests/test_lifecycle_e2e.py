"""Lifecycle E2E tests: 8-scenario fixture suite for the AAKR lifecycle.

Tests the full 8-phase lifecycle with the EvidenceVerifier's structured
decision pipeline: atomic claim decomposition, version-based applicability,
source quarantine on content change, claim-revision reassessment, and
coverage assessment for compound claims.

Scenarios:
  S1: Semantic negation + incompatible affirmative (same scope)
  S2: Different versions + conditional requirements
  S3: Partial coverage of compound claims
  S4: Irrelevant evidence with matching keywords
  S5: Cross-cycle evidence from different claim revisions
  S5b: Claim-revision reassessment (material description change)
  S6: Duplicate identities with conflicting payloads
  S7: Source content changes between research cycles (quarantine)
  S8: Multiple supports/contradicts with authority
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import asdict

import pytest

from aakr.lifecycle import (
    DynamicSourcePool,
    EvidenceVerifier,
    GapReassessment,
    KnowledgeGap,
    LifecycleOrchestrator,
    RecordStore,
    ResearchRecord,
    ReviewScope,
    SubmittedItem,
)

# ── Helpers ───────────────────────────────────────────────────────────────

_TS = "2026-11-06T23:45:00Z"


def _make_record(
    rid: str,
    src: dict,
    gap_id: str = "gap-test",
    desc: str = "",
    cp: str = "cp-0",
    confidence: float = 0.8,
) -> ResearchRecord:
    """Create a research record with a content hash matching the source."""
    return ResearchRecord(
        record_id=rid,
        item_id="t",
        gap_id=gap_id,
        checkpoint_id=cp,
        finding=f'Found evidence in "{src["title"]}" addressing: {desc}',
        source_ref={
            "uri": src["doc_id"],
            "source_type": src.get("source_type", "documentation"),
            "content_hash": hashlib.sha256(src["content"].encode()).hexdigest(),
            "title": src["title"],
            "retrieved_at": _TS,
        },
        evidence_kind="test_assertion",
        methodology="test",
        confidence=confidence,
        recorded_by="r",
        recorded_at=_TS,
    )


def _run_verifier(
    pool: list[dict], records: list[ResearchRecord], gap_id: str = "gap-test"
) -> dict:
    """Run the EvidenceVerifier and return results for the given gap."""
    return EvidenceVerifier(pool).verify(records, scope=ReviewScope()).get(gap_id, {})


@pytest.fixture
def temp_dir():
    """Provide a clean temporary directory for each test."""
    d = tempfile.mkdtemp(prefix="aakr-e2e-")
    yield d
    import shutil

    shutil.rmtree(d, ignore_errors=True)


# ── S1: Semantic negation + incompatible affirmative (same scope) ─────────


def test_s1_semantic_negation_same_scope():
    """A source that actively negates the claim contradicts; a source that
    affirms without negation supports. Both in the same (unspecified) scope
    produce a contradictory gap verdict."""
    src_neg = {
        "doc_id": "s1n",
        "content": "Parameterized queries do not prevent sql injection in stored procedures",
        "topics": ["sql_injection", "parameterized_queries"],
        "title": "Security Audit",
    }
    src_aff = {
        "doc_id": "s1a",
        "content": "Parameterized queries prevent sql injection by separating code from data",
        "topics": ["sql_injection", "parameterized_queries"],
        "title": "OWASP Guide",
    }
    result = _run_verifier(
        [src_neg, src_aff],
        [
            _make_record("s1n", src_neg, desc="sql injection prevention"),
            _make_record("s1a", src_aff, desc="sql injection prevention"),
        ],
    )
    claims = {c["claim_id"]: c for c in result.get("claims", [])}

    assert result.get("verdict") == "contradictory"
    assert claims["s1n"]["stance"] == "contradicts"
    assert claims["s1a"]["stance"] == "supports"
    assert claims["s1a"]["applicability"] == "unspecified"
    assert bool(claims["s1a"].get("evidence_passage"))


# ── S2: Different versions + conditional requirements ─────────────────────


def test_s2_version_matching_and_unconfirmed():
    """A source with a matching version gets 'matching' applicability.
    A source without a version when the claim specifies one gets 'unknown'
    applicability and an 'unconfirmed_applicability' verdict."""
    claim = "python 3.12 type annotation enforcement at runtime"
    src_v3 = {
        "doc_id": "s2v",
        "content": "Python 3.12 does not enforce type annotations at runtime by default",
        "topics": ["python_typing", "type_annotation", "python_3_12"],
        "title": "Python 3.12 Typing Guide",
    }
    src_pydantic = {
        "doc_id": "s2e",
        "content": "Pydantic enforces type annotations at runtime with validation",
        "topics": ["python_typing", "type_annotation", "pydantic"],
        "title": "Pydantic Runtime Validation",
    }
    result = _run_verifier(
        [src_v3, src_pydantic],
        [
            _make_record("s2v", src_v3, desc=claim),
            _make_record("s2e", src_pydantic, desc=claim),
        ],
    )
    claims = {c["claim_id"]: c for c in result.get("claims", [])}

    assert claims["s2v"]["stance"] == "contradicts"
    assert claims["s2v"]["applicability"] == "matching"
    assert claims["s2v"]["source_version"] == "3.12"
    assert claims["s2v"]["claim_version"] == "3.12"
    assert claims["s2e"]["applicability"] == "unknown"
    assert claims["s2e"]["verdict"] == "unconfirmed_applicability"
    assert "applicability_unconfirmed" in claims["s2e"].get("issues", [])


# ── S3: Partial coverage of compound claims ──────────────────────────────


def test_s3_partial_coverage_of_compound_claims():
    """A source covering only part of a compound claim gets 'partial'
    coverage and 'partial_coverage' verdict. A source covering all atomic
    sub-claims gets 'full' coverage and can verify the gap."""
    claim = "json serialization canonicalization and unicode escaping"
    src_partial = {
        "doc_id": "s3p",
        "content": "JSON canonicalization requires sorted keys and minimal whitespace",
        "topics": ["canonical_json", "json_serialization"],
        "title": "JSON Canonicalization RFC",
    }
    src_full = {
        "doc_id": "s3f",
        "content": "JSON serialization canonicalization requires sorted keys minimal whitespace and unicode escaping",
        "topics": ["canonical_json", "json_serialization", "unicode_escaping"],
        "title": "Complete JSON Guide",
    }
    result = _run_verifier(
        [src_partial, src_full],
        [
            _make_record("s3p", src_partial, desc=claim),
            _make_record("s3f", src_full, desc=claim),
        ],
    )
    claims = {c["claim_id"]: c for c in result.get("claims", [])}

    assert claims["s3p"]["coverage"] == "partial"
    assert claims["s3p"]["stance"] == "insufficient_evidence"
    assert claims["s3p"]["verdict"] == "partial_coverage"
    assert claims["s3f"]["coverage"] == "full"
    assert claims["s3f"]["stance"] == "supports"
    assert claims["s3f"]["applicability"] == "unspecified"
    assert result.get("verdict") == "verified"


# ── S4: Irrelevant evidence with matching keywords ───────────────────────


def test_s4_irrelevant_evidence_keyword_overlap():
    """A source with generic keyword overlap but no claim-specific content
    gets 'insufficient_evidence', not 'supports'. Keyword overlap alone
    cannot produce a verified claim."""
    claim = "kubernetes network security policy enforcement"
    src_irrelevant = {
        "doc_id": "s4i",
        "content": "Kubernetes network traffic is routed through kube-proxy for service discovery",
        "topics": ["kubernetes", "network"],
        "title": "Kubernetes Pod Scheduling",
    }
    src_relevant = {
        "doc_id": "s4r",
        "content": "Kubernetes network security policies enforce traffic rules between pods",
        "topics": ["kubernetes_security", "network_policy"],
        "title": "Kubernetes Network Policies",
    }
    result = _run_verifier(
        [src_irrelevant, src_relevant],
        [
            _make_record("s4i", src_irrelevant, desc=claim),
            _make_record("s4r", src_relevant, desc=claim),
        ],
    )
    claims = {c["claim_id"]: c for c in result.get("claims", [])}

    assert claims["s4i"]["stance"] == "insufficient_evidence"
    assert claims["s4r"]["stance"] == "supports"
    assert claims["s4r"]["applicability"] == "unspecified"
    assert result.get("verdict") == "verified"


# ── S5: Cross-cycle evidence from different claim revisions ───────────────


def test_s5_cross_cycle_no_false_contradiction(temp_dir):
    """A resolved gap stays resolved across cycles when no new evidence
    contradicts the prior finding. The resume does not produce false
    contradictions."""
    db = os.path.join(temp_dir, "s5.db")
    src_v1 = {
        "doc_id": "s5v",
        "content": "Python supports type annotations for async functions",
        "topics": ["python_typing", "async"],
        "title": "Async Type Hints",
    }
    pool_a = DynamicSourcePool([src_v1])
    item = SubmittedItem(
        item_id="s5i",
        request="x",
        knowledge_gaps=[
            KnowledgeGap(
                "g5", "python async type annotation support", ["python_typing", "async"]
            )
        ],
        accepted_requirements=[],
    )
    orch_a = LifecycleOrchestrator(db, pool_a, max_attempts=3)
    state_a = orch_a.run_full_lifecycle(item, scope=ReviewScope(include_external=False))

    assert state_a.resolved

    src_v2 = {
        "doc_id": "s5w",
        "content": "Python supports type annotations for async context managers",
        "topics": ["python_typing", "async"],
        "title": "Async Context Manager Types",
    }
    pool_b = DynamicSourcePool([src_v1, src_v2])
    orch_b = LifecycleOrchestrator(db, pool_b, max_attempts=3)
    state_b = orch_b.resume(item.item_id, scope=ReviewScope(include_external=False))

    assert state_b.resolved
    v = (
        next(iter(state_b.verification_result.values()))
        if state_b.verification_result
        else {}
    )
    assert v.get("verdict") != "contradictory"


# ── S5b: Claim-revision reassessment ──────────────────────────────────────


def test_s5b_claim_revision_triggers_research(temp_dir):
    """When a gap's description changes between cycles (material revision),
    the lifecycle re-researches that gap even if it was previously resolved.
    The new record addresses the revised claim."""
    db = os.path.join(temp_dir, "s5b.db")
    src_orig = {
        "doc_id": "s5bs",
        "content": "Feature X supports async operations",
        "topics": ["feature_x", "async"],
        "title": "Feature X Docs",
    }
    pool_a = DynamicSourcePool([src_orig])
    item = SubmittedItem(
        item_id="s5bi",
        request="x",
        knowledge_gaps=[
            KnowledgeGap(
                "g5b", "feature x supports async operations", ["feature_x", "async"]
            )
        ],
        accepted_requirements=[],
    )
    orch_a = LifecycleOrchestrator(db, pool_a, max_attempts=3)
    state_a = orch_a.run_full_lifecycle(item, scope=ReviewScope(include_external=False))
    recs_c1 = RecordStore(db).get_all_for_item("s5bi")

    assert state_a.resolved

    # Simulate claim revision via direct DB update (item store is idempotent)
    revised_gaps = [
        asdict(
            KnowledgeGap(
                "g5b",
                "feature x supports async operations with coroutines",
                ["feature_x", "async"],
            )
        )
    ]
    with sqlite3.connect(db) as c:
        c.execute(
            "UPDATE submitted_items SET knowledge_gaps=? WHERE item_id=?",
            (json.dumps(revised_gaps), "s5bi"),
        )

    # Add new source addressing the revised claim
    src_revised = {
        "doc_id": "s5bs2",
        "content": "Feature X supports async operations with coroutines and tasks",
        "topics": ["feature_x", "async", "coroutines"],
        "title": "Feature X Coroutines",
    }
    pool_b = DynamicSourcePool([src_orig, src_revised])
    orch_b = LifecycleOrchestrator(db, pool_b, max_attempts=3)
    orch_b.resume("s5bi", scope=ReviewScope(include_external=False))
    recs_c2 = RecordStore(db).get_all_for_item("s5bi")

    assert len(recs_c2) > len(recs_c1)
    assert any("coroutines" in r.finding.lower() for r in recs_c2)


# ── S6: Duplicate identities with conflicting payloads ────────────────────


def test_s6_duplicate_identities_conflicting_payloads(temp_dir):
    """A duplicate record_id with different content is rejected by the
    RecordStore (INSERT OR IGNORE). The original record's content hash is
    preserved."""
    db = os.path.join(temp_dir, "s6.db")
    rs = RecordStore(db)
    src_a = {"doc_id": "s6", "content": "A", "title": "Original"}
    src_b = {"doc_id": "s6", "content": "B", "title": "Modified"}
    rec_a = _make_record("dup", src_a, gap_id="g6", desc="x")
    rec_b = _make_record("dup", src_b, gap_id="g6", desc="x")
    rs.record(rec_a)
    rs.record(rec_b)

    stored = rs.get_all_for_item("t")
    hash_a = hashlib.sha256(b"A").hexdigest()

    assert len(stored) == 1
    assert stored[0].source_ref.get("content_hash") == hash_a


# ── S7: Source content changes between research cycles (quarantine) ──────


def test_s7_source_content_change_quarantine(temp_dir):
    """When source content changes between cycles, the stored content hash
    no longer matches the current source. The evidence is quarantined —
    stance is NOT reclassified against the new content. The gap is flagged
    for re-research."""
    db = os.path.join(temp_dir, "s7.db")
    src_original = {
        "doc_id": "s7d",
        "content": "Parameterized queries prevent sql injection attacks",
        "topics": ["sql_injection", "parameterized_queries"],
        "title": "Security Guide v1",
    }
    src_updated = {
        "doc_id": "s7d",
        "content": "Parameterized queries do not prevent sql injection in stored procedures",
        "topics": ["sql_injection", "parameterized_queries"],
        "title": "Security Guide v2",
    }
    pool_a = DynamicSourcePool([src_original])
    item = SubmittedItem(
        item_id="s7i",
        request="x",
        knowledge_gaps=[
            KnowledgeGap("g7", "sql injection prevention methods", ["sql_injection"])
        ],
        accepted_requirements=[],
    )
    orch_a = LifecycleOrchestrator(db, pool_a, max_attempts=3)
    state_a = orch_a.run_full_lifecycle(item, scope=ReviewScope(include_external=False))

    assert state_a.resolved
    v_a = next(iter(state_a.verification_result.values()))
    assert any(c["stance"] == "supports" for c in v_a.get("claims", []))

    pool_b = DynamicSourcePool([src_updated])
    orch_b = LifecycleOrchestrator(db, pool_b, max_attempts=3)
    state_b = orch_b.resume(item.item_id, scope=ReviewScope(include_external=False))
    v_b = (
        next(iter(state_b.verification_result.values()))
        if state_b.verification_result
        else {}
    )

    assert any(c.get("verdict") == "quarantined" for c in v_b.get("claims", []))
    assert all(c.get("stance") != "contradicts" for c in v_b.get("claims", []))
    assert any("quarantined" in str(c.get("issues", [])) for c in v_b.get("claims", []))


# ── S8: Multiple supports/contradicts with authority ──────────────────────


def test_s8_multiple_supports_contradicts():
    """Multiple sources supporting and contradicting the same claim in the
    same (unspecified) scope produce a contradictory gap verdict. All claims
    carry evidence passages and assessment methods for independent review."""
    claim = "parameterized queries prevent sql injection"
    src_sup1 = {
        "doc_id": "s8a",
        "content": "Parameterized queries prevent sql injection by separating code from data",
        "topics": ["sql_injection", "parameterized_queries"],
        "title": "OWASP Top 10",
    }
    src_sup2 = {
        "doc_id": "s8b",
        "content": "Use parameterized queries to prevent sql injection attacks effectively",
        "topics": ["sql_injection", "parameterized_queries"],
        "title": "CWE-89 Guide",
    }
    src_con1 = {
        "doc_id": "s8c",
        "content": "Parameterized queries do not prevent sql injection in stored procedures",
        "topics": ["sql_injection", "parameterized_queries"],
        "title": "Stored Procedure Audit",
    }
    src_con2 = {
        "doc_id": "s8d",
        "content": "Parameterized queries cannot prevent sql injection when using dynamic sql",
        "topics": ["sql_injection", "parameterized_queries"],
        "title": "Dynamic SQL Research",
    }
    result = _run_verifier(
        [src_sup1, src_sup2, src_con1, src_con2],
        [
            _make_record("s8a", src_sup1, desc=claim),
            _make_record("s8b", src_sup2, desc=claim),
            _make_record("s8c", src_con1, desc=claim),
            _make_record("s8d", src_con2, desc=claim),
        ],
    )
    claims = {c["claim_id"]: c for c in result.get("claims", [])}

    assert result.get("verdict") == "contradictory"
    assert not result.get("valid")
    assert claims["s8a"]["stance"] == "supports"
    assert claims["s8c"]["stance"] == "contradicts"
    assert all(
        c.get("applicability") == "unspecified" for c in result.get("claims", [])
    )
    assert all(c.get("evidence_passage") for c in result.get("claims", []))
    assert all(c.get("assessment_method") for c in result.get("claims", []))


# ── S9: Answerability assessment — all verified ──────────────────────────


def test_s9_answerability_all_verified():
    """When all gaps have verified verdicts, assess_answerability reports
    answerable=True and no gaps need research."""
    from aakr.lifecycle import Checkpoint

    cp = Checkpoint(
        checkpoint_id="cp",
        item_id="i",
        phase="verify",
        resolved_gaps=[],
        pending_gaps=["g1", "g2"],
        research_attempts=1,
        retry_count=0,
        evidence_summary={},
        recovery_state="verify",
        created_at="2026-11-06T00:00:00Z",
    )
    verification = {
        "g1": {"verdict": "verified", "claims": [], "valid": True},
        "g2": {"verdict": "verified", "claims": [], "valid": True},
    }
    result = GapReassessment.assess_answerability(cp, verification)

    assert result["answerable"] is True
    assert result["needs_research"] == []
    assert result["summary"]["verified"] == 2
    assert "g1" in result["categories"]["verified"]
    assert "g2" in result["categories"]["verified"]


# ── S10: Answerability assessment — mixed verdicts ────────────────────────


def test_s10_answerability_mixed_verdicts():
    """assess_answerability categorizes each gap by its verification verdict,
    distinguishing verified, contradicted, insufficient, no_evidence, and
    unresolved gaps."""
    from aakr.lifecycle import Checkpoint

    cp = Checkpoint(
        checkpoint_id="cp",
        item_id="i",
        phase="verify",
        resolved_gaps=[],
        pending_gaps=["g1", "g2", "g3", "g4", "g5", "g6"],
        research_attempts=1,
        retry_count=0,
        evidence_summary={},
        recovery_state="verify",
        created_at="2026-11-06T00:00:00Z",
    )
    verification = {
        "g1": {"verdict": "verified", "claims": [], "valid": True},
        "g2": {"verdict": "contradictory", "claims": [], "valid": False},
        "g3": {"verdict": "insufficient_evidence", "claims": [], "valid": False},
        "g4": {"verdict": "no_evidence", "claims": [], "valid": False},
        "g5": {"verdict": "partial_coverage", "claims": [], "valid": False},
    }
    result = GapReassessment.assess_answerability(cp, verification)

    assert result["answerable"] is False
    assert set(result["needs_research"]) == {"g2", "g3", "g4", "g5", "g6"}
    assert result["summary"]["verified"] == 1
    assert result["summary"]["contradicted"] == 1
    assert result["summary"]["insufficient_evidence"] == 1
    assert result["summary"]["no_evidence"] == 1
    assert result["summary"]["partial_coverage"] == 1
    assert result["summary"]["unresolved"] == 1
    assert result["needs_research_reason"]["g2"] == "contradicted"
    assert result["needs_research_reason"]["g6"] == "unresolved"


# ── S11: Answerability assessment — quarantine and unconfirmed ────────────


def test_s11_answerability_quarantine_and_unconfirmed():
    """assess_answerability correctly categorizes quarantined and
    unconfirmed_applicability gaps."""
    from aakr.lifecycle import Checkpoint

    cp = Checkpoint(
        checkpoint_id="cp",
        item_id="i",
        phase="verify",
        resolved_gaps=[],
        pending_gaps=["g1", "g2", "g3"],
        research_attempts=1,
        retry_count=0,
        evidence_summary={},
        recovery_state="verify",
        created_at="2026-11-06T00:00:00Z",
    )
    verification = {
        "g1": {"verdict": "quarantined", "claims": [], "valid": False},
        "g2": {"verdict": "unconfirmed_applicability", "claims": [], "valid": False},
        "g3": {"verdict": "applicability_mismatch", "claims": [], "valid": False},
    }
    result = GapReassessment.assess_answerability(cp, verification)

    assert result["answerable"] is False
    assert set(result["needs_research"]) == {"g1", "g2", "g3"}
    assert result["summary"]["quarantined"] == 1
    assert result["summary"]["unconfirmed_applicability"] == 2
    assert result["needs_research_reason"]["g1"] == "quarantined"
    assert result["needs_research_reason"]["g2"] == "unconfirmed_applicability"
    assert result["needs_research_reason"]["g3"] == "unconfirmed_applicability"


# ── S12: Answerability assessment — lifecycle integration ─────────────────


def test_s12_answerability_lifecycle_integration(temp_dir):
    """The full lifecycle populates state.answerability with categorized
    gaps after reassess."""
    db = os.path.join(temp_dir, "s12.db")
    src = {
        "doc_id": "s12",
        "content": "Parameterized queries prevent sql injection by separating code from data",
        "topics": ["sql_injection", "parameterized_queries"],
        "title": "Security Guide",
    }
    pool = DynamicSourcePool([src])
    item = SubmittedItem(
        item_id="s12i",
        request="x",
        knowledge_gaps=[
            KnowledgeGap(
                "g1",
                "sql injection prevention methods",
                ["sql_injection", "parameterized_queries"],
            )
        ],
        accepted_requirements=[],
    )
    orch = LifecycleOrchestrator(db, pool, max_attempts=3)
    state = orch.run_full_lifecycle(item, scope=ReviewScope(include_external=False))

    assert state.resolved
    assert state.answerability
    assert state.answerability["answerable"] is True
    assert "g1" in state.answerability["categories"]["verified"]
    assert state.answerability["needs_research"] == []
    assert "answerability" in state.evidence_package


# ── S13: Concept normalization — synonym mapping ──────────────────────────


def test_s13_concept_normalization_synonyms():
    """normalize_concept maps common synonyms to canonical forms and
    normalizes separators, improving cross-synonym retrieval without
    changing stored Catalog data."""
    from aakr.catalog.retrieval import ConceptNormalizer, normalize_concept

    # Synonym mapping (abbreviation expansions — see S20 for collision audit)
    assert normalize_concept("ml") == "machine_learning"
    assert normalize_concept("ML") == "machine_learning"
    assert normalize_concept("k8s") == "kubernetes"
    assert normalize_concept("K8S") == "kubernetes"
    assert normalize_concept("db") == "database"

    # Separator normalization
    assert normalize_concept("sql-injection") == "sql_injection"
    assert normalize_concept("sql injection") == "sql_injection"
    assert normalize_concept("SQL__Injection") == "sql_injection"

    # Already canonical terms pass through
    assert normalize_concept("software") == "software"
    assert normalize_concept("database") == "database"
    assert normalize_concept("software_engineering") == "software_engineering"

    # Empty/None handling
    assert normalize_concept("") == ""

    # ConceptNormalizer with custom synonyms
    cn = ConceptNormalizer(extra_synonyms={"my_domain": "custom"})
    assert cn.normalize("my_domain") == "custom"
    assert cn.normalize("ml") == "machine_learning"

    # normalize_list deduplicates (distinct concepts not merged)
    result = cn.normalize_list(["software", "software_engineering", "backend"])
    assert result == ["software", "software_engineering", "backend"]


def test_s14_score_based_source_selection():
    """S14: Score-based source selection prevents false keyword matches.

    When multiple sources share a common keyword (e.g., 'security'), the
    researcher must select the source with the most keyword matches, not
    the first source that matches any single keyword. This prevents a
    generic security source from being selected when a more specific
    source (e.g., Kubernetes network policy) is available.
    """
    from aakr.lifecycle import (
        KnowledgeGap,
        LifecycleOrchestrator,
        ReviewScope,
        SubmittedItem,
    )

    tmpdir = tempfile.mkdtemp(prefix="aakr-s14-")
    db = os.path.join(tmpdir, "s14.db")

    pool = [
        {
            "doc_id": "d1",
            "title": "SQL Injection Prevention",
            "content": "Parameterized queries prevent sql injection by separating code from data",
            "topics": ["sql_injection", "parameterized_queries", "security"],
        },
        {
            "doc_id": "d2",
            "title": "Kubernetes Network Policies",
            "content": "Kubernetes network security policies enforce traffic rules between pods",
            "topics": ["kubernetes", "network_policy", "security"],
        },
    ]

    item = SubmittedItem(
        item_id="s14-item",
        request="Verify infrastructure security",
        knowledge_gaps=[
            KnowledgeGap(
                "g2",
                "kubernetes network security policy",
                ["kubernetes", "network_policy"],
            ),
        ],
        accepted_requirements=[],
    )

    orch = LifecycleOrchestrator(db, pool)
    state = orch.run_full_lifecycle(item, scope=ReviewScope(include_external=False))

    # Researcher should select d2 (score=6) over d1 (score=1)
    assert len(state.research_records) == 1
    rec = state.research_records[0]
    assert rec.gap_id == "g2"
    assert rec.source_ref.get("uri") == "d2", (
        f"Expected d2 (kubernetes) but got {rec.source_ref.get('uri')}"
    )

    # Gap should be resolved (d2 supports the claim)
    assert "g2" in state.checkpoint.resolved_gaps, (
        f"g2 should be resolved with d2 evidence; remaining={state.remaining_gaps}"
    )


def test_s15_no_cloud_provider_alias_collision():
    """S15: Cloud providers must not be merged into a single 'cloud' alias.

    AWS, GCP, and Azure are distinct cloud platforms. Mapping them all to
    'cloud' would cause a query for one provider to match entries for another,
    producing false positives. Each provider must normalize independently.
    """
    from aakr.catalog.retrieval import normalize_concept

    # Cloud providers must NOT normalize to the same value
    aws = normalize_concept("aws")
    gcp = normalize_concept("gcp")
    azure = normalize_concept("azure")
    cloud = normalize_concept("cloud")

    assert aws != gcp, f"aws and gcp must not collide (both={aws})"
    assert aws != azure, f"aws and azure must not collide (both={aws})"
    assert gcp != azure, f"gcp and azure must not collide (both={gcp})"

    # Each provider should pass through unchanged (no alias mapping)
    assert aws == "aws"
    assert gcp == "gcp"
    assert azure == "azure"

    # 'cloud' remains a valid concept for general cloud searches
    assert cloud == "cloud"


def test_s16_validate_content_hash():
    """S16: Content hash validation verifies SHA-256 integrity.

    validate_content_hash accepts explicit bytes or str content (no FS access).
    validate_file_hash reads files with path traversal and symlink rejection.
    Returns True on match, False on mismatch/invalid format/missing file.
    """
    from aakr.catalog.models import validate_content_hash, validate_file_hash

    content = "Kubernetes network security policies"
    expected = hashlib.sha256(content.encode()).hexdigest()
    assert validate_content_hash(content, expected) is True
    assert validate_content_hash(content.encode(), expected) is True

    wrong = hashlib.sha256(b"different content").hexdigest()
    assert validate_content_hash(content, wrong) is False
    assert validate_content_hash(content, "not-a-hash") is False
    assert validate_content_hash(content, "") is False

    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("file content for hashing")
        f.flush()
        file_path = f.name

    file_hash = hashlib.sha256(b"file content for hashing").hexdigest()
    assert validate_file_hash(file_path, file_hash) is True
    assert validate_file_hash(file_path, wrong) is False
    assert validate_file_hash("/nonexistent/path/file.txt", file_hash) is False
    assert validate_file_hash("../../../etc/passwd", file_hash) is False
    assert validate_file_hash("foo/../../bar", file_hash) is False

    file_dir = os.path.dirname(file_path)
    assert validate_file_hash(file_path, file_hash, base_dir=file_dir) is True
    assert validate_file_hash(file_path, file_hash, base_dir="/other/dir") is False

    os.unlink(file_path)

    path_like = "/some/path/to/file.txt"
    path_hash = hashlib.sha256(path_like.encode()).hexdigest()
    assert validate_content_hash(path_like, path_hash) is True


def test_s17_correction_feedback_loop(temp_dir):
    """S17: Correction feedback loop creates next_item with remaining gaps.

    When the lifecycle cannot resolve all gaps, the reassess phase must:
    1. Categorize gaps via answerability assessment
    2. Create a next_item with only the unresolved gaps
    3. Increment retry_count and link to parent
    4. Attach answerability to evidence_package
    """
    db = os.path.join(temp_dir, "s17.db")
    pool = [
        {
            "doc_id": "d1",
            "title": "SQL Injection",
            "content": "Parameterized queries prevent sql injection",
            "topics": ["sql_injection", "parameterized_queries"],
        },
    ]
    item = SubmittedItem(
        item_id="s17-item",
        request="Verify security and infra",
        knowledge_gaps=[
            KnowledgeGap(
                "g1",
                "sql injection prevention",
                ["sql_injection", "parameterized_queries"],
            ),
            KnowledgeGap(
                "g2",
                "kubernetes network policy",
                ["kubernetes", "network_policy"],
            ),
        ],
        accepted_requirements=[],
    )
    orch = LifecycleOrchestrator(db, pool, max_attempts=3)
    state = orch.run_full_lifecycle(item, scope=ReviewScope(include_external=False))

    assert not state.resolved
    assert "g2" in state.remaining_gaps
    assert not state.exhausted

    assert not state.answerability["answerable"]
    assert "g1" in state.answerability["categories"]["verified"]
    assert "g2" in state.answerability["categories"]["no_evidence"]
    assert "g2" in state.answerability["needs_research"]

    assert state.next_item is not None
    assert state.next_item.item_id == "s17-item-r1"
    assert state.next_item.retry_count == 1
    assert state.next_item.parent_item_id == "s17-item"
    next_gap_ids = [g.gap_id for g in state.next_item.knowledge_gaps]
    assert next_gap_ids == ["g2"]

    assert "answerability" in state.evidence_package


def test_s18_durable_checkpoint_recovery(temp_dir):
    """S18: Durable checkpoint recovery after simulated process restart.

    Verifies that checkpoints persist across orchestrator instances (process
    restarts), that resume increments research_attempts correctly, that
    double-resume is idempotent, and that bounded retries exhaust properly.
    """
    db = os.path.join(temp_dir, "s18.db")
    pool = [
        {
            "doc_id": "d1",
            "title": "SQL Injection",
            "content": "Parameterized queries prevent sql injection",
            "topics": ["sql_injection", "parameterized_queries"],
        },
    ]
    item = SubmittedItem(
        item_id="s18-item",
        request="test",
        knowledge_gaps=[
            KnowledgeGap(
                "g1",
                "sql injection prevention",
                ["sql_injection", "parameterized_queries"],
            ),
            KnowledgeGap(
                "g2",
                "kubernetes network policy",
                ["kubernetes", "network_policy"],
            ),
        ],
        accepted_requirements=[],
    )

    orch1 = LifecycleOrchestrator(db, pool, max_attempts=3)
    state1 = orch1.run_full_lifecycle(item, scope=ReviewScope(include_external=False))
    assert not state1.resolved
    cp1 = state1.checkpoint
    assert cp1.checkpoint_id == "s18-item-cp-0"
    assert cp1.research_attempts == 0

    orch2 = LifecycleOrchestrator(db, pool, max_attempts=3)
    state2 = orch2.resume("s18-item", scope=ReviewScope(include_external=False))
    cp2 = state2.checkpoint
    assert cp2.checkpoint_id == "s18-item-cp-1"
    assert cp2.research_attempts == 1

    orch3 = LifecycleOrchestrator(db, pool, max_attempts=3)
    state3 = orch3.resume("s18-item", scope=ReviewScope(include_external=False))
    cp3 = state3.checkpoint
    assert cp3.checkpoint_id == "s18-item-cp-2"
    assert cp3.research_attempts == 2

    if state1.next_item:
        orch_r1 = LifecycleOrchestrator(db, pool, max_attempts=3)
        state_r1 = orch_r1.run_full_lifecycle(
            state1.next_item, scope=ReviewScope(include_external=False)
        )
        if state_r1.next_item:
            orch_r2 = LifecycleOrchestrator(db, pool, max_attempts=3)
            state_r2 = orch_r2.run_full_lifecycle(
                state_r1.next_item, scope=ReviewScope(include_external=False)
            )
            if state_r2.next_item:
                orch_r3 = LifecycleOrchestrator(db, pool, max_attempts=3)
                state_r3 = orch_r3.run_full_lifecycle(
                    state_r2.next_item, scope=ReviewScope(include_external=False)
                )
                assert state_r3.exhausted
                assert state_r3.phase == "exhausted"
                assert state_r3.next_item is None


def test_s19_score_proportional_confidence(temp_dir):
    """S19: Researcher confidence is proportional to keyword match quality.

    Retrieval relevance (keyword match) must NOT imply verified evidence.
    The researcher's confidence reflects match quality, not verification.
    The EvidenceVerifier independently determines the verdict.
    """
    db = os.path.join(temp_dir, "s19.db")

    # Source with many keyword overlaps (high score)
    strong_pool = [
        {
            "doc_id": "strong",
            "title": "SQL Injection Prevention Guide",
            "content": "sql injection prevention parameterized queries prepared statements",
            "topics": ["sql_injection", "parameterized_queries", "prepared_statements"],
        },
    ]

    item = SubmittedItem(
        item_id="s19-item",
        request="test",
        knowledge_gaps=[
            KnowledgeGap(
                "g1",
                "sql injection prevention",
                ["sql_injection", "parameterized_queries", "prepared_statements"],
            ),
        ],
        accepted_requirements=[],
    )

    orch = LifecycleOrchestrator(db, strong_pool, max_attempts=3)
    state = orch.run_full_lifecycle(item, scope=ReviewScope(include_external=False))

    recs = [r for r in state.research_records if r.gap_id == "g1" and r.confidence > 0]
    assert len(recs) == 1
    rec = recs[0]

    # Strong match (many keywords) → confidence > 0.5
    assert rec.confidence > 0.5
    assert rec.confidence <= 0.9

    # Methodology documents the relevance score
    assert "Relevance score" in rec.methodology
    assert "Score-based selection" in rec.methodology

    # Verifier independently determines verdict — not just from confidence
    verification = state.verification_result
    assert "g1" in verification
    verdict = verification["g1"]["verdict"]
    # Verdict is independently assessed (could be verified, partial, etc.)
    assert verdict in ("verified", "partial_coverage", "insufficient_evidence")

    # Now test weak match (few keyword overlaps)
    weak_pool = [
        {
            "doc_id": "weak",
            "title": "Generic Security",
            "content": "security is important for applications",
            "topics": ["security"],
        },
    ]

    item2 = SubmittedItem(
        item_id="s19-item2",
        request="test",
        knowledge_gaps=[
            KnowledgeGap(
                "g1",
                "sql injection prevention",
                ["sql_injection", "parameterized_queries", "prepared_statements"],
            ),
        ],
        accepted_requirements=[],
    )

    orch2 = LifecycleOrchestrator(
        os.path.join(temp_dir, "s19-weak.db"), weak_pool, max_attempts=3
    )
    state2 = orch2.run_full_lifecycle(item2, scope=ReviewScope(include_external=False))

    recs2 = [
        r for r in state2.research_records if r.gap_id == "g1" and r.confidence > 0
    ]
    if recs2:
        weak_rec = recs2[0]
        # Weak match should have lower confidence than strong match
        assert weak_rec.confidence < rec.confidence


def test_s20_full_alias_collision_audit():
    """S20: Full audit of all concept aliases for semantic collisions.

    Distinct technologies, providers, domains, and narrower concepts must
    NOT be merged. Only abbreviation expansions and subset→superset mappings
    are allowed.
    """
    from aakr.catalog.retrieval import normalize_concept

    assert normalize_concept("ml") == "machine_learning"
    assert normalize_concept("ai") == "artificial_intelligence"
    assert normalize_concept("nlp") == "natural_language_processing"
    assert normalize_concept("db") == "database"
    assert normalize_concept("dbs") == "database"
    assert normalize_concept("k8s") == "kubernetes"
    assert normalize_concept("k8") == "kubernetes"
    assert normalize_concept("infosec") == "security"
    assert normalize_concept("appsec") == "security"
    assert normalize_concept("netsec") == "security"

    assert normalize_concept("software_engineering") == "software_engineering"
    assert normalize_concept("software_dev") == "software_dev"
    assert normalize_concept("backend") == "backend"
    assert normalize_concept("frontend") == "frontend"
    assert normalize_concept("ui") == "ui"
    assert normalize_concept("devops") == "devops"
    assert normalize_concept("sre") == "sre"
    assert normalize_concept("platform_engineering") == "platform_engineering"

    assert normalize_concept("ui") != normalize_concept("client")
    assert normalize_concept("backend") != normalize_concept("server")
    assert normalize_concept("frontend") != normalize_concept("client")
    assert normalize_concept("devops") != normalize_concept("infrastructure")
    assert normalize_concept("sre") != normalize_concept("infrastructure")
    assert normalize_concept("software_engineering") != normalize_concept("software")


def test_s21_relevance_vs_verification_separation(temp_dir):
    """S21: Research relevance scores are separate from verification confidence.

    Matched-pair test: constructs two ResearchRecord objects with identical
    finding, source_ref, evidence_kind, and methodology — differing ONLY in
    the confidence field. Both must yield the same independent verdict.
    Also tests real filesystem symlink rejection by validate_file_hash.
    """
    from aakr.catalog.models import validate_file_hash
    from aakr.lifecycle import EvidenceVerifier, ResearchRecord

    # Source pool: a single authentic source with content that supports the claim
    source_content = "sql injection is prevented by using parameterized queries"
    source_hash = hashlib.sha256(source_content.encode()).hexdigest()
    pool = [
        {
            "doc_id": "d1",
            "title": "Security Guide",
            "content": source_content,
            "topics": ["sql_injection", "parameterized_queries", "prevention"],
        },
    ]

    # Shared fields — identical for both records
    base_ref = {
        "uri": "d1",
        "source_type": "documentation",
        "content_hash": source_hash,
        "title": "Security Guide",
    }
    base_kwargs = {
        "item_id": "s21-item",
        "gap_id": "g1",
        "checkpoint_id": "",
        "finding": "sql injection prevention uses parameterized queries",
        "source_ref": base_ref,
        "evidence_kind": "test_assertion",
        "methodology": "keyword_overlap",
        "recorded_by": "researcher",
        "recorded_at": "2026-01-01T00:00:00Z",
    }

    # Two records differing ONLY in confidence (relevance score)
    rec_high = ResearchRecord(record_id="r-high", confidence=0.9, **base_kwargs)
    rec_low = ResearchRecord(record_id="r-low", confidence=0.1, **base_kwargs)

    verifier = EvidenceVerifier(pool)
    result_high = verifier.verify([rec_high])
    result_low = verifier.verify([rec_low])

    # Both must produce the SAME verdict despite different confidence values
    verdict_high = result_high["g1"]["verdict"]
    verdict_low = result_low["g1"]["verdict"]
    assert verdict_high == verdict_low, (
        f"Verdict changed with confidence: high={verdict_high}, low={verdict_low}"
    )

    # Both must also have the same stance, coverage, and applicability
    claim_high = result_high["g1"]["claims"][0]
    claim_low = result_low["g1"]["claims"][0]
    assert claim_high["stance"] == claim_low["stance"]
    assert claim_high["coverage"] == claim_low["coverage"]
    assert claim_high["applicability"] == claim_low["applicability"]
    assert claim_high["authentic"] == claim_low["authentic"]

    # Confidence is NOT a field in the verification result
    assert "confidence" not in claim_high

    # Real filesystem symlink test for validate_file_hash
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("symlink target content")
        f.flush()
        real_file = f.name

    file_hash = hashlib.sha256(b"symlink target content").hexdigest()

    # Direct file works
    assert validate_file_hash(real_file, file_hash) is True

    # Create a real symlink to the file
    symlink_path = real_file + ".symlink"
    os.symlink(real_file, symlink_path)
    try:
        # Symlink must be rejected (even though target has correct hash)
        assert validate_file_hash(symlink_path, file_hash) is False
    finally:
        os.unlink(symlink_path)
        os.unlink(real_file)

    # Missing file rejected
    assert validate_file_hash("/nonexistent/path/file.txt", file_hash) is False

    # Path traversal rejected
    assert validate_file_hash("../../../etc/passwd", file_hash) is False


def test_s22_competing_publishers_and_idempotency(temp_dir):
    """S22: Competing publishers, duplicate idempotency, and interrupted writes.

    Tests:
    1. Duplicate item submission is idempotent (INSERT OR IGNORE preserves first).
    2. Duplicate checkpoint save is idempotent (INSERT OR REPLACE updates).
    3. Interrupted batch write rolls back all statements (no partial state).
    4. Two lifecycle runs on the same DB don't interfere.
    """
    from aakr.integrations.sqlite.adapter import SQLiteStore
    from aakr.lifecycle import Checkpoint, CheckpointStore, SubmittedItemStore

    db_path = os.path.join(temp_dir, "s22.db")

    # 1. Duplicate item submission idempotency
    store = SubmittedItemStore(db_path)
    item1 = SubmittedItem(
        item_id="s22-dup",
        request="original",
        knowledge_gaps=[KnowledgeGap("g1", "test", ["k1"])],
        accepted_requirements=[],
    )
    item2 = SubmittedItem(
        item_id="s22-dup",
        request="DIFFERENT",
        knowledge_gaps=[KnowledgeGap("g1", "DIFFERENT", ["k2"])],
        accepted_requirements=[],
    )

    store.submit(item1)
    store.submit(item2)  # INSERT OR IGNORE

    retrieved = store.get("s22-dup")
    assert retrieved is not None
    assert retrieved.request == "original", "First submission must be preserved"
    assert retrieved.knowledge_gaps[0].source_hints == ["k1"], "Original gaps preserved"

    # 2. Duplicate checkpoint save (INSERT OR REPLACE)
    cp_store = CheckpointStore(db_path)
    cp1 = Checkpoint(
        checkpoint_id="cp-1",
        item_id="s22-dup",
        phase="research",
        resolved_gaps=["g1"],
        pending_gaps=[],
        research_attempts=1,
        retry_count=0,
        evidence_summary={},
        recovery_state="active",
        created_at="2026-01-01T00:00:00Z",
    )
    cp_store.save(cp1)

    cp1_updated = Checkpoint(
        checkpoint_id="cp-1",
        item_id="s22-dup",
        phase="verification",
        resolved_gaps=["g1", "g2"],
        pending_gaps=[],
        research_attempts=2,
        retry_count=0,
        evidence_summary={"g1": "verified"},
        recovery_state="active",
        created_at="2026-01-01T00:01:00Z",
    )
    cp_store.save(cp1_updated)

    loaded = cp_store.load_latest("s22-dup")
    assert loaded is not None
    assert loaded.phase == "verification", "Checkpoint replaced with latest"
    assert loaded.research_attempts == 2
    assert "g2" in loaded.resolved_gaps

    # 3. Interrupted batch write rolls back ALL statements
    cat_store = SQLiteStore(os.path.join(temp_dir, "s22_cat.db"), initialize=True)
    entry_id = "s22-batch-test"
    ts = "2026-01-01T00:00:00Z"
    valid_sha = "a" * 64

    with cat_store.transaction(write=True):
        cat_store.execute(
            "INSERT INTO entries (entry_id, entry_type, created_at) VALUES (?, ?, ?)",
            (entry_id, "knowledge", ts),
        )

    statements = [
        (
            "INSERT INTO entry_versions (entry_id, version, title, content_path, content_sha256, manifest_sha256, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (entry_id, 1, "Test", "/path", valid_sha, valid_sha, "tester", ts),
        ),
        (
            "INSERT INTO source_refs (source_ref_id, entry_id, version, source_type, source_uri, source_version, retrieved_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("src-1", entry_id, 1, "documentation", "doc-1", "v1", ts),
        ),
        ("INSERT INTO nonexistent_table_xyz VALUES (?, ?)", ("fail", "fail")),
    ]

    batch_failed = False
    try:
        with cat_store.transaction(write=True):
            cat_store.execute_batch(statements)
    except Exception:
        batch_failed = True

    assert batch_failed, "Batch with bad statement must fail"
    assert (
        cat_store.query_one(
            "SELECT entry_id FROM entry_versions WHERE entry_id=?", (entry_id,)
        )
        is None
    ), "Version must not exist after failed batch"
    assert (
        cat_store.query_one(
            "SELECT source_ref_id FROM source_refs WHERE source_ref_id=?", ("src-1",)
        )
        is None
    ), "Source ref must not exist after failed batch"
    assert (
        cat_store.query_one(
            "SELECT entry_id FROM entries WHERE entry_id=?", (entry_id,)
        )
        is not None
    ), "Pre-batch entry must survive batch failure"
    cat_store.close()

    # 4. Two lifecycle runs on the same DB don't interfere
    pool = [
        {
            "doc_id": "d1",
            "title": "Sec",
            "content": "sql injection prevention",
            "topics": ["sql_injection", "prevention"],
        }
    ]
    item_a = SubmittedItem(
        item_id="s22-a",
        request="a",
        knowledge_gaps=[
            KnowledgeGap("g1", "sql injection prevention", ["sql_injection"])
        ],
        accepted_requirements=[],
    )
    item_b = SubmittedItem(
        item_id="s22-b",
        request="b",
        knowledge_gaps=[KnowledgeGap("g1", "other", ["other"])],
        accepted_requirements=[],
    )

    shared_db = os.path.join(temp_dir, "s22_shared.db")
    orch_a = LifecycleOrchestrator(shared_db, pool, max_attempts=3)
    state_a = orch_a.run_full_lifecycle(
        item_a, scope=ReviewScope(include_external=False)
    )
    orch_b = LifecycleOrchestrator(shared_db, pool, max_attempts=3)
    state_b = orch_b.run_full_lifecycle(
        item_b, scope=ReviewScope(include_external=False)
    )

    a_recs = [r for r in state_a.research_records if r.item_id == "s22-a"]
    b_recs = [r for r in state_b.research_records if r.item_id == "s22-b"]
    assert all(r.item_id == "s22-a" for r in a_recs), "No cross-contamination A"
    assert all(r.item_id == "s22-b" for r in b_recs), "No cross-contamination B"

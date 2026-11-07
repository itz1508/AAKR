"""Document-library retrieval evaluation for the AAKR lifecycle.

Paired comparison: identical requests through the lifecycle vs baseline retrieval.
Measures coverage, evidence quality, time, cost, gap resolution.

Interrupt/resume test: lifecycle survives interruption, resumes from persisted
checkpoint, and advances only when new evidence addresses remaining gaps.
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any

from aakr.lifecycle import (
    AcceptedRequirement,
    KnowledgeGap,
    LifecycleOrchestrator,
    SubmittedItem,
)


@dataclass
class Document:
    doc_id: str
    title: str
    content: str
    domain: str
    topics: list[str]
    source_type: str = "documentation"


class DocumentLibrary:
    """A document library serving as the source pool for research."""

    def __init__(self):
        self._docs: dict[str, Document] = {}

    def add(self, doc: Document):
        self._docs[doc.doc_id] = doc

    def search(self, keywords: list[str]) -> list[Document]:
        results = []
        for doc in self._docs.values():
            doc_text = (
                doc.title + " " + doc.content + " " + " ".join(doc.topics)
            ).lower()
            if any(kw.lower() in doc_text for kw in keywords if len(kw) > 2):
                results.append(doc)
        return results

    @property
    def size(self):
        return len(self._docs)

    def as_source_pool(self) -> list[dict[str, Any]]:
        """Return documents as source pool dicts for the AutonomousResearcher."""
        return [
            {
                "doc_id": d.doc_id,
                "title": d.title,
                "content": d.content,
                "domain": d.domain,
                "topics": d.topics,
            }
            for d in self._docs.values()
        ]


@dataclass
class PairedMetrics:
    """Metrics from paired comparison: lifecycle vs baseline."""

    item_id: str
    # Lifecycle metrics
    lifecycle_resolved: bool
    lifecycle_gaps_resolved: int
    lifecycle_gaps_total: int
    lifecycle_sources_found: int
    lifecycle_time_ms: float
    lifecycle_research_attempts: int
    # Baseline metrics
    baseline_sources_found: int
    baseline_time_ms: float
    # Comparison
    coverage_delta: float  # lifecycle - baseline
    time_delta_ms: float
    evidence_quality: float  # avg confidence of lifecycle records
    gap_resolution: float  # fraction of gaps resolved


class PairedComparisonEvaluator:
    """Compares lifecycle-guided research against baseline retrieval.

    Same request through both paths. Measures which approach finds more
    relevant sources, resolves more gaps, and at what cost.
    """

    def __init__(self, library: DocumentLibrary):
        self.library = library
        self._temp_dir = tempfile.mkdtemp(prefix="aakr-eval-")
        self._db_path = os.path.join(self._temp_dir, "eval.db")

    def evaluate(self, item: SubmittedItem) -> PairedMetrics:
        # Baseline: direct keyword search (no lifecycle)
        t0 = time.perf_counter()
        baseline_sources = set()
        for gap in item.knowledge_gaps + item.accepted_requirements:
            keywords = gap.source_hints + gap.description.split()
            docs = self.library.search(keywords)
            for d in docs:
                baseline_sources.add(d.doc_id)
        baseline_time = (time.perf_counter() - t0) * 1000

        # Lifecycle: guided research through the full pipeline
        orchestrator = LifecycleOrchestrator(
            self._db_path, self.library.as_source_pool()
        )
        t0 = time.perf_counter()
        state = orchestrator.run_full_lifecycle(item)
        lifecycle_time = (time.perf_counter() - t0) * 1000

        # Collect lifecycle metrics
        lifecycle_sources = set()
        total_confidence = 0.0
        records_with_evidence = 0
        for rec in state.research_records:
            src_uri = (
                rec.source_ref.get("uri", "")
                if isinstance(rec.source_ref, dict)
                else rec.source_uri
            )
            if src_uri:
                lifecycle_sources.add(src_uri)
                total_confidence += rec.confidence
                records_with_evidence += 1

        total_gaps = len(item.knowledge_gaps) + len(item.accepted_requirements)
        resolved_gaps = len(state.checkpoint.resolved_gaps) if state.checkpoint else 0

        return PairedMetrics(
            item_id=item.item_id,
            lifecycle_resolved=state.resolved,
            lifecycle_gaps_resolved=resolved_gaps,
            lifecycle_gaps_total=total_gaps,
            lifecycle_sources_found=len(lifecycle_sources),
            lifecycle_time_ms=round(lifecycle_time, 2),
            lifecycle_research_attempts=state.checkpoint.research_attempts
            if state.checkpoint
            else 0,
            baseline_sources_found=len(baseline_sources),
            baseline_time_ms=round(baseline_time, 2),
            coverage_delta=round(len(lifecycle_sources) - len(baseline_sources), 2),
            time_delta_ms=round(lifecycle_time - baseline_time, 2),
            evidence_quality=round(total_confidence / records_with_evidence, 3)
            if records_with_evidence
            else 0.0,
            gap_resolution=round(resolved_gaps / total_gaps, 3) if total_gaps else 0.0,
        )

    def close(self):
        import shutil

        shutil.rmtree(self._temp_dir, ignore_errors=True)


class InterruptResumeTest:
    """Tests that the lifecycle survives interruption and resumes correctly.

    1. Start lifecycle on an unresolved item
    2. Interrupt after first research cycle (simulated by running first cycle)
    3. Resume from persisted checkpoint
    4. Verify it advances only when new evidence addresses remaining gaps
    """

    def __init__(self, library: DocumentLibrary):
        self.library = library
        self._temp_dir = tempfile.mkdtemp(prefix="aakr-resume-")
        self._db_path = os.path.join(self._temp_dir, "resume.db")

    def test_interrupt_resume(
        self, item: SubmittedItem, expanded_pool: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Run lifecycle, interrupt, expand source pool, resume.

        expanded_pool: sources available only after interruption (simulates
        new evidence becoming available). If None, resume uses same pool.
        """
        results: dict[str, Any] = {}

        # Phase 1: Initial lifecycle run with limited pool
        # Use a pool that only covers SOME gaps (simulates incomplete research)
        limited_pool = [
            s
            for s in self.library.as_source_pool()
            if s["doc_id"] in self._get_limited_ids(item)
        ]
        orch1 = LifecycleOrchestrator(self._db_path, limited_pool)
        state1 = orch1.run_full_lifecycle(item)
        results["after_first_cycle"] = {
            "resolved": state1.resolved,
            "resolved_gaps": state1.checkpoint.resolved_gaps
            if state1.checkpoint
            else [],
            "remaining_gaps": state1.remaining_gaps,
            "phase": state1.phase,
            "attempts": state1.checkpoint.research_attempts if state1.checkpoint else 0,
        }

        # Simulate interruption: the process stops here.
        # The checkpoint is persisted in the SQLite database.

        # Phase 2: Resume with expanded pool (new evidence available)
        resume_pool = expanded_pool or self.library.as_source_pool()
        orch2 = LifecycleOrchestrator(self._db_path, resume_pool)
        state2 = orch2.resume(item.item_id)
        results["after_resume"] = {
            "resolved": state2.resolved,
            "resolved_gaps": state2.checkpoint.resolved_gaps
            if state2.checkpoint
            else [],
            "remaining_gaps": state2.remaining_gaps,
            "phase": state2.phase,
            "attempts": state2.checkpoint.research_attempts if state2.checkpoint else 0,
        }

        # Verify: resumed checkpoint should have more resolved gaps
        results["advanced"] = len(results["after_resume"]["resolved_gaps"]) > len(
            results["after_first_cycle"]["resolved_gaps"]
        )
        results["evidence_addressed_remaining"] = len(
            results["after_resume"]["remaining_gaps"]
        ) < len(results["after_first_cycle"]["remaining_gaps"])

        return results

    def _get_limited_ids(self, item: SubmittedItem) -> set[str]:
        """Return doc_ids for only the first gap (simulates incomplete research)."""
        if not item.knowledge_gaps:
            return set()
        first_gap = item.knowledge_gaps[0]
        docs = self.library.search(
            first_gap.source_hints + first_gap.description.split()
        )
        return {d.doc_id for d in docs[:1]}  # Only first matching doc

    def close(self):
        import shutil

        shutil.rmtree(self._temp_dir, ignore_errors=True)


def create_test_library() -> DocumentLibrary:
    """Create a test document library with 8 documents."""
    lib = DocumentLibrary()
    docs = [
        Document(
            "doc1",
            "Python Type Hints Guide",
            "Use type hints for all public functions",
            "software",
            ["python_typing", "type_annotation"],
        ),
        Document(
            "doc2",
            "SQL Injection Prevention",
            "Use parameterized queries to prevent injection",
            "security",
            ["sql_injection", "parameterized_queries"],
        ),
        Document(
            "doc3",
            "Delta Lake Best Practices",
            "Use append-only tables for audit logs",
            "data",
            ["delta_lake", "append_only"],
        ),
        Document(
            "doc4",
            "Unity Catalog Permissions",
            "Grant SELECT on tables through catalogs",
            "data",
            ["unity_catalog", "permissions"],
        ),
        Document(
            "doc5",
            "PySpark Partitioning Guide",
            "Repartition by key for skewed joins",
            "software",
            ["pyspark_partitioning", "spark_repartition"],
        ),
        Document(
            "doc6",
            "RFC 8785 JCS Canonicalization",
            "Canonical JSON serialization with SHA-256",
            "software",
            ["canonical_json", "rfc8785"],
        ),
        Document(
            "doc7",
            "Test-Driven Development",
            "Write tests before implementation",
            "software",
            ["test_driven", "pytest"],
        ),
        Document(
            "doc8",
            "Data Lineage Tracking",
            "Track provenance across pipelines",
            "data",
            ["data_lineage", "provenance"],
        ),
    ]
    for doc in docs:
        lib.add(doc)
    return lib


def create_test_items() -> list[SubmittedItem]:
    """Create test research items with knowledge gaps."""
    items = []

    # Item 1: All gaps resolvable from library
    items.append(
        SubmittedItem(
            item_id="research-typing",
            request="Verify Python typing standards for the project",
            knowledge_gaps=[
                KnowledgeGap(
                    "gap1",
                    "python type annotation requirements",
                    ["python_typing", "type_annotation"],
                ),
                KnowledgeGap(
                    "gap2",
                    "canonical JSON serialization standard",
                    ["canonical_json", "rfc8785"],
                ),
            ],
            accepted_requirements=[
                AcceptedRequirement(
                    "req1", "type hints on public functions", ["python_typing"]
                ),
            ],
        )
    )

    # Item 2: One gap unresolvable (no matching document)
    items.append(
        SubmittedItem(
            item_id="research-security",
            request="Verify security standards for SQL injection prevention",
            knowledge_gaps=[
                KnowledgeGap(
                    "gap1",
                    "SQL injection prevention methods",
                    ["sql_injection", "parameterized_queries"],
                ),
                KnowledgeGap(
                    "gap2",
                    "kubernetes network security policy",
                    ["kubernetes_security", "network_policy"],
                ),
            ],
            accepted_requirements=[],
        )
    )

    # Item 3: Any_of style — multiple gaps, all resolvable
    items.append(
        SubmittedItem(
            item_id="research-data",
            request="Verify data engineering lineage tracking",
            knowledge_gaps=[
                KnowledgeGap(
                    "gap1",
                    "delta lake append-only pattern",
                    ["delta_lake", "append_only"],
                ),
                KnowledgeGap(
                    "gap2",
                    "data lineage and provenance tracking",
                    ["data_lineage", "provenance"],
                ),
            ],
            accepted_requirements=[
                AcceptedRequirement(
                    "req1", "lineage across pipeline stages", ["data_lineage"]
                ),
            ],
        )
    )

    return items

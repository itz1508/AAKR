"""Evaluated Refinement: bounded evaluator-optimizer for AAKR ingestion.

Generalizes the Anthropic evaluator-optimizer pattern with AAKR's
invariants (its four notebook weaknesses are rejected by construction):
- BOUNDED: never while-True. Termination predicates are exact and every
  run ends with a termination_reason in TERMINATION_REASONS. Limits
  (max_iterations, llm call budget, wall timeout) are set by the Host.
- DETERMINISTIC-FIRST: the injected deterministic validator always runs;
  the (potentially LLM-backed) evaluator is invoked ONLY when the
  deterministic report is clean AND semantic criteria are present.
- NO ACCUMULATED CONTEXT: evaluator and refiner receive only the current
  candidate, the original task, source evidence, the latest feedback, and
  the criteria - never prior attempts or conversation history.
- OBSERVABLE ARTIFACTS ONLY: no chain-of-thought is recorded. Records carry
  hashes, findings, patches, and evidence references.
- REGRESSION-AWARE: every revision is evaluated against the COMPLETE
  criteria set; a fix that breaks a previously valid criterion surfaces as
  a finding and can fail the run gate.
- HOST AUTHORITY: the continuation policy callback decides whether another
  refinement runs; this module defines NO publish path. Publication remains
  Curator-governed; drafts only.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from aakr.catalog.models import CatalogError
from aakr.extraction import ExtractedRecord, ExtractionSchema, SourceRef, validate_record

TERMINATION_REASONS = ("satisfied", "budget_exhausted", "oscillation", "host_stopped", "max_iterations")
FINDING_STATUSES = ("valid", "not_valid", "unevaluated")
OVERALL_STATUSES = ("valid", "not_valid", "unevaluated")

# Mirror of extraction._TYPE_MAP (kept local so refinement adds no coupling
# to extraction internals beyond the public validate_record contract).
_TYPE_MAP = {"str": str, "int": int, "float": float, "bool": bool, "list": list, "dict": dict}

# Deterministic checks of extraction.validate_record -> rubric criteria.
# Factual support is NOT mapped here: in Application A it is judged
# semantically by the evaluator against source evidence.
_CHECK_TO_CRITERION = {
    "schema_valid": "schema_structure",
    "field_types_valid": "schema_structure",
    "required_fields_present": "parameter_completeness",
    "hash_integrity": "hash_integrity",
    "provenance_present": "provenance",
}


# Patch sentinel: remove the named field (targeted repairs must be able to
# delete unexpected fields, not just set values).
DELETE: Any = object()


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _sha(obj: Any) -> str:
    return hashlib.sha256(_canonical(obj).encode()).hexdigest()


@dataclass(frozen=True)
class Criterion:
    """One evaluation requirement. kind: 'deterministic' | 'semantic'."""

    name: str
    kind: str
    description: str = ""

    def __post_init__(self):
        if not self.name.strip():
            raise CatalogError("criterion requires a nonblank name")
        if self.kind not in ("deterministic", "semantic"):
            raise CatalogError(f"criterion kind must be deterministic|semantic, got {self.kind!r}")


@dataclass(frozen=True)
class EvaluationCriteria:
    """A versioned rubric for one artifact type."""

    artifact_type: str
    version: int
    criteria: tuple[Criterion, ...]

    def __post_init__(self):
        if not self.artifact_type.strip():
            raise CatalogError("criteria requires a nonblank artifact_type")
        if self.version < 1:
            raise CatalogError("criteria version must be >= 1")
        if not self.criteria:
            raise CatalogError("criteria must contain at least one criterion")
        names = [c.name for c in self.criteria]
        if len(names) != len(set(names)):
            raise CatalogError(f"duplicate criterion names: {sorted(names)}")

    @property
    def semantic(self) -> tuple[Criterion, ...]:
        return tuple(c for c in self.criteria if c.kind == "semantic")

    @property
    def deterministic(self) -> tuple[Criterion, ...]:
        return tuple(c for c in self.criteria if c.kind == "deterministic")


@dataclass
class Finding:
    """One criterion's evaluated status with evidence and correction."""

    criterion: str
    status: str  # valid | not_valid | unevaluated
    evidence_refs: list[str] = field(default_factory=list)
    finding: str = ""  # populated when not_valid
    correction: str = ""  # actionable guidance when not_valid

    def __post_init__(self):
        if self.status not in FINDING_STATUSES:
            raise CatalogError(f"finding status must be one of {FINDING_STATUSES}, got {self.status!r}")
        if self.status == "not_valid" and (not self.finding.strip() or not self.correction.strip()):
            raise CatalogError("not_valid findings require both a finding and a correction")
        if self.status == "valid" and (self.finding or self.correction):
            raise CatalogError("valid findings must not carry a finding or correction")

    def as_dict(self) -> dict[str, Any]:
        return {
            "criterion": self.criterion,
            "status": self.status,
            "evidence_refs": list(self.evidence_refs),
            "finding": self.finding,
            "correction": self.correction,
        }


@dataclass
class EvaluationReport:
    """Findings over the COMPLETE criteria set, hash-chained for the run."""

    evaluator: str  # evaluator profile (for provenance; no provider binding)
    findings: list[Finding]
    overall_status: str = ""
    report_hash: str = ""

    def __post_init__(self):
        if not self.overall_status:
            statuses = {f.status for f in self.findings}
            if statuses and statuses <= {"valid"}:
                self.overall_status = "valid"
            elif "not_valid" in statuses:
                self.overall_status = "not_valid"
            else:
                self.overall_status = "unevaluated"
        if self.overall_status not in OVERALL_STATUSES:
            raise CatalogError(f"overall_status must be one of {OVERALL_STATUSES}")
        if not self.report_hash:
            self.report_hash = _sha(
                {"evaluator": self.evaluator, "findings": [f.as_dict() for f in self.findings]}
            )

    def valid_count(self) -> int:
        return sum(1 for f in self.findings if f.status == "valid")

    def failing_set(self) -> tuple[str, ...]:
        return tuple(sorted(f.criterion for f in self.findings if f.status == "not_valid"))

    def corrections(self) -> list[str]:
        return [f.correction for f in self.findings if f.status == "not_valid"]


@dataclass
class CandidateArtifact:
    """The current generated content under evaluation."""

    artifact_id: str
    artifact_type: str
    payload: dict[str, Any]
    source_refs: list[SourceRef]
    task: str = ""
    content_hash: str = ""

    def __post_init__(self):
        if not self.artifact_id.strip() or not self.artifact_type.strip():
            raise CatalogError("artifact requires artifact_id and artifact_type")
        if not self.source_refs:
            raise CatalogError("artifact requires at least one SourceRef (provenance)")
        if not self.content_hash:
            self.content_hash = _sha(
                {"artifact_id": self.artifact_id, "artifact_type": self.artifact_type, "payload": self.payload}
            )

    def with_patches(self, patches: list[tuple[str, Any]]) -> "CandidateArtifact":
        """Apply targeted patches: unmodified fields stay byte-equal; the
        source_refs list identity is preserved (provenance is never rewritten)."""
        if not patches:
            raise CatalogError("a revision requires at least one patch")
        new_payload = dict(self.payload)
        for fname, value in patches:
            if not str(fname).strip():
                raise CatalogError("patch field names must be nonblank")
            if value is DELETE:
                if fname not in new_payload:
                    raise CatalogError(f"DELETE patch must target an existing field: {fname!r}")
                del new_payload[fname]
            else:
                new_payload[fname] = value
        return CandidateArtifact(
            artifact_id=self.artifact_id,
            artifact_type=self.artifact_type,
            payload=new_payload,
            source_refs=self.source_refs,
            task=self.task,
        )


@dataclass(frozen=True)
class RefinementRevision:
    """One immutable refinement step in the run's hash chain."""

    revision_no: int
    parent_hash: str | None  # content_hash of the parent artifact
    content_hash: str  # content_hash of the artifact AFTER patches
    patches: tuple[tuple[str, Any], ...]
    report_hash: str  # evaluation report the patches responded to

    def __post_init__(self):
        if self.revision_no < 1:
            raise CatalogError("revision_no must be >= 1")
        if not self.patches:
            raise CatalogError("a revision must record at least one patch")


@dataclass
class RefinementLimits:
    """Host-owned loop limits."""

    max_iterations: int = 3
    call_budget: int = 6  # max combined evaluator+refiner calls
    wall_timeout_seconds: float = 60.0

    def __post_init__(self):
        if self.max_iterations < 1:
            raise CatalogError("max_iterations must be >= 1")
        if self.call_budget < 1:
            raise CatalogError("call_budget must be >= 1")
        if self.wall_timeout_seconds <= 0:
            raise CatalogError("wall_timeout_seconds must be > 0")


@dataclass
class RefinementRun:
    """Observable execution record: costs, limits, history, termination."""

    limits: RefinementLimits
    llm_calls: int = 0
    iterations: int = 0
    started_at: float = field(default_factory=time.monotonic)
    elapsed_seconds: float = 0.0
    termination_reason: str = ""
    revisions: list[RefinementRevision] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)  # per-evaluation: content_hash, report_hash, valid_count, failing_set

    def __post_init__(self):
        if self.termination_reason and self.termination_reason not in TERMINATION_REASONS:
            raise CatalogError(f"termination_reason must be one of {TERMINATION_REASONS}")


@dataclass
class RefinementResult:
    """Final artifact + report + run record. Never a publication."""

    artifact: CandidateArtifact
    report: EvaluationReport
    run: RefinementRun


# Injected callable protocols (LLM or test double; no provider binding).
Validator = Callable[[CandidateArtifact, EvaluationCriteria], EvaluationReport]
Evaluator = Callable[[CandidateArtifact, EvaluationCriteria, str], EvaluationReport]  # (artifact, criteria, source_text)
Refiner = Callable[[CandidateArtifact, EvaluationReport, str, str], list[tuple[str, Any]]]  # (artifact, report, task, source_text) -> patches
Policy = Callable[[EvaluationReport, RefinementRun], bool]


def default_policy(report: EvaluationReport, run: RefinementRun) -> bool:
    """Host-default continuation rule: continue only when the count of valid
    criteria strictly increased over the previous revision (no history is
    passed around; the run record supplies the previous counts)."""
    if len(run.history) < 2:
        return True  # first evaluation: allow one refinement attempt
    return run.history[-1]["valid_count"] > run.history[-2]["valid_count"]


def _evaluate(
    current: CandidateArtifact,
    criteria: EvaluationCriteria,
    validator: Validator,
    evaluator: Evaluator | None,
    run: RefinementRun,
    source_text: str,
) -> EvaluationReport:
    """Deterministic-first layered evaluation.

    The deterministic validator ALWAYS runs. The (potentially LLM-backed)
    evaluator is invoked ONLY when the deterministic report is clean AND
    semantic criteria are present. When both run, findings merge over the
    complete criteria set (semantic findings override on name collision).
    """
    det = validator(current, criteria)
    if det.overall_status != "valid" or not criteria.semantic or evaluator is None:
        return det
    sem = evaluator(current, criteria, source_text)
    run.llm_calls += 1
    merged: dict[str, Finding] = {f.criterion: f for f in det.findings}
    for f in sem.findings:
        merged[f.criterion] = f
    return EvaluationReport(evaluator=sem.evaluator, findings=list(merged.values()))


def refine(
    artifact: CandidateArtifact,
    criteria: EvaluationCriteria,
    *,
    validator: Validator,
    evaluator: Evaluator | None = None,
    refiner: Refiner | None = None,
    policy: Policy | None = None,
    limits: RefinementLimits | None = None,
    source_text: str = "",
) -> RefinementResult:
    """Run the bounded evaluator-optimizer loop. Never while-True.

    Termination precedence (exact predicates, first match wins):
      1. satisfied          - every criterion valid
      2. budget_exhausted   - llm_calls > call_budget OR elapsed > wall_timeout
      3. oscillation        - artifact content_hash recurred in this run, OR the
                              failing-criterion set repeated identically twice,
                              OR the refiner produced no patches (no progress)
      4. max_iterations     - iterations >= max_iterations
      5. host_stopped       - the continuation policy declined
    The cycle bound is structural: at most max_iterations refinement steps
    can execute, after which the loop cannot continue.

    Evaluator/refiner receive only the current candidate, task, source
    evidence, and latest feedback - no prior attempts, no conversation
    history. The result is never a publication: drafts only.
    """
    limits = limits or RefinementLimits()
    policy_fn = policy or default_policy
    if criteria.semantic and evaluator is None:
        raise CatalogError("semantic criteria require an injected evaluator")
    if refiner is None:
        raise CatalogError("refine() requires an injected refiner")

    run = RefinementRun(limits)
    current = artifact
    report = EvaluationReport(
        evaluator="none", findings=[Finding(c.name, "unevaluated") for c in criteria.criteria]
    )

    # Structurally bounded: one cycle per allowed refinement + the final one.
    for _cycle in range(limits.max_iterations + 1):
        report = _evaluate(current, criteria, validator, evaluator, run, source_text)
        run.history.append(
            {
                "content_hash": current.content_hash,
                "report_hash": report.report_hash,
                "valid_count": report.valid_count(),
                "failing_set": list(report.failing_set()),
            }
        )

        if report.overall_status == "valid":
            run.termination_reason = "satisfied"
            break

        run.elapsed_seconds = time.monotonic() - run.started_at
        if run.llm_calls > limits.call_budget or run.elapsed_seconds > limits.wall_timeout_seconds:
            run.termination_reason = "budget_exhausted"
            break

        hashes = [h["content_hash"] for h in run.history]
        if hashes.count(current.content_hash) > 1:
            run.termination_reason = "oscillation"
            break
        failing_sets = [tuple(h["failing_set"]) for h in run.history]
        if len(failing_sets) >= 2 and failing_sets[-1] == failing_sets[-2] and failing_sets[-1]:
            run.termination_reason = "oscillation"
            break

        if run.iterations >= limits.max_iterations:
            run.termination_reason = "max_iterations"
            break

        if not policy_fn(report, run):
            run.termination_reason = "host_stopped"
            break

        patches = refiner(current, report, artifact.task, source_text)
        run.llm_calls += 1
        if not patches:
            run.termination_reason = "oscillation"  # no progress possible
            break
        new_artifact = current.with_patches(patches)
        run.iterations += 1
        run.revisions.append(
            RefinementRevision(
                revision_no=run.iterations,
                parent_hash=current.content_hash,
                content_hash=new_artifact.content_hash,
                patches=tuple(patches),
                report_hash=report.report_hash,
            )
        )
        current = new_artifact
    else:
        # Belt-only: the max_iterations check above fires first; if the cycle
        # bound is ever reached without a reason, terminate conservatively.
        run.termination_reason = run.termination_reason or "max_iterations"

    run.elapsed_seconds = time.monotonic() - run.started_at
    return RefinementResult(artifact=current, report=report, run=run)


# ---------------------------------------------------------------------------
# Application A wiring helpers: deterministic-first adapter, regression
# surfacing, and findings-to-patch mapping.
# ---------------------------------------------------------------------------


def regressed_criteria(previous: EvaluationReport, current: EvaluationReport) -> list[str]:
    """Criteria that were valid and became not_valid: the regression signal.
    A fix that breaks a previously valid criterion surfaces here."""
    before = {f.criterion: f.status for f in previous.findings}
    after = {f.criterion: f.status for f in current.findings}
    return sorted(c for c, s in after.items() if s == "not_valid" and before.get(c) == "valid")


def patch_targets(report: EvaluationReport) -> list[str]:
    """The ONLY criteria a targeted refiner may address. Untouched criteria
    keep their byte-equal field values and provenance identity."""
    return [f.criterion for f in report.findings if f.status == "not_valid"]


def extraction_validator(schema: ExtractionSchema, extracted_by: str = "refinement-adapter") -> Validator:
    """Deterministic-first layer over the proven extraction gates.

    Adapts extraction.validate_record to the Validator protocol: each
    deterministic check maps to a rubric criterion; criteria the rubric
    does not declare are skipped (the rubric governs), and checks the rubric
    leaves semantic (e.g. source_support) are not claimed here. Corrections
    carry exact actionable specifics - missing fields, unexpected fields,
    type mismatches - so a refiner can patch precisely.
    """

    def v(a: CandidateArtifact, criteria: EvaluationCriteria) -> EvaluationReport:
        rec = ExtractedRecord(
            record_id=a.artifact_id,
            record_type=a.artifact_type,
            payload=dict(a.payload),
            source_refs=a.source_refs,
            extracted_by=extracted_by,
        )
        result = validate_record(rec, schema)
        ok_by: dict[str, bool] = {}
        for check, ok in result.checks.items():
            name = _CHECK_TO_CRITERION.get(check)
            if name is None:
                continue
            ok_by[name] = ok_by.get(name, True) and ok

        corrections: dict[str, str] = {}
        if not ok_by.get("schema_structure", True):
            parts: list[str] = []
            if a.artifact_type != schema.record_type:
                parts.append(f"record_type must be {schema.record_type!r}")
            extra = sorted(set(a.payload) - set(schema.field_types))
            if extra:
                parts.append(f"remove unexpected fields: {extra}")
            bad_types = [
                f for f, t in schema.field_types.items()
                if f in a.payload and not isinstance(a.payload[f], _TYPE_MAP[t])
            ]
            if bad_types:
                parts.append(f"fix field types: {bad_types}")
            corrections["schema_structure"] = "; ".join(parts) or "schema conformance failure"
        if not ok_by.get("parameter_completeness", True):
            missing = [f for f in schema.required_fields if f not in a.payload]
            corrections["parameter_completeness"] = f"add missing required fields: {missing}"
        if not ok_by.get("hash_integrity", True):
            corrections["hash_integrity"] = "rebuild the record so content_hash matches the payload"
        if not ok_by.get("provenance", True):
            corrections["provenance"] = "attach at least one SourceRef"

        evidence = [ref.uri for ref in a.source_refs]
        findings: list[Finding] = []
        for crit in criteria.criteria:
            if crit.name not in ok_by:
                continue  # semantic criteria are the evaluator's to judge
            if ok_by[crit.name]:
                findings.append(Finding(crit.name, "valid", evidence))
            else:
                findings.append(
                    Finding(
                        crit.name, "not_valid", evidence,
                        f"{crit.name} failed deterministic validation",
                        corrections.get(crit.name, "see validation errors"),
                    )
                )
        return EvaluationReport(f"deterministic:{extracted_by}", findings)

    return v


def extraction_criteria() -> EvaluationCriteria:
    """Application A rubric: the layered extraction criteria.

    Deterministic: schema_structure, parameter_completeness, hash_integrity,
    provenance (from extraction.validate_record via extraction_validator).
    Semantic: source_support (values grounded in source evidence - judged by
    the evaluator against source_text, since a refinement's grounding cannot
    be mechanically derived from the original extractor's claims).
    """
    return EvaluationCriteria(
        "tool_reference", 1,
        (
            Criterion("schema_structure", "deterministic", "closed-schema conformance and types"),
            Criterion("parameter_completeness", "deterministic", "all required fields present"),
            Criterion("hash_integrity", "deterministic", "content_hash matches the payload"),
            Criterion("provenance", "deterministic", "at least one SourceRef"),
            Criterion("source_support", "semantic", "every value grounded in the source evidence"),
        ),
    )


def refine_extraction(
    result: "ExtractionResult",
    schema: ExtractionSchema,
    *,
    evaluator: Evaluator,
    refiner: Refiner,
    source_text: str,
    provenance: list[SourceRef],
    criteria: EvaluationCriteria | None = None,
    limits: RefinementLimits | None = None,
    policy: Policy | None = None,
    extracted_by: str = "refined-extraction",
    task: str = "",
) -> tuple["ExtractionResult", list[RefinementResult]]:
    """Application A: refine an ExtractionResult's invalid records.

    Candidates are ONLY the invalid (payload, errors) entries. Each is run
    through the bounded refine() loop; a record that reaches 'satisfied' is
    rebuilt as an ExtractedRecord, revalidated through the proven
    extraction.validate_record gate, and - only then - moved into the valid
    set. Records that do not satisfy stay invalid with their final errors
    surfaced (never silently dropped). Output flows through the unchanged
    DraftBundle -> Curator.create_draft path; no publish path exists here.

    Note on factual support: refined records carry factual_support=None - the
    grounding of refined values is judged by the semantic source_support
    criterion against source_text, not by reusing the original extractor's
    per-field claims.
    """
    from aakr.extraction import ExtractionResult, ExtractedRecord

    if not provenance:
        raise CatalogError("refine_extraction requires provenance SourceRefs")
    crit = criteria or extraction_criteria()
    validator = extraction_validator(schema, extracted_by=extracted_by)
    new_result = ExtractionResult(valid=list(result.valid))
    runs: list[RefinementResult] = []

    for i, (payload, _errors) in enumerate(result.invalid):
        if isinstance(payload, dict):
            rid = str(payload.get("record_id") or f"{schema.record_type}-ref-{i + 1}")
            clean = {k: v for k, v in payload.items() if k not in ("record_id", "factual_support")}
        else:
            rid = f"{schema.record_type}-ref-{i + 1}"
            clean = {"payload": payload}
        artifact = CandidateArtifact(
            artifact_id=rid,
            artifact_type=schema.record_type,
            payload=clean,
            source_refs=list(provenance),
            task=task,
        )
        ref = refine(
            artifact,
            crit,
            validator=validator,
            evaluator=evaluator,
            refiner=refiner,
            policy=policy,
            limits=limits,
            source_text=source_text,
        )
        runs.append(ref)
        if ref.run.termination_reason == "satisfied":
            rec = ExtractedRecord(
                record_id=rid,
                record_type=schema.record_type,
                payload=dict(ref.artifact.payload),
                source_refs=list(provenance),
                extracted_by=extracted_by,
            )
            vr = validate_record(rec, schema)
            if vr.ok:
                new_result.valid.append(rec)
            else:
                new_result.invalid.append((payload, vr.errors))
        else:
            new_result.invalid.append(
                (payload, [f"refinement {ref.run.termination_reason}: "
                           + "; ".join(ref.report.corrections() or ["unresolved criteria"])])
            )
    return new_result, runs

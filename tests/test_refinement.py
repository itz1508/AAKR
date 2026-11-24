"""Phase R1: Evaluated Refinement tests.

Records, bounded loop termination, layered (deterministic-first) evaluation,
targeted patch semantics, regression-awareness, oscillation detection, and
the extraction wiring (application A). Stdlib only - evaluator/refiner/policy
are test doubles; no provider binding; no publish path anywhere.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aakr.catalog.models import CatalogError
from aakr.extraction import SourceRef
import aakr.refinement as rm
from aakr.refinement import (
    CandidateArtifact,
    Criterion,
    EvaluationCriteria,
    EvaluationReport,
    Finding,
    RefinementLimits,
    RefinementRevision,
    RefinementRun,
)

H = "a" * 64
PRINCIPAL = "mistraldaddy@outlook.com"


def refs():
    return [SourceRef("documentation", "https://example.org/doc", H)]


def artifact(**over):
    kw = dict(artifact_id="t1", artifact_type="tool_reference",
              payload={"a": 1, "b": 2}, source_refs=refs())
    kw.update(over)
    return CandidateArtifact(**kw)


def criteria():
    return EvaluationCriteria("tool_reference", 2, (
        Criterion("schema_structure", "deterministic", "closed schema conformance"),
        Criterion("parameter_completeness", "semantic", "all documented parameters present"),
    ))


def report(**over):
    kw = dict(evaluator="test-double", findings=[
        Finding("schema_structure", "valid"),
        Finding("parameter_completeness", "not_valid", ["source://doc#s3"],
                "timeout parameter missing", "Extract the timeout parameter"),
    ])
    kw.update(over)
    return EvaluationReport(**kw)


class RecordsContract(unittest.TestCase):
    """R1.1-2 section 1: the five records' invariants and hash chaining."""

    def test_criterion_kind_and_name_invariants(self):
        with self.assertRaises(CatalogError):
            Criterion("", "deterministic")
        with self.assertRaises(CatalogError):
            Criterion("x", "probabilistic")

    def test_criteria_versioning_invariants(self):
        with self.assertRaises(CatalogError):
            EvaluationCriteria("", 1, (Criterion("x", "deterministic"),))
        with self.assertRaises(CatalogError):
            EvaluationCriteria("t", 0, (Criterion("x", "deterministic"),))
        with self.assertRaises(CatalogError):
            EvaluationCriteria("t", 1, ())
        with self.assertRaises(CatalogError):
            EvaluationCriteria("t", 1, (Criterion("x", "deterministic"), Criterion("x", "semantic")))
        c = criteria()
        self.assertEqual([x.name for x in c.deterministic], ["schema_structure"])
        self.assertEqual([x.name for x in c.semantic], ["parameter_completeness"])

    def test_finding_structure(self):
        with self.assertRaises(CatalogError):
            Finding("x", "invalid_status")
        with self.assertRaises(CatalogError):
            Finding("x", "not_valid")  # missing finding+correction
        with self.assertRaises(CatalogError):
            Finding("x", "not_valid", finding="missing", correction="")
        with self.assertRaises(CatalogError):
            Finding("x", "valid", finding="must be empty when valid")

    def test_report_overall_status_and_hash(self):
        rep = report()
        self.assertEqual(rep.overall_status, "not_valid")
        self.assertEqual(rep.valid_count(), 1)
        self.assertEqual(rep.failing_set(), ("parameter_completeness",))
        clean = EvaluationReport("test-double", [Finding("schema_structure", "valid")])
        self.assertEqual(clean.overall_status, "valid")
        # report_hash is content-derived and deterministic
        self.assertEqual(report().report_hash, report().report_hash)
        self.assertNotEqual(rep.report_hash, clean.report_hash)

    def test_artifact_hash_and_patch_preservation(self):
        a = artifact()
        b = artifact()
        self.assertEqual(a.content_hash, b.content_hash)  # deterministic
        a2 = a.with_patches([("a", 99)])
        self.assertNotEqual(a.content_hash, a2.content_hash)
        # targeted-only: unmodified fields byte-equal, provenance identity preserved
        self.assertEqual(a2.payload["b"], a.payload["b"])
        self.assertIs(a2.source_refs, a.source_refs)
        with self.assertRaises(CatalogError):
            a.with_patches([])
        with self.assertRaises(CatalogError):
            a.with_patches([("  ", 1)])

    def test_revision_hash_chain(self):
        a = artifact()
        a2 = a.with_patches([("a", 99)])
        rev = RefinementRevision(1, a.content_hash, a2.content_hash, (("a", 99),), report().report_hash)
        self.assertEqual(rev.revision_no, 1)
        self.assertEqual(rev.parent_hash, a.content_hash)
        with self.assertRaises(CatalogError):
            RefinementRevision(0, a.content_hash, a2.content_hash, (("a", 99),), "x" * 64)
        with self.assertRaises(CatalogError):
            RefinementRevision(1, a.content_hash, a2.content_hash, (), "x" * 64)

    def test_limits_invariants(self):
        with self.assertRaises(CatalogError):
            RefinementLimits(max_iterations=0)
        with self.assertRaises(CatalogError):
            RefinementLimits(call_budget=0)
        with self.assertRaises(CatalogError):
            RefinementLimits(wall_timeout_seconds=0.0)

    def test_run_records_termination_reason_enum(self):
        run = RefinementRun(RefinementLimits())
        self.assertEqual(run.termination_reason, "")
        with self.assertRaises(CatalogError):
            RefinementRun(RefinementLimits(), termination_reason="never_terminates")
        for reason in rm.TERMINATION_REASONS:
            RefinementRun(RefinementLimits(), termination_reason=reason)  # all valid

    def test_module_exposes_no_publish_path(self):
        for forbidden in ("publish", "publish_draft", "deprecate", "approve"):
            self.assertFalse(hasattr(rm, forbidden), forbidden)


class TerminationContract(unittest.TestCase):
    """R1.2-2 section 2: every termination reason fires exactly under its predicate."""

    # -- doubles -------------------------------------------------------

    @staticmethod
    def _art():
        """Fresh artifact with NO 'b' present (unfinished by design)."""
        return artifact(payload={"a": 1})

    @staticmethod
    def _validator(specs):
        """specs: {criterion_name: predicate(payload) -> bool}."""
        def v(a, c):
            findings = []
            for crit in c.criteria:
                ok = specs[crit.name](a.payload)
                findings.append(Finding(
                    crit.name, "valid" if ok else "not_valid", [],
                    "" if ok else f"{crit.name} not met",
                    "" if ok else f"fix {crit.name}"))
            return EvaluationReport("deterministic", findings)
        return v

    @staticmethod
    def _det_criteria(*names):
        return EvaluationCriteria("tool_reference", 1, tuple(Criterion(n, "deterministic") for n in names))

    @staticmethod
    def _make_refiner(fn):
        calls = {"n": 0}
        def r(a, rep, task, s):
            calls["n"] += 1
            return fn(a, rep, calls["n"])
        r.calls = calls
        return r

    # -- termination predicates ---------------------------------------

    def test_satisfied_when_all_criteria_valid(self):
        crit = self._det_criteria("completeness")
        val = self._validator({"completeness": lambda p: "b" in p})
        ref = self._make_refiner(lambda a, rep, n: [("b", 5)])
        res = rm.refine(self._art(), crit, validator=val, refiner=ref)
        self.assertEqual(res.run.termination_reason, "satisfied")
        self.assertEqual(res.report.overall_status, "valid")
        self.assertEqual(res.run.iterations, 1)
        self.assertEqual(res.run.llm_calls, 1)  # one refiner call; criteria are deterministic-only
        ev_called = {"n": 0}
        def ev(a, c, s):
            ev_called["n"] += 1
            return EvaluationReport("sem", [])
        rm.refine(self._art(), crit, validator=val, evaluator=ev, refiner=ref)
        self.assertEqual(ev_called["n"], 0)  # deterministic settled it: evaluator never invoked
        self.assertEqual(len(res.run.revisions), 1)
        self.assertEqual(res.run.revisions[0].parent_hash, res.run.history[0]["content_hash"])

    def test_max_iterations_predicate(self):
        crit = self._det_criteria("c1", "c2", "c3")
        specs = {"c1": lambda p: p.get("b", 0) >= 1, "c2": lambda p: p.get("b", 0) >= 2,
                 "c3": lambda p: p.get("b", 0) >= 3}
        ref = self._make_refiner(lambda a, rep, n: [("b", a.payload.get("b", 0) + 1)])
        res = rm.refine(self._art(), crit, validator=self._validator(specs), refiner=ref,
                        limits=RefinementLimits(max_iterations=2))
        self.assertEqual(res.run.termination_reason, "max_iterations")
        self.assertEqual(res.run.iterations, 2)  # bounded: exactly the limit, no more
        self.assertEqual(res.report.failing_set(), ("c3",))

    def test_iteration_count_never_exceeds_limit(self):
        crit = self._det_criteria("c1", "c2", "c3", "c4", "c5", "c6")
        specs = {f"c{i}": (lambda i: lambda p: p.get("b", 0) >= i)(i) for i in range(1, 7)}
        ref = self._make_refiner(lambda a, rep, n: [("b", a.payload.get("b", 0) + 1)])
        res = rm.refine(self._art(), crit, validator=self._validator(specs), refiner=ref,
                        limits=RefinementLimits(max_iterations=5),
                        policy=lambda rep, run: True)  # Host always wants more; loop still bounded
        self.assertEqual(res.run.termination_reason, "max_iterations")
        self.assertEqual(res.run.iterations, 5)

    def test_budget_exhausted_on_call_budget(self):
        crit = self._det_criteria("c1", "c2", "c3")
        specs = {"c1": lambda p: p.get("b", 0) >= 1, "c2": lambda p: p.get("b", 0) >= 2,
                 "c3": lambda p: p.get("b", 0) >= 3}
        ref = self._make_refiner(lambda a, rep, n: [("b", a.payload.get("b", 0) + 1)])
        res = rm.refine(self._art(), crit, validator=self._validator(specs), refiner=ref,
                        limits=RefinementLimits(max_iterations=10, call_budget=1))
        self.assertEqual(res.run.termination_reason, "budget_exhausted")
        self.assertLessEqual(res.run.llm_calls, 2)  # stopped promptly after exceeding
        self.assertLess(res.run.iterations, 10)     # budget beat the iteration limit

    def test_budget_exhausted_on_wall_timeout(self):
        crit = self._det_criteria("completeness")
        val = self._validator({"completeness": lambda p: False})
        ref = self._make_refiner(lambda a, rep, n: [("b", n)])
        res = rm.refine(self._art(), crit, validator=val, refiner=ref,
                        limits=RefinementLimits(wall_timeout_seconds=0.0000001))
        self.assertEqual(res.run.termination_reason, "budget_exhausted")
        self.assertEqual(res.run.iterations, 0)

    def test_oscillation_on_hash_recurrence(self):
        # refiner walks b: 0 -> 2 -> 0, returning the artifact to an earlier
        # content hash (failing sets change each cycle, so only the hash
        # recurrence can fire)
        crit = self._det_criteria("c1", "c2")
        specs = {"c1": lambda p: p.get("b", 0) >= 1, "c2": lambda p: p.get("b", 0) <= 1}
        seq = iter([2, 0])
        ref = self._make_refiner(lambda a, rep, n: [("b", next(seq))])
        res = rm.refine(artifact(payload={"a": 1, "b": 0}), crit,
                        validator=self._validator(specs), refiner=ref,
                        policy=lambda rep, run: True)
        self.assertEqual(res.run.termination_reason, "oscillation")
        self.assertEqual(res.run.iterations, 2)
        hashes = [h["content_hash"] for h in res.run.history]
        self.assertEqual(len(hashes), len(set(hashes)) + 1)  # exactly one recurrence

    def test_oscillation_on_repeated_failing_set(self):
        # refiner patches an unrelated field each time (hash always changes)
        # but the failing criterion set never changes
        crit = self._det_criteria("completeness")
        val = self._validator({"completeness": lambda p: "b" in p})
        ref = self._make_refiner(lambda a, rep, n: [("noise", n)])
        res = rm.refine(self._art(), crit, validator=val, refiner=ref)
        self.assertEqual(res.run.termination_reason, "oscillation")
        self.assertEqual(res.run.iterations, 1)
        hashes = [h["content_hash"] for h in res.run.history]
        self.assertEqual(len(hashes), len(set(hashes)))  # hashes differed; set repeated

    def test_oscillation_on_empty_patches(self):
        crit = self._det_criteria("completeness")
        val = self._validator({"completeness": lambda p: False})
        ref = self._make_refiner(lambda a, rep, n: [])  # refiner gives up
        res = rm.refine(self._art(), crit, validator=val, refiner=ref)
        self.assertEqual(res.run.termination_reason, "oscillation")
        self.assertEqual(res.run.iterations, 0)
        self.assertEqual(res.run.llm_calls, 1)

    def test_host_stopped_when_policy_declines(self):
        crit = self._det_criteria("completeness")
        val = self._validator({"completeness": lambda p: "b" in p})
        ref = self._make_refiner(lambda a, rep, n: [("b", 1)])
        res = rm.refine(self._art(), crit, validator=val, refiner=ref,
                        policy=lambda rep, run: False)
        self.assertEqual(res.run.termination_reason, "host_stopped")
        self.assertEqual(res.run.iterations, 0)
        self.assertEqual(ref.calls["n"], 0)  # refiner never invoked

    def test_satisfied_precedes_budget(self):
        crit = EvaluationCriteria("t", 1, (Criterion("completeness", "semantic"),))
        state = {"calls": 0}
        det = self._validator({"completeness": lambda p: "b" in p})
        def sem(a, c, s):
            state["calls"] += 1
            ok = "b" in a.payload
            return EvaluationReport("sem-double", [Finding("completeness", "valid" if ok else "not_valid",
                                            [], "" if ok else "b missing", "" if ok else "add b")])
        ref = self._make_refiner(lambda a, rep, n: [("b", 7)])
        res = rm.refine(self._art(), crit, validator=det, evaluator=sem, refiner=ref,
                        limits=RefinementLimits(call_budget=1))
        # cycle1: det failed (no evaluator call) -> refiner (llm_calls=1)
        # cycle2: det clean -> evaluator called (llm_calls=2 > budget) but ALL valid
        self.assertEqual(res.run.termination_reason, "satisfied")
        self.assertEqual(res.run.llm_calls, 2)
        self.assertEqual(state["calls"], 1)

    def test_default_policy_requires_strict_improvement(self):
        run = RefinementRun(RefinementLimits())
        run.history = [{"valid_count": 2}, {"valid_count": 3}]
        self.assertTrue(rm.default_policy(None, run))   # improved: continue
        run.history = [{"valid_count": 3}, {"valid_count": 3}]
        self.assertFalse(rm.default_policy(None, run))  # flat: stop
        run.history = [{"valid_count": 3}]
        self.assertTrue(rm.default_policy(None, run))   # first attempt allowed

    def test_entry_contract_validation(self):
        crit = self._det_criteria("completeness")
        val = self._validator({"completeness": lambda p: True})
        with self.assertRaises(CatalogError):
            rm.refine(self._art(), crit, validator=val, refiner=None)  # refiner required
        sem_crit = EvaluationCriteria("t", 1, (Criterion("completeness", "semantic"),))
        with self.assertRaises(CatalogError):
            rm.refine(self._art(), sem_crit, validator=val, refiner=val)  # semantic requires evaluator


class EvaluationPatchContract(unittest.TestCase):
    """R1.3-2 section 3: layered evaluation, targeted patches, regression."""

    @classmethod
    def setUpClass(cls):
        from aakr.extraction import ExtractionSchema
        cls.schema = ExtractionSchema(
            "tool_reference", 1,
            required_fields=("name", "category", "summary", "invocation", "timeout_seconds"),
            field_types={"name": "str", "category": "str", "summary": "str",
                         "invocation": "str", "timeout_seconds": "int"},
        )
        cls.criteria = EvaluationCriteria("tool_reference", 1, (
            Criterion("schema_structure", "deterministic"),
            Criterion("parameter_completeness", "deterministic"),
            Criterion("hash_integrity", "deterministic"),
            Criterion("provenance", "deterministic"),
            Criterion("source_support", "semantic"),
        ))

    def _artifact(self, payload=None, **over):
        kw = dict(artifact_id="tool-1", artifact_type="tool_reference",
                  payload=payload if payload is not None else {
                      "name": "discover_tools", "category": "retrieval",
                      "summary": "Find tools", "invocation": "GET /discover"},  # 4 of 5: no timeout
                  source_refs=refs())
        kw.update(over)
        return CandidateArtifact(**kw)

    def _sem_evaluator(self, grounded=None):
        state = {"calls": 0, "seen": []}
        def ev(a, c, s):
            state["calls"] += 1
            state["seen"].append(dict(a.payload))
            ok = (grounded(a.payload) if grounded else len(a.payload) == 5)
            return EvaluationReport("sem-double", [Finding(
                "source_support", "valid" if ok else "not_valid", [r.uri for r in a.source_refs],
                "" if ok else "values not grounded in source evidence",
                "" if ok else "ground every field in the source text")])
        return ev, state

    def test_adapter_maps_checks_to_criteria_with_precise_corrections(self):
        val = rm.extraction_validator(self.schema)
        art = self._artifact(payload={"name": "x", "category": "y", "summary": "z",
                                      "invocation": "GET /", "bad_extra": 1})
        rep = val(art, self.criteria)
        self.assertEqual(rep.failing_set(), ("parameter_completeness", "schema_structure"))
        by = {f.criterion: f for f in rep.findings}
        self.assertIn("timeout_seconds", by["parameter_completeness"].correction)  # missing named
        self.assertIn("bad_extra", by["schema_structure"].correction)              # extra named
        # semantic criteria are NOT claimed by the deterministic layer
        self.assertNotIn("source_support", [f.criterion for f in rep.findings])
        # clean artifact: all deterministic criteria valid
        clean = val(self._artifact(payload={"name": "x", "category": "y", "summary": "z",
                                            "invocation": "GET /", "timeout_seconds": 5}), self.criteria)
        self.assertEqual(clean.failing_set(), ())

    def test_adapter_respects_the_rubric(self):
        val = rm.extraction_validator(self.schema)
        tiny = EvaluationCriteria("tool_reference", 1, (Criterion("parameter_completeness", "deterministic"),))
        rep = val(self._artifact(), tiny)  # missing timeout: only declared criteria judged
        self.assertEqual([f.criterion for f in rep.findings], ["parameter_completeness"])

    def test_delete_patch_semantics(self):
        a = self._artifact()
        a2 = a.with_patches([("timeout_seconds", 5)])
        a3 = a2.with_patches([("timeout_seconds", rm.DELETE)])
        self.assertNotIn("timeout_seconds", a3.payload)
        self.assertEqual(a3.payload, a.payload)              # deletion restored the original payload
        self.assertIs(a3.source_refs, a.source_refs)
        with self.assertRaises(CatalogError):
            a.with_patches([("never_existed", rm.DELETE)])

    def test_full_loop_repairs_and_satisfies_with_layered_evaluation(self):
        val = rm.extraction_validator(self.schema)
        ev, ev_state = self._sem_evaluator()
        ref_calls = {"n": 0}
        def refiner(a, rep, task, s):
            ref_calls["n"] += 1
            targets = set(rm.patch_targets(rep))
            patches = []
            if "parameter_completeness" in targets and "timeout_seconds" not in a.payload:
                patches.append(("timeout_seconds", 30))  # value grounded in source text
            return patches
        art = self._artifact()
        res = rm.refine(art, self.criteria, validator=val, evaluator=ev, refiner=refiner)
        self.assertEqual(res.run.termination_reason, "satisfied")
        self.assertEqual(res.artifact.payload["timeout_seconds"], 30)
        # layered call discipline: evaluator ran exactly once - only after det was clean
        self.assertEqual(ev_state["calls"], 1)
        self.assertEqual(res.run.llm_calls, 2)  # one evaluator + one refiner
        # targeted-only: unmodified fields byte-equal, provenance identity preserved
        for f in ("name", "category", "summary", "invocation"):
            self.assertEqual(res.artifact.payload[f], art.payload[f])
        self.assertIs(res.artifact.source_refs, art.source_refs)
        self.assertEqual(ref_calls["n"], 1)

    def test_deterministic_first_evaluator_never_runs_when_det_fails(self):
        val = rm.extraction_validator(self.schema)
        ev, ev_state = self._sem_evaluator()
        # refiner never fixes: loop must stop via oscillation with ZERO evaluator calls
        def refiner(a, rep, task, s):
            return [("noise", "n")]
        res = rm.refine(self._artifact(), self.criteria, validator=val, evaluator=ev, refiner=refiner,
                        policy=lambda rep, run: True)  # Host forces continuation; tripwire must catch it
        self.assertEqual(ev_state["calls"], 0)  # deterministic never clean -> semantic never invoked
        self.assertEqual(res.run.termination_reason, "oscillation")

    def test_regression_aware_re_evaluation_catches_collateral_damage(self):
        val = rm.extraction_validator(self.schema)
        ev, ev_state = self._sem_evaluator(grounded=lambda p: False)  # never satisfied semantically
        cycles = {"reports": []}
        def refiner(a, rep, task, s):
            cycles["reports"].append(rep)
            targets = set(rm.patch_targets(rep))
            if "parameter_completeness" in targets and "timeout_seconds" not in a.payload:
                # collateral damage: fix timeout but corrupt a previously valid field
                return [("timeout_seconds", 30), ("name", {"oops": True})]
            if "schema_structure" in targets:
                return [("name", "discover_tools")]  # repair the collateral damage
            return []
        res = rm.refine(self._artifact(), self.criteria, validator=val, evaluator=ev,
                        refiner=refiner, policy=lambda rep, run: True,
                        limits=RefinementLimits(max_iterations=4, call_budget=8))
        # the collateral damage WAS caught: schema_structure (previously valid) not_valid at revision 2
        self.assertEqual(len(cycles["reports"]), 3)
        self.assertEqual(cycles["reports"][0].failing_set(), ("parameter_completeness",))
        self.assertIn("schema_structure", cycles["reports"][1].failing_set())  # regression visible
        self.assertEqual(rm.regressed_criteria(cycles["reports"][0], cycles["reports"][1]), ["schema_structure"])
        # after repair, det clean again and the semantic evaluator re-judged everything
        self.assertIn("source_support", cycles["reports"][2].failing_set())
        self.assertGreaterEqual(ev_state["calls"], 1)
        self.assertEqual(res.run.iterations, 2)

    def test_regressed_criteria_helper(self):
        prev = EvaluationReport("x", [Finding("a", "valid"), Finding("b", "not_valid", [], "m", "c")])
        curr = EvaluationReport("x", [Finding("a", "not_valid", [], "m", "c"), Finding("b", "valid")])
        self.assertEqual(rm.regressed_criteria(prev, curr), ["a"])
        self.assertEqual(rm.patch_targets(curr), ["a"])


class ExtractionWiringContract(unittest.TestCase):
    """R1.4-2 section 4: Application A end-to-end wiring, draft-only governance."""

    @classmethod
    def setUpClass(cls):
        from aakr.extraction import ExtractionSchema
        cls.schema = ExtractionSchema(
            "tool_reference", 1,
            required_fields=("name", "category", "summary", "invocation", "timeout_seconds"),
            field_types={"name": "str", "category": "str", "summary": "str",
                         "invocation": "str", "timeout_seconds": "int"},
        )
        cls.source_text = ("Tool discover_tools. Category: retrieval. Summary: Find tools. "
                            "Invocation: GET /discover. The request timeout_seconds is 30.")
        cls.provenance = [SourceRef("documentation", "https://example.org/doc", H)]

    def _evaluator(self, grounded=None):
        state = {"calls": 0}
        def ev(a, c, s):
            state["calls"] += 1
            ok = grounded(a.payload) if grounded else (
                len(a.payload) == 5 and a.payload.get("timeout_seconds") == 30)
            return EvaluationReport("wiring-sem-double", [Finding(
                "source_support", "valid" if ok else "not_valid", [r.uri for r in a.source_refs],
                "" if ok else "values not grounded in source evidence",
                "" if ok else "ground every field in the source text")])
        return ev, state

    @staticmethod
    def _refiner(a, rep, task, s):
        targets = set(rm.patch_targets(rep))
        if "parameter_completeness" in targets and "timeout_seconds" not in a.payload:
            # value grounded in the source text: 'timeout_seconds is 30'
            return [("timeout_seconds", 30)]
        return []

    def test_4_of_5_refined_to_5_of_5_draft_persisted_zero_publish_events(self):
        import tempfile
        from aakr.curator import Curator
        from aakr.extraction import DraftBundle, extract_records
        from aakr.integrations.sqlite.adapter import SQLiteStore

        # 1. extractor double returns a 4-of-5-parameter payload
        extractor = lambda text, schema: [{
            "record_id": "tool-1", "name": "discover_tools", "category": "retrieval",
            "summary": "Find tools", "invocation": "GET /discover",
        }]
        result = extract_records(extractor, self.source_text, self.schema, self.provenance,
                                 extracted_by="wiring-extractor")
        self.assertEqual(len(result.valid), 0)
        self.assertEqual(len(result.invalid), 1)  # missing timeout_seconds

        # 2. bounded refinement repairs it from the source evidence
        ev, ev_state = self._evaluator()
        refined, runs = rm.refine_extraction(
            result, self.schema, evaluator=ev, refiner=self._refiner,
            source_text=self.source_text, provenance=self.provenance,
            extracted_by="wiring-refiner")
        self.assertEqual(len(refined.invalid), 0)
        self.assertEqual(len(refined.valid), 1)
        self.assertEqual(runs[0].run.termination_reason, "satisfied")
        rec = refined.valid[0]
        self.assertEqual(rec.record_id, "tool-1")
        self.assertEqual(rec.payload["timeout_seconds"], 30)
        self.assertEqual(set(rec.payload), set(self.schema.required_fields))  # 5 of 5
        self.assertEqual(rec.extracted_by, "wiring-refiner")
        self.assertEqual(ev_state["calls"], 1)  # semantic layer ran once, after det was clean

        # 3. unchanged draft path: DraftBundle -> Curator.create_draft, ZERO publish events
        with tempfile.TemporaryDirectory() as d:
            store = SQLiteStore(str(Path(d) / "wiring.db"), initialize=True)
            curator = Curator(store, principal=PRINCIPAL, fixture_ascii_only=True)
            bundle, digest = DraftBundle("EXT-r1-wiring", refined.valid, PRINCIPAL).to_catalog_bundle(
                fixture_ascii_only=True)
            ret = curator.create_draft(bundle, digest)
            self.assertRegex(ret, r"^[0-9a-f]{64}$")  # returns a digest, not a publication
            # The version exists but is NOT published (state via the store)
            state = store.state_bundle("EXT-r1-wiring", 1)
            self.assertIsNotNone(state)
            statuses = state.get("status", []) if isinstance(state, dict) else state or []
            pub_events = [
                s for s in statuses
                if (s.get("status") if isinstance(s, dict) else s) == "published"
            ]
            self.assertEqual(pub_events, [], "refinement must never auto-publish")
            self.assertEqual(store.published_versions(), [])  # ZERO publish events
            store.close()

    def test_unrepairable_record_stays_invalid_with_errors_never_dropped(self):
        from aakr.extraction import ExtractionResult
        payload = {"record_id": "tool-bad", "name": "x", "category": "y"}  # 2 of 5
        result = ExtractionResult(valid=[], invalid=[(payload, ["missing required fields: [summary, invocation, timeout_seconds]"])])
        ev, _ = self._evaluator(grounded=lambda p: False)  # semantically never satisfied
        def bad_refiner(a, rep, task, s):
            return [("noise", 1)]  # never repairs
        refined, runs = rm.refine_extraction(
            result, self.schema, evaluator=ev, refiner=bad_refiner,
            source_text=self.source_text, provenance=self.provenance)
        self.assertEqual(len(refined.valid), 0)
        self.assertEqual(len(refined.invalid), 1)  # never silently dropped
        self.assertTrue(any("refinement" in e for e in refined.invalid[0][1]))
        self.assertIn(runs[0].run.termination_reason, rm.TERMINATION_REASONS)

    def test_valid_records_pass_through_untouched(self):
        from aakr.extraction import ExtractionResult, ExtractedRecord
        good = ExtractedRecord(
            record_id="tool-ok", record_type="tool_reference",
            payload={"name": "discover_tools", "category": "retrieval", "summary": "Find tools",
                     "invocation": "GET /discover", "timeout_seconds": 30},
            source_refs=self.provenance, extracted_by="wiring-extractor")
        bad = ({"record_id": "tool-1", "name": "discover_tools", "category": "retrieval",
                "summary": "Find tools", "invocation": "GET /discover"}, ["missing required fields: ['timeout_seconds']"])
        result = ExtractionResult(valid=[good], invalid=[bad])
        ev, _ = self._evaluator()
        refined, runs = rm.refine_extraction(
            result, self.schema, evaluator=ev, refiner=self._refiner,
            source_text=self.source_text, provenance=self.provenance)
        self.assertEqual(len(refined.valid), 2)
        self.assertIn(good, refined.valid)  # original valid record untouched, same object
        self.assertEqual(len(refined.invalid), 0)
        self.assertEqual(len(runs), 1)  # only the invalid record was refined

    def test_refine_extraction_requires_provenance(self):
        from aakr.extraction import ExtractionResult
        with self.assertRaises(CatalogError):
            rm.refine_extraction(ExtractionResult(), self.schema,
                                 evaluator=self._evaluator()[0], refiner=self._refiner,
                                 source_text=self.source_text, provenance=[])


class ContextDisciplineContract(unittest.TestCase):
    """Gap-fix tests: no accumulated context; observable artifacts only."""

    @classmethod
    def setUpClass(cls):
        from aakr.extraction import ExtractionSchema
        cls.schema = ExtractionSchema(
            "tool_reference", 1,
            required_fields=("name", "category", "summary"),
            field_types={"name": "str", "category": "str", "summary": "str"},
        )
        cls.criteria = EvaluationCriteria("tool_reference", 1, (
            Criterion("schema_structure", "deterministic"),
            Criterion("parameter_completeness", "deterministic"),
            Criterion("hash_integrity", "deterministic"),
            Criterion("provenance", "deterministic"),
            Criterion("source_support", "semantic"),
        ))

    def test_no_accumulated_context_reaches_evaluator_or_refiner(self):
        """The evaluator and refiner receive ONLY their pinned arguments:
        current candidate + task + evidence + latest feedback - never prior
        attempts, run history, or any accumulated conversation."""
        val = rm.extraction_validator(self.schema)
        ev_seen = []
        ref_seen = []
        def ev(a, c, s):
            ev_seen.append((a, c, s))
            ok = set(a.payload) >= {"name", "category", "summary"}
            return EvaluationReport("ctx-sem-double", [Finding(
                "source_support", "valid" if ok else "not_valid", [],
                "" if ok else "incomplete", "" if ok else "complete the fields")])
        def ref(a, rep, task, s):
            ref_seen.append((a, rep, task, s))
            if "summary" not in a.payload:
                return [("summary", "Find tools")]  # grounded in source text
            return []
        art = CandidateArtifact("t1", "tool_reference",
                                {"name": "discover_tools", "category": "retrieval"}, refs())
        res = rm.refine(art, self.criteria, validator=val, evaluator=ev, refiner=ref)
        self.assertEqual(res.run.termination_reason, "satisfied")
        self.assertEqual(res.run.iterations, 1)
        # Evaluator called once, with exactly (artifact, criteria, source_text) - no extras
        self.assertEqual(len(ev_seen), 1)
        e_a, e_c, e_s = ev_seen[0]
        self.assertIsInstance(e_a, CandidateArtifact)
        self.assertIs(e_c, self.criteria)
        self.assertIsInstance(e_s, str)
        # Refiner called once, with exactly (artifact, report, task, source_text)
        self.assertEqual(len(ref_seen), 1)
        r_a, r_rep, r_task, r_s = ref_seen[0]
        self.assertIsInstance(r_a, CandidateArtifact)
        self.assertIsInstance(r_rep, EvaluationReport)
        self.assertIsInstance(r_task, str)
        self.assertIsInstance(r_s, str)
        # The refiner saw the CURRENT artifact, not the original or any history
        self.assertNotIn("summary", r_a.payload)
        # The only feedback context is the latest report - and since the
        # deterministic layer had not yet passed, it is the DETERMINISTIC
        # report (the semantic evaluator has not run at all yet)
        self.assertTrue(r_rep.evaluator.startswith("deterministic:"))
        det = [f.correction for f in r_rep.findings if f.status == "not_valid"]
        self.assertEqual(det, ["add missing required fields: ['summary']"])
        # No prior-attempt artifacts or run history leak: findings reference
        # only the CURRENT candidate's failures
        self.assertTrue(all(f.criterion in {"parameter_completeness"}
                            for f in r_rep.findings if f.status == "not_valid"))

    def test_run_history_records_observable_artifacts_only(self):
        """RefinementRun.history carries ONLY digests and counts - never
        payloads, findings text, or any chain-of-thought."""
        val = rm.extraction_validator(self.schema)
        def ev(a, c, s):
            ok = set(a.payload) >= {"name", "category", "summary"}
            return EvaluationReport("ctx-sem-double", [Finding(
                "source_support", "valid" if ok else "not_valid", [],
                "" if ok else "incomplete", "" if ok else "complete the fields")])
        def ref(a, rep, task, s):
            return [("summary", "Find tools")] if "summary" not in a.payload else []
        art = CandidateArtifact("t1", "tool_reference",
                                {"name": "discover_tools", "category": "retrieval"}, refs())
        res = rm.refine(art, self.criteria, validator=val, evaluator=ev, refiner=ref)
        self.assertEqual(res.run.termination_reason, "satisfied")
        for entry in res.run.history:
            self.assertEqual(sorted(entry.keys()),
                             ["content_hash", "failing_set", "report_hash", "valid_count"])
            self.assertRegex(entry["content_hash"], r"^[0-9a-f]{64}$")
            self.assertRegex(entry["report_hash"], r"^[0-9a-f]{64}$")
            self.assertIsInstance(entry["valid_count"], int)
            self.assertIsInstance(entry["failing_set"], list)
        # No payload or finding text leaked into the serialized run record.
        # Patch TUPLES are recorded by design (the audit trail of exact
        # changes applied - observable, like a git diff); the candidate
        # payload itself and any findings/reasoning text must NOT appear.
        import json
        serialized = json.dumps({
            "history": res.run.history,
            "revisions": [r.__dict__ for r in res.run.revisions],
        })
        for leak in ("discover_tools", "complete the fields", "retrieval"):
            self.assertNotIn(leak, serialized)
        # The applied patch IS recorded - exact-change observability by design
        self.assertIn('["summary", "Find tools"]', serialized)


class ContractEvaluationContract(unittest.TestCase):
    """R2.2: contract optimization evaluator - draft-only, never mutates
    published rows, no publish path."""

    SRC = ("Implementation: handlers.explore.run. Input schema is an object "
           "with property q. Output schema is an object. Scope: "
           "databricks-statement-execution.")
    REFS = [SourceRef("api-docs", "https://example.org/api/explore", H)]

    @staticmethod
    def good_contract():
        return {
            "tool_contract_id": "T1-tc1", "entry_id": "T1", "version": 1,
            "input_schema": '{"type":"object","properties":{"q":{}}}',
            "output_schema": '{"type":"object"}',
            "handler_ref": "handlers.explore.run",
            "invocation_scope": "databricks-statement-execution",
        }

    def _evaluator(self, grounded=None, state=None):
        def ev(a, c, s):
            if state is not None:
                state["calls"] = state.get("calls", 0) + 1
            ok = grounded(a.payload) if grounded else (
                a.payload.get("handler_ref") == "handlers.explore.run"
                and a.payload.get("invocation_scope") == "databricks-statement-execution")
            return EvaluationReport("contract-sem-double", [Finding(
                "contract_consistency", "valid" if ok else "not_valid", [],
                "" if ok else "contract disagrees with implementation",
                "" if ok else "align the contract with the API documentation")])
        return ev

    def test_draft_contract_refined_and_persisted_as_new_draft_zero_publish(self):
        import tempfile
        from aakr.curator import Curator
        from aakr.catalog.models import manifest
        from aakr.integrations.sqlite.adapter import SQLiteStore

        draft = self.good_contract()
        draft["handler_ref"] = "handlers explore run"  # malformed: whitespace
        draft.pop("invocation_scope")  # missing field
        original = dict(draft)
        state = {"calls": 0}

        def refiner(a, rep, task, s):
            targets = set(rm.patch_targets(rep))
            patches = []
            if "handler_ref_format" in targets:
                patches.append(("handler_ref", "handlers.explore.run"))  # grounded in SRC
            if "required_fields" in targets:
                patches.append(("invocation_scope", "databricks-statement-execution"))
            return patches

        refined, run = rm.refine_contract(
            draft, evaluator=self._evaluator(state=state), refiner=refiner,
            source_text=self.SRC, provenance=self.REFS)
        self.assertEqual(run.run.termination_reason, "satisfied")
        self.assertIsNotNone(refined)
        # NEW dict with exactly the catalog contract fields; original untouched
        self.assertEqual(sorted(refined), sorted(rm.CONTRACT_FIELDS))
        self.assertEqual(draft, original)  # caller input never mutated
        self.assertNotIn("invocation_scope", draft)
        # Deterministic-first: the semantic evaluator ran once, only after det was clean
        self.assertEqual(state["calls"], 1)
        self.assertEqual(run.run.iterations, 1)

        # Persist the refined contract as part of a NEW draft tool bundle
        with tempfile.TemporaryDirectory() as d:
            store = SQLiteStore(str(Path(d) / "contract.db"), initialize=True)
            curator = Curator(store, principal=PRINCIPAL, fixture_ascii_only=True)
            bundle = {
                "entry": {"entry_id": "T1", "entry_type": "tool"},
                "version": {
                    "entry_id": "T1", "version": 1, "title": "Data Explorer Tool",
                    "content_path": "blob://T1/1", "content_sha256": H,
                    "created_by": PRINCIPAL, "created_at": "2026-11-24T01:00:00Z",
                    "supersedes_version": None,
                    "derived_from_ref": None, "derived_from_sha256": None,
                },
                "tags": [], "groups": [], "conditions": [], "sections": [],
                "tool_contracts": [dict(refined, entry_id="T1", version=1)],
                "links": [], "sources": [], "questions": [],
            }
            digest = manifest(bundle, fixture_ascii_only=True)
            curator.create_draft(bundle, digest)
            self.assertIsNotNone(store.state_bundle("T1", 1))
            self.assertEqual(store.published_versions(), [])  # ZERO publish events
            store.close()

    def test_invalid_json_schema_flagged_and_repaired(self):
        draft = self.good_contract()
        draft["input_schema"] = "{not json"
        def refiner(a, rep, task, s):
            if "schema_validity" in set(rm.patch_targets(rep)):
                return [("input_schema", '{"type":"object","properties":{"q":{}}}')]
            return []
        refined, run = rm.refine_contract(
            draft, evaluator=self._evaluator(), refiner=refiner,
            source_text=self.SRC, provenance=self.REFS)
        self.assertEqual(run.run.termination_reason, "satisfied")
        import json as _json
        self.assertEqual(_json.loads(refined["input_schema"]),
                         {"type": "object", "properties": {"q": {}}})

    def test_unrepairable_contract_returns_none_with_termination_reason(self):
        draft = self.good_contract()
        draft["version"] = 0  # identity violation the refiner never fixes
        def refiner(a, rep, task, s):
            return [("noise", 1)]  # never repairs identity_consistency
        refined, run = rm.refine_contract(
            draft, evaluator=self._evaluator(), refiner=refiner,
            source_text=self.SRC, provenance=self.REFS)
        self.assertIsNone(refined)
        self.assertIn(run.run.termination_reason, rm.TERMINATION_REASONS)
        self.assertNotEqual(run.run.termination_reason, "satisfied")

    def test_refine_contract_input_guards(self):
        good = self.good_contract()
        with self.assertRaises(CatalogError):
            rm.refine_contract(good, evaluator=self._evaluator(), refiner=lambda *a: [],
                              source_text=self.SRC, provenance=[])
        with self.assertRaises(CatalogError):
            rm.refine_contract("not-a-dict", evaluator=self._evaluator(),
                              refiner=lambda *a: [], source_text=self.SRC,
                              provenance=self.REFS)

    def test_module_exposes_no_publish_or_store_write_path(self):
        names = [n for n in dir(rm) if not n.startswith("_")]
        for banned in ("publish", "delete_version", "update_version", "promote"):
            self.assertFalse([n for n in names if banned in n],
                             f"refinement module must expose no {banned} path")


class SummaryEvaluationContract(unittest.TestCase):
    """R3.3: shadow summary refinement - derived artifacts never replace
    contracts."""

    SRC = ("The AAKR catalog stores knowledge entries. Publication is "
           "Curator-governed. All writes are append-only.")
    REFS = [SourceRef("documentation", "https://example.org/aakr-docs", H)]

    @staticmethod
    def _evaluator(state=None):
        def ev(a, c, s):
            if state is not None:
                state["calls"] = state.get("calls", 0) + 1
            text = a.payload.get("summary", "")
            covered = "entries" in text.lower() or "catalog" in text.lower()
            consistent = ("append-only" in text.lower() or "governed" in text.lower()
                          or not text)
            findings = [
                rm.Finding("coverage", "valid" if covered else "not_valid", [],
                           "" if covered else "missing key points",
                           "" if covered else "cover the catalog/entries key point"),
                rm.Finding("factual_consistency", "valid" if consistent else "not_valid", [],
                           "" if consistent else "unsupported claim",
                           "" if consistent else "remove unsupported claims"),
            ]
            return rm.EvaluationReport("summary-sem-double", findings)
        return ev

    def test_summary_refined_to_satisfaction(self):
        state = {"calls": 0}
        def refiner(a, rep, task, s):
            targets = set(rm.patch_targets(rep))
            text = a.payload.get("summary", "")
            if "coverage" in targets and "catalog" not in text.lower():
                return [("summary", "The AAKR catalog stores knowledge entries.")]
            if "factual_consistency" in targets:
                return [("summary", "The AAKR catalog stores knowledge entries. "
                                   "Publication is Curator-governed.")]
            return []
        refined, run = rm.refine_summary(
            "An orange cat naps loudly.",  # well-formed but unsupported
            evaluator=self._evaluator(state), refiner=refiner,
            source_text=self.SRC, provenance=self.REFS)
        self.assertEqual(run.run.termination_reason, "satisfied")
        self.assertIn("catalog", refined.lower())
        # deterministic layer passed from the start, so only the semantic
        # criteria were failing; the evaluator ran each iteration
        self.assertGreaterEqual(state["calls"], 1)
        self.assertLessEqual(run.run.iterations, rm.RefinementLimits().max_iterations)

    GROUNDED = "The AAKR catalog stores knowledge entries. Publication is Curator-governed."

    def test_blank_summary_fails_deterministic_layer_evaluator_skipped(self):
        state = {"calls": 0}
        def refiner(a, rep, task, s):
            if "well_formed" in set(rm.patch_targets(rep)):
                return [("summary", self.GROUNDED)]  # fixes form AND semantics
            return []
        refined, run = rm.refine_summary(
            "   ",  # blank: deterministic well_formed fails
            evaluator=self._evaluator(state), refiner=refiner,
            source_text=self.SRC, provenance=self.REFS)
        self.assertEqual(run.run.termination_reason, "satisfied")
        self.assertEqual(refined, self.GROUNDED)
        self.assertEqual(state["calls"], 1)  # only after the deterministic fix

    def test_overlong_summary_rejected_until_fixed(self):
        def refiner(a, rep, task, s):
            if "well_formed" in set(rm.patch_targets(rep)):
                return [("summary", self.GROUNDED)]
            return []
        ev = self._evaluator()
        refined, run = rm.refine_summary(
            "x" * (rm.SUMMARY_MAX_LEN + 1),
            evaluator=ev, refiner=refiner, source_text=self.SRC, provenance=self.REFS)
        self.assertEqual(run.run.termination_reason, "satisfied")
        self.assertEqual(refined, self.GROUNDED)

    def test_non_progressing_refiner_terminates_on_oscillation_not_hang(self):
        # A refiner that re-applies the same patch every iteration must be
        # stopped by the oscillation tripwire - never an infinite loop.
        def refiner(a, rep, task, s):
            return [("summary", "An orange cat naps loudly.")]  # never fixes
        refined, run = rm.refine_summary(
            "An orange cat naps loudly.",
            evaluator=self._evaluator(), refiner=refiner,
            source_text=self.SRC, provenance=self.REFS)
        self.assertIsNone(refined)
        self.assertEqual(run.run.termination_reason, "oscillation")
        self.assertLessEqual(run.run.iterations, rm.RefinementLimits().max_iterations)

    def test_refine_summary_input_guards(self):
        with self.assertRaises(CatalogError):
            rm.refine_summary("ok", evaluator=self._evaluator(), refiner=lambda *a: [],
                              source_text=self.SRC, provenance=[])
        with self.assertRaises(CatalogError):
            rm.refine_summary(42, evaluator=self._evaluator(), refiner=lambda *a: [],
                              source_text=self.SRC, provenance=self.REFS)


if __name__ == "__main__":
    unittest.main()

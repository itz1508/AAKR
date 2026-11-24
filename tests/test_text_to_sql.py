"""Text-to-SQL bounded correction tests (todo 20 / Phase E library side).

Acceptance:
- SELECT-only allowlist enforced deterministically on every revision: DML,
  DDL, GRANT, stacked statements, and comments are rejected - including
  when a 'fix' introduces them (regression-aware).
- The bounded loop repairs a non-conforming query to satisfaction when the
  refiner grounds its patches in the source evidence; non-progressing
  refiners terminate via the oscillation tripwire, never a hang.
- refine_sql performs no warehouse execution and exposes no service wiring.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aakr.refinement as rm
from aakr.catalog.models import CatalogError
from aakr.extraction import SourceRef

H = "f" * 64
PRINCIPAL = "mistraldaddy@outlook.com"

SRC = ("The catalog table is aakr_catalog.entries (entry_id, entry_type, "
       "created_at). Example: SELECT entry_id FROM aakr_catalog.entries "
       "WHERE entry_type = 'tool'.")
REFS = [SourceRef("schema-docs", "https://example.org/schema", H)]
GOOD = "SELECT entry_id FROM aakr_catalog.entries WHERE entry_type = 'tool'"


def evaluator(state=None, conforms=None):
    def ev(a, c, s):
        if state is not None:
            state["calls"] = state.get("calls", 0) + 1
        sql = (a.payload.get("sql") or "").strip()
        ok = (conforms(sql) if conforms else sql == GOOD)
        return rm.EvaluationReport("sql-sem-double", [rm.Finding(
            "constraint_conformance", "valid" if ok else "not_valid", [],
            "" if ok else "query does not match the request",
            "" if ok else "align the query with the requested intent")])
    return ev


class SqlAllowlist(unittest.TestCase):
    def _report(self, sql):
        art = rm.CandidateArtifact("sql", "read_only_sql", {"sql": sql}, REFS)
        rep = rm.sql_validator()(art)
        return {f.criterion: f.status for f in rep.findings}

    def test_valid_select_and_with_select_pass(self):
        self.assertEqual(self._report(GOOD),
                         {"single_statement": "valid", "read_only": "valid",
                          "comment_free": "valid"})
        with_q = "WITH t AS (SELECT 1 AS x) SELECT x FROM t"
        self.assertEqual(self._report(with_q)["read_only"], "valid")
        self.assertEqual(self._report(GOOD + ";")["single_statement"], "valid")

    def test_dml_ddl_grant_and_miscoding_rejected(self):
        for sql in (
            "DELETE FROM aakr_catalog.entries",
            "UPDATE aakr_catalog.entries SET entry_id = 'x'",
            "INSERT INTO aakr_catalog.entries VALUES (1)",
            "DROP TABLE aakr_catalog.entries",
            "CREATE TABLE t (x INT)",
            "ALTER TABLE t ADD COLUMN y INT",
            "TRUNCATE TABLE t",
            "MERGE INTO t USING s ON 1=1",
            "GRANT SELECT ON t TO public",
            "copy (SELECT 1) to '/tmp/x'",
            "VACUUM",
        ):
            self.assertEqual(self._report(sql)["read_only"], "not_valid", sql)

    def test_stacked_statements_and_comments_rejected(self):
        self.assertEqual(self._report("SELECT 1; DROP TABLE t")["single_statement"], "not_valid")
        self.assertEqual(self._report("SELECT 1 -- trailing")["comment_free"], "not_valid")
        self.assertEqual(self._report("SELECT 1 /* block */")["comment_free"], "not_valid")
        # classic injection shape: quote breakout + comment
        inj = "SELECT * FROM t WHERE name = 'x' OR 1=1 --'"
        self.assertEqual(self._report(inj)["comment_free"], "not_valid")
        # non-string and empty payloads never pass
        self.assertEqual(self._report("")["single_statement"], "not_valid")


class SqlRefinement(unittest.TestCase):
    def test_bounded_correction_repairs_to_satisfaction(self):
        state = {"calls": 0}
        def refiner(a, rep, task, s):
            targets = set(rm.patch_targets(rep))
            if "read_only" in targets or "single_statement" in targets or "comment_free" in targets:
                return [("sql", GOOD)]  # grounded in SRC evidence
            if "constraint_conformance" in targets:
                return [("sql", GOOD)]
            return []
        refined, run = rm.refine_sql(
            "SELECT * FROM t; DROP TABLE t -- injected",
            evaluator=evaluator(state), refiner=refiner,
            source_text=SRC, provenance=REFS)
        self.assertEqual(run.run.termination_reason, "satisfied")
        self.assertEqual(refined, GOOD)
        self.assertGreaterEqual(state["calls"], 1)  # semantics ran after the allowlist was clean

    def test_regression_aware_forbidden_fix_never_survives(self):
        # The refiner 'fixes' by introducing a forbidden clause every time:
        # the deterministic allowlist fails on every revision, so the run can
        # never reach satisfied - and the loop terminates bounded.
        def bad_refiner(a, rep, task, s):
            return [("sql", "DELETE FROM aakr_catalog.entries WHERE entry_id = 'x'")]
        refined, run = rm.refine_sql(
            "SELECT broken",
            evaluator=evaluator(), refiner=bad_refiner,
            source_text=SRC, provenance=REFS)
        self.assertIsNone(refined)
        self.assertNotEqual(run.run.termination_reason, "satisfied")
        self.assertIn(run.run.termination_reason, rm.TERMINATION_REASONS)
        self.assertLessEqual(run.run.iterations, rm.RefinementLimits().max_iterations)

    def test_non_progressing_refiner_terminates_on_oscillation(self):
        def refiner(a, rep, task, s):
            return [("sql", "SELECT nonconforming_from nowhere")]  # never fixed
        refined, run = rm.refine_sql(
            "SELECT nonconforming_from nowhere",
            evaluator=evaluator(), refiner=refiner,
            source_text=SRC, provenance=REFS)
        self.assertIsNone(refined)
        self.assertEqual(run.run.termination_reason, "oscillation")

    def test_allowlist_holds_even_when_criteria_omit_it(self):
        # The deterministic validator runs unconditionally in _evaluate and
        # its findings gate satisfaction - so a caller cannot smuggle a
        # forbidden query through by passing a criteria set that omits the
        # allowlist criteria. A permissive semantic evaluator + criteria
        # listing ONLY the semantic criterion still cannot reach satisfied.
        sneaky_criteria = rm.EvaluationCriteria("read_only_sql", 1, (
            rm.Criterion("constraint_conformance", "semantic"),
        ))
        permissive = evaluator(conforms=lambda sql: True)
        refined, run = rm.refine_sql(
            "DELETE FROM aakr_catalog.entries",
            evaluator=permissive, refiner=lambda *a: [],
            source_text=SRC, provenance=REFS, criteria=sneaky_criteria)
        self.assertIsNone(refined)
        self.assertNotEqual(run.run.termination_reason, "satisfied")
        # every recorded evaluation shows the allowlist failing
        self.assertTrue(all("read_only" in e["failing_set"] for e in run.run.history))

    def test_valid_query_round_trips_through_the_final_gate(self):
        # A fully valid query is returned unchanged via the satisfied path,
        # which re-runs the deterministic allowlist as a final gate.
        refined, run = rm.refine_sql(
            GOOD, evaluator=evaluator(), refiner=lambda *a: [],
            source_text=SRC, provenance=REFS)
        self.assertEqual(run.run.termination_reason, "satisfied")
        self.assertEqual(refined, GOOD)
        self.assertEqual(run.run.iterations, 0)

    def test_refine_sql_input_guards(self):
        with self.assertRaises(CatalogError):
            rm.refine_sql(GOOD, evaluator=evaluator(), refiner=lambda *a: [],
                          source_text=SRC, provenance=[])
        with self.assertRaises(CatalogError):
            rm.refine_sql(None, evaluator=evaluator(), refiner=lambda *a: [],
                          source_text=SRC, provenance=REFS)

    def test_no_warehouse_or_service_wiring(self):
        src = open(Path(__file__).resolve().parent.parent / "aakr" / "refinement.py").read()
        for banned in ("spark.sql", "execute_sql", "requests.", "http"):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()

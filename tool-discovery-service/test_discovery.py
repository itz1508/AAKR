"""Gap tests for the discovery pipeline (stdlib only - no Flask/SDK/network).

Covers the two gap fixes against the plan:
- Task 8 (candidate reconciliation): every ranked candidate must be an
  established tool record in the AAKR published snapshot. Ghost, draft-only,
  deprecated, and superseded candidates are never returned; drops carry
  reason codes.
- Task 9 (per-caller persona grants): grants are table-backed per caller,
  applied post-ranking; an ungranted caller sees none of that persona, and
  the ranking order of permitted results is unchanged.

Also checks the SQL statement builders (escaping + shape), which the service
uses verbatim against the catalog and grants tables.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from discovery import (
    aakr_schemas_statement,
    build_response,
    grants_statement,
    reconciliation_statement,
    schemas_statement,
)

ALL = {"researcher", "auditor", "planner", "builder"}
SCHEMA = {"t1": {"type": "object"}, "t2": {"type": "object"}, "t3": {"type": "object"}}


def _cand(tid, persona, score):
    return {
        "tool_id": tid,
        "tool_name": f"tool-{tid}",
        "persona": persona,
        "description": f"desc-{tid}",
        "similarity_score": score,
    }


def _states(**kw):
    return {k: {"state": v, "version": 1} for k, v in kw.items()}


class Reconciliation(unittest.TestCase):
    def test_published_established_returned_in_ranked_order(self):
        ranked = [_cand("t1", "researcher", 0.9), _cand("t2", "builder", 0.8), _cand("t3", "auditor", 0.7)]
        results, dropped = build_response(
            ranked,
            persona_filter=None,
            caller_grants=ALL,
            service_allowed_personas=ALL,
            catalog_states=_states(t1="published", t2="published", t3="published"),
            schemas=SCHEMA,
        )
        self.assertEqual([r["tool_id"] for r in results], ["t1", "t2", "t3"])
        self.assertEqual([r["similarity_score"] for r in results], [0.9, 0.8, 0.7])
        self.assertEqual(dropped, [])

    def test_ghost_never_returned(self):
        ranked = [_cand("ghost", "researcher", 0.95), _cand("t1", "researcher", 0.5)]
        results, dropped = build_response(
            ranked,
            persona_filter=None,
            caller_grants=ALL,
            service_allowed_personas=ALL,
            catalog_states=_states(t1="published"),  # ghost absent from catalog
            schemas=SCHEMA,
        )
        self.assertEqual([r["tool_id"] for r in results], ["t1"])
        self.assertIn({"tool_id": "ghost", "reason": "not_in_catalog"}, dropped)

    def test_unpublished_never_returned(self):
        for bad_state in ("not_published",):
            ranked = [_cand("t1", "researcher", 0.9)]
            results, dropped = build_response(
                ranked,
                persona_filter=None,
                caller_grants=ALL,
                service_allowed_personas=ALL,
                catalog_states=_states(t1=bad_state),
                schemas=SCHEMA,
            )
            self.assertEqual(results, [])
            self.assertEqual(dropped, [{"tool_id": "t1", "reason": "not_published"}])

    def test_deprecated_and_superseded_never_returned(self):
        """Deprecated and superseded records both surface as not_published."""
        ranked = [_cand("deprecated-tool", "auditor", 0.9), _cand("superseded-tool", "auditor", 0.85)]
        results, dropped = build_response(
            ranked,
            persona_filter=None,
            caller_grants=ALL,
            service_allowed_personas=ALL,
            catalog_states={
                "deprecated-tool": {"state": "not_published", "version": 1},
                "superseded-tool": {"state": "not_published", "version": 1},
            },
            schemas=SCHEMA,
        )
        self.assertEqual(results, [])
        self.assertEqual(len(dropped), 2)
        self.assertTrue(all(d["reason"] == "not_published" for d in dropped))

    def test_empty_catalog_drops_everything(self):
        ranked = [_cand("t1", "researcher", 0.9)]
        results, dropped = build_response(
            ranked,
            persona_filter=None,
            caller_grants=ALL,
            service_allowed_personas=ALL,
            catalog_states={},
            schemas=SCHEMA,
        )
        self.assertEqual(results, [])
        self.assertEqual(dropped, [{"tool_id": "t1", "reason": "not_in_catalog"}])

    def test_missing_schema_never_returned(self):
        ranked = [_cand("t1", "researcher", 0.9)]
        results, dropped = build_response(
            ranked,
            persona_filter=None,
            caller_grants=ALL,
            service_allowed_personas=ALL,
            catalog_states=_states(t1="published"),
            schemas={},
        )
        self.assertEqual(results, [])
        self.assertEqual(dropped, [{"tool_id": "t1", "reason": "schema_unresolved"}])

    def test_published_version_reported_in_results(self):
        ranked = [_cand("t1", "researcher", 0.9)]
        results, _ = build_response(
            ranked,
            persona_filter=None,
            caller_grants=ALL,
            service_allowed_personas=ALL,
            catalog_states={"t1": {"state": "published", "version": 3}},
            schemas=SCHEMA,
        )
        self.assertEqual(results[0]["version"], 3)


class PersonaGrants(unittest.TestCase):
    def test_ungranted_caller_sees_none_of_that_persona(self):
        ranked = [_cand("t1", "auditor", 0.9), _cand("t2", "researcher", 0.8)]
        results, dropped = build_response(
            ranked,
            persona_filter=None,
            caller_grants={"researcher"},  # auditor not granted to this caller
            service_allowed_personas=ALL,
            catalog_states=_states(t1="published", t2="published"),
            schemas=SCHEMA,
        )
        self.assertEqual([r["tool_id"] for r in results], ["t2"])
        self.assertIn({"tool_id": "t1", "reason": "not_granted"}, dropped)

    def test_no_grants_sees_nothing(self):
        ranked = [_cand("t1", "researcher", 0.9)]
        results, dropped = build_response(
            ranked,
            persona_filter=None,
            caller_grants=set(),  # unknown/unauthenticated caller
            service_allowed_personas=ALL,
            catalog_states=_states(t1="published"),
            schemas=SCHEMA,
        )
        self.assertEqual(results, [])
        self.assertEqual(dropped, [{"tool_id": "t1", "reason": "not_granted"}])

    def test_grants_do_not_reorder_permitted_results(self):
        ranked = [
            _cand("t1", "researcher", 0.9),
            _cand("t2", "auditor", 0.85),
            _cand("t3", "researcher", 0.8),
        ]
        results, _ = build_response(
            ranked,
            persona_filter=None,
            caller_grants={"researcher"},  # only researcher granted
            service_allowed_personas=ALL,
            catalog_states=_states(t1="published", t2="published", t3="published"),
            schemas=SCHEMA,
        )
        self.assertEqual([r["tool_id"] for r in results], ["t1", "t3"])
        self.assertEqual([r["similarity_score"] for r in results], [0.9, 0.8])

    def test_explicit_persona_filter_applies_post_ranking(self):
        ranked = [_cand("t1", "researcher", 0.9), _cand("t2", "builder", 0.8)]
        results, dropped = build_response(
            ranked,
            persona_filter="builder",
            caller_grants=ALL,
            service_allowed_personas=ALL,
            catalog_states=_states(t1="published", t2="published"),
            schemas=SCHEMA,
        )
        self.assertEqual([r["tool_id"] for r in results], ["t2"])
        self.assertIn({"tool_id": "t1", "reason": "persona_filter"}, dropped)

    def test_service_allowlist_drops_disallowed_persona(self):
        ranked = [_cand("t1", "builder", 0.9)]
        results, dropped = build_response(
            ranked,
            persona_filter=None,
            caller_grants=ALL,
            service_allowed_personas={"researcher"},  # builder off this deployment
            catalog_states=_states(t1="published"),
            schemas=SCHEMA,
        )
        self.assertEqual(results, [])
        self.assertEqual(dropped, [{"tool_id": "t1", "reason": "persona_not_allowed"}])


class Statements(unittest.TestCase):
    def test_schemas_statement_escapes_quotes(self):
        s = schemas_statement("workspace.default.tool_library", ["x'y"])
        self.assertIn("'x''y'", s)
        self.assertIn("workspace.default.tool_library", s)

    def test_grants_statement_escapes_caller(self):
        s = grants_statement("workspace.default.discovery_persona_grants", "a'b@c.d")
        self.assertIn("'a''b@c.d'", s)
        self.assertIn("caller = ", s)

    def test_reconciliation_statement_shape(self):
        s = reconciliation_statement("workspace.aakr_catalog", ["t1", "t2"])
        self.assertIn("VALUES ('t1'),('t2')", s)
        self.assertIn("workspace.aakr_catalog.entries", s)
        self.assertIn("workspace.aakr_catalog.version_current_status", s)
        self.assertIn("entry_type = 'tool'", s)
        self.assertIn("s.status = 'published'", s)
        self.assertIn("'absent'", s)
        self.assertIn("'not_published'", s)

    def test_reconciliation_statement_escapes_quotes(self):
        s = reconciliation_statement("workspace.aakr_catalog", ["it's"])
        self.assertIn("('it''s')", s)


class AAKRSchemaSource(unittest.TestCase):
    """Phase B (cutover step 6): SCHEMA_SOURCE=aakr resolves input schemas
    from the AAKR catalog of record - currently published version only."""

    AAKR = "workspace.aakr_catalog"

    def test_statement_targets_published_contracts_only(self):
        sql = aakr_schemas_statement(self.AAKR, ["t1", "t2"])
        # Published-version CTE, identical semantics to reconciliation
        self.assertIn("version_current_status", sql)
        self.assertIn("status = 'published'", sql)
        self.assertIn("entry_type = 'tool'", sql)
        self.assertIn("MAX(s.version)", sql)
        # Schemas come from tool_contracts at the published version
        self.assertIn("tool_contracts tc", sql)
        self.assertIn("tc.version = p.version", sql)
        # Row key aliased so both sources yield {tool_id: input_schema}
        self.assertIn("AS tool_id", sql)
        self.assertIn("'t1','t2'", sql)

    def test_statement_id_escaping_is_injection_safe(self):
        sql = aakr_schemas_statement(self.AAKR, ["x'; DROP TABLE entries; --"])
        self.assertIn("'x''; DROP TABLE entries; --'", sql)
        self.assertNotIn("'x';", sql)

    def test_both_sources_yield_tool_id_keyed_rows(self):
        legacy = schemas_statement("workspace.default.tool_library", ["t1"])
        aakr = aakr_schemas_statement(self.AAKR, ["t1"])
        self.assertIn("SELECT tool_id, input_schema", legacy)
        self.assertIn("SELECT p.entry_id AS tool_id, tc.input_schema", aakr)

    def test_deprecated_and_draft_versions_never_selected(self):
        sql = aakr_schemas_statement(self.AAKR, ["t1"])
        # The CTE only exposes entries whose CURRENT status is published;
        # there is no path from draft/deprecated rows to the join.
        self.assertNotIn("status = 'draft'", sql)
        self.assertNotIn("status = 'deprecated'", sql)


if __name__ == "__main__":
    unittest.main()

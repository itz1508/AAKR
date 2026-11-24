"""Phase A extraction capability tests.

Acceptance:
- Closed, versioned schemas: additional fields and wrong types rejected;
  required-field coverage enforced.
- Hash integrity: any tampered payload fails validation.
- Provenance: records require SourceRefs with 64-hex digests; unsafe URIs
  rejected at construction.
- Invalid extractions are reported with errors, never silently dropped.
- OUTPUT IS DRAFT-ONLY: the module exposes no publish path, DraftBundle is
  forced to status='draft', and a real Curator.create_draft persists the
  bundle as a draft attributed to the authenticated principal.
- Tool summaries are derived artifacts and never replace contracts.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aakr.extraction as extraction
from aakr.catalog.models import CatalogError, manifest
from aakr.curator import Curator
from aakr.extraction import (
    DraftBundle,
    ExtractionSchema,
    ExtractedRecord,
    SourceRef,
    extract_records,
    record_hash,
    summarize_tool,
    validate_record,
)
from aakr.integrations.sqlite.adapter import SQLiteStore

H = "a" * 64
PRINCIPAL = "mistraldaddy@outlook.com"


def schema():
    return ExtractionSchema(
        record_type="tool_reference",
        version=1,
        required_fields=("tool_name", "description", "input_schema"),
        field_types={"tool_name": "str", "description": "str", "input_schema": "str"},
    )


def refs():
    return [SourceRef("documentation", "https://example.org/tool-doc", H)]


def valid_record():
    return ExtractedRecord(
        record_id="tr-1",
        record_type="tool_reference",
        payload={
            "tool_name": "get_table_lineage",
            "description": "Trace table lineage",
            "input_schema": '{"type": "object"}',
        },
        source_refs=refs(),
        extracted_by="deterministic-test-extractor",
        factual_support={"tool_name": True, "description": True, "input_schema": True},
    )


class ValidationGates(unittest.TestCase):
    def test_valid_record_passes_all_checks(self):
        vr = validate_record(valid_record(), schema())
        self.assertTrue(vr.ok, vr.errors)
        self.assertEqual(
            vr.checks,
            {"schema_valid": True, "required_fields_present": True,
             "field_types_valid": True, "hash_integrity": True,
             "provenance_present": True, "factual_support": True},
        )

    def test_missing_required_field_rejected(self):
        r = valid_record()
        r.payload = {"tool_name": "x", "description": "y"}  # no input_schema
        r.content_hash = record_hash(r.record_id, r.record_type, r.payload)
        vr = validate_record(r, schema())
        self.assertFalse(vr.ok)
        self.assertFalse(vr.checks["required_fields_present"])
        self.assertTrue(any("missing required fields" in e for e in vr.errors))

    def test_field_type_mismatch_rejected(self):
        r = valid_record()
        r.payload = {"tool_name": 7, "description": "y", "input_schema": "{}"}
        r.content_hash = record_hash(r.record_id, r.record_type, r.payload)
        vr = validate_record(r, schema())
        self.assertFalse(vr.checks["field_types_valid"])

    def test_closed_schema_rejects_additional_fields(self):
        r = valid_record()
        r.payload = dict(r.payload, unexpected_extra="no additionalProperties")
        r.content_hash = record_hash(r.record_id, r.record_type, r.payload)
        vr = validate_record(r, schema())
        self.assertFalse(vr.checks["schema_valid"])
        self.assertTrue(any("unexpected fields" in e for e in vr.errors))

    def test_hash_integrity_tamper_detected(self):
        r = valid_record()
        r.payload["tool_name"] = "TAMPERED"  # hash now stale
        vr = validate_record(r, schema())
        self.assertFalse(vr.checks["hash_integrity"])
        self.assertFalse(vr.ok)

    def test_missing_provenance_rejected(self):
        r = valid_record()
        r.source_refs = []
        vr = validate_record(r, schema())
        self.assertFalse(vr.checks["provenance_present"])

    def test_unsupported_required_field_flagged(self):
        r = valid_record()
        r.factual_support = {"tool_name": True, "description": False, "input_schema": True}
        vr = validate_record(r, schema())
        self.assertFalse(vr.checks["factual_support"])


class SourceRefConstruction(unittest.TestCase):
    def test_non_hex_digest_rejected(self):
        with self.assertRaises(CatalogError):
            SourceRef("documentation", "https://x.example", "not-hex")

    def test_unsafe_uri_rejected(self):
        with self.assertRaises(CatalogError):
            SourceRef("documentation", "https://x.example' OR 1=1 --", H)
        with self.assertRaises(CatalogError):
            SourceRef("documentation", "https://x.example/a b", H)

    def test_blank_source_type_rejected(self):
        with self.assertRaises(CatalogError):
            SourceRef("  ", "https://x.example", H)


class Extraction(unittest.TestCase):
    def test_extractor_output_partitioned_valid_and_invalid(self):
        payloads = [
            {"record_id": "ok-1", "tool_name": "a", "description": "b", "input_schema": "{}",
             "factual_support": {"tool_name": True, "description": True, "input_schema": True}},
            {"record_id": "bad-1", "tool_name": 42, "description": "b", "input_schema": "{}"},
            "not a dict",
        ]
        result = extract_records(
            lambda text, sch: payloads, "source text", schema(),
            refs(), extracted_by="test-extractor",
        )
        self.assertEqual(len(result.valid), 1)
        self.assertEqual(result.valid[0].record_id, "ok-1")
        self.assertEqual(len(result.invalid), 2)
        self.assertTrue(any("type" in e for _, errs in result.invalid for e in errs))
        self.assertTrue(any("must be a dict" in e for _, errs in result.invalid for e in errs))

    def test_injection_content_hashed_safely(self):
        hostile = {"record_id": "x'; DROP TABLE entries; --", "tool_name": "a'b\"c\nd",
                   "description": "<script>", "input_schema": "{}"}
        result = extract_records(
            lambda t, s: [hostile], "src", schema(), refs(), extracted_by="e",
        )
        self.assertEqual(len(result.valid), 1)
        self.assertEqual(result.valid[0].content_hash, record_hash(
            result.valid[0].record_id, result.valid[0].record_type, result.valid[0].payload))

    def test_extraction_requires_provenance_and_extractor_profile(self):
        with self.assertRaises(CatalogError):
            extract_records(lambda t, s: [], "src", schema(), [], extracted_by="e")
        with self.assertRaises(CatalogError):
            extract_records(lambda t, s: [], "src", schema(), refs(), extracted_by="  ")


class DraftOnlyGovernance(unittest.TestCase):
    def test_module_exposes_no_publish_path(self):
        self.assertFalse(hasattr(extraction, "publish"))
        self.assertFalse(hasattr(extraction, "publish_draft"))
        self.assertFalse(hasattr(DraftBundle, "publish"))

    def test_draft_bundle_forced_to_draft(self):
        with self.assertRaises(CatalogError):
            DraftBundle("e", [valid_record()], PRINCIPAL, status="published")
        with self.assertRaises(CatalogError):
            DraftBundle("", [valid_record()], PRINCIPAL)
        with self.assertRaises(CatalogError):
            DraftBundle("e", [valid_record()], "  ")

    def test_real_curator_persists_draft_not_published(self):
        """Wiring: a DraftBundle becomes a Curator draft under the
        authenticated principal - and nothing is published."""
        with tempfile.TemporaryDirectory() as d:
            store = SQLiteStore(str(Path(d) / "t.db"), initialize=True)
            curator = Curator(store, principal=PRINCIPAL, fixture_ascii_only=True)
            bundle_obj = DraftBundle("EXT-test-entry", [valid_record()], PRINCIPAL)
            bundle, digest = bundle_obj.to_catalog_bundle(fixture_ascii_only=True)
            # Digest matches what the Curator will compute
            self.assertEqual(digest, manifest(bundle, fixture_ascii_only=True))
            curator.create_draft(bundle, digest)
            # The version exists but is NOT published (state via the store)
            state = store.state_bundle("EXT-test-entry", 1)
            self.assertIsNotNone(state)
            statuses = state.get("status", []) if isinstance(state, dict) else state or []
            pub_events = [
                s for s in statuses
                if (s.get("status") if isinstance(s, dict) else s) == "published"
            ]
            self.assertEqual(pub_events, [], "extraction must never auto-publish")

    def test_curator_rejects_principal_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            store = SQLiteStore(str(Path(d) / "t.db"), initialize=True)
            curator = Curator(store, principal="someone@else.example", fixture_ascii_only=True)
            bundle, digest = DraftBundle("EXT-mismatch", [valid_record()], PRINCIPAL).to_catalog_bundle(
                fixture_ascii_only=True)
            with self.assertRaises(CatalogError):
                curator.create_draft(bundle, digest)


class DerivedSummaries(unittest.TestCase):
    def test_summary_is_derived_artifact(self):
        s1 = summarize_tool("get_table_lineage", "Trace lineage", '{"type": "object"}')
        s2 = summarize_tool("get_table_lineage", "Trace lineage", '{"type": "object"}')
        self.assertTrue(s1["payload"]["derived"])
        self.assertFalse(s1["payload"]["replaces_contract"])
        self.assertEqual(s1["content_hash"], s2["content_hash"])  # deterministic
        self.assertEqual(s1["artifact_type"], "tool_summary")


class SchemaValidation(unittest.TestCase):
    def test_schema_requires_types_for_required_fields(self):
        with self.assertRaises(CatalogError):
            ExtractionSchema("t", 1, required_fields=("a",), field_types={})
        with self.assertRaises(CatalogError):
            ExtractionSchema("", 1, required_fields=(), field_types={})
        with self.assertRaises(CatalogError):
            ExtractionSchema("t", 0, required_fields=(), field_types={})


if __name__ == "__main__":
    unittest.main()

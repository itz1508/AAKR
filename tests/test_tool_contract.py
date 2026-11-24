"""Manifest v2 tool and playbook contract tests.

Acceptance (task 3): tool entry round-trips persisted_bundle manifest:
- tool entries require a tool_contracts child collection (input/output
  schema, handler_ref, invocation_scope) — drafts without one are rejected;
- playbook entries require ordered steps (integer ordinal) — unordered
  or non-integer steps are rejected;
- a published tool entry round-trips through persisted_bundle() with the
  exact same manifest digest (persisted v2 manifest parity);
- persona is now an allowed tag type.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))

from aakr.catalog.models import CatalogError, manifest, normalize_bundle
from aakr.curator import Curator
from aakr.integrations.sqlite.adapter import SQLiteStore

H = "c" * 64


def _bundle(eid: str = "T1", entry_type: str = "tool", version: int = 1) -> dict:
    return {
        "entry": {"entry_id": eid, "entry_type": entry_type},
        "version": {
            "entry_id": eid,
            "version": version,
            "title": "Data Explorer Tool",
            "content_path": f"blob://{eid}/{version}",
            "content_sha256": H,
            "created_by": "author",
            "created_at": "2026-11-06T01:00:00Z",
            "supersedes_version": None,
            "derived_from_ref": None,
            "derived_from_sha256": None,
        },
        "tags": [
            {
                "tag_id": f"{eid}-persona",
                "entry_id": eid,
                "version": version,
                "tag_type": "persona",
                "value": "analyst",
            }
        ],
        "groups": [],
        "conditions": [],
        "sections": [],
        "tool_contracts": (
            [
                {
                    "tool_contract_id": f"{eid}-tc1",
                    "entry_id": eid,
                    "version": version,
                    "input_schema": '{"type":"object","properties":{"q":{}}}',
                    "output_schema": '{"type":"object"}',
                    "handler_ref": "handlers.explore.run",
                    "invocation_scope": "databricks-statement-execution",
                }
            ]
            if entry_type == "tool"
            else []
        ),
        "links": [],
        "sources": [],
        "questions": [],
    }


def _playbook_bundle(eid: str = "PB1", ordered: bool = True) -> dict:
    b = _bundle(eid, entry_type="playbook")
    b["version"]["title"] = "Onboarding Playbook"
    ords = [1, 2] if ordered else [None, None]
    b["sections"] = [
        {
            "section_id": f"{eid}-s{i}",
            "entry_id": eid,
            "version": 1,
            "heading": f"Step {i+1}",
            "summary": f"Do step {i+1}",
            "ordinal": o,
        }
        for i, o in enumerate(ords)
    ]
    return b


class ToolContractValidation(unittest.TestCase):
    def test_tool_entry_requires_contract(self):
        b = _bundle()
        b["tool_contracts"] = []
        store = SQLiteStore(":memory:", initialize=True)
        curator = Curator(store, principal="author", fixture_ascii_only=True)
        with self.assertRaises(CatalogError) as cm:
            curator.create_draft(b, manifest(b, fixture_ascii_only=True))
        self.assertIn("tool contract", str(cm.exception))

    def test_tool_contract_requires_handler_and_scope(self):
        b = _bundle()
        b["tool_contracts"][0]["handler_ref"] = ""
        store = SQLiteStore(":memory:", initialize=True)
        curator = Curator(store, principal="author", fixture_ascii_only=True)
        with self.assertRaises(CatalogError) as cm:
            curator.create_draft(b, manifest(b, fixture_ascii_only=True))
        self.assertIn("handler_ref", str(cm.exception))

    def test_playbook_requires_ordered_steps(self):
        b = _playbook_bundle(ordered=False)
        store = SQLiteStore(":memory:", initialize=True)
        curator = Curator(store, principal="author", fixture_ascii_only=True)
        with self.assertRaises(CatalogError) as cm:
            curator.create_draft(b, manifest(b, fixture_ascii_only=True))
        self.assertIn("ordinal", str(cm.exception))


class ToolContractRoundTrip(unittest.TestCase):
    def test_tool_entry_round_trips_persisted_bundle(self):
        store = SQLiteStore(":memory:", initialize=True)
        curator = Curator(store, principal="author", fixture_ascii_only=True)
        b = _bundle()
        m = manifest(b, fixture_ascii_only=True)
        curator.create_draft(b, m)
        curator.publish("T1", 1)

        # The persisted bundle's manifest digest must equal the original.
        pb = store.persisted_bundle("T1", 1)
        self.assertEqual(manifest(pb, fixture_ascii_only=True), m)
        self.assertEqual(len(pb["tool_contracts"]), 1)
        tc = pb["tool_contracts"][0]
        self.assertEqual(tc["handler_ref"], "handlers.explore.run")
        self.assertEqual(tc["invocation_scope"], "databricks-statement-execution")
        self.assertTrue(tc["input_schema"].startswith('{"type"'))
        self.assertEqual(
            normalize_bundle(pb)["profile"], "oneshot-catalog-manifest-v2"
        )

    def test_persona_tag_allowed(self):
        b = _bundle()
        b["tool_contracts"] = []  # not a tool entry: switch to knowledge
        b["entry"]["entry_type"] = "knowledge"
        store = SQLiteStore(":memory:", initialize=True)
        curator = Curator(store, principal="author", fixture_ascii_only=True)
        curator.create_draft(b, manifest(b, fixture_ascii_only=True))  # persona tag accepted


class EntryTypeVocabulary(unittest.TestCase):
    def test_unknown_entry_type_rejected(self):
        b = _bundle()
        b["entry"]["entry_type"] = "gadget"
        store = SQLiteStore(":memory:", initialize=True)
        curator = Curator(store, principal="author", fixture_ascii_only=True)
        with self.assertRaises(CatalogError):
            curator.create_draft(b, manifest(b, fixture_ascii_only=True))


if __name__ == "__main__":
    unittest.main()

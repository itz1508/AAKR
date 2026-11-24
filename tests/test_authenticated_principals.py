"""Authenticated principal enforcement tests.

- Curator and Verifier refuse construction without an authenticated
  principal (no unauthenticated writes).
- A Curator cannot author a bundle whose created_by names another identity,
  and publish/deprecate actors must match the principal.
- A Verifier only appends events under its own authenticated principal, and
  the entry creator can never verify their own version — enforced in code so
  SQLite (trigger) and Databricks (no triggers) behave identically.
- Learning proposals (derived_from) require a 64-hex derived_from_sha256
  digest and a trusted validation receipt.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))

from aakr.catalog.models import CatalogError, manifest
from aakr.curator import Curator
from aakr.integrations.sqlite.adapter import SQLiteStore
from aakr.verifier import Verifier

H = "a" * 64
H2 = "b" * 64
TS = "2026-11-06T01:00:00Z"


def _bundle(eid="E", version=1, *, created_by="author"):
    return {
        "entry": {"entry_id": eid, "entry_type": "template"},
        "version": {
            "entry_id": eid,
            "version": version,
            "title": "Auto Discovery",
            "content_path": f"blob://{eid}/{version}",
            "content_sha256": H,
            "created_by": created_by,
            "created_at": TS,
            "supersedes_version": None,
            "derived_from_ref": None,
            "derived_from_sha256": None,
        },
        "tags": [],
        "groups": [
            {
                "group_id": f"{eid}-g-{version}",
                "entry_id": eid,
                "version": version,
                "group_mode": "all_of",
            },
        ],
        "conditions": [
            {
                "condition_id": f"{eid}-c-{version}",
                "entry_id": eid,
                "version": version,
                "group_id": f"{eid}-g-{version}",
                "signal": "file_signal",
                "operator": "eq",
                "value": "test",
                "required_evidence": "test_assertion",
            },
        ],
        "sections": [],
        "links": [],
        "sources": [
            {
                "source_ref_id": f"{eid}-r-{version}",
                "entry_id": eid,
                "version": version,
                "source_type": "documentation",
                "source_uri": "https://example.org/doc",
                "source_version": "1",
                "retrieved_at": TS,
                "target_condition_id": f"{eid}-c-{version}",
                "target_section_id": None,
                "target_link_id": None,
            }
        ],
        "questions": [],
        "tool_contracts": [],
    }


class PrincipalEnforcement(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteStore(":memory:", initialize=True)

    def tearDown(self):
        self.store.close()

    def _draft_and_publish(self, principal="author", **kw):
        b = _bundle(**kw)
        curator = Curator(
            self.store, principal=principal, fixture_ascii_only=True
        )
        curator.create_draft(b, manifest(b, fixture_ascii_only=True))
        curator.publish("E", 1)
        return curator

    def test_curator_requires_principal(self):
        with self.assertRaisesRegex(CatalogError, "authenticated principal"):
            Curator(self.store)
        with self.assertRaisesRegex(CatalogError, "authenticated principal"):
            Curator(self.store, principal="")
        with self.assertRaisesRegex(CatalogError, "authenticated principal"):
            Curator(self.store, principal="   ")

    def test_draft_rejects_mismatched_created_by(self):
        curator = Curator(self.store, principal="author", fixture_ascii_only=True)
        b = _bundle(created_by="impersonator")
        with self.assertRaisesRegex(CatalogError, "created_by"):
            curator.create_draft(b, manifest(b, fixture_ascii_only=True))
        self.assertEqual(
            self.store.query_one("SELECT count(*) AS n FROM entries")["n"], 0
        )

    def test_publish_actor_must_match_principal(self):
        self._draft_and_publish()
        curator = Curator(self.store, principal="author", fixture_ascii_only=True)
        b = _bundle(eid="E2")
        curator.create_draft(b, manifest(b, fixture_ascii_only=True))
        with self.assertRaisesRegex(CatalogError, "actor"):
            curator.publish("E2", 1, actor="someone-else")
        # Status attribution uses the authenticated principal by default.
        curator.publish("E2", 1)
        row = self.store.query_one(
            "SELECT actor FROM version_status "
            "WHERE entry_id='E2' AND version=1 AND status='published'"
        )
        self.assertEqual(row["actor"], "author")

    def test_deprecate_actor_must_match_principal(self):
        self._draft_and_publish()
        curator = Curator(self.store, principal="author", fixture_ascii_only=True)
        with self.assertRaisesRegex(CatalogError, "actor"):
            curator.deprecate("E", 1, actor="someone-else")
        curator.deprecate("E", 1)
        row = self.store.query_one(
            "SELECT actor FROM version_status "
            "WHERE entry_id='E' AND version=1 AND status='deprecated'"
        )
        self.assertEqual(row["actor"], "author")

    def test_verifier_requires_principal(self):
        with self.assertRaisesRegex(CatalogError, "authenticated principal"):
            Verifier(self.store)
        with self.assertRaisesRegex(CatalogError, "authenticated principal"):
            Verifier(self.store, actor_identity="  ")

    def test_verifier_identity_enforced(self):
        self._draft_and_publish()
        verifier = Verifier(self.store, actor_identity="verifier-b")
        with self.assertRaisesRegex(CatalogError, "authenticated verifier"):
            verifier.verify(
                "E-r-1", "verified", "someone-else", H2, excerpt="excerpt"
            )
        verifier.verify("E-r-1", "verified", "verifier-b", H2, excerpt="excerpt")

    def test_creator_cannot_verify_own_version(self):
        self._draft_and_publish(principal="author")
        # The creator principal may not verify its own version.
        with self.assertRaisesRegex(CatalogError, "creator cannot verify"):
            Verifier(self.store, actor_identity="author").verify(
                "E-r-1", "verified", "author", H2, excerpt="excerpt"
            )
        # A separate authenticated verifier may.
        Verifier(self.store, actor_identity="verifier-b").verify(
            "E-r-1", "verified", "verifier-b", H2, excerpt="excerpt"
        )

    def test_curator_verify_source_rejects_creator_in_code(self):
        """Code-level check (identical behavior without SQLite triggers)."""
        curator = self._draft_and_publish(principal="author")
        with self.assertRaisesRegex(CatalogError, "creator cannot verify"):
            curator.verify_source("E-r-1", "verified", "author", H2, excerpt="ex")
        curator.verify_source(
            "E-r-1", "verified", "verifier-b", H2, excerpt="ex"
        )

    def test_learning_proposal_requires_hex_digest(self):
        # Non-hex derived_from_sha256 is rejected even with a receipt callback.
        curator = Curator(
            self.store,
            principal="author",
            fixture_ascii_only=True,
            validation_receipt=lambda ref, digest: True,
        )
        b = _bundle()
        b["version"]["derived_from_ref"] = "validated/task-1"
        b["version"]["derived_from_sha256"] = "nothex"
        with self.assertRaisesRegex(CatalogError, "derived_from_sha256"):
            curator.create_draft(b, manifest(b, fixture_ascii_only=True))
        # No receipt callback at all is still rejected for trusted derivations.
        plain = Curator(self.store, principal="author", fixture_ascii_only=True)
        b2 = _bundle()
        b2["version"]["derived_from_ref"] = "validated/task-1"
        b2["version"]["derived_from_sha256"] = H2
        with self.assertRaisesRegex(CatalogError, "trusted final validation"):
            plain.create_draft(b2, manifest(b2, fixture_ascii_only=True))


if __name__ == "__main__":
    unittest.main()

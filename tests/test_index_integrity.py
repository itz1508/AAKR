"""Index generation integrity tests (digest reproducibility + honest channels).

Regression tests for the AAKR index builder contract:
- index_sha256 must be re-derivable from IndexGeneration.to_manifest()
  for both the fulltext and semantic channels.
- No channel may claim availability without content: fulltext requires a
  native full-text backend (SQLite FTS5) and rows carry searchable text;
  semantic requires real embedding vectors.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Add project root to Python path for aakr package imports
_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))

from aakr.catalog.index_builder import IndexBuilder
from aakr.catalog.models import CatalogError, manifest, sha
from aakr.curator import Curator
from aakr.integrations.sqlite.adapter import SQLiteStore

H = "c" * 64


def _bundle(eid: str = "E", version: int = 1) -> dict:
    return {
        "entry": {"entry_id": eid, "entry_type": "template"},
        "version": {
            "entry_id": eid,
            "version": version,
            "title": "Auto Discovery",
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
                "tag_id": f"{eid}-domain-{version}",
                "entry_id": eid,
                "version": version,
                "tag_type": "domain",
                "value": "software",
            }
        ],
        "groups": [],
        "conditions": [],
        "sections": [
            {
                "section_id": f"{eid}-s-{version}",
                "entry_id": eid,
                "version": version,
                "heading": "Overview",
                "summary": "Reusable discovery template",
            }
        ],
        "links": [],
        "sources": [],
        "questions": [],
    }


def _published_store() -> SQLiteStore:
    store = SQLiteStore(":memory:", initialize=True)
    curator = Curator(store, principal="author", fixture_ascii_only=True)
    b = _bundle()
    curator.create_draft(b, manifest(b, fixture_ascii_only=True))
    curator.publish("E", 1)
    return store


class IndexDigestIntegrity(unittest.TestCase):
    def test_fulltext_digest_reproducible_from_manifest(self):
        store = _published_store()
        gen = IndexBuilder(store, fixture_ascii_only=True).build_fulltext()
        self.assertEqual(gen.index_status, "available")
        self.assertTrue(gen.verify_digest(fixture_ascii_only=True))

    def test_fulltext_digest_equals_sha_of_to_manifest(self):
        store = _published_store()
        gen = IndexBuilder(store, fixture_ascii_only=True).build_fulltext()
        self.assertEqual(
            gen.index_sha256,
            sha(gen.to_manifest(), fixture_ascii_only=True),
        )

    def test_semantic_digest_reproducible_from_manifest(self):
        store = _published_store()

        def embed(texts: list[str]) -> list[list[float]]:
            return [[float(len(t) % 7), 1.0] for t in texts]

        gen = IndexBuilder(store, fixture_ascii_only=True).build_semantic(
            model_id="test-embed", dimensions=2, embed_fn=embed
        )
        self.assertIsNotNone(gen)
        self.assertEqual(gen.index_status, "available")
        self.assertTrue(gen.verify_digest(fixture_ascii_only=True))


class HonestChannels(unittest.TestCase):
    def test_fulltext_rows_carry_text_and_real_fts_index(self):
        store = _published_store()
        gen = IndexBuilder(store, fixture_ascii_only=True).build_fulltext()
        self.assertTrue(all(r["text"].strip() for r in gen.rows))
        # The native FTS5 index actually matches on published content.
        hit = store.c.execute(
            "SELECT entry_id FROM catalog_fts WHERE catalog_fts MATCH 'discovery'"
        ).fetchone()
        self.assertIsNotNone(hit)

    def test_fulltext_unavailable_without_native_backend(self):
        class NoFtsStore:
            def published_versions(self):
                return _published_store().published_versions()

        gen = IndexBuilder(NoFtsStore(), fixture_ascii_only=True).build_fulltext()
        self.assertEqual(gen.index_status, "unavailable")
        self.assertIsNone(gen.index_sha256)
        self.assertEqual(gen.rows, [])  # never content-free metadata rows

    def test_semantic_unavailable_without_embed_fn(self):
        store = _published_store()
        gen = IndexBuilder(store, fixture_ascii_only=True).build_semantic(
            model_id="m", dimensions=4
        )
        self.assertIsNone(gen)  # channel unavailable, never fabricated

    def test_semantic_rows_carry_real_vectors(self):
        store = _published_store()

        def embed(texts: list[str]) -> list[list[float]]:
            return [[0.5, 0.25] for _ in texts]

        gen = IndexBuilder(store, fixture_ascii_only=True).build_semantic(
            model_id="m", dimensions=2, embed_fn=embed
        )
        self.assertIsNotNone(gen)
        self.assertTrue(all(len(r["embedding"]) == 2 for r in gen.rows))
        self.assertEqual({r["embedding_model"] for r in gen.rows}, {"m"})

    def test_semantic_dimension_mismatch_rejected(self):
        store = _published_store()

        def embed(texts: list[str]) -> list[list[float]]:
            return [[0.5, 0.25, 0.1] for _ in texts]  # wrong dims

        with self.assertRaises(CatalogError):
            IndexBuilder(store, fixture_ascii_only=True).build_semantic(
                model_id="m", dimensions=2, embed_fn=embed
            )


if __name__ == "__main__":
    unittest.main()

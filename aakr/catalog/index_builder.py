"""Derived, reproducible search indexes.

Build structured tags first. Build an immutable full-text generation over
only published titles/summaries/content summaries that actually exist.
For SQLite, use FTS5 if available, else an explicitly labeled fallback.
For Databricks, use AI Search fulltext only after testing workspace features.
For optional semantic/vector search, record embedding model/version/dimensions.

Every index generation must have a stable ID and index_sha256 over a
canonical logical generation manifest. A hash of a mutable index alias
is not a proof of reproducibility. Always intersect hits with an
authoritative published-version snapshot.
"""

from __future__ import annotations

from typing import Any

from aakr.catalog.models import sha

INDEX_PROFILE = "oneshot-catalog-index-v1"


class IndexGeneration:
    """An immutable index generation with reproducibility proof."""

    def __init__(
        self,
        generation_id: str,
        index_sha256: str,
        rows: list[dict[str, Any]],
        tokenizer: str = "keyword",
        source_state: str = "",
        model_id: str | None = None,
        channel: str = "fulltext",
    ):
        self.generation_id = generation_id
        self.index_sha256 = index_sha256
        self.rows = rows
        self.tokenizer = tokenizer
        self.source_state = source_state
        self.model_id = model_id
        self.channel = channel

    def to_manifest(self) -> dict[str, Any]:
        return {
            "profile": INDEX_PROFILE,
            "generation_id": self.generation_id,
            "channel": self.channel,
            "tokenizer": self.tokenizer,
            "model_id": self.model_id,
            "source_state": self.source_state,
            "row_count": len(self.rows),
            "row_ids": sorted(
                [
                    r.get("entry_id", "") + ":" + str(r.get("version", ""))
                    for r in self.rows
                ]
            ),
        }


class IndexBuilder:
    """Build derived search index generations from published versions."""

    def __init__(self, store, *, fixture_ascii_only: bool = False):
        self.store = store
        self.fixture_ascii_only = fixture_ascii_only

    def build_fulltext(self, generation_id: str = "gen-1") -> IndexGeneration:
        """Build a full-text index generation over published titles/summaries.

        Only published versions are included. Drafts, superseded/deprecated
        versions are excluded. The generation is immutable once built.
        """
        published = self.store.published_versions()
        rows = []
        for v in published:
            rows.append(
                {
                    "entry_id": v["entry_id"],
                    "version": v["version"],
                    "title": v.get("title", ""),
                    "content_path": v.get("content_path", ""),
                }
            )

        manifest_data = {
            "profile": INDEX_PROFILE,
            "generation_id": generation_id,
            "channel": "fulltext",
            "tokenizer": "keyword",
            "model_id": None,
            "row_count": len(rows),
            "row_ids": sorted([r["entry_id"] + ":" + str(r["version"]) for r in rows]),
            "content_hashes": sorted([r["content_path"] for r in rows]),
        }
        index_sha = sha(manifest_data, fixture_ascii_only=self.fixture_ascii_only)
        return IndexGeneration(
            generation_id=generation_id,
            index_sha256=index_sha,
            rows=rows,
            tokenizer="keyword",
            channel="fulltext",
        )

    def build_semantic(
        self, generation_id: str = "gen-sem-1", model_id: str = "", dimensions: int = 0
    ) -> IndexGeneration | None:
        """Build a semantic/vector index generation.

        Optional. If embeddings are not available, return None and report
        channel unavailable. Do not hallucinate embeddings or similarity scores.
        """
        if not model_id or dimensions <= 0:
            return None  # Semantic search not available

        published = self.store.published_versions()
        rows = []
        for v in published:
            rows.append(
                {
                    "entry_id": v["entry_id"],
                    "version": v["version"],
                    "embedding_model": model_id,
                    "dimensions": dimensions,
                }
            )

        manifest_data = {
            "profile": INDEX_PROFILE,
            "generation_id": generation_id,
            "channel": "semantic",
            "tokenizer": "none",
            "model_id": model_id,
            "dimensions": dimensions,
            "row_count": len(rows),
            "row_ids": sorted([r["entry_id"] + ":" + str(r["version"]) for r in rows]),
        }
        index_sha = sha(manifest_data, fixture_ascii_only=self.fixture_ascii_only)
        return IndexGeneration(
            generation_id=generation_id,
            index_sha256=index_sha,
            rows=rows,
            tokenizer="none",
            model_id=model_id,
            channel="semantic",
        )

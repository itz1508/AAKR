"""Derived, reproducible search indexes.

Build structured tags first. Build an immutable full-text generation over
only published titles/summaries/content summaries that actually exist.
A functioning full-text channel requires a native full-text backend on
the store (SQLite FTS5 via create_fulltext_index); adapters without one
get an explicitly unavailable channel — never metadata-only rows. For
semantic search an embedding function must be supplied; every row then
carries a real embedding vector, and model/version/dimensions are recorded.

Every index generation has a stable ID and index_sha256 computed over
EXACTLY what IndexGeneration.to_manifest() returns, so the digest is
verifiable from the manifest alone. A hash of a mutable index alias
is not a proof of reproducibility. Always intersect hits with an
authoritative published-version snapshot — the index is never the authority.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from aakr.catalog.models import CatalogError, sha

INDEX_PROFILE = "oneshot-catalog-index-v1"


def _manifest_payload(
    *,
    generation_id: str,
    channel: str,
    tokenizer: str,
    model_id: str | None,
    source_state: str,
    rows: list[dict[str, Any]],
    content_hashes: list[str] | None = None,
    dimensions: int | None = None,
    index_status: str = "available",
) -> dict[str, Any]:
    """The canonical generation manifest — the exact payload that is hashed.

    IndexGeneration.to_manifest() reuses this builder so index_sha256 is
    always re-derivable from the manifest alone.
    """
    payload: dict[str, Any] = {
        "profile": INDEX_PROFILE,
        "generation_id": generation_id,
        "channel": channel,
        "index_status": index_status,
        "tokenizer": tokenizer,
        "model_id": model_id,
        "source_state": source_state,
        "row_count": len(rows),
        "row_ids": sorted(
            r.get("entry_id", "") + ":" + str(r.get("version", "")) for r in rows
        ),
    }
    if channel == "fulltext" and content_hashes is not None:
        payload["content_hashes"] = sorted(content_hashes)
    if channel == "semantic" and dimensions is not None:
        payload["dimensions"] = dimensions
    return payload


class IndexGeneration:
    """An immutable index generation with a reproducibility proof.

    index_sha256 is the SHA-256 of to_manifest(); verify_digest()
    re-derives and compares. An unavailable channel has a null digest,
    no rows, and index_status="unavailable".
    """

    def __init__(
        self,
        generation_id: str,
        index_sha256: str | None,
        rows: list[dict[str, Any]],
        tokenizer: str = "keyword",
        source_state: str = "",
        model_id: str | None = None,
        channel: str = "fulltext",
        content_hashes: list[str] | None = None,
        dimensions: int | None = None,
        index_status: str = "available",
    ) -> None:
        self.generation_id = generation_id
        self.index_sha256 = index_sha256
        self.rows = rows
        self.tokenizer = tokenizer
        self.source_state = source_state
        self.model_id = model_id
        self.channel = channel
        self.content_hashes = content_hashes
        self.dimensions = dimensions
        self.index_status = index_status

    def to_manifest(self) -> dict[str, Any]:
        return _manifest_payload(
            generation_id=self.generation_id,
            channel=self.channel,
            tokenizer=self.tokenizer,
            model_id=self.model_id,
            source_state=self.source_state,
            rows=self.rows,
            content_hashes=self.content_hashes,
            dimensions=self.dimensions,
            index_status=self.index_status,
        )

    def verify_digest(self, *, fixture_ascii_only: bool = False) -> bool:
        """True iff index_sha256 equals the digest of to_manifest()."""
        if self.index_status != "available" or self.index_sha256 is None:
            return False
        return sha(
            self.to_manifest(), fixture_ascii_only=fixture_ascii_only
        ) == self.index_sha256


class IndexBuilder:
    """Build derived search index generations from published versions."""

    def __init__(self, store, *, fixture_ascii_only: bool = False):
        self.store = store
        self.fixture_ascii_only = fixture_ascii_only

    def _published_text(self) -> list[dict[str, Any]]:
        """Published rows with searchable text (title + section summaries)."""
        published = self.store.published_versions()
        sections: dict[tuple[str, int], list[str]] = {}
        try:
            for row in self.store.query_all(
                "SELECT entry_id, version, heading, summary FROM content_sections"
            ):
                parts = [str(row.get("heading") or ""), str(row.get("summary") or "")]
                text = " ".join(p for p in parts if p.strip())
                if text:
                    sections.setdefault(
                        (row["entry_id"], row["version"]), []
                    ).append(text)
        except Exception:  # noqa: BLE001 — adapters without a sections table
            pass
        rows = []
        for v in published:
            text = " ".join(
                [str(v.get("title") or "")]
                + sections.get((v["entry_id"], v["version"]), [])
            ).strip()
            rows.append(
                {
                    "entry_id": v["entry_id"],
                    "version": v["version"],
                    "title": str(v.get("title") or ""),
                    "text": text,
                    "content_sha256": str(v.get("content_sha256") or ""),
                }
            )
        return rows

    def build_fulltext(self, generation_id: str = "gen-1") -> IndexGeneration:
        """Build a full-text index generation over published entries.

        Only published versions are included; drafts, superseded and
        deprecated versions are excluded. A functioning full-text channel
        requires the store to provide create_fulltext_index (SQLite FTS5).
        Without native support the channel is reported unavailable with a
        null digest and no rows — never metadata-only.
        """
        rows = self._published_text()
        create_fts = getattr(self.store, "create_fulltext_index", None)
        native = bool(callable(create_fts) and create_fts(rows))
        if not native:
            return IndexGeneration(
                generation_id=generation_id,
                index_sha256=None,
                rows=[],
                tokenizer="none",
                source_state="",
                channel="fulltext",
                content_hashes=None,
                index_status="unavailable",
            )
        content_hashes = [r["content_sha256"] for r in rows if r.get("content_sha256")]
        payload = _manifest_payload(
            generation_id=generation_id,
            channel="fulltext",
            tokenizer="keyword",
            model_id=None,
            source_state="",
            rows=rows,
            content_hashes=content_hashes,
        )
        return IndexGeneration(
            generation_id=generation_id,
            index_sha256=sha(payload, fixture_ascii_only=self.fixture_ascii_only),
            rows=rows,
            tokenizer="keyword",
            source_state="",
            channel="fulltext",
            content_hashes=content_hashes,
            index_status="available",
        )

    def build_semantic(
        self,
        generation_id: str = "gen-sem-1",
        model_id: str = "",
        dimensions: int = 0,
        *,
        embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
    ) -> IndexGeneration | None:
        """Build a semantic/vector index generation.

        Requires a model_id, positive dimensions, AND an embed_fn mapping
        texts to vectors. Without any of them the channel is unavailable
        (returns None). Never fabricate embeddings or similarity scores:
        every returned row carries a real embedding vector. The generation
        digest proves which rows are indexed and under which model
        contract; embeddings themselves are verified by re-embedding.
        """
        if not model_id or dimensions <= 0 or embed_fn is None:
            return None  # Semantic search not available

        rows = self._published_text()
        if not rows:
            return None
        vectors = embed_fn([r["text"] for r in rows])
        if len(vectors) != len(rows):
            raise CatalogError("embed_fn returned the wrong number of vectors")
        out: list[dict[str, Any]] = []
        for r, vec in zip(rows, vectors):
            if len(vec) != dimensions:
                raise CatalogError("embedding dimension mismatch")
            out.append(
                {
                    **r,
                    "embedding_model": model_id,
                    "dimensions": dimensions,
                    "embedding": [float(x) for x in vec],
                }
            )
        payload = _manifest_payload(
            generation_id=generation_id,
            channel="semantic",
            tokenizer="none",
            model_id=model_id,
            source_state="",
            rows=out,
            dimensions=dimensions,
        )
        return IndexGeneration(
            generation_id=generation_id,
            index_sha256=sha(payload, fixture_ascii_only=self.fixture_ascii_only),
            rows=out,
            tokenizer="none",
            source_state="",
            model_id=model_id,
            channel="semantic",
            dimensions=dimensions,
            index_status="available",
        )

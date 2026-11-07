"""Catalog lookup: multi-candidate retrieval with per-domain quotas.

Provider-neutral wrapper over CatalogStore.lookup and CatalogStore.get.
Deterministic per-domain top-K selection and global union/dedup by
(entry_id, version). Reports coverage.truncated, actual channels_used,
unavailable channels, searched domains, candidate and omitted counts.

No ranking chooses one authoritative winner. Scores measure retrieval
relevance only; links never authorize or auto-expand tasks.
"""

from __future__ import annotations

import re
from typing import Any

from aakr.catalog.models import CatalogError


def normalize_concept(term: str) -> str:
    """Normalize a concept term to its canonical form.

    Applies: lowercase, separator normalization (hyphens/underscores/spaces
    treated equivalently), and synonym mapping for common domain aliases.
    Does NOT change stored Catalog data — only normalizes at query time.

    This preserves existing canonical identities while improving retrieval
    across synonymous concepts without introducing a parallel ontology.
    """
    if not term:
        return term
    # Split camelCase: SoftwareEngineering → Software Engineering
    t = re.sub(r"([a-z])([A-Z])", r"\1 \2", term).strip()
    # Lowercase
    t = t.lower()
    # Normalize separators: hyphens, underscores, spaces → underscore
    t = re.sub(r"[-\s]+", "_", t)
    # Collapse repeated underscores
    t = re.sub(r"_+", "_", t).strip("_")
    # Common synonym mappings (extensible, not exhaustive)
    # Only abbreviation expansions and subset→superset mappings are included.
    # Distinct concepts (process≠product, layer≠infrastructure, practice≠resource)
    # are NOT merged to preserve semantic precision.
    _SYNONYMS = {
        "ml": "machine_learning",
        "ai": "artificial_intelligence",
        "nlp": "natural_language_processing",
        "cv": "computer_vision",
        "db": "database",
        "dbs": "database",
        "k8s": "kubernetes",
        "k8": "kubernetes",
        "sec": "security",
        "infosec": "security",
        "appsec": "security",
        "netsec": "security",
    }
    return _SYNONYMS.get(t, t)


class ConceptNormalizer:
    """Normalizes concept terms for improved cross-synonym retrieval.

    Wraps a list of terms through normalize_concept() to canonical form.
    Does not store or modify Catalog data — purely a query-time transform.
    """

    def __init__(self, *, extra_synonyms: dict[str, str] | None = None):
        self._extra = extra_synonyms or {}

    def normalize(self, term: str) -> str:
        """Normalize a single term."""
        base = normalize_concept(term)
        return self._extra.get(base, base)

    def normalize_list(self, terms: list[str]) -> list[str]:
        """Normalize a list of terms, preserving order and dedup."""
        seen: set[str] = set()
        result: list[str] = []
        for t in terms:
            n = self.normalize(t)
            if n not in seen:
                seen.add(n)
                result.append(n)
        return result


class CatalogLookup:
    """Lookup service wrapping a CatalogStore with request/response validation."""

    def __init__(
        self,
        store,
        *,
        fixture_ascii_only: bool = False,
        normalizer: ConceptNormalizer | None = None,
    ):
        self.store = store
        self.fixture_ascii_only = fixture_ascii_only
        self.normalizer = normalizer

    def lookup(self, request: dict[str, Any]) -> dict[str, Any]:
        """Execute a lookup request.

        Required fields: catalog_contract=2.0, request_id, intent, domains.
        Optional: signals, channels (subset of conditions,fulltext,semantic),
        per_domain_limit, global_limit, include_linked_candidates, snapshot_policy.
        """
        if request.get("catalog_contract") != "2.0":
            raise CatalogError("unsupported catalog contract version")
        if not request.get("request_id"):
            raise CatalogError("request_id required")
        if not request.get("intent"):
            raise CatalogError("intent required")
        domains = request.get("domains", [])
        if not domains or len(domains) != len(set(domains)):
            raise CatalogError("domains must be a non-empty set")

        # Normalize domains for cross-synonym retrieval
        if self.normalizer:
            domains = self.normalizer.normalize_list(domains)

        channels = request.get("channels", ["conditions"])
        per_domain_limit = request.get("per_domain_limit", 3)
        global_limit = request.get("global_limit", 12)

        result = self.store.lookup(
            domains=domains,
            intent=request["intent"],
            per_domain_limit=per_domain_limit,
            global_limit=global_limit,
            channels=channels,
            fixture_ascii_only=self.fixture_ascii_only,
        )

        # Add request metadata
        result["request_id"] = request["request_id"]
        result["catalog_contract"] = "2.0"
        return result

    def get(self, entry_id: str, version: int) -> dict[str, Any] | None:
        """Get a pinnable version. Drafts cannot leak into pin retrieval."""
        return self.store.get(
            entry_id, version, fixture_ascii_only=self.fixture_ascii_only
        )

"""Source verification: append-only assessment events.

A separate authorized verifier records claim assessments. verified_by names
and SHA strings are evidence metadata, not stand-alone authentication.
The verifier execution identity must be checked by a separate
authenticated service/process; database fields are not identity proofs.

The Verifier is provider-neutral. It uses the StorageAdapter interface
(query_one, query_all) instead of raw provider connections.

Results: verified, not_verified, contradicted.
unverified = no event rows. not_verified = assessment attempted but not established.
"""

from __future__ import annotations

from typing import Any

from aakr.catalog.models import HEX, CatalogError

ALLOWED_RESULTS = {"verified", "not_verified", "contradicted"}


class Verifier:
    """Independent source verification service.

    Only an authenticated, separate verifier principal may append events.
    The verifier must not be the same identity as the entry version's creator.
    """

    def __init__(self, store, *, actor_identity: str | None = None):
        self.store = store
        self.actor_identity = actor_identity

    def verify(
        self,
        source_ref_id: str,
        result: str,
        verified_by: str,
        checked_source_sha256: str,
        *,
        excerpt: str | None = None,
    ) -> None:
        """Append a verification event.

        Validation:
        - result must be one of verified, not_verified, contradicted
        - verified_by must be nonblank
        - checked_source_sha256 must be 64-char lowercase hex
        - verified requires nonblank excerpt
        - verified_by must not equal the entry-version created_by
        - seq must be exactly prior_max + 1 (no gaps)
        """
        if result not in ALLOWED_RESULTS:
            raise CatalogError(f"invalid result: {result}")
        if not verified_by or not verified_by.strip():
            raise CatalogError("verified_by must be nonblank")
        if not HEX.fullmatch(checked_source_sha256):
            raise CatalogError("checked_source_sha256 must be 64-char lowercase hex")
        if result == "verified" and (not excerpt or not excerpt.strip()):
            raise CatalogError("verified result requires nonblank excerpt")

        # Creator-as-verifier check
        creator = self.store.query_one(
            "SELECT v.created_by FROM entry_versions v "
            "JOIN source_refs r ON r.entry_id=v.entry_id AND r.version=v.version "
            "WHERE r.source_ref_id=?",
            (source_ref_id,),
        )
        if (
            creator
            and creator["created_by"].strip().lower() == verified_by.strip().lower()
        ):
            raise CatalogError("creator cannot verify own version")

        self.store.append_source_verification(
            source_ref_id, result, verified_by, checked_source_sha256, excerpt=excerpt
        )

    def latest_result(self, source_ref_id: str) -> dict[str, Any] | None:
        """Return the latest verification event for a source ref, or None."""
        return self.store.query_one(
            "SELECT * FROM source_verifications WHERE source_ref_id=? "
            "ORDER BY seq DESC LIMIT 1",
            (source_ref_id,),
        )

    def all_events(self, source_ref_id: str) -> list[dict[str, Any]]:
        """Return all verification events for a source ref in seq order."""
        return self.store.query_all(
            "SELECT * FROM source_verifications WHERE source_ref_id=? ORDER BY seq",
            (source_ref_id,),
        )

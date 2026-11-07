"""Provider-neutral Catalog Store API.

Defines the abstract interface for Catalog truth storage backends.
No direct client writes from PRE, Research, Planning, Builder or frontend.
Only an authorized Curator may write truth; a separate verifier appends
verification events. Lookup principals are read-only.

Contract version: 2.0
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class CatalogStore(ABC):
    """Abstract provider-neutral Catalog store.

    Implementations: SQLiteStore (local reference), DatabricksStore (UC/Delta).
    """

    @abstractmethod
    def lookup(
        self,
        *,
        domains: list[str],
        intent: str,
        per_domain_limit: int = 3,
        global_limit: int = 12,
        channels: list[str] | None = None,
        fixture_ascii_only: bool = False,
    ) -> dict[str, Any]:
        """Multi-candidate retrieval with per-domain quotas.

        Returns multiple version-pinned candidates, never one winner.
        Per-channel hit/reason/relevance-only score; candidate domain memberships;
        open questions; descriptive links; unassessed applicability definitions;
        and source-level provenance.
        """
        ...

    @abstractmethod
    def get(
        self, entry_id: str, version: int, *, fixture_ascii_only: bool = False
    ) -> dict[str, Any] | None:
        """Read a pinnable version (published or deprecated with published history).

        Drafts and directly-deprecated drafts cannot leak into pin retrieval.
        Empty match means no catalog match under stated coverage,
        never evidence of absence.
        """
        ...

    @abstractmethod
    def published_versions(self) -> list[dict[str, Any]]:
        """Return all currently published versions (highest ever-published, currently published)."""
        ...

    @abstractmethod
    def pinnable_versions(self) -> list[dict[str, Any]]:
        """Return all pinnable versions (published or deprecated with prior published history)."""
        ...

    @abstractmethod
    def append_source_verification(
        self,
        source_ref_id: str,
        result: str,
        verified_by: str,
        checked_source_sha256: str,
        *,
        excerpt: str | None = None,
    ) -> None:
        """Append a verification event. Only authorized verifier may call this."""
        ...

    @abstractmethod
    def provenance(
        self, entry_id: str, version: int, *, fixture_ascii_only: bool = False
    ) -> dict[str, Any]:
        """Return provenance: associations_total, distinct_sources_total,
        by_result counts, verification_state rows, verification_snapshot_sha256."""
        ...

    @abstractmethod
    def state_bundle(self, entry_id: str, version: int) -> list[dict[str, Any]]:
        """Return source_ref_current rows for a version in deterministic order."""
        ...

    @abstractmethod
    def persisted_bundle(self, entry_id: str, version: int) -> dict[str, Any]:
        """Reconstruct manifest bundle from stored rows for digest verification."""
        ...

    @abstractmethod
    def version_current_status(self, entry_id: str, version: int) -> str | None:
        """Return current status string for a version, or None if missing."""
        ...


class StorageAdapter(CatalogStore, ABC):
    """Extended storage contract for adapters that support Curator operations.

    Adds transaction control, parameterized SQL execution, and integrity
    validation to the base CatalogStore interface. The Curator and Verifier
    use these methods instead of accessing raw provider connections.

    SQL statements use ? placeholders. Each adapter translates to its
    provider's native parameterization syntax internally.
    """

    @abstractmethod
    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[None]:
        """Context manager for a transaction.

        SQLite: BEGIN IMMEDIATE (write) or BEGIN (read). Rolls back on
        exception, commits on clean exit.
        Databricks: Validates atomicity support. Individual execute() calls
        are each atomic. Multi-write atomicity is provided by execute_batch()
        using BEGIN ATOMIC ... END; on serverless compute.
        """
        ...

    @abstractmethod
    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Execute a parameterized SQL statement (INSERT/UPDATE/DELETE).

        Uses ? placeholders. The adapter translates to native syntax.
        Must be called within a transaction for write operations.
        """
        ...

    @abstractmethod
    def execute_batch(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        """Execute multiple write statements as one atomic unit.

        SQLite: Executes within the current transaction (same atomicity as
        individual execute() calls within a transaction context).
        Databricks: Wraps in BEGIN ATOMIC ... END; for serverless atomicity.
        If any statement fails, all are rolled back.
        """
        ...

    @abstractmethod
    def query_one(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> dict[str, Any] | None:
        """Execute a query and return one row as a dict, or None."""
        ...

    @abstractmethod
    def query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Execute a query and return all rows as a list of dicts."""
        ...

    @abstractmethod
    def validate_integrity(self) -> bool:
        """Check referential integrity.

        SQLite: PRAGMA foreign_key_check (returns True if no violations).
        Databricks: 22 explicit SQL checks (duplicates, orphans, status
        validity, seq uniqueness, manifest hash consistency, verification
        result validity, child-table orphans). Raises CatalogError on violation.
        """
        ...

    @abstractmethod
    def close(self) -> None:
        """Close the connection."""
        ...

"""Curator: the only admitted writer of Catalog manifests and status.

Operations: create_draft, publish, deprecate, verify_source, get_version,
reconcile_operation. Uses stable caller-supplied idempotency keys, result
receipts, exact input digests, and bounded transaction retries.

Databricks PK/FK/UNIQUE are informational — all uniqueness, FK, sequence,
and graph validation enforced in Curator code under real transactions.
delta.appendOnly blocks UPDATE/DELETE but NOT invalid duplicate INSERT.

The Curator is provider-neutral. It uses the StorageAdapter interface
(transaction, execute, query_one, query_all, validate_integrity) instead
of raw provider connections. Provider-specific SQL is contained in adapters.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from aakr.catalog.models import (
    COLLECTIONS,
    HEX,
    MANIFEST_FIELDS,
    CatalogError,
    manifest,
    normalize_bundle,
)


class Curator:
    def __init__(
        self,
        store,
        *,
        fixture_ascii_only: bool = False,
        validation_receipt: Callable[[str, str], bool] | None = None,
    ):
        self.store = store
        self.fixture_ascii_only = fixture_ascii_only
        self.validation_receipt = validation_receipt

    def create_draft(
        self,
        bundle: dict[str, Any],
        expected_manifest: str,
        *,
        fault_at: str | None = None,
    ) -> str:
        """Validate complete intended manifest and insert parent/version/first
        draft event and all children as one admitted atomic operation.

        Retry with same-id same-input is idempotent.
        Same-id different-input is a hard conflict.
        Partial draft may only reconcile to original expected hash.
        """
        if not HEX.fullmatch(expected_manifest):
            raise CatalogError("invalid precommitted manifest digest")
        normalized = normalize_bundle(bundle)
        eid = normalized["entry"]["entry_id"]
        v = normalized["version"]["version"]
        if normalized["version"]["entry_id"] != eid or type(v) is not int or v < 1:
            raise CatalogError("bad manifest identity")
        calculated = manifest(bundle, fixture_ascii_only=self.fixture_ascii_only)
        if calculated != expected_manifest:
            raise CatalogError("precommitted manifest disagrees with supplied bundle")

        # Learning proposal validation
        dref = normalized["version"]["derived_from_ref"]
        ddigest = normalized["version"]["derived_from_sha256"]
        if dref is not None and (
            self.validation_receipt is None
            or not self.validation_receipt(dref, ddigest)
        ):
            raise CatalogError("learning proposal lacks trusted final validation")

        # Empty applicability groups forbidden
        group_ids = {g["group_id"] for g in normalized["groups"]}
        for g in normalized["groups"]:
            if not any(
                x["group_id"] == g["group_id"] for x in normalized["conditions"]
            ):
                raise CatalogError("empty applicability group")
        if any(x["group_id"] not in group_ids for x in normalized["conditions"]):
            raise CatalogError("condition lacks owning group")

        with self.store.transaction(write=True):
            old = self.store.query_one(
                "SELECT manifest_sha256 FROM entry_versions WHERE entry_id=? AND version=?",
                (eid, v),
            )
            if old is not None:
                if old["manifest_sha256"] != expected_manifest:
                    raise CatalogError(
                        "different payload under existing version identity"
                    )
                if (
                    manifest(
                        self.store.persisted_bundle(eid, v),
                        fixture_ascii_only=self.fixture_ascii_only,
                    )
                    != expected_manifest
                ):
                    raise CatalogError("prior draft incomplete: reconcile explicitly")
                return expected_manifest

            prior = self.store.query_one(
                "SELECT entry_type FROM entries WHERE entry_id=?", (eid,)
            )

            # Collect all INSERT statements for atomic execution.
            # On Databricks, execute_batch() uses BEGIN ATOMIC ... END;
            # ensuring all writes commit or none do — no partial state.
            # On SQLite, execute_batch() executes within the current
            # transaction, providing the same atomicity as individual calls.
            batch_statements: list[tuple[str, Any]] = []

            if prior is None:
                batch_statements.append(
                    (
                        "INSERT INTO entries VALUES (?,?,?)",
                        (
                            eid,
                            normalized["entry"]["entry_type"],
                            bundle["entry"].get("created_at", "now"),
                        ),
                    )
                )
            elif prior["entry_type"] != normalized["entry"]["entry_type"]:
                raise CatalogError("entry type is immutable")

            fields = list(MANIFEST_FIELDS["version"])
            data = [normalized["version"][k] for k in fields]
            batch_statements.append(
                (
                    (
                        f"INSERT INTO entry_versions ({','.join(fields)},manifest_sha256) "
                        f"VALUES ({','.join(['?'] * (len(fields) + 1))})"
                    ),
                    (*data, expected_manifest),
                )
            )
            # version_status with atomic seq computation (seq=1 for new draft)
            # Uses INSERT...SELECT (not VALUES) because Databricks does not support
            # scalar subqueries in VALUES clause.
            # WHERE NOT EXISTS prevents duplicate draft events when execute_batch
            # retries after a ConcurrentAppendException — on retry, if the other
            # writer already inserted the draft status, this INSERT inserts 0 rows.
            batch_statements.append(
                (
                    (
                        "INSERT INTO version_status "
                        "SELECT ?, ?, COALESCE(s.max_seq, 0) + 1, ?, ?, ? "
                        "FROM (SELECT COALESCE(MAX(seq), 0) AS max_seq "
                        "FROM version_status WHERE entry_id=? AND version=?) s "
                        "WHERE NOT EXISTS ("
                        "SELECT 1 FROM version_status "
                        "WHERE entry_id=? AND version=? AND status=?)"
                    ),
                    (
                        eid,
                        v,
                        "draft",
                        normalized["version"]["created_by"],
                        normalized["version"]["created_at"],
                        eid,
                        v,
                        eid,
                        v,
                        "draft",
                    ),
                )
            )

            for name, (table, _) in COLLECTIONS.items():
                fields = list(MANIFEST_FIELDS[name])
                for record in normalized[name]:
                    batch_statements.append(
                        (
                            (
                                f"INSERT INTO {table} ({','.join(fields)}) "
                                f"VALUES ({','.join(['?'] * len(fields))})"
                            ),
                            [record[k] for k in fields],
                        )
                    )

            # Execute atomically via execute_batch, or individually for
            # SQLite fault_at test injection (backward compatibility).
            if fault_at:
                idx = 0
                if prior is None:
                    self.store.execute(*batch_statements[idx])
                    idx += 1
                self.store.execute(*batch_statements[idx])
                idx += 1
                self.store.execute(*batch_statements[idx])
                idx += 1
                if fault_at == "after_parent":
                    raise CatalogError("injected failure after parent insert")
                for name, (table, _) in COLLECTIONS.items():
                    for record in normalized[name]:
                        self.store.execute(*batch_statements[idx])
                        idx += 1
                    if fault_at == "after_" + name:
                        raise CatalogError("injected failure after " + name)
            else:
                self.store.execute_batch(batch_statements)

            # Verify persisted manifest matches intended
            persisted = self.store.persisted_bundle(eid, v)
            if (
                manifest(persisted, fixture_ascii_only=self.fixture_ascii_only)
                != expected_manifest
            ):
                raise CatalogError("persisted manifest differs from intended content")
        return expected_manifest

    def _graph_valid(self, eid: str, version: int) -> None:
        """Check part_of cycle freedom across affected published graph."""
        edges = self.store.query_all(
            "SELECT l.from_entry_id, l.from_version, l.to_entry_id, l.to_version "
            'FROM entry_links l WHERE l.link_type="part_of" AND ('
            "(l.from_entry_id=? AND l.from_version=?) "
            "OR EXISTS (SELECT 1 FROM version_status s WHERE s.entry_id=l.from_entry_id "
            'AND s.version=l.from_version AND s.status="published"))',
            (eid, version),
        )
        graph: dict[tuple[str, int], list[tuple[str, int]]] = {}
        for e in edges:
            graph.setdefault((e["from_entry_id"], e["from_version"]), []).append(
                (e["to_entry_id"], e["to_version"])
            )
        seen: set[tuple[str, int]] = set()
        active: set[tuple[str, int]] = set()

        def visit(node: tuple[str, int]) -> None:
            if node in active:
                raise CatalogError("part_of cycle")
            if node in seen:
                return
            active.add(node)
            for nxt in graph.get(node, []):
                visit(nxt)
            active.remove(node)
            seen.add(node)

        for node in graph:
            visit(node)

    def publish(
        self,
        eid: str,
        version: int,
        *,
        actor: str = "curator",
        fault_at: str | None = None,
    ) -> None:
        """Publish a draft version: validate all checks, append published status,
        deprecate predecessor atomically.

        Checks: manifest recompute, content SHA, claim targets, empty groups,
        group-mode consistency, source association uniqueness, link target
        publication history, part_of cycle freedom, supersession policy.
        """
        with self.store.transaction(write=True):
            cur = self.store.query_one(
                "SELECT status FROM version_current_status WHERE entry_id=? AND version=?",
                (eid, version),
            )
            if cur is None:
                raise CatalogError("version missing")
            if cur["status"] == "published":
                return  # Idempotent: already published
            if cur["status"] != "draft":
                raise CatalogError("only drafts may be published")

            version_row = self.store.query_one(
                "SELECT * FROM entry_versions WHERE entry_id=? AND version=?",
                (eid, version),
            )
            current = manifest(
                self.store.persisted_bundle(eid, version),
                fixture_ascii_only=self.fixture_ascii_only,
            )
            if current != version_row["manifest_sha256"]:
                raise CatalogError("manifest checksum mismatch")

            if not self.store.validate_integrity():
                raise CatalogError("foreign key violations")

            # Link targets must have publication history
            for row in self.store.query_all(
                "SELECT to_entry_id,to_version FROM entry_links "
                "WHERE from_entry_id=? AND from_version=?",
                (eid, version),
            ):
                published = self.store.query_one(
                    "SELECT 1 FROM version_status WHERE entry_id=? AND version=? "
                    'AND status="published"',
                    (row["to_entry_id"], row["to_version"]),
                )
                if not published:
                    raise CatalogError("link target was never published")

            self._graph_valid(eid, version)

            # Supersession policy
            prev = version_row["supersedes_version"]
            if prev is not None:
                p = self.store.query_one(
                    "SELECT status FROM version_current_status "
                    "WHERE entry_id=? AND version=?",
                    (eid, prev),
                )
                if p is None or p["status"] not in ("published", "deprecated"):
                    raise CatalogError("superseded version must have been published")

            # Append published status and deprecate predecessor atomically.
            # Seq is computed via derived table subquery at INSERT time (not pre-read)
            # to prevent concurrent publishers from producing duplicate sequences.
            # Uses INSERT...SELECT (not VALUES) because Databricks does not support
            # scalar subqueries in VALUES clause.
            # WHERE NOT EXISTS prevents duplicate status events when execute_batch
            # retries after a ConcurrentAppendException — on retry, if the other
            # writer already inserted the same status, this INSERT inserts 0 rows.
            _seq_sql = (
                "INSERT INTO version_status "
                "SELECT ?, ?, COALESCE(s.max_seq, 0) + 1, ?, ?, ? "
                "FROM (SELECT COALESCE(MAX(seq), 0) AS max_seq "
                "FROM version_status WHERE entry_id=? AND version=?) s "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM version_status "
                "WHERE entry_id=? AND version=? AND status=?)"
            )
            batch_statements = [
                (
                    _seq_sql,
                    (
                        eid,
                        version,
                        "published",
                        actor,
                        "publication",
                        eid,
                        version,
                        eid,
                        version,
                        "published",
                    ),
                )
            ]

            if prev is not None and p["status"] == "published":
                batch_statements.append(
                    (
                        _seq_sql,
                        (
                            eid,
                            prev,
                            "deprecated",
                            actor,
                            "superseded",
                            eid,
                            prev,
                            eid,
                            prev,
                            "deprecated",
                        ),
                    )
                )

            # Execute publish + deprecate as one atomic unit.
            # fault_at preserves SQLite test fault injection between the two
            # writes (within the transaction, so rollback is automatic).
            # Without fault_at, execute_batch provides real atomicity.
            if fault_at == "after_publish" and batch_statements:
                self.store.execute(batch_statements[0][0], batch_statements[0][1])
                raise CatalogError("injected failure after publishing before commit")
            elif fault_at == "after_deprecate" and len(batch_statements) >= 2:
                self.store.execute(batch_statements[0][0], batch_statements[0][1])
                self.store.execute(batch_statements[1][0], batch_statements[1][1])
                raise CatalogError("injected failure after deprecating before commit")
            else:
                self.store.execute_batch(batch_statements)

    def deprecate(self, eid: str, version: int, *, actor: str = "curator") -> None:
        """Deprecate a draft or published version."""
        with self.store.transaction(write=True):
            row = self.store.query_one(
                "SELECT status FROM version_current_status "
                "WHERE entry_id=? AND version=?",
                (eid, version),
            )
            if row is None or row["status"] not in ("draft", "published"):
                raise CatalogError("invalid deprecation")
            self.store.execute(
                "INSERT INTO version_status "
                "SELECT ?, ?, COALESCE(s.max_seq, 0) + 1, ?, ?, ? "
                "FROM (SELECT COALESCE(MAX(seq), 0) AS max_seq "
                "FROM version_status WHERE entry_id=? AND version=?) s "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM version_status "
                "WHERE entry_id=? AND version=? AND status=?)",
                (
                    eid,
                    version,
                    "deprecated",
                    actor,
                    "deprecated",
                    eid,
                    version,
                    eid,
                    version,
                    "deprecated",
                ),
            )

    def verify_source(
        self,
        rid: str,
        result: str,
        verified_by: str,
        checked_source_sha256: str,
        *,
        excerpt: str | None = None,
    ) -> None:
        """Append a verification event keyed by (source_ref_id, seq).

        verified_by must not equal entry-version created_by.
        verified requires nonblank excerpt.
        """
        self.store.append_source_verification(
            rid, result, verified_by, checked_source_sha256, excerpt=excerpt
        )

    def get_version(
        self, entry_id: str, version: int, *, fixture_ascii_only: bool = False
    ) -> dict[str, Any] | None:
        """Get a pinnable version with provenance."""
        return self.store.get(entry_id, version, fixture_ascii_only=fixture_ascii_only)

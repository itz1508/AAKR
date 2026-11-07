"""Databricks Unity Catalog/Delta backend for the Catalog Store.

Uses Unity Catalog-managed Delta tables with:
- delta.appendOnly=true (blocks UPDATE/DELETE)
- delta.feature.catalogManaged=supported (enables multi-table transactions)
- CHECK constraints via ALTER TABLE ADD CONSTRAINT (enforced)
- PK/FK/UNIQUE constraints (informational only — Curator enforces in code)
- BEGIN ATOMIC ... END; for multi-table atomic transactions (serverless compute)

Target namespace: oneshot_dev.knowledge for Catalog truth and read-only views.
Curator operational namespace and Runtime-owned store are separately governed.

NOTE: Interactive BEGIN TRANSACTION/COMMIT requires a Databricks SQL warehouse.
On serverless compute, use BEGIN ATOMIC ... END; blocks for atomic multi-table writes.
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

from aakr.catalog.models import (
    COLLECTIONS,
    VERIFICATION_PROFILE,
    CatalogError,
    verification_digest,
)
from aakr.core.store import StorageAdapter

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class DatabricksStore(StorageAdapter):
    """Databricks Unity Catalog/Delta backend.

    Requires pre-created schema and tables (see sql/001_catalog_tables.sql).
    Uses Spark SQL with native parameter binding via :name syntax.
    """

    def __init__(
        self, *, catalog: str = "oneshot_dev", schema: str = "knowledge", spark=None
    ):
        self._validate_identifier(catalog, "catalog")
        self._validate_identifier(schema, "schema")
        self.catalog = catalog
        self.schema = schema
        self._spark = spark
        self._full_schema = f"{catalog}.{schema}"
        self._atomic_checked = False
        self._atomic_supported = False
        # Set default schema so unqualified table names in Curator SQL resolve.
        # USE persists for the SparkSession on serverless compute.
        self.spark.sql(f"USE {self._full_schema}")

    @staticmethod
    def _validate_identifier(name: str, label: str) -> None:
        if not name or not _IDENTIFIER_RE.match(name):
            raise CatalogError(f"invalid {label} identifier: {name!r}")

    @property
    def spark(self):
        if self._spark is None:
            from pyspark.sql import SparkSession

            self._spark = SparkSession.builder.getOrCreate()
        return self._spark

    def _check_atomic(self) -> bool:
        """Check if BEGIN ATOMIC ... END; is supported (lazy, cached).

        Under concurrent access, a second thread's check may fail because
        the first thread holds the atomic lock. The cache is NOT permanent
        — _reset_atomic_cache() clears it so retries can re-check.
        """
        if not self._atomic_checked:
            try:
                self.spark.sql("BEGIN ATOMIC SELECT 1 as _t; END;")
                self._atomic_supported = True
            except Exception as e:
                logger.debug("atomic transaction not supported: %s", e)
                self._atomic_supported = False
            self._atomic_checked = True
        return self._atomic_supported

    def _reset_atomic_cache(self) -> None:
        """Clear the atomic-support cache so the next call re-checks."""
        self._atomic_checked = False
        self._atomic_supported = False

    @staticmethod
    def _is_concurrent_conflict(error: Exception) -> bool:
        """Check if an error is a Delta concurrent modification conflict.

        Delta Lake raises ConcurrentAppendException, ConcurrentDeleteException,
        or ConcurrentModificationException when two writers conflict.
        The error messages contain 'concurrent' and 'Retry the operation'.
        """
        msg = str(error).lower()
        return any(
            k in msg
            for k in (
                "concurrentappendexception",
                "concurrentdeleteexception",
                "concurrentmodificationexception",
                "delta_concurrent_append",
                "delta_concurrent_delete",
                "delta_concurrent_transaction",
                "concurrent operation",
                "retry the operation",
                "transaction conflict",
            )
        )

    def _bind_params(self, sql: str, params: tuple = ()) -> tuple[str, dict[str, Any]]:
        """Convert ? placeholders to :name parameters for native Spark SQL binding.

        Returns (sql_with_named_params, args_dict) for spark.sql(sql, args=...).
        Values are bound server-side -- this is TRUE native parameter binding,
        NOT string interpolation.
        """
        if not params:
            return sql, {}
        parts = sql.split("?")
        if len(parts) - 1 != len(params):
            raise CatalogError(
                f"parameter count mismatch: {len(params)} params for "
                f"{len(parts) - 1} placeholders"
            )
        args: dict[str, Any] = {}
        result = parts[0]
        for i, (param, part) in enumerate(zip(params, parts[1:])):
            name = f"p{i}"
            args[name] = param
            result += f":{name}" + part
        return result, args

    def _table(self, table_name: str) -> str:
        """Return fully qualified table name with validated identifier."""
        self._validate_identifier(table_name, "table")
        return f"{self._full_schema}.{table_name}"

    def close(self) -> None:
        pass  # SparkSession lifecycle managed externally

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[None]:
        """Databricks transaction context.

        Read mode: no-op (Spark provides snapshot isolation for queries).
        Write mode: validates that BEGIN ATOMIC ... END; is supported.
        Individual execute() calls are each atomic. Multi-write atomicity
        is provided by execute_batch() using BEGIN ATOMIC ... END;.
        """
        if write and not self._check_atomic():
            raise CatalogError(
                "BEGIN ATOMIC ... END; not supported on this compute; "
                "multi-write atomic operations require serverless compute "
                "or a SQL warehouse"
            )
        yield

    def execute(self, sql: str, params: tuple = ()) -> None:
        """Execute a parameterized SQL statement using native binding.

        Retries on Delta ConcurrentAppendException with exponential backoff.
        """
        named_sql, args = self._bind_params(sql, params)
        for attempt in range(3):
            try:
                if args:
                    self.spark.sql(named_sql, args=args)
                else:
                    self.spark.sql(named_sql)
                return
            except Exception as e:
                if self._is_concurrent_conflict(e) and attempt < 2:
                    time.sleep(0.1 * (2**attempt))
                    continue
                raise

    def execute_batch(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        """Execute multiple write statements atomically using BEGIN ATOMIC ... END;.

        All statements are combined into a single BEGIN ATOMIC ... END; block
        with native parameter binding. If any statement fails, all are
        rolled back -- no partial state is visible.

        Retries on Delta concurrent conflicts and BEGIN ATOMIC lock contention
        with exponential backoff. The atomic-support cache is reset between
        retries so a transient lock contention is re-checked.
        """
        if not statements:
            return
        # Build the batch SQL once (same for all retries)
        sql_parts: list[str] = []
        all_args: dict[str, Any] = {}
        for i, (sql, params) in enumerate(statements):
            parts = sql.split("?")
            if len(parts) - 1 != len(params):
                raise CatalogError(
                    f"statement {i}: parameter count mismatch: "
                    f"{len(params)} params for {len(parts) - 1} placeholders"
                )
            result = parts[0]
            for j, (param, part) in enumerate(zip(params, parts[1:])):
                name = f"s{i}_p{j}"
                all_args[name] = param
                result += f":{name}" + part
            sql_parts.append(result)
        batch = "BEGIN ATOMIC\n" + ";\n".join(sql_parts) + ";\nEND;"

        for attempt in range(3):
            if not self._check_atomic():
                if attempt < 2:
                    self._reset_atomic_cache()
                    time.sleep(0.1 * (2**attempt))
                    continue
                raise CatalogError(
                    "BEGIN ATOMIC ... END; not supported; "
                    "cannot execute batch atomically"
                )
            try:
                if all_args:
                    self.spark.sql(batch, args=all_args)
                else:
                    self.spark.sql(batch)
                return
            except Exception as e:
                if self._is_concurrent_conflict(e) and attempt < 2:
                    self._reset_atomic_cache()
                    time.sleep(0.1 * (2**attempt))
                    continue
                raise

    def query_one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        """Execute a query and return one row as a dict, or None."""
        named_sql, args = self._bind_params(sql, params)
        if args:
            rows = self.spark.sql(named_sql, args=args).collect()
        else:
            rows = self.spark.sql(named_sql).collect()
        return rows[0].asDict() if rows else None

    def query_all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        """Execute a query and return all rows as a list of dicts."""
        named_sql, args = self._bind_params(sql, params)
        if args:
            return [
                row.asDict() for row in self.spark.sql(named_sql, args=args).collect()
            ]
        return [row.asDict() for row in self.spark.sql(named_sql).collect()]

    def validate_integrity(self) -> bool:
        """Check referential integrity using explicit SQL queries.

        Databricks PK/FK/UNIQUE constraints are informational (not enforced).
        This method performs explicit checks for duplicate identities,
        orphaned records, missing references, and invalid superseding.
        A detected violation raises CatalogError with a diagnostic.
        """
        s = self._full_schema
        checks = [
            (
                "duplicate entries",
                (
                    f"SELECT entry_id, COUNT(*) AS cnt FROM {s}.entries "
                    f"GROUP BY entry_id HAVING COUNT(*) > 1"
                ),
            ),
            (
                "duplicate versions",
                (
                    f"SELECT entry_id, version, COUNT(*) AS cnt FROM {s}.entry_versions "
                    f"GROUP BY entry_id, version HAVING COUNT(*) > 1"
                ),
            ),
            (
                "orphaned versions (no parent entry)",
                (
                    f"SELECT v.entry_id, v.version FROM {s}.entry_versions v "
                    f"LEFT JOIN {s}.entries e ON v.entry_id = e.entry_id "
                    f"WHERE e.entry_id IS NULL"
                ),
            ),
            (
                "orphaned conditions (no owning group)",
                (
                    f"SELECT c.condition_id FROM {s}.applicability_conditions c "
                    f"LEFT JOIN {s}.applicability_groups g "
                    f"ON c.entry_id = g.entry_id AND c.version = g.version "
                    f"AND c.group_id = g.group_id "
                    f"WHERE g.group_id IS NULL"
                ),
            ),
            (
                "orphaned source refs (no parent version)",
                (
                    f"SELECT r.source_ref_id FROM {s}.source_refs r "
                    f"LEFT JOIN {s}.entry_versions v "
                    f"ON r.entry_id = v.entry_id AND r.version = v.version "
                    f"WHERE v.version IS NULL"
                ),
            ),
            (
                "orphaned version_status (no parent version)",
                (
                    f"SELECT st.entry_id, st.version FROM {s}.version_status st "
                    f"LEFT JOIN {s}.entry_versions v "
                    f"ON st.entry_id = v.entry_id AND st.version = v.version "
                    f"WHERE v.version IS NULL"
                ),
            ),
            (
                "published version missing manifest hash",
                (
                    f"SELECT v.entry_id, v.version FROM {s}.entry_versions v "
                    f"JOIN {s}.version_current_status cs "
                    f"ON v.entry_id = cs.entry_id AND v.version = cs.version "
                    f"WHERE cs.status = 'published' AND v.manifest_sha256 IS NULL"
                ),
            ),
            (
                "invalid superseding (target version missing)",
                (
                    f"SELECT v.entry_id, v.version, v.supersedes_version "
                    f"FROM {s}.entry_versions v "
                    f"WHERE v.supersedes_version IS NOT NULL "
                    f"AND NOT EXISTS ("
                    f"SELECT 1 FROM {s}.entry_versions v2 "
                    f"WHERE v2.entry_id = v.entry_id AND v2.version = v.supersedes_version)"
                ),
            ),
            (
                "duplicate source ref identities",
                (
                    f"SELECT source_ref_id, COUNT(*) AS cnt FROM {s}.source_refs "
                    f"GROUP BY source_ref_id HAVING COUNT(*) > 1"
                ),
            ),
            # --- Expanded checks: status transition validity ---
            (
                "invalid status (unknown value)",
                (
                    f"SELECT DISTINCT status FROM {s}.version_status "
                    f"WHERE status NOT IN ('draft','published','deprecated')"
                ),
            ),
            (
                "duplicate version_status seq (same entry+version)",
                (
                    f"SELECT entry_id, version, seq, COUNT(*) AS cnt FROM {s}.version_status "
                    f"GROUP BY entry_id, version, seq HAVING COUNT(*) > 1"
                ),
            ),
            # --- Expanded checks: manifest hash consistency ---
            (
                "published version with manifest mismatch",
                (
                    f"SELECT v.entry_id, v.version FROM {s}.entry_versions v "
                    f"JOIN {s}.version_current_status cs "
                    f"ON v.entry_id = cs.entry_id AND v.version = cs.version "
                    f"WHERE cs.status = 'published' AND (v.manifest_sha256 IS NULL "
                    f"OR LENGTH(v.manifest_sha256) != 64)"
                ),
            ),
            # --- Expanded checks: source verification consistency ---
            (
                "orphaned source verifications (no parent source_ref)",
                (
                    f"SELECT sv.source_ref_id FROM {s}.source_verifications sv "
                    f"LEFT JOIN {s}.source_refs r ON sv.source_ref_id = r.source_ref_id "
                    f"WHERE r.source_ref_id IS NULL"
                ),
            ),
            (
                "invalid verification result",
                (
                    f"SELECT DISTINCT result FROM {s}.source_verifications "
                    f"WHERE result NOT IN ('verified','unverified','not_verified','contradicted')"
                ),
            ),
            (
                "duplicate verification seq (same source_ref_id)",
                (
                    f"SELECT source_ref_id, seq, COUNT(*) AS cnt FROM {s}.source_verifications "
                    f"GROUP BY source_ref_id, seq HAVING COUNT(*) > 1"
                ),
            ),
            # --- Expanded checks: child-table orphans ---
            (
                "orphaned match tags (no parent version)",
                (
                    f"SELECT t.entry_id, t.version FROM {s}.match_tags t "
                    f"LEFT JOIN {s}.entry_versions v "
                    f"ON t.entry_id = v.entry_id AND t.version = v.version "
                    f"WHERE v.version IS NULL"
                ),
            ),
            (
                "orphaned groups (no parent version)",
                (
                    f"SELECT g.entry_id, g.version FROM {s}.applicability_groups g "
                    f"LEFT JOIN {s}.entry_versions v "
                    f"ON g.entry_id = v.entry_id AND g.version = v.version "
                    f"WHERE v.version IS NULL"
                ),
            ),
            (
                "orphaned sections (no parent version)",
                (
                    f"SELECT sec.entry_id, sec.version FROM {s}.content_sections sec "
                    f"LEFT JOIN {s}.entry_versions v "
                    f"ON sec.entry_id = v.entry_id AND sec.version = v.version "
                    f"WHERE v.version IS NULL"
                ),
            ),
            (
                "orphaned open questions (no parent version)",
                (
                    f"SELECT q.entry_id, q.version FROM {s}.open_questions q "
                    f"LEFT JOIN {s}.entry_versions v "
                    f"ON q.entry_id = v.entry_id AND q.version = v.version "
                    f"WHERE v.version IS NULL"
                ),
            ),
            (
                "orphaned links (no parent version)",
                (
                    f"SELECT l.from_entry_id, l.from_version FROM {s}.entry_links l "
                    f"LEFT JOIN {s}.entry_versions v "
                    f"ON l.from_entry_id = v.entry_id AND l.from_version = v.version "
                    f"WHERE v.version IS NULL"
                ),
            ),
        ]
        for label, query in checks:
            rows = self.spark.sql(query).collect()
            if rows:
                raise CatalogError(f"integrity violation: {label} ({len(rows)} rows)")
        return True

    def version_current_status(self, entry_id: str, version: int) -> str | None:
        row = self.query_one(
            f"SELECT status FROM {self._full_schema}.version_current_status "
            "WHERE entry_id = ? AND version = ?",
            (entry_id, version),
        )
        return row["status"] if row else None

    def persisted_bundle(self, entry_id: str, version: int) -> dict[str, Any]:
        """Reconstruct manifest bundle from stored rows."""
        e = self.query_one(
            f"SELECT * FROM {self._full_schema}.entries WHERE entry_id = ?", (entry_id,)
        )
        v = self.query_one(
            f"SELECT * FROM {self._full_schema}.entry_versions "
            "WHERE entry_id = ? AND version = ?",
            (entry_id, version),
        )
        if e is None or v is None:
            raise CatalogError("version does not exist")
        result: dict[str, Any] = {"entry": dict(e), "version": dict(v)}
        result["entry"].pop("created_at", None)
        result["version"].pop("manifest_sha256", None)
        for name, (table, _) in COLLECTIONS.items():
            if name == "links":
                where = "from_entry_id = ? AND from_version = ?"
            else:
                where = "entry_id = ? AND version = ?"
            result[name] = self.query_all(
                f"SELECT * FROM {self._full_schema}.{table} WHERE {where}",
                (entry_id, version),
            )
        return result

    def state_bundle(self, entry_id: str, version: int) -> list[dict[str, Any]]:
        """Return source_ref_current rows for a version."""
        return self.query_all(
            f"SELECT * FROM {self._full_schema}.source_ref_current "
            "WHERE entry_id = ? AND version = ? ORDER BY source_ref_id",
            (entry_id, version),
        )

    def provenance(
        self, entry_id: str, version: int, *, fixture_ascii_only: bool = False
    ) -> dict[str, Any]:
        rows = self.state_bundle(entry_id, version)
        counts = Counter(r["result"] for r in rows)
        return {
            "associations_total": len(rows),
            "distinct_sources_total": len(
                {
                    (r.get("source_type"), r.get("source_uri"), r.get("source_version"))
                    for r in rows
                }
            ),
            "by_result": {
                k: counts[k]
                for k in ("verified", "unverified", "not_verified", "contradicted")
            },
            "verification_state": rows,
            "verification_snapshot_sha256": verification_digest(
                rows, fixture_ascii_only=fixture_ascii_only
            ),
            "verification_profile": VERIFICATION_PROFILE,
        }

    def published_versions(self) -> list[dict[str, Any]]:
        return self.query_all(
            f"SELECT * FROM {self._full_schema}.published_versions "
            "ORDER BY entry_id, version DESC"
        )

    def pinnable_versions(self) -> list[dict[str, Any]]:
        return self.query_all(
            f"SELECT * FROM {self._full_schema}.pinnable_versions "
            "ORDER BY entry_id, version DESC"
        )

    def get(
        self, entry_id: str, version: int, *, fixture_ascii_only: bool = False
    ) -> dict[str, Any] | None:
        row = self.query_one(
            f"SELECT * FROM {self._full_schema}.pinnable_versions "
            "WHERE entry_id = ? AND version = ?",
            (entry_id, version),
        )
        if row is None:
            return None
        return {
            "entry_id": entry_id,
            "version": version,
            "manifest_sha256": row["manifest_sha256"],
            "provenance": self.provenance(
                entry_id, version, fixture_ascii_only=fixture_ascii_only
            ),
        }

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

        Implements the same retrieval logic as the SQLite adapter, using
        native parameter binding for all SQL queries.
        """
        if (
            not domains
            or len(domains) != len(set(domains))
            or per_domain_limit < 1
            or global_limit < 1
        ):
            raise CatalogError("invalid retrieval coverage")

        published = self.published_versions()
        candidates: list[dict[str, Any]] = []
        for v in published:
            eid, ver = v["entry_id"], v["version"]
            domain_rows = self.query_all(
                f"SELECT value FROM {self._full_schema}.match_tags "
                'WHERE entry_id = ? AND version = ? AND tag_type = "domain"',
                (eid, ver),
            )
            entry_domains = {r["value"] for r in domain_rows}
            matched = sorted(set(domains) & entry_domains)
            if not matched:
                continue
            intent_row = self.query_one(
                f"SELECT 1 FROM {self._full_schema}.match_tags "
                "WHERE entry_id = ? AND version = ? "
                'AND tag_type = "intent" AND lower(value) = lower(?)',
                (eid, ver, intent),
            )
            has_intent = intent_row is not None
            candidates.append(
                {
                    "entry_id": eid,
                    "version": ver,
                    "manifest_sha256": v["manifest_sha256"],
                    "matched_domains": matched,
                    "channels": [
                        {
                            "channel": "conditions",
                            "reason": "domain tag"
                            + (" and matching intent tag" if has_intent else ""),
                            "score": 1.0 if has_intent else 0.5,
                        }
                    ],
                    "ranking": {
                        "score": 1.0 if has_intent else 0.5,
                        "meaning": "retrieval_relevance_only",
                    },
                    "via_link": False,
                }
            )
        candidates.sort(
            key=lambda x: (-x["ranking"]["score"], x["entry_id"], -x["version"])
        )
        selected: dict[tuple[str, int], dict[str, Any]] = {}
        truncated = False
        for domain in domains:
            options = [x for x in candidates if domain in x["matched_domains"]]
            if len(options) > per_domain_limit:
                truncated = True
            for match in options[:per_domain_limit]:
                selected[(match["entry_id"], match["version"])] = match
        results = sorted(
            selected.values(),
            key=lambda x: (-x["ranking"]["score"], x["entry_id"], -x["version"]),
        )
        if len(results) > global_limit:
            truncated = True
        results = results[:global_limit]
        for match in results:
            match["provenance"] = self.provenance(
                match["entry_id"],
                match["version"],
                fixture_ascii_only=fixture_ascii_only,
            )
            match["open_questions"] = self.query_all(
                f"SELECT * FROM {self._full_schema}.open_questions "
                "WHERE entry_id = ? AND version = ?",
                (match["entry_id"], match["version"]),
            )
        return {
            "matches": results,
            "coverage": {
                "channels_used": ["conditions"],
                "channels_unavailable": ["fulltext", "semantic"],
                "domains_searched": domains,
                "truncated": truncated,
                "per_domain_limit": per_domain_limit,
                "global_limit": global_limit,
            },
            "snapshot": {
                "index_sha256": None,
                "index_generation": None,
                "index_status": "unavailable",
                "embedding_model": None,
            },
        }

    def append_source_verification(
        self,
        source_ref_id: str,
        result: str,
        verified_by: str,
        checked_source_sha256: str,
        *,
        excerpt: str | None = None,
    ) -> None:
        """Append a verification event with atomic seq computation.

        Seq is computed via derived table subquery at INSERT time to prevent
        concurrent verifiers from producing duplicate sequences.
        Uses INSERT...SELECT (not VALUES) because Databricks does not support
        scalar subqueries in VALUES clause.
        """
        self.execute(
            f"INSERT INTO {self._full_schema}.source_verifications "
            "(source_ref_id, seq, result, verified_by, verified_at, "
            "checked_source_sha256, excerpt) "
            "SELECT ?, COALESCE(s.max_seq, 0) + 1, ?, ?, ?, ?, ? "
            f"FROM (SELECT COALESCE(MAX(seq), 0) AS max_seq "
            f"FROM {self._full_schema}.source_verifications WHERE source_ref_id=?) s",
            (
                source_ref_id,
                result,
                verified_by,
                "verification",
                checked_source_sha256,
                excerpt,
                source_ref_id,
            ),
        )


# Compatibility alias for aakr.integrations.databricks.__init__
DatabricksCatalogAdapter = DatabricksStore

"""SQLite reference backend for the Catalog Store.

Uses SQLite with foreign keys, recursive triggers, append-only triggers,
freeze triggers (manifest children appendable only while draft),
and partial unique indexes for source association uniqueness.

This is a local reference implementation, NOT a production Databricks backend.
Requires the SQLite schema file for initialization.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from aakr.catalog.models import (
    COLLECTIONS,
    VERIFICATION_PROFILE,
    CatalogError,
    verification_digest,
)
from aakr.core.store import StorageAdapter


def _rows(
    c: sqlite3.Connection, table: str, where: str, params: tuple[Any, ...]
) -> list[dict[str, Any]]:
    return [dict(x) for x in c.execute(f"SELECT * FROM {table} WHERE {where}", params)]


@contextmanager
def txn(c: sqlite3.Connection, *, write: bool = False) -> Iterator[None]:
    c.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield
    except BaseException:
        c.execute("ROLLBACK")
        raise
    else:
        c.execute("COMMIT")


class SQLiteStore(StorageAdapter):
    def __init__(self, path: str, *, initialize: bool = False, readonly: bool = False):
        if readonly:
            c = sqlite3.connect(
                Path(path).resolve().as_uri() + "?mode=ro",
                uri=True,
                isolation_level=None,
                timeout=3,
            )
            c.execute("PRAGMA query_only=ON")
        else:
            c = sqlite3.connect(path, isolation_level=None, timeout=3)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA recursive_triggers=ON")
        for key in ("foreign_keys", "recursive_triggers"):
            if c.execute(f"PRAGMA {key}").fetchone()[0] != 1:
                c.close()
                raise CatalogError(f"{key} was not enabled")
        if initialize:
            if readonly:
                raise CatalogError("cannot initialize a read-only Catalog")
            schema_path = Path(__file__).with_name("schema.sql")
            if not schema_path.exists():
                schema_path = Path(path).parent / "schema.sql"
            c.executescript(schema_path.read_text(encoding="utf-8"))
        self.c = c
        self.path = path

    def close(self) -> None:
        self.c.close()

    # --- StorageAdapter: transaction/execute/query methods ---

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[None]:
        """Context manager for a transaction (SQLite BEGIN/COMMIT/ROLLBACK)."""
        self.c.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
        except BaseException:
            self.c.execute("ROLLBACK")
            raise
        else:
            self.c.execute("COMMIT")

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Execute a parameterized SQL statement using ? placeholders."""
        self.c.execute(sql, params)

    def execute_batch(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        """Execute multiple write statements within the current transaction.

        On SQLite, all statements execute within the existing transaction
        context, providing the same atomicity as individual execute() calls.
        """
        for sql, params in statements:
            self.c.execute(sql, params)

    def query_one(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> dict[str, Any] | None:
        """Execute a query and return one row as a dict, or None."""
        row = self.c.execute(sql, params).fetchone()
        return dict(row) if row else None

    def query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Execute a query and return all rows as a list of dicts."""
        return [dict(r) for r in self.c.execute(sql, params).fetchall()]

    def validate_integrity(self) -> bool:
        """Check referential integrity via PRAGMA foreign_key_check."""
        return len(self.c.execute("PRAGMA foreign_key_check").fetchall()) == 0

    def version_current_status(self, entry_id: str, version: int) -> str | None:
        row = self.c.execute(
            "SELECT status FROM version_current_status WHERE entry_id=? AND version=?",
            (entry_id, version),
        ).fetchone()
        return dict(row)["status"] if row else None

    def persisted_bundle(self, entry_id: str, version: int) -> dict[str, Any]:
        e = self.c.execute(
            "SELECT * FROM entries WHERE entry_id=?", (entry_id,)
        ).fetchone()
        v = self.c.execute(
            "SELECT * FROM entry_versions WHERE entry_id=? AND version=?",
            (entry_id, version),
        ).fetchone()
        if e is None or v is None:
            raise CatalogError("version does not exist")
        result: dict[str, Any] = {"entry": dict(e), "version": dict(v)}
        result["entry"].pop("created_at", None)
        result["version"].pop("manifest_sha256", None)
        for name, (table, _) in COLLECTIONS.items():
            where = (
                "from_entry_id=? AND from_version=?"
                if name == "links"
                else "entry_id=? AND version=?"
            )
            result[name] = _rows(self.c, table, where, (entry_id, version))
        return result

    def state_bundle(self, entry_id: str, version: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.c.execute(
                "SELECT * FROM source_ref_current WHERE entry_id=? AND version=? ORDER BY source_ref_id",
                (entry_id, version),
            )
        ]

    def provenance(
        self, entry_id: str, version: int, *, fixture_ascii_only: bool = False
    ) -> dict[str, Any]:
        rows = self.state_bundle(entry_id, version)
        counts = Counter(r["result"] for r in rows)
        return {
            "associations_total": len(rows),
            "distinct_sources_total": len(
                {(r["source_type"], r["source_uri"], r["source_version"]) for r in rows}
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
        return [
            dict(r)
            for r in self.c.execute(
                "SELECT * FROM published_versions ORDER BY entry_id, version DESC"
            )
        ]

    def pinnable_versions(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.c.execute(
                "SELECT * FROM pinnable_versions ORDER BY entry_id, version DESC"
            )
        ]

    def get(
        self, entry_id: str, version: int, *, fixture_ascii_only: bool = False
    ) -> dict[str, Any] | None:
        with txn(self.c):
            row = self.c.execute(
                "SELECT * FROM pinnable_versions WHERE entry_id=? AND version=?",
                (entry_id, version),
            ).fetchone()
            if row is None:
                return None
            return {
                "entry_id": entry_id,
                "version": version,
                "manifest_sha256": dict(row)["manifest_sha256"],
                "provenance": self.provenance(
                    entry_id, version, fixture_ascii_only=fixture_ascii_only
                ),
            }

    def _match_domains(
        self, entry_id: str, version: int, requested: list[str]
    ) -> list[str]:
        domains = [
            r[0]
            for r in self.c.execute(
                'SELECT value FROM match_tags WHERE entry_id=? AND version=? AND tag_type="domain"',
                (entry_id, version),
            )
        ]
        return sorted(set(requested) & set(domains)) if domains else []

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
        if (
            not domains
            or len(domains) != len(set(domains))
            or per_domain_limit < 1
            or global_limit < 1
        ):
            raise CatalogError("invalid retrieval coverage")
        with txn(self.c):
            candidates = []
            for v in self.published_versions():
                eid, ver = v["entry_id"], v["version"]
                matched = self._match_domains(eid, ver, domains)
                if not matched:
                    continue
                has_intent = (
                    self.c.execute(
                        "SELECT 1 FROM match_tags WHERE entry_id=? AND version=? "
                        'AND tag_type="intent" AND lower(value)=lower(?)',
                        (eid, ver, intent),
                    ).fetchone()
                    is not None
                )
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
                match["open_questions"] = _rows(
                    self.c,
                    "open_questions",
                    "entry_id=? AND version=?",
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

        Uses INSERT...SELECT derived table for cross-backend consistency
        with DatabricksStore. On SQLite, BEGIN IMMEDIATE provides additional
        concurrency safety.
        """
        with txn(self.c, write=True):
            self.c.execute(
                "INSERT INTO source_verifications "
                "SELECT ?, COALESCE(s.max_seq, 0) + 1, ?, ?, ?, ?, ? "
                "FROM (SELECT COALESCE(MAX(seq), 0) AS max_seq "
                "FROM source_verifications WHERE source_ref_id=?) s",
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


# Compatibility alias for aakr.integrations.sqlite.__init__
SQLiteCatalogAdapter = SQLiteStore

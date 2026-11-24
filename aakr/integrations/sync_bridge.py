"""Sync bridge with checkpoints, content hashes, bounded retries, and
periodic full reconciliation.

Projects versioned source rows into a target projection store:
- Idempotent: a row whose (row_id, version, content_hash) already exists in
  the target is never rewritten.
- Checkpointed: per-row sync state is persisted (bridge_id, checkpoint_id,
  row_id) so an interrupted run resumes exactly where it stopped — rows
  already recorded 'synced' in the checkpoint are skipped; 'pending' rows
  are retried on the next run (partial-update recovery).
- Bounded retries: a row whose write keeps failing is retried at most
  max_retries times per run, then recorded 'failed' with its attempt count —
  it never blocks the rest of the batch.
- Reconcilable: reconcile() compares a full source snapshot against the
  target and reports missing, stale (older version), hash_mismatch (same
  version, different content), and orphaned rows; repair() re-projects only
  the drifted rows.

Storage-agnostic: the target is any SyncTarget; the state store is SQLite
(or an injected DB-API connection_factory), matching StoreConnection in
aakr.lifecycle. Runs without Spark so the mechanics are unit-testable.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from aakr.catalog.models import CatalogError


def content_hash(row_id: str, version: int, payload: dict[str, Any]) -> str:
    """Per-row SHA-256 over the canonical (id, version, payload) JSON."""
    canonical = json.dumps(
        {"row_id": row_id, "version": version, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class SyncRow:
    row_id: str
    version: int
    payload: dict[str, Any]


class SyncTarget(Protocol):
    """Target projection store. write() must be idempotent on (id, version)."""

    def write(
        self, row_id: str, version: int, hash_: str, payload: dict[str, Any]
    ) -> None: ...

    def load(self) -> dict[str, tuple[int, str]]:
        """row_id -> (current_version, content_hash) for the whole projection."""
        ...


class SyncStateConnection:
    """SQLite-backed sync_state store (injectable connection_factory)."""

    def __init__(self, db_path: str | None = None, *, connection_factory=None):
        if connection_factory is None and not db_path:
            raise ValueError("db_path or connection_factory is required")
        self.db_path = db_path
        self._connection_factory = connection_factory
        self._init_schema()

    def _conn(self):
        if self._connection_factory is not None:
            return self._connection_factory()
        c = sqlite3.connect(self.db_path, isolation_level=None, timeout=3)
        c.row_factory = sqlite3.Row
        return c

    def _init_schema(self):
        with self._conn() as c:
            c.executescript(
                """CREATE TABLE IF NOT EXISTS sync_state (
                bridge_id TEXT NOT NULL, checkpoint_id TEXT NOT NULL,
                row_id TEXT NOT NULL, version INTEGER NOT NULL,
                content_hash TEXT NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                PRIMARY KEY (bridge_id, checkpoint_id, row_id)
            );"""
            )

    def record(self, bridge_id, checkpoint_id, row_id, version, hash_, status, attempts):
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO sync_state (bridge_id, checkpoint_id, row_id, "
                "version, content_hash, status, attempts, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (bridge_id, checkpoint_id, row_id, version, hash_, status, attempts, ts),
            )

    def get(self, bridge_id, checkpoint_id, row_id) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM sync_state WHERE bridge_id=? AND checkpoint_id=? "
                "AND row_id=?",
                (bridge_id, checkpoint_id, row_id),
            ).fetchone()

    def rows_for_checkpoint(self, bridge_id, checkpoint_id) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM sync_state WHERE bridge_id=? AND checkpoint_id=?",
                (bridge_id, checkpoint_id),
            ).fetchall()


@dataclass
class SyncResult:
    checkpoint_id: str
    written: int = 0
    skipped_idempotent: int = 0
    failed: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


@dataclass
class ReconcileReport:
    in_sync: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)  # target has older version
    hash_mismatch: list[str] = field(default_factory=list)  # same version, content differs
    orphaned: list[str] = field(default_factory=list)  # target row absent from source

    def summary(self) -> dict[str, int]:
        return {k: len(v) for k, v in self._asdict().items()}

    def _asdict(self):
        return {
            "in_sync": self.in_sync,
            "missing": self.missing,
            "stale": self.stale,
            "hash_mismatch": self.hash_mismatch,
            "orphaned": self.orphaned,
        }

    @property
    def has_drift(self) -> bool:
        return bool(self.missing or self.stale or self.hash_mismatch or self.orphaned)


class SyncBridge:
    """Projects versioned rows into a target, checkpointed and idempotent."""

    def __init__(
        self,
        bridge_id: str,
        target: SyncTarget,
        state: SyncStateConnection,
        *,
        max_retries: int = 3,
    ):
        if max_retries < 1:
            raise CatalogError(f"max_retries must be >= 1, got {max_retries}")
        self.bridge_id = bridge_id
        self.target = target
        self.state = state
        self.max_retries = max_retries

    # ── projection (incremental, checkpointed, idempotent) ─────────────
    def project(self, rows: list[SyncRow], checkpoint_id: str) -> SyncResult:
        result = SyncResult(checkpoint_id=checkpoint_id)
        target_snapshot = self.target.load()
        for row in rows:
            h = content_hash(row.row_id, row.version, row.payload)
            existing = target_snapshot.get(row.row_id)
            # Idempotent: exact (version, hash) already projected.
            if existing and existing[0] == row.version and existing[1] == h:
                self.state.record(
                    self.bridge_id, checkpoint_id, row.row_id, row.version, h, "synced", 0
                )
                result.skipped_idempotent += 1
                continue
            # Partial-update recovery: skip rows this checkpoint already synced.
            prior = self.state.get(self.bridge_id, checkpoint_id, row.row_id)
            if prior and prior["status"] == "synced" and prior["content_hash"] == h:
                result.skipped_idempotent += 1
                continue
            attempts = 0
            last_error: Exception | None = None
            for attempt in range(1, self.max_retries + 1):
                attempts = attempt
                try:
                    self.target.write(row.row_id, row.version, h, row.payload)
                    last_error = None
                    break
                except Exception as e:  # noqa: BLE001 - record and retry bounded
                    last_error = e
            if last_error is None:
                self.state.record(
                    self.bridge_id, checkpoint_id, row.row_id, row.version, h, "synced", attempts
                )
                result.written += 1
            else:
                self.state.record(
                    self.bridge_id, checkpoint_id, row.row_id, row.version, h, "failed", attempts
                )
                result.failed.append(
                    {
                        "row_id": row.row_id,
                        "attempts": attempts,
                        "reason": str(last_error),
                    }
                )
        return result

    # ── full reconciliation (periodic) ──────────────────────────────────
    def reconcile(self, rows: list[SyncRow]) -> ReconcileReport:
        report = ReconcileReport()
        target_snapshot = self.target.load()
        source: dict[str, tuple[int, str]] = {
            r.row_id: (r.version, content_hash(r.row_id, r.version, r.payload))
            for r in rows
        }
        for row_id, (src_ver, src_hash) in source.items():
            tgt = target_snapshot.get(row_id)
            if tgt is None:
                report.missing.append(row_id)
            elif tgt[0] < src_ver:
                report.stale.append(row_id)
            elif tgt[0] == src_ver and tgt[1] != src_hash:
                report.hash_mismatch.append(row_id)
            else:
                report.in_sync.append(row_id)
        for row_id in target_snapshot:
            if row_id not in source:
                report.orphaned.append(row_id)
        return report

    def repair(self, rows: list[SyncRow], report: ReconcileReport, checkpoint_id: str) -> SyncResult:
        """Re-project only the drifted rows (missing, stale, hash_mismatch)."""
        drifted = set(report.missing) | set(report.stale) | set(report.hash_mismatch)
        subset = [r for r in rows if r.row_id in drifted]
        return self.project(subset, checkpoint_id)

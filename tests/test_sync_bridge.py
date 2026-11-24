"""Task 7 sync bridge tests.

Acceptance:
- Idempotency: re-projecting the same rows writes nothing new.
- Drift injection: reconcile detects missing / stale / hash_mismatch /
  orphaned rows; repair re-projects only the drifted ones.
- Partial-update recovery: a write failure mid-batch leaves a checkpoint;
  the next run completes the remaining rows without duplicating the rest.
- Bounded retries: a persistently failing row is retried at most
  max_retries times and never blocks the rest of the batch.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aakr.catalog.models import CatalogError
from aakr.integrations.sync_bridge import (
    SyncBridge,
    SyncRow,
    SyncStateConnection,
    content_hash,
)


class DictTarget:
    """In-memory SyncTarget whose writes are observable and corruptible."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.fail_on: set[str] = set()  # row_ids that raise on write
        self.write_counts: dict[str, int] = {}

    def write(self, row_id, version, hash_, payload):
        if row_id in self.fail_on:
            raise RuntimeError(f"simulated write failure for {row_id}")
        self.write_counts[row_id] = self.write_counts.get(row_id, 0) + 1
        self.rows[row_id] = {
            "version": version,
            "content_hash": hash_,
            "payload": dict(payload),
        }

    def load(self):
        return {
            rid: (r["version"], r["content_hash"]) for rid, r in self.rows.items()
        }

    def corrupt(self, row_id, payload=None, version=None):
        if payload is not None:
            self.rows[row_id]["payload"] = payload
            self.rows[row_id]["content_hash"] = "corrupted"
        if version is not None:
            self.rows[row_id]["version"] = version


def _state(tmpdir):
    return SyncStateConnection(str(Path(tmpdir) / "sync_state.db"))


def _rows():
    return [
        SyncRow("r1", 1, {"name": "alpha", "schema": {"type": "object"}}),
        SyncRow("r2", 1, {"name": "beta", "schema": {"type": "array"}}),
        SyncRow("r3", 2, {"name": "gamma", "schema": {"type": "string"}}),
    ]


class Idempotency(unittest.TestCase):
    def test_first_project_writes_all(self):
        target = DictTarget()
        with tempfile.TemporaryDirectory() as d:
            bridge = SyncBridge("b1", target, _state(d))
            result = bridge.project(_rows(), "cp1")
        self.assertTrue(result.ok)
        self.assertEqual(result.written, 3)
        self.assertEqual(result.skipped_idempotent, 0)

    def test_reproject_is_idempotent(self):
        target = DictTarget()
        with tempfile.TemporaryDirectory() as d:
            bridge = SyncBridge("b1", target, _state(d))
            bridge.project(_rows(), "cp1")
            result = bridge.project(_rows(), "cp2")
        self.assertTrue(result.ok)
        self.assertEqual(result.written, 0)
        self.assertEqual(result.skipped_idempotent, 3)
        self.assertEqual(sum(target.write_counts.values()), 3)  # no extra writes

    def test_checkpoint_state_survives_restart(self):
        """A new bridge over the same state + target skips checkpointed rows."""
        target = DictTarget()
        with tempfile.TemporaryDirectory() as d:
            state = _state(d)
            SyncBridge("b1", target, state).project(_rows(), "cp1")
            bridge2 = SyncBridge("b1", target, _state(d))
            result = bridge2.project(_rows(), "cp1")  # same checkpoint id
        self.assertEqual(result.written, 0)


class DriftAndReconciliation(unittest.TestCase):
    def test_reconcile_clean_when_in_sync(self):
        target = DictTarget()
        with tempfile.TemporaryDirectory() as d:
            bridge = SyncBridge("b1", target, _state(d))
            bridge.project(_rows(), "cp1")
            report = bridge.reconcile(_rows())
        self.assertFalse(report.has_drift)
        self.assertEqual(len(report.in_sync), 3)

    def test_drift_injection_detected_and_repaired(self):
        target = DictTarget()
        with tempfile.TemporaryDirectory() as d:
            bridge = SyncBridge("b1", target, _state(d))
            bridge.project(_rows(), "cp1")
            # Inject drift: same version, different content
            target.corrupt("r2", payload={"name": "EVIL", "schema": None})
            report = bridge.reconcile(_rows())
            self.assertIn("r2", report.hash_mismatch)
            self.assertTrue(report.has_drift)
            result = bridge.repair(_rows(), report, "cp2")
            self.assertEqual(result.written, 1)
            self.assertFalse(bridge.reconcile(_rows()).has_drift)

    def test_stale_version_detected_and_repaired(self):
        target = DictTarget()
        with tempfile.TemporaryDirectory() as d:
            bridge = SyncBridge("b1", target, _state(d))
            bridge.project(_rows(), "cp1")
            # Source bumps r3 to v3 -> target is stale
            bumped = [SyncRow("r3", 3, {"name": "gamma", "schema": {"type": "string"}})]
            report = bridge.reconcile(_rows()[:2] + bumped)
            self.assertIn("r3", report.stale)
            result = bridge.repair(_rows()[:2] + bumped, report, "cp2")
            self.assertEqual(result.written, 1)
            self.assertIn("r3", bridge.reconcile(_rows()[:2] + bumped).in_sync)

    def test_missing_and_orphaned_reported(self):
        target = DictTarget()
        with tempfile.TemporaryDirectory() as d:
            bridge = SyncBridge("b1", target, _state(d))
            bridge.project(_rows(), "cp1")
            # source drops r1 (orphaned in target) and gains r4 (missing)
            new_source = _rows()[1:] + [SyncRow("r4", 1, {"name": "delta"})]
            report = bridge.reconcile(new_source)
            self.assertIn("r1", report.orphaned)
            self.assertIn("r4", report.missing)
            result = bridge.repair(new_source, report, "cp2")
            self.assertEqual(result.written, 1)  # only the missing row

    def test_content_hash_is_per_row_and_payload_sensitive(self):
        h1 = content_hash("r1", 1, {"a": 1})
        h2 = content_hash("r2", 1, {"a": 1})
        h3 = content_hash("r1", 1, {"a": 2})
        h4 = content_hash("r1", 2, {"a": 1})
        self.assertEqual(len({h1, h2, h3, h4}), 4)


class PartialUpdateAndRetries(unittest.TestCase):
    def test_partial_update_recovery(self):
        """A mid-batch failure leaves a usable checkpoint; the next run
        completes only the missing rows without duplicating the written ones."""
        target = DictTarget()
        target.fail_on.add("r2")
        with tempfile.TemporaryDirectory() as d:
            bridge = SyncBridge("b1", target, _state(d), max_retries=3)
            first = bridge.project(_rows(), "cp1")
            self.assertFalse(first.ok)
            self.assertEqual(first.written, 2)  # r1, r3 synced despite r2 failing
            self.assertEqual(len(first.failed), 1)
            self.assertEqual(first.failed[0]["attempts"], 3)
            # Writer recovers; resume the same checkpoint
            target.fail_on.clear()
            second = bridge.project(_rows(), "cp1")
            self.assertTrue(second.ok)
            self.assertEqual(second.written, 1)  # only r2
            self.assertEqual(second.skipped_idempotent, 2)  # r1, r3 via checkpoint
            # Successful writes only: r1=1, r3=1, r2 succeeds on resume (=1);
            # r2's three failed attempts in run 1 are not writes.
            self.assertEqual(sum(target.write_counts.values()), 3)
        self.assertEqual(target.write_counts, {"r1": 1, "r3": 1, "r2": 1})

    def test_bounded_retries_never_block_batch(self):
        target = DictTarget()
        target.fail_on.update({"r1", "r2"})
        with tempfile.TemporaryDirectory() as d:
            bridge = SyncBridge("b1", target, _state(d), max_retries=2)
            result = bridge.project(_rows(), "cp1")
        self.assertEqual(result.written, 1)  # r3 still written
        self.assertEqual(len(result.failed), 2)
        self.assertTrue(all(f["attempts"] == 2 for f in result.failed))

    def test_failed_rows_marked_in_state(self):
        target = DictTarget()
        target.fail_on.add("r2")
        with tempfile.TemporaryDirectory() as d:
            state = _state(d)
            bridge = SyncBridge("b1", target, state, max_retries=1)
            bridge.project(_rows(), "cp1")
            row = state.get("b1", "cp1", "r2")
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["attempts"], 1)
            synced = [r["row_id"] for r in state.rows_for_checkpoint("b1", "cp1") if r["status"] == "synced"]
            self.assertEqual(sorted(synced), ["r1", "r3"])

    def test_invalid_max_retries_rejected(self):
        target = DictTarget()
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(CatalogError):
                SyncBridge("b1", target, _state(d), max_retries=0)


class SharedConnection(unittest.TestCase):
    def test_state_accepts_injected_connection_factory(self):
        conn = sqlite3.connect(":memory:", isolation_level=None, timeout=3)
        conn.row_factory = sqlite3.Row
        state = SyncStateConnection(connection_factory=lambda: conn)
        state.record("b", "cp", "r", 1, "h", "synced", 1)
        self.assertEqual(state.get("b", "cp", "r")["content_hash"], "h")

    def test_state_requires_db_or_factory(self):
        with self.assertRaises(ValueError):
            SyncStateConnection()


if __name__ == "__main__":
    unittest.main()

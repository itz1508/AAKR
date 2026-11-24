"""Task 6 governance tests: auto-approval removed from the default config.

Acceptance:
- Default review mode is manual_review: the Host (operator) is the sole
  workflow authority and scope approval always requires an explicit
  approve_scope call.
- auto_approve is opt-in only, unreachable without an explicit constructor
  argument, and unknown modes are rejected.
- Lifecycle stores are no longer hard-wired to a local SQLite file: an
  injected connection_factory (platform-backed storage) is supported.
- No auto-publish is reachable in the default configuration: a full
  lifecycle run never transitions any item to a published state.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aakr.catalog.models import CatalogError
from aakr.lifecycle import (
    REVIEW_AUTO,
    REVIEW_EXTERNAL,
    REVIEW_MANUAL,
    REVIEW_MODES,
    KnowledgeGap,
    LifecycleOrchestrator,
    ReviewScope,
    SubmittedItem,
    SubmittedItemStore,
)


def _pool():
    return [
        {
            "doc_id": "d1",
            "title": "Alpha Beta",
            "content": "alpha beta gamma delta",
            "topics": [],
            "source_type": "documentation",
        }
    ]


def _item():
    return SubmittedItem(
        item_id="t6-item",
        request="test request",
        knowledge_gaps=[
            KnowledgeGap(
                gap_id="g1", description="alpha beta gamma", source_hints=["alpha"]
            )
        ],
        accepted_requirements=[],
    )


class ReviewModeDefaults(unittest.TestCase):
    def test_default_review_mode_is_manual(self):
        with tempfile.TemporaryDirectory() as d:
            orch = LifecycleOrchestrator(str(Path(d) / "t.db"), _pool())
        self.assertEqual(orch.review_mode, REVIEW_MANUAL)
        self.assertIn(REVIEW_MANUAL, REVIEW_MODES)

    def test_default_scope_review_is_pending_never_approved(self):
        """In the default config, scope review never approves by itself."""
        with tempfile.TemporaryDirectory() as d:
            orch = LifecycleOrchestrator(str(Path(d) / "t.db"), _pool())
            state = LifecycleState_for(orch)
            decision = orch.scope_review(state, ReviewScope())
        self.assertEqual(decision.decision, "pending")
        self.assertEqual(decision.mode, REVIEW_MANUAL)

    def test_explicit_approve_scope_is_the_only_approval_path(self):
        with tempfile.TemporaryDirectory() as d:
            orch = LifecycleOrchestrator(str(Path(d) / "t.db"), _pool())
            state = LifecycleState_for(orch)
            orch.scope_review(state, ReviewScope())
            decision = orch.approve_scope(state, reviewer="host")
        self.assertEqual(decision.decision, "approved")
        self.assertEqual(decision.reviewer, "host")

    def test_external_review_mode_stays_pending_until_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            orch = LifecycleOrchestrator(
                str(Path(d) / "t.db"), _pool(), review_mode=REVIEW_EXTERNAL
            )
            state = LifecycleState_for(orch)
            decision = orch.scope_review(state, ReviewScope())
            self.assertEqual(decision.decision, "pending")
            approved = orch.approve_scope(state, reviewer="host")
        self.assertEqual(approved.decision, "approved")
        self.assertEqual(approved.mode, REVIEW_EXTERNAL)

    def test_auto_approve_is_opt_in_only(self):
        """AUTO requires an explicit argument; the default never reaches it."""
        with tempfile.TemporaryDirectory() as d:
            orch = LifecycleOrchestrator(
                str(Path(d) / "t.db"), _pool(), review_mode=REVIEW_AUTO
            )
            state = LifecycleState_for(orch)
            decision = orch.scope_review(state, ReviewScope())
        self.assertEqual(decision.decision, "approved")  # documented opt-in
        # but the default is manual, so auto is unreachable without opt-in
        with tempfile.TemporaryDirectory() as d2:
            default_orch = LifecycleOrchestrator(str(Path(d2) / "t.db"), _pool())
        self.assertNotEqual(default_orch.review_mode, REVIEW_AUTO)

    def test_invalid_review_mode_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(CatalogError):
                LifecycleOrchestrator(
                    str(Path(d) / "t.db"), _pool(), review_mode="yolo"
                )


class StoreBackend(unittest.TestCase):
    def test_stores_accept_injected_connection_factory(self):
        """Stores are not hard-wired to a local SQLite file."""
        conn = sqlite3.connect(":memory:", isolation_level=None, timeout=3)
        conn.row_factory = sqlite3.Row
        factory = lambda: conn  # noqa: E731 - stand-in for a platform backend

        store = SubmittedItemStore(connection_factory=factory)
        self.assertIsNone(store.db_path)
        store.submit(_item())
        self.assertTrue(store.exists("t6-item"))
        fetched = store.get("t6-item")
        self.assertEqual(fetched.request, "test request")

    def test_orchestrator_accepts_injected_backend(self):
        conn = sqlite3.connect(":memory:", isolation_level=None, timeout=3)
        conn.row_factory = sqlite3.Row
        factory = lambda: conn  # noqa: E731
        orch = LifecycleOrchestrator(None, _pool(), connection_factory=factory)
        orch.submit_item(_item())
        self.assertTrue(orch.item_store.exists("t6-item"))

    def test_neither_db_path_nor_factory_rejected(self):
        with self.assertRaises(ValueError):
            SubmittedItemStore()
        with self.assertRaises(ValueError):
            LifecycleOrchestrator(None, _pool())


class NoAutoPublishInDefaultConfig(unittest.TestCase):
    def test_full_lifecycle_never_publishes_by_default(self):
        """A complete default-config run produces evidence only; no item ever
        reaches a published state and publication stays a separate
        policy-governed operation."""
        with tempfile.TemporaryDirectory() as d:
            orch = LifecycleOrchestrator(str(Path(d) / "t.db"), _pool())
            state = orch.run_full_lifecycle(_item())
            final_item = orch.item_store.get("t6-item")
        self.assertTrue(state.resolved)
        self.assertIn(state.evidence_package["eligible_for_publication"], (True, False))
        # No published state exists in the lifecycle store: statuses only ever
        # transition submitted -> remaining/resolved/exhausted.
        self.assertIn(final_item.status, {"submitted", "remaining", "resolved", "exhausted"})
        self.assertNotEqual(final_item.status, "published")


def LifecycleState_for(orch):
    """Fresh LifecycleState bound to a submitted item (phase helper)."""
    item = _item()
    from aakr.lifecycle import LifecycleState

    orch.item_store.submit(item)
    return LifecycleState(item_id=item.item_id, phase="submitted", submitted_item=item)


if __name__ == "__main__":
    unittest.main()

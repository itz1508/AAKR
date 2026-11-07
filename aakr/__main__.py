"""AAKR CLI entry point.

Usage:
  python -m aakr doctor         Run environment checks
  python -m aakr run <db_path>   Run lifecycle demo (requires source pool)
  python -m aakr status <db_path> <item_id>  Show item status
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path


def cmd_doctor() -> int:
    """Run the environment doctor script."""
    project_root = Path(__file__).resolve().parent.parent
    doctor = project_root / "scripts" / "doctor.py"
    if not doctor.exists():
        print("ERROR: scripts/doctor.py not found", file=sys.stderr)
        return 1
    import subprocess

    return subprocess.call([sys.executable, str(doctor)], cwd=str(project_root))


def cmd_run(db_path: str) -> int:
    """Run a lifecycle demo with the built-in fixture source pool."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    from aakr.lifecycle import (
        KnowledgeGap,
        LifecycleOrchestrator,
        ReviewScope,
        SubmittedItem,
    )

    source_pool = [
        {
            "doc_id": "d1",
            "title": "SQL Injection Prevention",
            "content": "Parameterized queries prevent sql injection by separating code from data",
            "topics": ["sql_injection", "parameterized_queries"],
            "source_type": "documentation",
        },
    ]
    item = SubmittedItem(
        item_id="cli-run",
        request="Verify sql injection prevention",
        knowledge_gaps=[
            KnowledgeGap(
                "g1",
                "sql injection prevention methods",
                ["sql_injection", "parameterized_queries"],
            )
        ],
        accepted_requirements=[],
    )
    orch = LifecycleOrchestrator(db_path, source_pool, max_attempts=3)
    state = orch.run_full_lifecycle(item, scope=ReviewScope(include_external=False))
    print(f"Resolved: {state.resolved}")
    print(f"Remaining gaps: {len(state.remaining_gaps)}")
    if state.verification_result:
        for gap_id, result in state.verification_result.items():
            verdict = result.get("verdict", "unknown")
            claims = result.get("claims", [])
            print(f"  Gap {gap_id}: verdict={verdict}, claims={len(claims)}")
    return 0 if state.resolved else 1


def cmd_status(db_path: str, item_id: str) -> int:
    """Show the status of a submitted item."""
    from aakr.lifecycle import CheckpointStore, RecordStore, SubmittedItemStore

    item = SubmittedItemStore(db_path).get(item_id)
    if not item:
        print(f"Item not found: {item_id}", file=sys.stderr)
        return 1

    print(f"Item: {item.item_id}")
    print(f"  Status: {item.status}")
    print(f"  Retry count: {item.retry_count}")
    print(f"  Gaps: {len(item.knowledge_gaps)}")
    print(f"  Requirements: {len(item.accepted_requirements)}")

    cp = CheckpointStore(db_path).load_latest(item_id)
    if cp:
        print(f"  Checkpoint: {cp.checkpoint_id}")
        print(f"    Recovery state: {cp.recovery_state}")
        print(f"    Resolved gaps: {len(cp.resolved_gaps)}")
        print(f"    Research attempts: {cp.research_attempts}")
    else:
        print("  Checkpoint: none")

    records = RecordStore(db_path).get_all_for_item(item_id)
    print(f"  Research records: {len(records)}")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 1

    cmd = sys.argv[1]
    if cmd == "doctor":
        return cmd_doctor()
    elif cmd == "run":
        if len(sys.argv) < 3:
            print("Usage: python -m aakr run <db_path>", file=sys.stderr)
            return 1
        return cmd_run(sys.argv[2])
    elif cmd == "status":
        if len(sys.argv) < 4:
            print("Usage: python -m aakr status <db_path> <item_id>", file=sys.stderr)
            return 1
        return cmd_status(sys.argv[2], sys.argv[3])
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

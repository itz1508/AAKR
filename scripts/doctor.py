"""AAKR environment doctor — validates runtime prerequisites.

Checks:
  1. Python version (>= 3.11)
  2. Required dependencies (rfc8785)
  3. Optional dependencies (pyspark for Databricks backend)
  4. Config file presence (settings/config.yaml)
  5. SQLite connectivity (in-memory round-trip)
  6. AAKR package importability

Usage:
  python scripts/doctor.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add project root to sys.path so aakr package is importable
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

CHECKS_PASSED = 0
CHECKS_FAILED = 0
CHECKS_SKIPPED = 0


def _ok(label: str, detail: str = "") -> None:
    global CHECKS_PASSED
    CHECKS_PASSED += 1
    print(f"  [PASS] {label}{f': {detail}' if detail else ''}")


def _fail(label: str, detail: str = "") -> None:
    global CHECKS_FAILED
    CHECKS_FAILED += 1
    print(f"  [FAIL] {label}{f': {detail}' if detail else ''}")


def _skip(label: str, reason: str = "") -> None:
    global CHECKS_SKIPPED
    CHECKS_SKIPPED += 1
    print(f"  [SKIP] {label}{f': {reason}' if reason else ''}")


def check_python_version() -> None:
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 11):
        _ok("Python version", f"{major}.{minor}")
    else:
        _fail("Python version", f"{major}.{minor} (requires >= 3.11)")


def check_dependency(name: str, *, required: bool = True) -> None:
    try:
        mod = __import__(name)
        version = getattr(mod, "__version__", "unknown")
        _ok(f"Dependency: {name}", version)
    except ImportError:
        if required:
            _fail(f"Dependency: {name}", "not installed")
        else:
            _skip(f"Dependency: {name}", "optional, not installed")


def check_config_file() -> None:
    project_root = Path(__file__).resolve().parent.parent
    config_path = project_root / "settings" / "config.yaml"
    if config_path.exists():
        _ok("Config file", str(config_path.relative_to(project_root)))
    else:
        _fail("Config file", f"{config_path} not found")


def check_env_example() -> None:
    project_root = Path(__file__).resolve().parent.parent
    env_path = project_root / ".env.example"
    if env_path.exists():
        _ok(".env.example", "present")
    else:
        _skip(".env.example", "not found (optional)")


def check_sqlite() -> None:
    try:
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE _doctor_test (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO _doctor_test VALUES (1)")
        row = conn.execute("SELECT id FROM _doctor_test").fetchone()
        conn.close()
        if row and row[0] == 1:
            _ok("SQLite connectivity", f"sqlite3 {sqlite3.sqlite_version}")
        else:
            _fail("SQLite connectivity", "round-trip failed")
    except Exception as exc:
        _fail("SQLite connectivity", str(exc))


def check_aakr_import() -> None:
    try:
        import aakr

        _ok("AAKR package", f"v{aakr.__version__}")
    except ImportError as exc:
        _fail("AAKR package", str(exc))


def check_aakr_lifecycle() -> None:
    try:
        from aakr.lifecycle import LifecycleOrchestrator  # noqa: F401

        _ok("Lifecycle import", "LifecycleOrchestrator")
    except ImportError as exc:
        _fail("Lifecycle import", str(exc))


def main() -> int:
    print("AAKR Environment Doctor")
    print("=" * 50)

    print("\nPython:")
    check_python_version()

    print("\nDependencies:")
    check_dependency("rfc8785", required=True)
    check_dependency("pyspark", required=False)

    print("\nConfiguration:")
    check_config_file()
    check_env_example()

    print("\nStorage:")
    check_sqlite()

    print("\nPackage:")
    check_aakr_import()
    check_aakr_lifecycle()

    print("\n" + "=" * 50)
    print(f"  Passed:  {CHECKS_PASSED}")
    print(f"  Failed:  {CHECKS_FAILED}")
    print(f"  Skipped: {CHECKS_SKIPPED}")

    return 1 if CHECKS_FAILED > 0 else 0


if __name__ == "__main__":
    sys.exit(main())

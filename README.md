# OneShot Universal Catalog (AAKR)

Provider-neutral Catalog for reusable knowledge entries with manifest
canonicalization, append-only provenance, source verification, and
three-valued PRE applicability evaluation.

## Structure

```
oneshot-catalog/
  src/
    manifest.py            # RFC8785/JCS canonicalization, SHA-256 manifests
    catalog_store.py       # Abstract provider-neutral CatalogStore API
    sqlite_store.py        # SQLite reference backend
    databricks_store.py    # Databricks UC/Delta backend (partial)
    curator.py             # Curator: draft/publish/deprecate/verify/get
    verifier.py            # Source verification (creator-as-verifier rejected)
    catalog_lookup.py      # Lookup request/response validation
    applicability.py       # PRE three-valued condition/group evaluation
    index_builder.py       # Immutable fulltext/semantic index generation
    proposal_gateway.py    # Runtime learning proposals (outside Catalog)
    oneshot_catalog_schema_sqlite.sql  # SQLite schema
  tests/
    test_reference.py      # 42 fixture-only reference tests
  BUILD_REPORT.md          # Evidence-grounded build report
  README.md                # This file
```

## Quick Start (SQLite reference)

```python
import sys

sys.path.insert(0, "src")
from sqlite_store import SQLiteStore
from curator import Curator
from manifest import manifest

store = SQLiteStore(":memory:", initialize=True)
curator = Curator(store, fixture_ascii_only=True)

bundle = {...}  # See test_reference.py for fixture format
digest = manifest(bundle, fixture_ascii_only=True)
curator.create_draft(bundle, digest)
curator.publish("E", 1)
result = store.lookup(
    domains=["software"], intent="auto-discovery", fixture_ascii_only=True
)
```

## Databricks Backend

Tables are created in `oneshot_dev.knowledge` with:
- `delta.appendOnly = true` (blocks UPDATE/DELETE)
- `delta.feature.catalogManaged = supported` (multi-table atomic txns)
- CHECK constraints via `ALTER TABLE ADD CONSTRAINT` (enforced)
- PK/FK/UNIQUE constraints (informational — Curator enforces in code)
- `BEGIN ATOMIC ... END;` for multi-table atomic writes (serverless compute)

## Contracts

- Manifest profile: `oneshot-catalog-manifest-v2`
- Verification profile: `oneshot-catalog-verification-v1`
- Index profile: `oneshot-catalog-index-v1`
- Catalog contract: `2.0`

## Testing

```bash
cd tests && python -m unittest test_reference
# 42 tests, 0 failures, 0 errors
```

## Key Design Decisions

1. **Manifest canonicalization** uses RFC 8785 (JCS). Without the `rfc8785`
   package, a fixture-only ASCII serializer is used (`fixture_ascii_only=True`).
2. **Curator is the sole writer**. All validation (uniqueness, FK, sequence,
   graph cycles) is enforced in Python code under real transactions.
3. **PK/FK/UNIQUE are informational** on Databricks. The Curator enforces them.
4. **Source verification is append-only**. The verifier must not be the
   entry version's creator.
5. **PRE is three-valued**: satisfied, not_satisfied, evidence_unavailable.
   Missing evidence does not false-match.
6. **Learning proposals live outside Catalog truth** in a Runtime-owned store.
   Accepted proposals create new drafts with `derived_from_ref/sha256`.
7. **No ranking chooses one authoritative winner**. Scores measure
   retrieval relevance only.
8. **No fake index digests**. Unavailable indexes report `index_status:
   unavailable` with null `index_sha256`.
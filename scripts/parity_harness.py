"""Task 10 parity harness (shadow phase): legacy channel vs AAKR channel.

Run in a Databricks session with Spark + SDK credentials (e.g. a notebook
or Genie Code executeCode). For every query:
1. Rank tools via the AI Search index (legacy authority over
   workspace.default.tool_library) - num_results covers the whole library.
2. Build the AAKR-gated view of the SAME ranking using the discovery
   service's actual reconciliation statement against
   workspace.aakr_catalog, plus version and input_schema from
   aakr_catalog.tool_contracts (AAKR is the catalog of record).
3. Parity holds when the gated list equals the legacy ranking and every
   kept tool has published state, a major-version match
   (tool_library "1.0.0" <-> AAKR int 1), and an identical input schema.

Acceptance: 100% parity across >= 20 queries before any cutover is
proposed. Any FAIL line names the drifted tool and which check failed.

Last run (2026-11-24): 28/28 queries OK - 100.0% parity over the 24-tool
library (24 tool-derived queries + 4 general queries).
"""

from __future__ import annotations

import sys

from pyspark.sql import SparkSession

sys.path.insert(0, "/Workspace/Users/mistraldaddy@outlook.com/tool-discovery-service")
from discovery import reconciliation_statement  # the service's own SQL

INDEX_NAME = "workspace.default.tool_library_index"
SOURCE_TABLE = "workspace.default.tool_library"
AAKR_SCHEMA = "workspace.aakr_catalog"


def main() -> int:
    from databricks.sdk import WorkspaceClient

    spark = SparkSession.builder.getOrCreate()
    w = WorkspaceClient()

    tools = {
        r["tool_id"]: r.asDict()
        for r in spark.sql(
            f"SELECT tool_id, tool_name, persona, version, description, "
            f"input_schema, text FROM {SOURCE_TABLE}"
        ).collect()
    }
    all_ids = sorted(tools)

    states_rows = spark.sql(
        reconciliation_statement(AAKR_SCHEMA, all_ids)
    ).collect()
    aakr_states = {r[0]: {"state": r[1], "version": r[2]} for r in states_rows}
    aakr_schemas = {
        r["entry_id"]: r["input_schema"]
        for r in spark.sql(
            f"SELECT entry_id, input_schema FROM {AAKR_SCHEMA}.tool_contracts "
            "WHERE version = 1"
        ).collect()
    }

    def version_major(v):
        return int(str(v).split(".")[0])

    queries = [t["description"] for t in tools.values()] + [
        "find tables with sales data",
        "build a scheduled report",
        "why did my job fail",
        "run a read-only query",
    ]
    print(f"harness: {len(queries)} queries, {len(all_ids)} tools")

    parity_ok, failures = 0, []
    for q in queries:
        resp = w.api_client.do(
            "POST",
            f"/api/2.0/vector-search/indexes/{INDEX_NAME}/query",
            body={"query_text": q, "columns": ["tool_id", "persona"], "num_results": 24},
        )
        ranked = [row[0] for row in resp["result"]["data_array"]]
        aakr_ids, mismatches = [], []
        for tid in ranked:
            st = aakr_states.get(tid, {}).get("state")
            if st != "published":
                mismatches.append(f"{tid}:state={st}")
                continue
            if version_major(tools[tid]["version"]) != aakr_states[tid]["version"]:
                mismatches.append(f"{tid}:version")
                continue
            if aakr_schemas.get(tid) != tools[tid]["input_schema"]:
                mismatches.append(f"{tid}:schema")
                continue
            aakr_ids.append(tid)
        ok = aakr_ids == ranked and not mismatches
        if ok:
            parity_ok += 1
        else:
            failures.append((q, mismatches))
        print(f"  [{'OK ' if ok else 'FAIL'}] ({len(ranked):2d} results) {q[:70]}")

    rate = 100.0 * parity_ok / len(queries)
    print(f"parity: {parity_ok}/{len(queries)} queries = {rate:.1f}%")
    for q, mm in failures:
        print("  FAIL:", q, mm)
    return 0 if parity_ok == len(queries) else 1


if __name__ == "__main__":
    raise SystemExit(main())

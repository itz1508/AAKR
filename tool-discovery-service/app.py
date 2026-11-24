"""Tool Discovery Service.

Semantic tool discovery over a curated Databricks capability registry.
A natural-language task goes in; ranked tool definitions come out.

Design rules (enforced below):
- Ranking is done by the AI Search index workspace.default.tool_library_index
  (endpoint dbindex). The indexed copy is used ONLY for ranking.
- Every ranked candidate is reconciled against the AAKR published snapshot
  (workspace.aakr_catalog): the tool record must exist, be a tool entry, and
  have a currently published (not draft, not deprecated/superseded) version.
  Unestablished candidates are dropped with reason codes and never returned.
- Canonical input schemas are resolved from the workspace.default.tool_library
  Delta table (the registry of record) on every request - never from the index.
- Persona authorization is per-caller and table-backed (discovery_persona_grants
  keyed on the caller's authenticated email): grants are checked AFTER ranking
  and never change the ranking order. A caller sees none of a persona they
  have no grant for; unauthenticated or unknown callers see nothing.
- This service never executes any discovered tool - it returns definitions only.
- Plain HTTP/JSON: independent of MCP and agent frameworks.
"""

import json
import os

import mlflow
from databricks.sdk import WorkspaceClient
from flask import Flask, jsonify, request

from discovery import (
    aakr_schemas_statement,
    build_response,
    grants_statement,
    reconciliation_statement,
    schemas_statement,
)

INDEX_NAME = "workspace.default.tool_library_index"
ENDPOINT_NAME = "dbindex"
SOURCE_TABLE = "workspace.default.tool_library"
AAKR_SCHEMA = os.environ.get("AAKR_CATALOG", "workspace.aakr_catalog")
GRANTS_TABLE = os.environ.get("GRANTS_TABLE", "workspace.default.discovery_persona_grants")
# SCHEMA_SOURCE selects where canonical input schemas are resolved from:
#   legacy - workspace.default.tool_library (pre-cutover registry of record)
#   aakr   - workspace.aakr_catalog.tool_contracts, currently published
#            version only (post-cutover catalog of record). Both paths are
#            gated by AAKR reconciliation regardless; this only changes the
#            schema resolution source. Deploy default is legacy so a rollout
#            is a behavioral no-op until the flag is flipped.
SCHEMA_SOURCE = os.environ.get("SCHEMA_SOURCE", "legacy").strip().lower()
if SCHEMA_SOURCE not in ("legacy", "aakr"):
    raise ValueError(
        f"SCHEMA_SOURCE must be 'legacy' or 'aakr', got {SCHEMA_SOURCE!r}"
    )
VALID_PERSONAS = {"researcher", "auditor", "planner", "builder"}
DEFAULT_ALLOWED = "researcher,auditor,planner,builder"

WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")
ALLOWED_PERSONAS = {
    p.strip()
    for p in os.environ.get("ALLOWED_PERSONAS", DEFAULT_ALLOWED).split(",")
    if p.strip()
}

app = Flask(__name__)

# The app's built-in OAuth credentials are picked up automatically -
# no tokens in code, no secrets to manage.
w = WorkspaceClient()

# --- MLflow GenAI tracing -------------------------------------------------
# Spans: tool_discovery (CHAIN) > similarity_search (RETRIEVER) + resolve_schemas (TOOL).
# Enrichment follows the Databricks "enrich traces" guidance: user/session
# context in trace metadata, service/query facts in tags. Tracing must never
# take the discovery service down, so setup and enrichment are best-effort.
TRACING_ENABLED = (
    os.environ.get("ENABLE_MLFLOW_TRACING", "true").lower() in ("1", "true", "yes")
)
if TRACING_ENABLED:
    try:
        mlflow.set_experiment(
            os.environ.get("MLFLOW_EXPERIMENT_NAME", "tool-discovery-service")
        )
    except Exception:  # noqa: BLE001
        pass


def _error(status, message):
    return jsonify({"error": message}), status


def _parse_schema(v):
    """Schema values arrive as JSON strings; tolerate dict/null/bad-JSON."""
    if v is None or isinstance(v, dict):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return v


def _sql_rows(statement: str):
    """Run one SQL statement via the Statement Execution API, return rows."""
    resp = w.api_client.do(
        "POST",
        "/api/2.0/sql/statements",
        body={
            "warehouse_id": WAREHOUSE_ID,
            "statement": statement,
            "wait": {"timeout": "120s"},
            "disposition": "INLINE",
            "format": "JSON_ARRAY",
        },
    )
    result = resp.get("result") or {}
    return result.get("data_array") or []


@mlflow.trace(
    name="similarity_search",
    span_type="RETRIEVER",
    attributes={"index": INDEX_NAME, "endpoint": ENDPOINT_NAME},
)
def query_index(task, top_k):
    """Rank tools by semantic similarity.

    The response rows carry the requested columns plus the similarity score
    appended as the final element.
    """
    resp = w.api_client.do(
        "POST",
        f"/api/2.0/vector-search/indexes/{INDEX_NAME}/query",
        body={
            "query_text": task,
            "columns": ["tool_id", "tool_name", "persona", "description"],
            "num_results": top_k,
        },
    )
    return resp.get("result", {}).get("data_array", []) or []


@mlflow.trace(
    name="resolve_schemas",
    span_type="TOOL",
    attributes={"source_table": SOURCE_TABLE, "schema_source": SCHEMA_SOURCE},
)
def resolve_schemas(tool_ids):
    """Resolve canonical input schemas (source depends on SCHEMA_SOURCE).

    legacy: the registry table (pre-cutover source of truth).
    aakr: the currently published version's contract in the AAKR catalog
    (post-cutover source of record). Both use the SQL Statement Execution
    API against a serverless warehouse.
    """
    if not tool_ids:
        return {}
    if SCHEMA_SOURCE == "aakr":
        statement = aakr_schemas_statement(AAKR_SCHEMA, tool_ids)
    else:
        statement = schemas_statement(SOURCE_TABLE, tool_ids)
    data = _sql_rows(statement)
    if not data:
        raise RuntimeError("schema lookup returned no rows")
    return {row[0]: row[1] for row in data if len(row) >= 2}


@mlflow.trace(
    name="reconcile_catalog",
    span_type="TOOL",
    attributes={"aakr_schema": AAKR_SCHEMA},
)
def catalog_states(tool_ids):
    """Classify candidates against the AAKR published snapshot.

    Returns {tool_id: {"state": absent|not_published|published, "version": int}}.
    Only published candidates may be returned; a tool_id missing from the map
    is treated as absent (ghost).
    """
    if not tool_ids:
        return {}
    rows = _sql_rows(reconciliation_statement(AAKR_SCHEMA, tool_ids))
    states = {}
    for row in rows:
        if len(row) >= 2 and row[0]:
            states[row[0]] = {"state": row[1], "version": row[2] if len(row) > 2 else None}
    return states


def caller_grants(caller):
    """Per-caller persona grants from the grants table (deny by default).

    An unknown or unauthenticated caller, or a missing grants table, yields
    an empty grant set - the caller sees nothing until granted.
    """
    try:
        rows = _sql_rows(grants_statement(GRANTS_TABLE, caller))
    except Exception:  # noqa: BLE001
        return set()
    return {row[0] for row in rows if row and row[0]}


@app.route("/")
def root():
    return jsonify(
        {
            "service": "Tool Discovery Service",
            "description": "Natural-language tool discovery over the Databricks capability registry. Discovery only - tools are never executed.",
            "endpoints": {
                "POST /discover": {
                    "body": {
                        "task": "natural-language description of what you need (required)",
                        "persona": "optional filter: researcher|auditor|planner|builder",
                        "top_k": "optional result count, 1-10, default 5",
                    }
                },
                "GET /health": "index and endpoint status",
            },
        }
    )


@app.route("/health")
def health():
    try:
        endpoint = w.api_client.do(
            "GET", f"/api/2.0/vector-search/endpoints/{ENDPOINT_NAME}"
        )
        index = w.api_client.do(
            "GET",
            f"/api/2.0/vector-search/indexes/{INDEX_NAME}",
            query={"endpoint_name": ENDPOINT_NAME},
        )
        ep_state = (endpoint.get("endpoint_status") or {}).get("state")
        idx_status = index.get("status") or {}
        healthy = ep_state == "ONLINE" and bool(idx_status.get("ready"))
        return jsonify(
            {
                "status": "healthy" if healthy else "degraded",
                "endpoint": {"name": ENDPOINT_NAME, "state": ep_state},
                "index": {
                    "name": INDEX_NAME,
                    "detailed_state": idx_status.get("detailed_state"),
                    "ready": idx_status.get("ready"),
                    "indexed_row_count": idx_status.get("indexed_row_count"),
                },
            }
        ), 200 if healthy else 503
    except Exception as e:  # noqa: BLE001
        return _error(503, f"health check failed: {e}")


@app.route("/discover", methods=["POST"])
@mlflow.trace(name="tool_discovery", span_type="CHAIN")
def discover():
    payload = request.get_json(silent=True) or {}

    task = (payload.get("task") or "").strip()
    if not task:
        return _error(400, "'task' is required: a natural-language description of what you need")

    persona = payload.get("persona")
    if persona is not None and persona not in VALID_PERSONAS:
        return _error(400, f"'persona' must be one of {sorted(VALID_PERSONAS)}")

    top_k = payload.get("top_k", 5)
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 10:
        return _error(400, "'top_k' must be an integer between 1 and 10")

    # 1) Rank tools via the AI Search index (index copy used only for ranking).
    try:
        hits = query_index(task, top_k)
    except Exception as e:  # noqa: BLE001
        return _error(502, f"index query failed: {e}")

    ranked = []
    for row in hits:
        values = list(row) + [None] * 5
        ranked.append(
            {
                "tool_id": values[0],
                "tool_name": values[1],
                "persona": values[2],
                "description": values[3],
                "similarity_score": values[4],
            }
        )

    # 2) Reconcile every candidate against the AAKR published snapshot
    #    (post-ranking; never re-orders results).
    try:
        states = catalog_states([r["tool_id"] for r in ranked])
    except Exception as e:  # noqa: BLE001
        return _error(502, f"catalog reconciliation failed: {e}")

    # 3) Per-caller persona grants (table-backed, post-ranking). The caller
    #    identity comes from the authenticated email forwarded by the platform.
    caller = (request.headers.get("X-Forwarded-Email", "") or "").strip().lower()
    grants = caller_grants(caller) if caller else set()

    # 4) Canonical schemas from the registry table (source of truth).
    try:
        schemas = resolve_schemas(
            [r["tool_id"] for r in ranked if states.get(r["tool_id"], {}).get("state") == "published"]
        )
    except Exception as e:  # noqa: BLE001
        return _error(502, f"schema resolution failed: {e}")

    results, dropped = build_response(
        ranked,
        persona_filter=persona,
        caller_grants=grants,
        service_allowed_personas=ALLOWED_PERSONAS,
        catalog_states=states,
        schemas={k: _parse_schema(v) for k, v in schemas.items()},
    )

    # --- Trace enrichment: user/session context + query tags -------------
    try:
        mlflow.update_current_trace(
            metadata={
                "mlflow.trace.user": request.headers.get(
                    "X-Forwarded-Email", "unauthenticated"
                ),
                "mlflow.trace.session": (
                    payload.get("session_id")
                    or request.headers.get("X-Session-Id", "default")
                ),
            },
            tags={
                "service": "tool-discovery-service",
                "persona_filter": persona or "none",
                "returned": str(len(results)),
            },
        )
    except Exception:  # noqa: BLE001
        pass

    return jsonify(
        {
            "query": task,
            "persona_filter": persona,
            "caller": caller or "unauthenticated",
            "returned": len(results),
            "results": results,
            "dropped": dropped,
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

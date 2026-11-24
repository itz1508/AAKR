"""Framework-free discovery pipeline: rank -> authorize -> reconcile -> resolve.

Kept dependency-free (stdlib only) so the post-ranking rules are testable
without Flask, the Databricks SDK, or network access. app.py wires the real
clients (AI Search index, SQL Statement Execution) into these functions.

Post-ranking invariants enforced here:
- Authorization (per-caller persona grants, the service-level allowlist, and
  an optional persona filter) never re-orders results: candidates are
  inspected in ranked order and kept or dropped in place.
- Reconciliation requires every returned candidate to be an established
  tool record in the AAKR published snapshot: present as a tool entry, with
  a currently published (not draft, not deprecated/superseded) version.
  Unestablished candidates are dropped with reason codes and never returned.
- A candidate whose canonical input schema does not resolve is never
  returned.
"""

from __future__ import annotations

from typing import Any


def _lit(value: str) -> str:
    """Single SQL string literal with doubled quotes (injection-safe)."""
    return "'" + str(value).replace("'", "''") + "'"


def schemas_statement(source_table: str, tool_ids: list[str]) -> str:
    """Statement resolving canonical input schemas from the registry table."""
    id_list = ",".join(_lit(t) for t in tool_ids)
    return f"SELECT tool_id, input_schema FROM {source_table} WHERE tool_id IN ({id_list})"


def aakr_schemas_statement(aakr_schema: str, tool_ids: list[str]) -> str:
    """Statement resolving input schemas from the AAKR catalog of record.

    Returns the input_schema of each tool's CURRENTLY PUBLISHED version from
    {aakr_schema}.tool_contracts (never draft/deprecated versions). Row key
    is aliased to tool_id so both sources produce {tool_id: input_schema}.
    Used when the discovery service runs with SCHEMA_SOURCE=aakr (post-cutover).
    """
    id_list = ",".join(_lit(t) for t in tool_ids)
    return (
        "WITH pub AS ("
        f"  SELECT s.entry_id, MAX(s.version) AS version "
        f"  FROM {aakr_schema}.version_current_status s "
        f"  JOIN {aakr_schema}.entries e ON e.entry_id = s.entry_id "
        "  WHERE e.entry_type = 'tool' AND s.status = 'published' "
        "  GROUP BY s.entry_id"
        ") "
        f"SELECT p.entry_id AS tool_id, tc.input_schema "
        f"FROM pub p "
        f"JOIN {aakr_schema}.tool_contracts tc ON tc.entry_id = p.entry_id AND tc.version = p.version "
        f"WHERE p.entry_id IN ({id_list})"
    )


def grants_statement(grants_table: str, caller: str) -> str:
    """Statement returning the personas granted to one caller."""
    return f"SELECT persona FROM {grants_table} WHERE caller = {_lit(caller)}"


def reconciliation_statement(aakr_schema: str, tool_ids: list[str]) -> str:
    """Statement classifying each candidate against the AAKR published snapshot.

    Rows: (tool_id, state, version) where state is one of
      - 'absent': no tool record for this id (ghost candidate)
      - 'not_published': tool record exists but no currently published
        version (draft-only, or deprecated/superseded)
      - 'published': has a currently published version; version is the
        highest currently published one.
    """
    values = ",".join(f"({_lit(t)})" for t in tool_ids)
    return (
        "WITH pub AS ("
        f"  SELECT s.entry_id, MAX(s.version) AS version "
        f"  FROM {aakr_schema}.version_current_status s "
        f"  JOIN {aakr_schema}.entries e ON e.entry_id = s.entry_id "
        "  WHERE e.entry_type = 'tool' AND s.status = 'published' "
        "  GROUP BY s.entry_id"
        ") "
        f"SELECT c.tool_id, "
        "CASE WHEN e.entry_id IS NULL OR e.entry_type <> 'tool' THEN 'absent' "
        "     WHEN p.entry_id IS NULL THEN 'not_published' "
        "     ELSE 'published' END AS state, "
        "p.version "
        f"FROM (VALUES {values}) AS c(tool_id) "
        f"LEFT JOIN {aakr_schema}.entries e ON e.entry_id = c.tool_id "
        "LEFT JOIN pub p ON p.entry_id = c.tool_id"
    )


def build_response(
    ranked: list[dict[str, Any]],
    *,
    persona_filter: str | None,
    caller_grants: set[str],
    service_allowed_personas: set[str],
    catalog_states: dict[str, dict[str, Any]],
    schemas: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply post-ranking filters in ranked order. Returns (results, dropped).

    Authorization and reconciliation never re-order candidates: each ranked
    candidate is kept or dropped in place, preserving the index ranking for
    everything that survives.
    """
    results: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []

    for r in ranked:
        tool_id = r["tool_id"]
        persona = r.get("persona")

        # 1) Authorization: per-caller grants, then the service-level
        #    allowlist, then the caller's explicit persona filter.
        if persona not in caller_grants:
            dropped.append({"tool_id": tool_id, "reason": "not_granted"})
            continue
        if persona not in service_allowed_personas:
            dropped.append({"tool_id": tool_id, "reason": "persona_not_allowed"})
            continue
        if persona_filter is not None and persona != persona_filter:
            dropped.append({"tool_id": tool_id, "reason": "persona_filter"})
            continue

        # 2) Reconciliation against the AAKR published snapshot.
        state = catalog_states.get(tool_id)
        state_name = state.get("state") if state else "absent"
        if state_name != "published":
            reason = "not_published" if state_name == "not_published" else "not_in_catalog"
            dropped.append({"tool_id": tool_id, "reason": reason})
            continue

        # 3) The canonical schema must resolve for the established record.
        schema = schemas.get(tool_id)
        if schema is None:
            dropped.append({"tool_id": tool_id, "reason": "schema_unresolved"})
            continue

        results.append(
            {
                "tool_id": tool_id,
                "tool_name": r.get("tool_name"),
                "persona": persona,
                "description": r.get("description"),
                "version": state.get("version"),
                "input_schema": schema,
                "similarity_score": r.get("similarity_score"),
            }
        )

    return results, dropped

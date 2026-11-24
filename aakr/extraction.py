"""Structured extraction: unstructured sources -> typed, provenance-carrying
DRAFT records for the AAKR catalog.

Design rules (enforced below):
- Extraction is injector-based: the LLM (or a test double) is a callable
  passed to extract_records(); this module never binds to a model provider.
- Schemas are explicit, versioned, and CLOSED: core catalog records reject
  additional properties (no additionalProperties:true for contracts).
- Every record carries a per-record SHA-256 over its canonical
  (record_id, record_type, payload) JSON and at least one SourceRef with a
  64-hex content hash. Validation gates: schema validity, required-field
  coverage, type conformance, hash integrity, provenance presence, and
  factual-support flags.
- Invalid extractions are reported with errors, never silently dropped.
- OUTPUT IS DRAFT-ONLY. This module defines no publish, deprecate, or status
  path. Drafts enter the catalog through Curator.create_draft under the
  authenticated principal, and publication remains a separate Host-governed
  operation (lifecycle default: manual_review). This mirrors the task-6
  governance invariant that nothing auto-publishes.
- Tool summaries produced here are DERIVED artifacts - they never replace
  published tool contracts.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from aakr.catalog.models import CatalogError, manifest

EXTRACTION_PROFILE = "aakr-structured-extraction-v1"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_URI = re.compile(r"^[^\s'\"]+$")


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def record_hash(record_id: str, record_type: str, payload: dict[str, Any]) -> str:
    """Per-record SHA-256 over the canonical (id, type, payload) JSON."""
    return hashlib.sha256(
        _canonical({"record_id": record_id, "record_type": record_type, "payload": payload}).encode()
    ).hexdigest()


@dataclass(frozen=True)
class SourceRef:
    source_type: str  # documentation | code | api_spec | ...
    uri: str
    content_sha256: str  # 64-hex digest of the exact source content
    source_version: str = "unversioned"  # source_refs.source_version is NOT NULL

    def __post_init__(self):
        if not self.source_type.strip():
            raise CatalogError("source_ref requires a nonblank source_type")
        if not _SAFE_URI.match(self.uri):
            raise CatalogError(f"unsafe source uri: {self.uri!r}")
        if not _HEX64.match(self.content_sha256):
            raise CatalogError("source_ref.content_sha256 must be 64 lowercase hex")


@dataclass(frozen=True)
class ExtractionSchema:
    """An explicit, versioned, CLOSED record schema."""

    record_type: str
    version: int
    required_fields: tuple[str, ...]
    field_types: dict[str, str]  # field -> "str"|"int"|"float"|"bool"|"list"|"dict"

    def __post_init__(self):
        if not self.record_type.strip():
            raise CatalogError("schema requires a nonblank record_type")
        if self.version < 1:
            raise CatalogError("schema version must be >= 1")
        missing = [f for f in self.required_fields if f not in self.field_types]
        if missing:
            raise CatalogError(f"required fields missing type declarations: {missing}")


@dataclass
class ExtractedRecord:
    record_id: str
    record_type: str
    payload: dict[str, Any]
    source_refs: list[SourceRef]
    extracted_by: str  # extractor profile (model id / tool name)
    factual_support: dict[str, bool] | None = None  # required field -> supported?
    content_hash: str = ""

    def __post_init__(self):
        if not self.content_hash:
            self.content_hash = record_hash(self.record_id, self.record_type, self.payload)


@dataclass
class ValidationResult:
    ok: bool
    checks: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


_TYPE_MAP = {"str": str, "int": int, "float": float, "bool": bool, "list": list, "dict": dict}


def validate_record(record: ExtractedRecord, schema: ExtractionSchema) -> ValidationResult:
    """Gate every extracted record: schema, fields, types, hash, provenance,
    factual support. Invalid records list their errors and are never
    silently dropped by callers."""
    checks: dict[str, bool] = {}
    errors: list[str] = []

    schema_valid = (
        record.record_type == schema.record_type
        and not (set(record.payload) - set(schema.field_types))
    )
    checks["schema_valid"] = schema_valid
    if not schema_valid:
        extra = set(record.payload) - set(schema.field_types)
        errors.append(f"record_type/schema mismatch; unexpected fields: {sorted(extra)}")

    missing = [f for f in schema.required_fields if f not in record.payload]
    checks["required_fields_present"] = not missing
    if missing:
        errors.append(f"missing required fields: {missing}")

    bad_types = [
        f for f, t in schema.field_types.items()
        if f in record.payload and not isinstance(record.payload[f], _TYPE_MAP[t])
    ]
    checks["field_types_valid"] = not bad_types
    if bad_types:
        errors.append(f"field type mismatches: {bad_types}")

    hash_ok = record.content_hash == record_hash(record.record_id, record.record_type, record.payload)
    checks["hash_integrity"] = hash_ok
    if not hash_ok:
        errors.append("content_hash does not match the record payload")

    provenance_ok = bool(record.source_refs)
    checks["provenance_present"] = provenance_ok
    if not provenance_ok:
        errors.append("record requires at least one SourceRef")

    support_ok = True
    if record.factual_support is not None:
        unsupported = [f for f in schema.required_fields if not record.factual_support.get(f, False)]
        support_ok = not unsupported
        if unsupported:
            errors.append(f"required fields without factual support: {unsupported}")
    checks["factual_support"] = support_ok

    return ValidationResult(ok=not errors, checks=checks, errors=errors)


@dataclass
class ExtractionResult:
    valid: list[ExtractedRecord] = field(default_factory=list)
    invalid: list[tuple[dict[str, Any], list[str]]] = field(default_factory=list)


Extractor = Callable[[str, ExtractionSchema], list[dict[str, Any]]]


def extract_records(
    extractor: Extractor,
    source_text: str,
    schema: ExtractionSchema,
    provenance: list[SourceRef],
    *,
    extracted_by: str,
) -> ExtractionResult:
    """Run the injected extractor and validate every returned payload.

    The extractor returns raw payload dicts; each is hashed, wrapped with
    provenance, and validated against the schema. Valid payloads become
    ExtractedRecords; invalid ones are kept with their errors."""
    if not provenance:
        raise CatalogError("extraction requires provenance SourceRefs")
    if not str(extracted_by).strip():
        raise CatalogError("extracted_by profile is required")
    payloads = extractor(source_text, schema)
    result = ExtractionResult()
    for i, payload in enumerate(payloads):
        if not isinstance(payload, dict):
            result.invalid.append(({"payload": payload}, ["payload must be a dict"]))
            continue
        rid = str(payload.get("record_id") or f"{schema.record_type}-{i + 1}")
        support = payload.get("factual_support")
        clean = {k: v for k, v in payload.items() if k not in ("record_id", "factual_support")}
        rec = ExtractedRecord(
            record_id=rid,
            record_type=schema.record_type,
            payload=clean,
            source_refs=list(provenance),
            extracted_by=extracted_by,
            factual_support=support if isinstance(support, dict) else None,
        )
        vr = validate_record(rec, schema)
        if vr.ok:
            result.valid.append(rec)
        else:
            result.invalid.append((payload, vr.errors))
    return result


@dataclass
class DraftBundle:
    """Draft-only extraction output. No publish path exists here."""

    entry_id: str
    records: list[ExtractedRecord]
    created_by: str
    created_at: str = ""
    status: str = "draft"

    def __post_init__(self):
        if not self.entry_id.strip() or not self.created_by.strip():
            raise CatalogError("draft bundle requires entry_id and created_by (principal)")
        if not self.created_at:
            self.created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if self.status != "draft":
            raise CatalogError("extraction output is draft-only")

    def to_catalog_bundle(self, *, fixture_ascii_only: bool = False) -> tuple[dict[str, Any], str]:
        """Build a Curator-ready manifest bundle (draft input only).

        Maps records to content_sections, and provenance to source_refs that
        each target exactly one section (the DDL single-target CHECK).
        Returns (bundle, manifest_digest) for Curator.create_draft.
        fixture_ascii_only mirrors Curator's setting for environments
        without the rfc8785 package."""
        if not self.records:
            raise CatalogError("draft bundle has no records")
        combined = hashlib.sha256(
            "".join(sorted(r.content_hash for r in self.records)).encode()
        ).hexdigest()
        sections = [
            {
                "section_id": r.record_id,
                "entry_id": self.entry_id,
                "version": 1,
                "heading": r.record_id,
                "summary": f"{r.record_type} (extracted by {r.extracted_by})",
                "ordinal": i + 1,
            }
            for i, r in enumerate(sorted(self.records, key=lambda r: r.record_id))
        ]
        sources = [
            {
                "source_ref_id": f"{s['section_id']}-src",
                "entry_id": self.entry_id,
                "version": 1,
                "source_type": ref.source_type,
                "source_uri": ref.uri,
                "source_version": ref.source_version,
                "retrieved_at": self.created_at,
                "target_condition_id": None,
                "target_section_id": s["section_id"],
                "target_link_id": None,
            }
            for s in sections
            for ref in next(r for r in self.records if r.record_id == s["section_id"]).source_refs
        ]
        bundle = {
            "entry": {"entry_id": self.entry_id, "entry_type": "knowledge"},
            "version": {
                "entry_id": self.entry_id,
                "version": 1,
                "title": self.entry_id,
                "content_path": f"extraction://{self.entry_id}",
                "content_sha256": combined,
                "created_by": self.created_by,
                "created_at": self.created_at,
                "supersedes_version": None,
                "derived_from_ref": None,
                "derived_from_sha256": None,
            },
            "sections": sections,
            "sources": sources,
        }
        return bundle, manifest(bundle, fixture_ascii_only=fixture_ascii_only)


def summarize_tool(tool_name: str, description: str, input_schema: str) -> dict[str, Any]:
    """A DERIVED tool summary artifact - never a replacement for a contract."""
    payload = {
        "tool": tool_name,
        "what_it_does": description,
        "expected_input": input_schema,
        "limitations": "see published contract",
        "derived": True,
        "replaces_contract": False,
    }
    return {
        "profile": EXTRACTION_PROFILE,
        "artifact_type": "tool_summary",
        "payload": payload,
        "content_hash": hashlib.sha256(_canonical(payload).encode()).hexdigest(),
    }

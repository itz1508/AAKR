"""Canonical manifest serialization using RFC 8785 (JCS) with SHA-256.

Production requires the rfc8785 package. A fixture-only ASCII/integer
serializer is provided for constrained test data only — it rejects Unicode,
floats, and any non-ASCII input. Never use it for production digests.

The manifest payload includes: profile ID, stable entry identity/type,
all immutable version fields except manifest_sha256, sorted tags, groups,
conditions, sections, links, source associations, and open questions.
Excludes: status events, verification events, mutable index state.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

MANIFEST_PROFILE = "oneshot-catalog-manifest-v1"
VERIFICATION_PROFILE = "oneshot-catalog-verification-v1"

HEX = re.compile(r"^[0-9a-f]{64}$")

ALLOWED_TAGS = {
    "domain",
    "task_type",
    "project_type",
    "language",
    "framework",
    "intent",
    "file_signal",
}
ALLOWED_GROUPS = {"all_of", "any_of", "none_of"}
ALLOWED_EVIDENCE = {
    "file_range",
    "call_path",
    "test_assertion",
    "command_output",
    "runtime_artifact",
}


class CatalogError(ValueError):
    pass


def _fixture_subset(data: Any) -> bool:
    """Check if data is ASCII-only for fixture serializer fallback."""
    if data is None or type(data) in (bool, int):
        return True
    if type(data) is str:
        return all(32 <= ord(ch) <= 126 for ch in data)
    if type(data) is list:
        return all(_fixture_subset(x) for x in data)
    if type(data) is dict:
        return all(
            type(k) is str and _fixture_subset(k) and _fixture_subset(v)
            for k, v in data.items()
        )
    return False


def canonical(data: Any, *, fixture_ascii_only: bool = False) -> bytes:
    """Serialize data using RFC 8785 (JCS) canonical form.

    When fixture_ascii_only=True, always uses the fixture-only ASCII/integer
    serializer (rejects Unicode, floats, and non-ASCII input) regardless of
    whether rfc8785 is installed. This ensures deterministic test digests.

    When fixture_ascii_only=False, uses rfc8785 (raises CatalogError if not installed).
    """
    if fixture_ascii_only:
        if not _fixture_subset(data):
            raise CatalogError(
                "fixture-only serializer rejects non-ASCII/float input; "
                "use fixture_ascii_only=False with rfc8785 for production"
            )
        return json.dumps(
            data, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    try:
        import rfc8785
    except ImportError as exc:
        raise CatalogError(
            "RFC8785 package required for production canonicalization"
        ) from exc
    return rfc8785.dumps(data)


def sha(data: Any, *, fixture_ascii_only: bool = False) -> str:
    """Compute SHA-256 hex digest of canonicalized data."""
    return hashlib.sha256(
        canonical(data, fixture_ascii_only=fixture_ascii_only)
    ).hexdigest()


MANIFEST_FIELDS = {
    "entry": ("entry_id", "entry_type"),
    "version": (
        "entry_id",
        "version",
        "title",
        "content_path",
        "content_sha256",
        "created_by",
        "created_at",
        "supersedes_version",
        "derived_from_ref",
        "derived_from_sha256",
    ),
    "tags": ("tag_id", "entry_id", "version", "tag_type", "value"),
    "groups": ("group_id", "entry_id", "version", "group_mode"),
    "conditions": (
        "condition_id",
        "entry_id",
        "version",
        "group_id",
        "signal",
        "operator",
        "value",
        "required_evidence",
    ),
    "sections": ("section_id", "entry_id", "version", "heading", "summary"),
    "links": (
        "link_id",
        "from_entry_id",
        "from_version",
        "to_entry_id",
        "to_version",
        "link_type",
    ),
    "sources": (
        "source_ref_id",
        "entry_id",
        "version",
        "source_type",
        "source_uri",
        "source_version",
        "retrieved_at",
        "target_condition_id",
        "target_section_id",
        "target_link_id",
    ),
    "questions": ("question_id", "entry_id", "version", "question", "evidence_needed"),
}

COLLECTIONS = {
    "tags": ("match_tags", "tag_id"),
    "groups": ("applicability_groups", "group_id"),
    "conditions": ("applicability_conditions", "condition_id"),
    "sections": ("content_sections", "section_id"),
    "links": ("entry_links", "link_id"),
    "sources": ("source_refs", "source_ref_id"),
    "questions": ("open_questions", "question_id"),
}


def normalize_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """Normalize a manifest bundle: validate fields, sort children by stable ID.

    Returns a dict with 'profile', 'entry', 'version', and all child collections
    sorted by their stable identifier. Excludes manifest_sha256, status events,
    verification events, and derived index state.
    """
    if "entry" not in bundle or "version" not in bundle:
        raise CatalogError("entry and version are required")
    result: dict[str, Any] = {"profile": MANIFEST_PROFILE}

    for key in ("entry", "version"):
        values = bundle[key]
        if not isinstance(values, dict) or set(values) - set(MANIFEST_FIELDS[key]):
            raise CatalogError(f"unknown or malformed {key} fields")
        result[key] = {col: values.get(col) for col in MANIFEST_FIELDS[key]}

    for name, (_, identity) in COLLECTIONS.items():
        records = bundle.get(name, [])
        if not isinstance(records, list):
            raise CatalogError(f"{name} must be an array")
        if any(
            not isinstance(rec, dict) or set(rec) - set(MANIFEST_FIELDS[name])
            for rec in records
        ):
            raise CatalogError(f"malformed {name} row")
        normalized = [
            {col: rec.get(col) for col in MANIFEST_FIELDS[name]} for rec in records
        ]
        identities = [str(rec[identity]) for rec in normalized]
        if any(rec[identity] is None for rec in normalized) or len(identities) != len(
            set(identities)
        ):
            raise CatalogError(f"duplicate or null {name} identifiers")
        result[name] = sorted(normalized, key=lambda r: str(r[identity]))
    return result


def manifest(bundle: dict[str, Any], *, fixture_ascii_only: bool = False) -> str:
    """Compute the manifest SHA-256 from a bundle dict."""
    return sha(normalize_bundle(bundle), fixture_ascii_only=fixture_ascii_only)


def validate_content_hash(content: bytes | str, expected_sha256: str) -> bool:
    """Verify explicit content SHA-256 hash against expected digest.

    Accepts bytes or str (encoded as UTF-8). Does NOT access the filesystem.
    Returns True only if the computed SHA-256 matches expected_sha256.
    Returns False for invalid hash format or hash mismatch.
    """
    if not HEX.fullmatch(expected_sha256):
        return False
    if isinstance(content, str):
        content = content.encode()
    return hashlib.sha256(content).hexdigest() == expected_sha256


def validate_file_hash(
    file_path: str,
    expected_sha256: str,
    *,
    base_dir: str | None = None,
) -> bool:
    """Verify file content SHA-256 hash against expected digest.

    Reads the file at file_path and compares its SHA-256 to expected_sha256.
    Security: rejects path traversal (../), symlinks, and files outside
    base_dir when provided. Returns False for missing files, invalid
    paths, symlink targets, or hash mismatch. Does NOT fall back to
    hashing the path string — missing files are not content.
    """
    if not HEX.fullmatch(expected_sha256):
        return False
    import os

    if ".." in os.path.normpath(file_path).split(os.sep):
        return False

    if base_dir is not None:
        base_real = os.path.realpath(base_dir)
        target_real = os.path.realpath(file_path)
        if not target_real.startswith(base_real + os.sep) and target_real != base_real:
            return False

    if os.path.islink(file_path):
        return False

    if not os.path.isfile(file_path):
        return False

    with open(file_path, "rb") as f:
        actual = hashlib.sha256(f.read()).hexdigest()
    return actual == expected_sha256


def verification_digest(
    rows: list[dict[str, Any]], *, fixture_ascii_only: bool = False
) -> str:
    """Compute verification snapshot digest over exact rows in deterministic order."""
    return sha(
        {"profile": VERIFICATION_PROFILE, "rows": rows},
        fixture_ascii_only=fixture_ascii_only,
    )

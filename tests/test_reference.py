"""Fixture-only isolated Curator + PRE reference tests. NOT Databricks E2E.

Adapted from test_oneshot_catalog_v2_reference.py (42 tests) to use
modular source imports. Uses fixture_ascii_only=True since rfc8785
package is not installed in this environment.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# Add project root to Python path for aakr package imports
_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))

from aakr.catalog.applicability import evaluate_condition, evaluate_group
from aakr.catalog.models import CatalogError, canonical, manifest
from aakr.curator import Curator
from aakr.integrations.sqlite.adapter import SQLiteStore

H = "c" * 64
H2 = "b" * 64


def bundle(
    eid="E",
    version=1,
    *,
    domains=("software", "data"),
    intent="auto-discovery",
    supersedes=None,
    derived=False,
    links=(),
    conditions=True,
    sources=True,
):
    return {
        "entry": {"entry_id": eid, "entry_type": "template"},
        "version": {
            "entry_id": eid,
            "version": version,
            "title": "Auto Discovery",
            "content_path": f"blob://{eid}/{version}",
            "content_sha256": H,
            "created_by": "author",
            "created_at": "2026-11-06T01:00:00Z",
            "supersedes_version": supersedes,
            "derived_from_ref": "validated/task-1" if derived else None,
            "derived_from_sha256": H2 if derived else None,
        },
        "tags": [
            {
                "tag_id": f"{eid}-domain-{k}-{version}",
                "entry_id": eid,
                "version": version,
                "tag_type": "domain",
                "value": k,
            }
            for k in domains
        ]
        + [
            {
                "tag_id": f"{eid}-intent-{version}",
                "entry_id": eid,
                "version": version,
                "tag_type": "intent",
                "value": intent,
            }
        ],
        "groups": [
            {
                "group_id": f"{eid}-g-{version}",
                "entry_id": eid,
                "version": version,
                "group_mode": "all_of",
            }
        ]
        if conditions
        else [],
        "conditions": [
            {
                "condition_id": f"{eid}-c-{version}",
                "entry_id": eid,
                "version": version,
                "group_id": f"{eid}-g-{version}",
                "signal": "example.language",
                "operator": "eq",
                "value": "python",
                "required_evidence": "file_range",
            }
        ]
        if conditions
        else [],
        "sections": [
            {
                "section_id": f"{eid}-s-{version}",
                "entry_id": eid,
                "version": version,
                "heading": "Overview",
                "summary": "Reusable across topics",
            }
        ],
        "links": [
            {
                "link_id": f"{eid}-l-{i}-{version}",
                "from_entry_id": eid,
                "from_version": version,
                "to_entry_id": to,
                "to_version": tov,
                "link_type": kind,
            }
            for i, (to, tov, kind) in enumerate(links)
        ],
        "sources": [
            {
                "source_ref_id": f"{eid}-r-{version}",
                "entry_id": eid,
                "version": version,
                "source_type": "documentation",
                "source_uri": "https://example.org/doc",
                "source_version": "1",
                "retrieved_at": "2026-11-06T01:00:00Z",
                "target_condition_id": f"{eid}-c-{version}",
                "target_section_id": None,
                "target_link_id": None,
            }
        ]
        if sources and conditions
        else [],
        "questions": [
            {
                "question_id": f"{eid}-q-{version}",
                "entry_id": eid,
                "version": version,
                "question": "Which topics?",
                "evidence_needed": "Research evidence",
            }
        ],
    }


def connect(path, *, initialize=False, readonly=False):
    return SQLiteStore(path, initialize=initialize, readonly=readonly)


def get(c, eid, version, *, fixture_ascii_only=False):
    return c.get(eid, version, fixture_ascii_only=fixture_ascii_only)


def lookup(
    c, *, domains, intent, per_domain_limit=3, global_limit=12, fixture_ascii_only=False
):
    return c.lookup(
        domains=domains,
        intent=intent,
        per_domain_limit=per_domain_limit,
        global_limit=global_limit,
        fixture_ascii_only=fixture_ascii_only,
    )


def provenance(c, eid, version, *, fixture_ascii_only=False):
    return c.provenance(eid, version, fixture_ascii_only=fixture_ascii_only)


def persisted_bundle(c, eid, version):
    return c.persisted_bundle(eid, version)


def state_bundle(c, eid, version):
    return c.state_bundle(eid, version)


class CuratorReference(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "catalog.db")
        self.c = connect(self.db, initialize=True)
        self.cur = Curator(self.c, fixture_ascii_only=True)
        self.b = bundle()
        self.digest = manifest(self.b, fixture_ascii_only=True)

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def draft(self, b=None, cur=None):
        b = b or self.b
        return (cur or self.cur).create_draft(b, manifest(b, fixture_ascii_only=True))

    def test_01_manifest_matches_persisted_rows(self):
        self.draft()
        self.assertEqual(
            manifest(persisted_bundle(self.c, "E", 1), fixture_ascii_only=True),
            self.digest,
        )

    def test_02_manifest_independent_of_child_input_order(self):
        b = bundle()
        b["tags"].reverse()
        self.assertEqual(manifest(b, fixture_ascii_only=True), self.digest)

    def test_03_manifest_includes_origin_and_questions(self):
        b = bundle()
        b["questions"][0]["evidence_needed"] = "Other"
        self.assertNotEqual(manifest(b, fixture_ascii_only=True), self.digest)
        b = bundle(derived=True)
        self.assertNotEqual(manifest(b, fixture_ascii_only=True), self.digest)

    def test_04_mismatched_precommit_rejected_without_side_effects(self):
        with self.assertRaises(CatalogError):
            self.cur.create_draft(self.b, H)
        self.assertEqual(
            self.c.c.execute("select count(*) from entries").fetchone()[0], 0
        )

    def test_05_partial_write_injection_rolls_back(self):
        for stage in (
            "after_parent",
            "after_tags",
            "after_groups",
            "after_conditions",
            "after_sections",
            "after_links",
            "after_sources",
            "after_questions",
        ):
            with self.subTest(stage=stage):
                with self.assertRaises(CatalogError):
                    self.cur.create_draft(self.b, self.digest, fault_at=stage)
                self.assertEqual(
                    self.c.c.execute("select count(*) from entry_versions").fetchone()[
                        0
                    ],
                    0,
                )

    def test_06_stable_retry_is_idempotent(self):
        self.draft()
        self.draft()
        self.assertEqual(
            self.c.c.execute("select count(*) from entry_versions").fetchone()[0], 1
        )
        self.assertEqual(
            self.c.c.execute("select count(*) from version_status").fetchone()[0], 1
        )

    def test_07_conflicting_retry_rejected(self):
        self.draft()
        bad = bundle()
        bad["questions"][0]["question"] = "new"
        with self.assertRaises(CatalogError):
            self.draft(bad)

    def test_08_unauthenticated_learning_proposal_rejected(self):
        with self.assertRaises(CatalogError):
            self.draft(bundle(derived=True))
        self.assertEqual(
            self.c.c.execute("select count(*) from entries").fetchone()[0], 0
        )

    def test_09_authenticated_learning_callback_accepts(self):
        b = bundle(derived=True)
        approved = Curator(
            self.c,
            fixture_ascii_only=True,
            validation_receipt=lambda ref, digest: (
                (ref, digest) == ("validated/task-1", H2)
            ),
        )
        self.draft(b, approved)
        self.assertEqual(
            self.c.c.execute("select derived_from_ref from entry_versions").fetchone()[
                0
            ],
            "validated/task-1",
        )

    def test_10_publish_and_get(self):
        self.draft()
        self.assertIsNone(get(self.c, "E", 1, fixture_ascii_only=True))
        self.cur.publish("E", 1)
        self.assertEqual(get(self.c, "E", 1, fixture_ascii_only=True)["version"], 1)

    def test_11_publish_rejects_invalid_hash(self):
        self.c.c.execute("INSERT INTO entries VALUES (?,?,?)", ("E", "template", "now"))
        self.c.c.execute(
            "INSERT INTO entry_versions (entry_id,version,title,content_path,content_sha256,manifest_sha256,created_by,created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("E", 1, "Incorrect", "blob://E/1", H, H2, "author", "now"),
        )
        self.c.c.execute(
            "INSERT INTO version_status VALUES (?,?,?,?,?,?)",
            ("E", 1, 1, "draft", "author", "now"),
        )
        with self.assertRaisesRegex(CatalogError, "manifest checksum mismatch"):
            self.cur.publish("E", 1)
        self.assertEqual(
            self.c.c.execute("SELECT status FROM version_current_status").fetchone()[0],
            "draft",
        )

    def test_12_link_to_draft_rejected(self):
        self.draft(bundle("X", 1, conditions=False, sources=False))
        b = bundle(links=[("X", 1, "depends_on")])
        self.draft(b)
        with self.assertRaisesRegex(CatalogError, "link target"):
            self.cur.publish("E", 1)
        self.assertEqual(
            self.c.c.execute(
                'select status from version_current_status where entry_id="E"'
            ).fetchone()[0],
            "draft",
        )

    def test_13_link_to_published_accepted(self):
        bx = bundle("X", 1, conditions=False, sources=False)
        self.draft(bx)
        self.cur.publish("X", 1)
        self.draft(bundle(links=[("X", 1, "depends_on")]))
        self.cur.publish("E", 1)
        self.assertEqual(
            self.c.c.execute("select count(*) from published_versions").fetchone()[0], 2
        )

    def test_14_publish_and_supersede_atomic(self):
        self.draft()
        self.cur.publish("E", 1)
        b = bundle(version=2, supersedes=1)
        self.draft(b)
        self.cur.publish("E", 2)
        self.assertEqual(
            self.c.c.execute(
                'select version from published_versions where entry_id="E"'
            ).fetchone()[0],
            2,
        )
        old = self.c.c.execute(
            'select status from version_current_status where entry_id="E" and version=1'
        ).fetchone()[0]
        self.assertEqual(old, "deprecated")
        self.assertIsNotNone(get(self.c, "E", 1, fixture_ascii_only=True))

    def test_15_publish_failure_after_status_append_rolls_back(self):
        self.draft()
        with self.assertRaises(CatalogError):
            self.cur.publish("E", 1, fault_at="after_publish")
        self.assertEqual(
            self.c.c.execute("select status from version_current_status").fetchone()[0],
            "draft",
        )

    def test_16_publish_failure_after_supersede_rolls_back(self):
        self.draft()
        self.cur.publish("E", 1)
        self.draft(bundle(version=2, supersedes=1))
        with self.assertRaises(CatalogError):
            self.cur.publish("E", 2, fault_at="after_deprecate")
        self.assertEqual(
            self.c.c.execute(
                "select status from version_current_status where version=2"
            ).fetchone()[0],
            "draft",
        )
        self.assertEqual(
            self.c.c.execute(
                "select status from version_current_status where version=1"
            ).fetchone()[0],
            "published",
        )

    def test_17_publish_retry_idempotent(self):
        self.draft()
        self.cur.publish("E", 1)
        self.cur.publish("E", 1)
        self.assertEqual(
            self.c.c.execute(
                'select count(*) from version_status where entry_id="E"'
            ).fetchone()[0],
            2,
        )

    def test_18_latest_current_does_not_resurrect(self):
        self.draft()
        self.cur.publish("E", 1)
        self.draft(bundle(version=2, supersedes=1))
        self.cur.publish("E", 2)
        self.cur.deprecate("E", 2)
        self.assertEqual(
            self.c.c.execute("select count(*) from published_versions").fetchone()[0], 0
        )

    def test_19_deprecated_draft_not_pinnable(self):
        self.draft()
        self.cur.deprecate("E", 1)
        self.assertIsNone(get(self.c, "E", 1, fixture_ascii_only=True))

    def test_20_provenance_verification_changes_independent_of_manifest(self):
        self.draft()
        self.cur.publish("E", 1)
        a = provenance(self.c, "E", 1, fixture_ascii_only=True)
        self.assertEqual(a["by_result"]["unverified"], 1)
        self.cur.verify_source(
            "E-r-1", "verified", "reviewer", H2, excerpt="Source supports claim"
        )
        b = provenance(self.c, "E", 1, fixture_ascii_only=True)
        self.assertEqual(b["by_result"]["verified"], 1)
        self.assertNotEqual(
            a["verification_snapshot_sha256"], b["verification_snapshot_sha256"]
        )
        self.assertEqual(
            self.c.c.execute("select manifest_sha256 from entry_versions").fetchone()[
                0
            ],
            self.digest,
        )

    def test_21_reverify_uses_latest_event(self):
        self.draft()
        self.cur.verify_source("E-r-1", "not_verified", "reviewer", H2)
        self.cur.verify_source("E-r-1", "contradicted", "reviewer", H2)
        p = provenance(self.c, "E", 1, fixture_ascii_only=True)
        self.assertEqual(p["by_result"]["contradicted"], 1)
        self.assertEqual(p["verification_state"][0]["seq"], 2)

    def test_22_lookup_cross_domain_dedup(self):
        self.draft()
        self.cur.publish("E", 1)
        result = lookup(
            self.c,
            domains=["data", "software"],
            intent="auto-discovery",
            fixture_ascii_only=True,
        )
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["matched_domains"], ["data", "software"])
        self.assertEqual(
            result["matches"][0]["ranking"]["meaning"], "retrieval_relevance_only"
        )

    def test_23_lookup_per_domain_quota_and_truncation(self):
        for id_ in ("A", "B", "C"):
            self.draft(bundle(id_, 1, domains=("software",), sources=False))
            self.cur.publish(id_, 1)
        r = lookup(
            self.c,
            domains=["software"],
            intent="auto-discovery",
            per_domain_limit=2,
            fixture_ascii_only=True,
        )
        self.assertEqual(len(r["matches"]), 2)
        self.assertTrue(r["coverage"]["truncated"])
        self.assertEqual([x["entry_id"] for x in r["matches"]], ["A", "B"])

    def test_24_no_match_cannot_mean_no_knowledge(self):
        self.draft()
        self.cur.publish("E", 1)
        r = lookup(
            self.c,
            domains=["hardware"],
            intent="auto-discovery",
            fixture_ascii_only=True,
        )
        self.assertEqual(r["matches"], [])
        self.assertEqual(r["coverage"]["domains_searched"], ["hardware"])

    def test_25_no_fake_derived_index_digest(self):
        self.draft()
        self.cur.publish("E", 1)
        r = lookup(
            self.c,
            domains=["software"],
            intent="auto-discovery",
            fixture_ascii_only=True,
        )
        self.assertIsNone(r["snapshot"]["index_sha256"])
        self.assertEqual(r["snapshot"]["index_status"], "unavailable")

    def test_26_readonly_connection_rejects_writes(self):
        self.draft()
        b = connect(self.db, readonly=True)
        try:
            self.assertEqual(b.c.execute("pragma query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                b.c.execute('insert into entries values ("U","pattern","now")')
        finally:
            b.close()

    def test_27_two_writers_transaction_conflict(self):
        b = connect(self.db)
        try:
            self.c.c.execute("BEGIN IMMEDIATE")
            b.c.execute("pragma busy_timeout=50")
            with self.assertRaises(sqlite3.OperationalError):
                b.c.execute("BEGIN IMMEDIATE")
            self.c.c.execute("ROLLBACK")
            b.c.execute("BEGIN IMMEDIATE")
            b.c.execute("ROLLBACK")
        finally:
            b.close()

    def test_28_snapshot_stability_under_verification_append(self):
        self.c.c.execute("pragma journal_mode=WAL")
        self.draft()
        reader = connect(self.db, readonly=True)
        try:
            reader.c.execute("BEGIN")
            before = provenance(reader, "E", 1, fixture_ascii_only=True)[
                "verification_snapshot_sha256"
            ]
            self.cur.verify_source(
                "E-r-1", "verified", "reviewer", H2, excerpt="verified"
            )
            self.assertEqual(
                provenance(reader, "E", 1, fixture_ascii_only=True)[
                    "verification_snapshot_sha256"
                ],
                before,
            )
            reader.c.execute("COMMIT")
            self.assertNotEqual(
                provenance(reader, "E", 1, fixture_ascii_only=True)[
                    "verification_snapshot_sha256"
                ],
                before,
            )
        finally:
            reader.close()

    def test_29_reject_unknown_manifest_field(self):
        b = bundle()
        b["version"]["new_undocumented_field"] = "X"
        with self.assertRaises(CatalogError):
            manifest(b, fixture_ascii_only=True)

    def test_30_no_unreviewed_empty_group(self):
        b = bundle()
        b["conditions"] = []
        with self.assertRaises(CatalogError):
            self.draft(b)

    def test_31_unsupported_unicode_fixture_fails_closed(self):
        with self.assertRaises(CatalogError):
            canonical({"key": "unicode caf\u00e9"}, fixture_ascii_only=True)

    def test_32_unsupported_float_fixture_fails_closed(self):
        with self.assertRaises(CatalogError):
            canonical({"value": 0.3}, fixture_ascii_only=True)


class PREThreeValued(unittest.TestCase):
    def test_33_all_of_false_wins(self):
        self.assertEqual(
            evaluate_group(
                "all_of", ["satisfied", "evidence_unavailable", "not_satisfied"]
            ),
            "not_satisfied",
        )

    def test_34_any_of_true_wins(self):
        self.assertEqual(
            evaluate_group(
                "any_of", ["not_satisfied", "evidence_unavailable", "satisfied"]
            ),
            "satisfied",
        )

    def test_35_none_of_true_is_negative(self):
        self.assertEqual(
            evaluate_group("none_of", ["not_satisfied", "satisfied"]), "not_satisfied"
        )

    def test_36_none_of_unknown_propagates(self):
        self.assertEqual(
            evaluate_group("none_of", ["not_satisfied", "evidence_unavailable"]),
            "evidence_unavailable",
        )

    def test_37_absent_no_evidence_unknown(self):
        self.assertEqual(
            evaluate_condition("absent", None, None, evidence_kind="file_range"),
            "evidence_unavailable",
        )

    def test_38_absent_requires_complete_scoped_evidence(self):
        self.assertEqual(
            evaluate_condition(
                "absent",
                None,
                {
                    "present": False,
                    "evidence_kind": "file_range",
                    "evidence_ref": "search:1",
                    "complete": False,
                },
                evidence_kind="file_range",
            ),
            "evidence_unavailable",
        )

    def test_39_absent_complete_scoped_evidence_satisfies(self):
        self.assertEqual(
            evaluate_condition(
                "absent",
                None,
                {
                    "present": False,
                    "evidence_kind": "file_range",
                    "evidence_ref": "search:1",
                    "complete": True,
                },
                evidence_kind="file_range",
            ),
            "satisfied",
        )

    def test_40_missing_evidence_does_not_false_match(self):
        self.assertEqual(
            evaluate_condition(
                "eq",
                "python",
                {"present": True, "value": "python", "evidence_kind": "other"},
                evidence_kind="file_range",
            ),
            "evidence_unavailable",
        )

    def test_41_invalid_vocabulary_unknown(self):
        self.assertEqual(
            evaluate_condition("fuzzy", "python", {}, evidence_kind="file_range"),
            "evidence_unavailable",
        )

    def test_42_empty_group_forbidden(self):
        with self.assertRaises(CatalogError):
            evaluate_group("all_of", [])


if __name__ == "__main__":
    unittest.main()

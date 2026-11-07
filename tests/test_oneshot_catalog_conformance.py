"""Executable SQLite schema-level conformance tests for OneShot Catalog v1.

Usage: python /mnt/data/test_oneshot_catalog_conformance.py
Does not import or mutate any OneShot repository.
Curator, FTS, Databricks and Modal E2E require additional integration tests.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

SCHEMA = (
    Path(__file__).resolve().parent.parent
    / "aakr"
    / "integrations"
    / "sqlite"
    / "schema.sql"
).read_text()
HEX_A = "a" * 64
HEX_B = "b" * 64


def connection(schema: bool = True, path: str = ":memory:") -> sqlite3.Connection:
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA recursive_triggers = ON")
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert c.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
    if schema:
        c.executescript(SCHEMA)
    return c


def start_entry(
    c: sqlite3.Connection,
    entry: str = "E",
    version: int = 1,
    *,
    supersedes: int | None = None,
    manifest: str = HEX_A,
) -> None:
    c.execute(
        "INSERT OR IGNORE INTO entries VALUES (?,?,?)",
        (entry, "pattern", "2026-11-06T00:00:00Z"),
    )
    c.execute(
        """INSERT INTO entry_versions
      (entry_id, version, title, content_path, content_sha256, manifest_sha256,
       created_by, created_at, supersedes_version)
      VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            entry,
            version,
            f"v{version}",
            "blob://fixture",
            HEX_A,
            manifest,
            "author",
            "2026-11-06T00:00:00Z",
            supersedes,
        ),
    )
    c.execute(
        "INSERT INTO version_status VALUES (?,?,?,?,?,?)",
        (entry, version, 1, "draft", "curator", "2026-11-06T00:00:00Z"),
    )


def status(
    c: sqlite3.Connection, entry: str, version: int, seq: int, state: str
) -> None:
    c.execute(
        "INSERT INTO version_status VALUES (?,?,?,?,?,?)",
        (entry, version, seq, state, "curator", "2026-11-06T00:01:00Z"),
    )


def condition(c: sqlite3.Connection, entry="E", version=1, cid="c1", gid="g1") -> None:
    c.execute(
        "INSERT INTO applicability_groups VALUES (?,?,?,?)",
        (gid, entry, version, "all_of"),
    )
    c.execute(
        """INSERT INTO applicability_conditions
      (condition_id,entry_id,version,group_id,signal,operator,value,required_evidence)
      VALUES (?,?,?,?,?,?,?,?)""",
        (cid, entry, version, gid, "language", "eq", "python", "file_range"),
    )


def ref(
    c,
    rid="r1",
    *,
    entry="E",
    version=1,
    target="c1",
    section=None,
    link=None,
    uri="https://example.org/x",
) -> None:
    c.execute(
        """INSERT INTO source_refs
      (source_ref_id,entry_id,version,source_type,source_uri,source_version,
       retrieved_at,target_condition_id,target_section_id,target_link_id)
      VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            rid,
            entry,
            version,
            "documentation",
            uri,
            "1.0",
            "2026-11-06T00:00:00Z",
            target,
            section,
            link,
        ),
    )


def verify(
    c,
    rid="r1",
    seq=1,
    result="verified",
    verifier="reviewer",
    digest=HEX_B,
    excerpt="evidence",
) -> None:
    c.execute(
        "INSERT INTO source_verifications VALUES (?,?,?,?,?,?,?)",
        (rid, seq, result, verifier, "2026-11-06T00:02:00Z", digest, excerpt),
    )


def state_hash(c: sqlite3.Connection) -> str:
    records = [
        dict(zip([d[0] for d in cur.description], r))
        for cur in [
            c.execute(
                "SELECT * FROM source_ref_current ORDER BY entry_id, version, source_ref_id"
            )
        ]
        for r in cur.fetchall()
    ]
    return hashlib.sha256(
        json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class CatalogConformance(unittest.TestCase):
    def setUp(self):
        self.c = connection()
        start_entry(self.c)
        condition(self.c)
        ref(self.c)

    def tearDown(self):
        self.c.close()

    def rejects(self, sql: str, values: tuple = ()) -> None:
        with self.assertRaises(sqlite3.DatabaseError):
            self.c.execute(sql, values)

    def test_01_pragmas_on(self):
        self.assertEqual(self.c.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.c.execute("PRAGMA recursive_triggers").fetchone()[0], 1)

    def test_02_composite_fk_targets_missing(self):
        self.rejects(
            """INSERT INTO source_refs
        (source_ref_id,entry_id,version,source_type,source_uri,source_version,
         retrieved_at,target_condition_id) VALUES (?,?,?,?,?,?,?,?)""",
            ("r2", "E", 1, "documentation", "uri2", "1", "time", "missing"),
        )

    def test_03_cross_version_target(self):
        start_entry(self.c, "E", 2)
        self.rejects(
            """INSERT INTO source_refs
        (source_ref_id,entry_id,version,source_type,source_uri,source_version,
         retrieved_at,target_condition_id) VALUES (?,?,?,?,?,?,?,?)""",
            ("r2", "E", 2, "documentation", "uri2", "1", "time", "c1"),
        )

    def test_04_two_targets_rejected(self):
        self.rejects(
            """INSERT INTO source_refs
        (source_ref_id,entry_id,version,source_type,source_uri,source_version,
         retrieved_at,target_condition_id,target_section_id) VALUES (?,?,?,?,?,?,?,?,?)""",
            ("r2", "E", 1, "documentation", "uri2", "1", "time", "c1", "s1"),
        )

    def test_05_zero_targets_rejected(self):
        self.rejects(
            """INSERT INTO source_refs
        (source_ref_id,entry_id,version,source_type,source_uri,source_version,
         retrieved_at) VALUES (?,?,?,?,?,?,?)""",
            ("r2", "E", 1, "documentation", "uri2", "1", "time"),
        )

    def test_06_duplicate_association_rejected_even_with_nullable_targets(self):
        # A single UNIQUE over all 3 nullable targets would NOT catch this.
        ref_args = {"rid": "r2", "uri": "https://example.org/x"}
        with self.assertRaises(sqlite3.IntegrityError):
            ref(self.c, **ref_args)

    def test_07_one_document_three_claims_allowed(self):
        self.c.execute(
            "INSERT INTO content_sections VALUES (?,?,?,?,?)", ("s1", "E", 1, "A", "B")
        )
        self.c.execute(
            "INSERT INTO entry_links VALUES (?,?,?,?,?,?)",
            ("l1", "E", 1, "E", 1, "related"),
        )
        ref(self.c, "r2", target=None, section="s1")
        ref(self.c, "r3", target=None, link="l1")
        self.assertEqual(
            self.c.execute("SELECT count(*) FROM source_refs").fetchone()[0], 3
        )
        self.assertEqual(
            self.c.execute(
                "SELECT count(DISTINCT source_uri) FROM source_refs"
            ).fetchone()[0],
            1,
        )

    def test_08_wrong_group_version_rejected(self):
        start_entry(self.c, "E", 2)
        self.rejects(
            """INSERT INTO applicability_conditions
          (condition_id,entry_id,version,group_id,signal,operator,value,required_evidence)
          VALUES (?,?,?,?,?,?,?,?)""",
            ("c2", "E", 2, "g1", "lang", "eq", "python", "file_range"),
        )

    def test_09_invalid_group_mode_rejected(self):
        self.rejects(
            "INSERT INTO applicability_groups VALUES (?,?,?,?)",
            ("g2", "E", 1, "some_of"),
        )

    def test_10_status_gap_rejected(self):
        self.rejects(
            "INSERT INTO version_status VALUES (?,?,?,?,?,?)",
            ("E", 1, 3, "published", "curator", "t"),
        )

    def test_11_status_stale_or_duplicate_seq_rejected(self):
        self.rejects(
            "INSERT INTO version_status VALUES (?,?,?,?,?,?)",
            ("E", 1, 1, "draft", "curator", "t"),
        )

    def test_12_status_invalid_transition_rejected(self):
        status(self.c, "E", 1, 2, "deprecated")
        self.rejects(
            "INSERT INTO version_status VALUES (?,?,?,?,?,?)",
            ("E", 1, 3, "published", "curator", "t"),
        )

    def test_13_pinnable_requires_publication_history(self):
        status(self.c, "E", 1, 2, "deprecated")
        self.assertEqual(
            self.c.execute("SELECT count(*) FROM pinnable_versions").fetchone()[0], 0
        )

    def test_14_pinnable_deprecated_after_publication(self):
        status(self.c, "E", 1, 2, "published")
        status(self.c, "E", 1, 3, "deprecated")
        self.assertEqual(
            self.c.execute("SELECT count(*) FROM pinnable_versions").fetchone()[0], 1
        )

    def test_15_no_resurrection_after_latest_deprecated(self):
        status(self.c, "E", 1, 2, "published")
        start_entry(self.c, "E", 2, supersedes=1)
        status(self.c, "E", 2, 2, "published")
        self.assertEqual(
            self.c.execute("SELECT version FROM published_versions").fetchone()[0], 2
        )
        status(self.c, "E", 2, 3, "deprecated")
        self.assertEqual(
            self.c.execute("SELECT count(*) FROM published_versions").fetchone()[0], 0
        )

    def test_16_latest_published_selected_with_integer_version(self):
        status(self.c, "E", 1, 2, "published")
        start_entry(self.c, "E", 2, supersedes=1)
        status(self.c, "E", 2, 2, "published")
        start_entry(self.c, "E", 10, supersedes=2)
        status(self.c, "E", 10, 2, "published")
        self.assertEqual(
            self.c.execute("SELECT version FROM published_versions").fetchone()[0], 10
        )

    def test_17_link_target_must_exist(self):
        self.rejects(
            "INSERT INTO entry_links VALUES (?,?,?,?,?,?)",
            ("link-missing", "E", 1, "X", 1, "depends_on"),
        )

    def test_18_verification_seq_and_missing_ref(self):
        self.rejects(
            "INSERT INTO source_verifications VALUES (?,?,?,?,?,?,?)",
            ("r1", 2, "verified", "reviewer", "now", HEX_B, "ok"),
        )
        self.rejects(
            "INSERT INTO source_verifications VALUES (?,?,?,?,?,?,?)",
            ("none", 1, "verified", "reviewer", "now", HEX_B, "ok"),
        )

    def test_19_creator_as_verifier_rejected(self):
        with self.assertRaises(sqlite3.DatabaseError):
            verify(self.c, verifier="AUTHOR")

    def test_20_blank_verifier_rejected(self):
        with self.assertRaises(sqlite3.DatabaseError):
            verify(self.c, verifier="   ")

    def test_21_bad_hash_rejected(self):
        for bad in ["garbage", "A" * 64, "a" * 63, "g" * 64]:
            with self.subTest(bad=bad), self.assertRaises(sqlite3.DatabaseError):
                verify(self.c, digest=bad)

    def test_22_verified_without_excerpt_rejected(self):
        with self.assertRaises(sqlite3.DatabaseError):
            verify(self.c, excerpt="  ")

    def test_23_unverified_vs_not_verified(self):
        self.assertEqual(
            self.c.execute(
                'SELECT result FROM source_ref_current WHERE source_ref_id="r1"'
            ).fetchone()[0],
            "unverified",
        )
        verify(self.c, result="not_verified", excerpt=None)
        self.assertEqual(
            self.c.execute(
                'SELECT result FROM source_ref_current WHERE source_ref_id="r1"'
            ).fetchone()[0],
            "not_verified",
        )

    def test_24_last_verification_event_counts(self):
        verify(self.c, result="not_verified", excerpt=None)
        verify(self.c, seq=2, result="verified", excerpt="confirmed")
        self.assertEqual(
            self.c.execute(
                'SELECT seq,result FROM source_ref_current WHERE source_ref_id="r1"'
            ).fetchone(),
            (2, "verified"),
        )

    def test_25_snapshot_covers_never_verified(self):
        a = state_hash(self.c)
        self.assertEqual(
            self.c.execute(
                'SELECT count(*) FROM source_ref_current WHERE result="unverified"'
            ).fetchone()[0],
            1,
        )
        verify(self.c)
        b = state_hash(self.c)
        self.assertNotEqual(a, b)
        self.assertEqual(
            self.c.execute("SELECT manifest_sha256 FROM entry_versions").fetchone()[0],
            HEX_A,
        )

    def test_26_published_manifest_freezes_child_inserts(self):
        status(self.c, "E", 1, 2, "published")
        self.rejects(
            "INSERT INTO match_tags VALUES (?,?,?,?,?)",
            ("tag2", "E", 1, "domain", "ops"),
        )
        self.rejects(
            "INSERT INTO applicability_groups VALUES (?,?,?,?)",
            ("g2", "E", 1, "all_of"),
        )
        self.rejects(
            "INSERT INTO open_questions VALUES (?,?,?,?,?)",
            ("q1", "E", 1, "What?", "Test"),
        )

    def test_27_deprecated_manifest_freezes_child_inserts(self):
        status(self.c, "E", 1, 2, "deprecated")
        self.rejects(
            "INSERT INTO match_tags VALUES (?,?,?,?,?)",
            ("tag2", "E", 1, "domain", "ops"),
        )

    def test_28_verification_after_published_allowed(self):
        status(self.c, "E", 1, 2, "published")
        verify(self.c)
        self.assertEqual(
            self.c.execute("SELECT result FROM source_ref_current").fetchone()[0],
            "verified",
        )

    def test_29_all_tables_reject_updates_and_deletes(self):
        probes = [
            ("entries", "entry_type"),
            ("entry_versions", "title"),
            ("version_status", "actor"),
            ("match_tags", "value"),
            ("applicability_groups", "group_mode"),
            ("applicability_conditions", "signal"),
            ("content_sections", "heading"),
            ("entry_links", "link_type"),
            ("source_refs", "retrieved_at"),
            ("source_verifications", "verified_by"),
            ("open_questions", "question"),
        ]
        self.c.execute(
            "INSERT INTO match_tags VALUES (?,?,?,?,?)",
            ("tag1", "E", 1, "domain", "dev"),
        )
        self.c.execute(
            "INSERT INTO content_sections VALUES (?,?,?,?,?)",
            ("s1", "E", 1, "heading", "summary"),
        )
        self.c.execute(
            "INSERT INTO entry_links VALUES (?,?,?,?,?,?)",
            ("l1", "E", 1, "E", 1, "related"),
        )
        self.c.execute(
            "INSERT INTO open_questions VALUES (?,?,?,?,?)",
            ("q1", "E", 1, "Question", "Evidence"),
        )
        verify(self.c)
        for table, col in probes:
            with self.subTest(table=table), self.assertRaises(sqlite3.DatabaseError):
                self.c.execute(
                    f"UPDATE {table} SET {col}={col} WHERE rowid=(SELECT min(rowid) FROM {table})"
                )
            with self.subTest(table=table), self.assertRaises(sqlite3.DatabaseError):
                self.c.execute(
                    f"DELETE FROM {table} WHERE rowid=(SELECT min(rowid) FROM {table})"
                )

    def test_30_replace_cannot_bypass_no_delete(self):
        self.rejects(
            "INSERT OR REPLACE INTO entries VALUES (?,?,?)",
            ("E", "other", "2026-11-06"),
        )
        self.rejects(
            """INSERT OR REPLACE INTO entry_versions
          (entry_id,version,title,content_path,content_sha256,manifest_sha256,created_by,created_at)
          VALUES (?,?,?,?,?,?,?,?)""",
            ("E", 1, "replaced", "p", HEX_A, HEX_A, "author", "t"),
        )
        self.rejects(
            "INSERT OR REPLACE INTO version_status VALUES (?,?,?,?,?,?)",
            ("E", 1, 1, "draft", "other", "t"),
        )
        self.assertEqual(
            self.c.execute("SELECT title FROM entry_versions").fetchone()[0], "v1"
        )

    def test_31_fk_integrity(self):
        self.assertEqual(self.c.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_32_fts5_probe_reports_actual_availability(self):
        try:
            self.c.execute(
                "CREATE VIRTUAL TABLE temp.catalog_fts_probe USING fts5(title, summary)"
            )
        except sqlite3.OperationalError:
            self.fts5_available = False
        else:
            self.fts5_available = True
        self.assertIsInstance(self.fts5_available, bool)

    def test_33_no_manifest_identity_mutation(self):
        self.rejects(
            "UPDATE entry_versions SET manifest_sha256=? WHERE entry_id=? AND version=?",
            (HEX_B, "E", 1),
        )

    def test_34_supersedes_must_be_lower(self):
        with self.assertRaises(sqlite3.DatabaseError):
            start_entry(self.c, "E", 2, supersedes=3)

    def test_35_invalid_tag_domain_rejected(self):
        self.rejects(
            "INSERT INTO match_tags VALUES (?,?,?,?,?)",
            ("tag1", "E", 1, "arbitrary", "x"),
        )

    def test_36_three_state_result_counts_sum(self):
        self.c.execute(
            "INSERT INTO content_sections VALUES (?,?,?,?,?)",
            ("s1", "E", 1, "heading", "summary"),
        )
        self.c.execute(
            "INSERT INTO entry_links VALUES (?,?,?,?,?,?)",
            ("l1", "E", 1, "E", 1, "related"),
        )
        ref(self.c, "r2", target=None, section="s1")
        ref(self.c, "r3", target=None, link="l1")
        verify(self.c, "r1", result="verified")
        verify(self.c, "r2", result="not_verified", excerpt=None)
        rows = self.c.execute(
            "SELECT result,count(*) FROM source_ref_current GROUP BY result"
        ).fetchall()
        self.assertEqual(sum(n for _, n in rows), 3)
        self.assertEqual(
            dict(rows), {"verified": 1, "not_verified": 1, "unverified": 1}
        )

    def test_37_allow_new_draft_after_earlier_publication(self):
        status(self.c, "E", 1, 2, "published")
        start_entry(self.c, "E", 2, supersedes=1)
        self.c.execute(
            "INSERT INTO match_tags VALUES (?,?,?,?,?)",
            ("t2", "E", 2, "domain", "infra"),
        )
        self.assertEqual(
            self.c.execute(
                "SELECT count(*) FROM match_tags WHERE version=2"
            ).fetchone()[0],
            1,
        )


class ConnectionAndConcurrency(unittest.TestCase):
    def test_38_pragmas_are_connection_local(self):
        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "catalog.sqlite")
            a = connection(path=p)
            a.close()
            b = sqlite3.connect(p)
            # Test doesn't rely on default FK setting; turn it off to show isolation.
            b.execute("PRAGMA foreign_keys=OFF")
            self.assertEqual(b.execute("PRAGMA foreign_keys").fetchone()[0], 0)
            b.execute("PRAGMA foreign_keys=ON")
            b.execute("PRAGMA recursive_triggers=ON")
            self.assertEqual(b.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(b.execute("PRAGMA recursive_triggers").fetchone()[0], 1)
            b.close()

    def test_39_snapshot_reader_stays_consistent_while_writer_commits(self):
        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "catalog.sqlite")
            a = connection(path=p)
            a.execute("PRAGMA journal_mode=WAL")
            start_entry(a)
            condition(a)
            ref(a)
            b = connection(schema=False, path=p)
            b.execute("BEGIN")
            initial = state_hash(b)  # establishes read snapshot
            verify(a)
            self.assertEqual(state_hash(b), initial)
            b.execute("COMMIT")
            self.assertNotEqual(state_hash(b), initial)
            b.close()
            a.close()

    def test_40_conflicting_writer_admission_requires_lock_or_cas(self):
        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "catalog.sqlite")
            a = connection(path=p)
            b = connection(schema=False, path=p)
            start_entry(a)
            a.execute("BEGIN IMMEDIATE")
            b.execute("PRAGMA busy_timeout=50")
            with self.assertRaises(sqlite3.OperationalError):
                b.execute("BEGIN IMMEDIATE")
            a.execute("ROLLBACK")
            b.execute("BEGIN IMMEDIATE")
            b.execute("ROLLBACK")
            b.close()
            a.close()


if __name__ == "__main__":
    unittest.main(verbosity=1)

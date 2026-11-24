"""Databricks DDL conformance tests — 10 checks ported from the SQLite suite.

These verify the Unity Catalog schema created by sql/001_catalog_tables.sql:
append-only enforcement, enforced CHECK constraints (hex digests, tag and
status vocabularies, tool contract fields, section ordinal, source-ref
targeting), and the four publication views' semantics.

The suite skips unless the schema is applied in the current catalog, so it
runs only after the DDL is executed (see task 11 live validation). Override
the schema with AAKR_DDL_SCHEMA (default: aakr_catalog).

Note: valid-row inserts use entry ids prefixed DDLCONF- and are append-only
by design; validation drills in the execution plan use fresh schemas.
test_10 uses a per-run unique entry id because its assertions resolve the
version_current_status view, which depends on the entry's full status
history — a fixed id would collide with rows from a previous run against
the same persistent catalog (PKs are informational, not enforced).
"""

from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))

SCHEMA = os.environ.get("AAKR_DDL_SCHEMA", "aakr_catalog")
HEX = "c" * 64


def _spark():
    from pyspark.sql import SparkSession

    return SparkSession.builder.getOrCreate()


class DDLConformance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spark = _spark()
        cls.catalog = cls.spark.sql("SELECT current_catalog() AS c").collect()[0]["c"]
        cls.full = f"{cls.catalog}.{SCHEMA}"
        try:
            names = {
                r["tableName"]
                for r in cls.spark.sql(f"SHOW TABLES IN {cls.full}").collect()
            }
        except Exception:
            raise unittest.SkipTest(
                f"schema {cls.full} not applied; run sql/001_catalog_tables.sql first"
            )
        required_tables = {
            "entries",
            "entry_versions",
            "version_status",
            "match_tags",
            "tool_contracts",
            "applicability_groups",
            "applicability_conditions",
            "content_sections",
            "entry_links",
            "source_refs",
            "source_verifications",
            "open_questions",
        }
        if not required_tables <= names:
            raise unittest.SkipTest(
                f"schema {cls.full} incomplete: missing {required_tables - names}"
            )

    def _rejects(self, sql: str) -> None:
        with self.assertRaises(Exception):
            self.spark.sql(sql)

    def _insert_entry(self, eid: str, version: int = 1) -> None:
        self.spark.sql(
            f"INSERT INTO {self.full}.entries VALUES "
            f"('{eid}', 'knowledge', '2026-11-24T00:00:00Z')"
        )
        self.spark.sql(
            f"INSERT INTO {self.full}.entry_versions VALUES ('{eid}', {version}, "
            f"'Title', 'blob://{eid}/{version}', '{HEX}', '{HEX}', 'author', "
            f"'2026-11-24T00:00:00Z', NULL, NULL, NULL)"
        )

    # 1. All catalog tables and views exist (ported: schema completeness)
    def test_01_all_catalog_tables_created(self):
        names = {
            r["tableName"]
            for r in self.spark.sql(f"SHOW TABLES IN {self.full}").collect()
        }
        expected = {
            "entries",
            "entry_versions",
            "version_status",
            "match_tags",
            "tool_contracts",
            "applicability_groups",
            "applicability_conditions",
            "content_sections",
            "entry_links",
            "source_refs",
            "source_verifications",
            "open_questions",
            "version_current_status",
            "published_versions",
            "pinnable_versions",
            "source_ref_current",
        }
        self.assertTrue(expected <= names, f"missing: {expected - names}")

    # 2. delta.appendOnly blocks UPDATE (ported: no_update triggers)
    def test_02_append_only_updates_rejected(self):
        self._insert_entry("DDLCONF-02")
        self._rejects(
            f"UPDATE {self.full}.entries SET entry_type = 'tool' "
            f"WHERE entry_id = 'DDLCONF-02'"
        )

    # 3. delta.appendOnly blocks DELETE (ported: no_delete triggers)
    def test_03_append_only_deletes_rejected(self):
        self._rejects(f"DELETE FROM {self.full}.entries WHERE entry_id = 'DDLCONF-02'")

    # 4. Hex-digest CHECK enforced (ported: content/manifest sha checks)
    def test_04_hex_digest_check_enforced(self):
        self._rejects(
            f"INSERT INTO {self.full}.entry_versions VALUES "
            f"('DDLCONF-04', 1, 'Title', 'blob://x', 'nothex', '{HEX}', "
            f"'author', '2026-11-24T00:00:00Z', NULL, NULL, NULL)"
        )

    # 5. Tag vocabulary CHECK enforced, persona allowed (ported: tag checks)
    def test_05_tag_vocabulary_check_enforced(self):
        self._rejects(
            f"INSERT INTO {self.full}.match_tags VALUES "
            f"('DDLCONF-05-bad', 'E', 1, 'arbitrary', 'x')"
        )
        self.spark.sql(
            f"INSERT INTO {self.full}.match_tags VALUES "
            f"('DDLCONF-05-persona', 'DDLCONF-05', 1, 'persona', 'analyst')"
        )

    # 6. Tool contract CHECK enforced (new in manifest v2)
    def test_06_tool_contract_checks_enforced(self):
        self._rejects(
            f"INSERT INTO {self.full}.tool_contracts VALUES "
            f"('DDLCONF-06-tc', 'E', 1, '{{}}', '', '', 'scope')"
        )

    # 7. Section ordinal CHECK enforced (new in manifest v2)
    def test_07_section_ordinal_check_enforced(self):
        self._rejects(
            f"INSERT INTO {self.full}.content_sections VALUES "
            f"('DDLCONF-07-bad', 'E', 1, 'h', 's', 0)"
        )
        self.spark.sql(
            f"INSERT INTO {self.full}.content_sections VALUES "
            f"('DDLCONF-07-ok', 'E', 1, 'h', 's', 1)"
        )

    # 8. Source refs target exactly one claim (ported: single-target CHECK)
    def test_08_source_refs_exactly_one_target(self):
        self._rejects(
            f"INSERT INTO {self.full}.source_refs VALUES "
            f"('DDLCONF-08-none', 'E', 1, 'documentation', 'uri://x', 'v1', "
            f"'2026-11-24T00:00:00Z', NULL, NULL, NULL)"
        )
        self._rejects(
            f"INSERT INTO {self.full}.source_refs VALUES "
            f"('DDLCONF-08-two', 'E', 1, 'documentation', 'uri://x', 'v1', "
            f"'2026-11-24T00:00:00Z', 'c1', 's1', NULL)"
        )

    # 9. Status vocabulary CHECK enforced (ported: status checks)
    def test_09_status_vocabulary_check_enforced(self):
        self._rejects(
            f"INSERT INTO {self.full}.version_status VALUES "
            f"('E', 1, 1, 'bogus', 'actor', 't')"
        )
        self.spark.sql(
            f"INSERT INTO {self.full}.version_status VALUES "
            f"('DDLCONF-09', 1, 1, 'draft', 'curator', '2026-11-24T00:00:00Z')"
        )

    # 10. Publication views resolve correct semantics (ported: view suite)
    def test_10_views_resolve_publication_semantics(self):
        # Unique per run: the view resolves the entry's full status history,
        # so a fixed id would see rows left by a previous run of this suite.
        eid = f"DDLCONF-10-{uuid.uuid4().hex[:8]}"
        self._insert_entry(eid)
        self.spark.sql(
            f"INSERT INTO {self.full}.version_status VALUES "
            f"('{eid}', 1, 1, 'draft', 'curator', 't1')"
        )
        row = self.spark.sql(
            f"SELECT status FROM {self.full}.version_current_status "
            f"WHERE entry_id = '{eid}' AND version = 1"
        ).collect()
        self.assertEqual([r["status"] for r in row], ["draft"])

        self.spark.sql(
            f"INSERT INTO {self.full}.version_status VALUES "
            f"('{eid}', 1, 2, 'published', 'curator', 't2')"
        )
        pub = self.spark.sql(
            f"SELECT entry_id, version FROM {self.full}.published_versions "
            f"WHERE entry_id = '{eid}'"
        ).collect()
        self.assertEqual([(r["entry_id"], r["version"]) for r in pub], [(eid, 1)])

        self.spark.sql(
            f"INSERT INTO {self.full}.version_status VALUES "
            f"('{eid}', 1, 3, 'deprecated', 'curator', 't3')"
        )
        pub2 = self.spark.sql(
            f"SELECT entry_id FROM {self.full}.published_versions "
            f"WHERE entry_id = '{eid}'"
        ).collect()
        self.assertEqual(pub2, [])
        pin = self.spark.sql(
            f"SELECT entry_id, version FROM {self.full}.pinnable_versions "
            f"WHERE entry_id = '{eid}'"
        ).collect()
        self.assertEqual([(r["entry_id"], r["version"]) for r in pin], [(eid, 1)])


if __name__ == "__main__":
    unittest.main()

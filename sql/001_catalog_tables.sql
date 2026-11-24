-- AAKR Catalog: Databricks Unity Catalog DDL, contract version 2
-- (manifest profile oneshot-catalog-manifest-v2: adds tool_contracts, section
--  ordinal, and the persona tag type).
--
-- Retargeted from the SQLite reference schema (aakr/integrations/sqlite/schema.sql).
-- Run inside the target catalog; creates schema aakr_catalog (prefixed namespace)
-- with append-only managed Delta tables, enforced CHECK constraints, and the four
-- read-only views the DatabricksStore adapter queries (aakr/integrations/databricks/adapter.py).
-- Column order is identical to the SQLite schema because Curator/adapter code
-- issues positional INSERTs.
--
-- Porting rules (SQLite -> Databricks):
-- * SQLite triggers do not exist. The status-sequence/transition rules, the
--   verification-sequence rule, and the creator-as-verifier rejection are
--   enforced by Curator code (and re-checked by validate_integrity()). The
--   "children only while draft" freeze rules are enforced by the Curator
--   draft-status gate before writes.
-- * Append-only (SQLite no_update/no_delete triggers) is enforced by the
--   delta.appendOnly=true table property: UPDATE and DELETE are rejected.
-- * PK/FK constraints are informational in Databricks; identity rules are
--   enforced in code. FKs are added after all tables exist and always
--   reference the parent's full primary key. UNIQUE constraints and the
--   partial unique indexes (uq_source_*) have no portable equivalent and
--   are enforced in code.
-- * CHECK constraints are enforced on managed Delta tables. Hex-digest
--   checks use rlike instead of SQLite GLOB.
-- * Run once; ALTER TABLE ADD CONSTRAINT is not idempotent.

CREATE SCHEMA IF NOT EXISTS aakr_catalog;
USE aakr_catalog;

CREATE TABLE IF NOT EXISTS aakr_catalog.entries (
  entry_id STRING NOT NULL,
  entry_type STRING NOT NULL CHECK (length(trim(entry_type)) > 0),
  created_at STRING NOT NULL CHECK (length(trim(created_at)) > 0),
  CONSTRAINT entries_pk PRIMARY KEY (entry_id)
) USING DELTA
  TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS aakr_catalog.entry_versions (
  entry_id STRING NOT NULL,
  version INT NOT NULL CHECK (version >= 1),
  title STRING NOT NULL CHECK (length(trim(title)) > 0),
  content_path STRING NOT NULL,
  content_sha256 STRING NOT NULL CHECK (rlike(content_sha256, '^[0-9a-f]{64}$')),
  manifest_sha256 STRING NOT NULL CHECK (rlike(manifest_sha256, '^[0-9a-f]{64}$')),
  created_by STRING NOT NULL CHECK (length(trim(created_by)) > 0),
  created_at STRING NOT NULL,
  supersedes_version INT,
  derived_from_ref STRING,
  derived_from_sha256 STRING
    CHECK (derived_from_sha256 IS NULL OR rlike(derived_from_sha256, '^[0-9a-f]{64}$')),
  CONSTRAINT entry_versions_pk PRIMARY KEY (entry_id, version),
  CONSTRAINT entry_versions_supersedes_lt
    CHECK (supersedes_version IS NULL OR supersedes_version < version),
  CONSTRAINT entry_versions_derived_pair
    CHECK ((derived_from_ref IS NULL) = (derived_from_sha256 IS NULL))
) USING DELTA
  TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS aakr_catalog.version_status (
  entry_id STRING NOT NULL,
  version INT NOT NULL,
  seq BIGINT NOT NULL CHECK (seq >= 1),
  status STRING NOT NULL CHECK (status IN ('draft','published','deprecated')),
  actor STRING NOT NULL CHECK (length(trim(actor)) > 0),
  recorded_at STRING NOT NULL,
  CONSTRAINT version_status_pk PRIMARY KEY (entry_id, version, seq)
) USING DELTA
  TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS aakr_catalog.match_tags (
  tag_id STRING NOT NULL,
  entry_id STRING NOT NULL,
  version INT NOT NULL,
  tag_type STRING NOT NULL CHECK (tag_type IN
    ('domain','task_type','project_type','language','framework','intent','file_signal','persona')),
  value STRING NOT NULL CHECK (length(trim(value)) > 0),
  CONSTRAINT match_tags_pk PRIMARY KEY (tag_id)
) USING DELTA
  TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS aakr_catalog.tool_contracts (
  tool_contract_id STRING NOT NULL,
  entry_id STRING NOT NULL,
  version INT NOT NULL,
  input_schema STRING NOT NULL CHECK (length(trim(input_schema)) > 0),
  output_schema STRING NOT NULL,
  handler_ref STRING NOT NULL CHECK (length(trim(handler_ref)) > 0),
  invocation_scope STRING NOT NULL CHECK (length(trim(invocation_scope)) > 0),
  CONSTRAINT tool_contracts_pk PRIMARY KEY (tool_contract_id, entry_id, version)
) USING DELTA
  TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS aakr_catalog.applicability_groups (
  group_id STRING NOT NULL,
  entry_id STRING NOT NULL,
  version INT NOT NULL,
  group_mode STRING NOT NULL CHECK (group_mode IN ('all_of','any_of','none_of')),
  CONSTRAINT applicability_groups_pk PRIMARY KEY (group_id, entry_id, version)
) USING DELTA
  TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS aakr_catalog.applicability_conditions (
  condition_id STRING NOT NULL,
  entry_id STRING NOT NULL,
  version INT NOT NULL,
  group_id STRING NOT NULL,
  signal STRING NOT NULL CHECK (length(trim(signal)) > 0),
  `operator` STRING NOT NULL CHECK (`operator` IN ('eq','ne','contains','exists','absent')),
  value STRING,
  required_evidence STRING NOT NULL CHECK (required_evidence IN
    ('file_range','call_path','test_assertion','command_output','runtime_artifact')),
  CONSTRAINT applicability_conditions_pk PRIMARY KEY (condition_id),
  CONSTRAINT applicability_conditions_operator_value
    CHECK ((`operator` IN ('exists','absent') AND value IS NULL) OR
           (`operator` NOT IN ('exists','absent') AND value IS NOT NULL))
) USING DELTA
  TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS aakr_catalog.content_sections (
  section_id STRING NOT NULL,
  entry_id STRING NOT NULL,
  version INT NOT NULL,
  heading STRING NOT NULL,
  summary STRING NOT NULL,
  ordinal INT CHECK (ordinal IS NULL OR ordinal >= 1),
  CONSTRAINT content_sections_pk PRIMARY KEY (section_id, entry_id, version)
) USING DELTA
  TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS aakr_catalog.entry_links (
  link_id STRING NOT NULL,
  from_entry_id STRING NOT NULL,
  from_version INT NOT NULL,
  to_entry_id STRING NOT NULL,
  to_version INT NOT NULL,
  link_type STRING NOT NULL CHECK (link_type IN
    ('depends_on','alternative_to','compatible_with','incompatible_with',
     'related','fixture_for','part_of')),
  CONSTRAINT entry_links_pk PRIMARY KEY (link_id)
) USING DELTA
  TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS aakr_catalog.source_refs (
  source_ref_id STRING NOT NULL,
  entry_id STRING NOT NULL,
  version INT NOT NULL,
  source_type STRING NOT NULL CHECK (source_type IN
    ('documentation','standard','release_notes','repository','curator_authored','other')),
  source_uri STRING NOT NULL CHECK (length(trim(source_uri)) > 0),
  source_version STRING NOT NULL CHECK (length(trim(source_version)) > 0),
  retrieved_at STRING NOT NULL,
  target_condition_id STRING,
  target_section_id STRING,
  target_link_id STRING,
  CONSTRAINT source_refs_pk PRIMARY KEY (source_ref_id),
  CONSTRAINT source_refs_exactly_one_target
    CHECK ((CASE WHEN target_condition_id IS NOT NULL THEN 1 ELSE 0 END
          + CASE WHEN target_section_id IS NOT NULL THEN 1 ELSE 0 END
          + CASE WHEN target_link_id IS NOT NULL THEN 1 ELSE 0 END) = 1)
) USING DELTA
  TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS aakr_catalog.source_verifications (
  source_ref_id STRING NOT NULL,
  seq BIGINT NOT NULL CHECK (seq >= 1),
  result STRING NOT NULL CHECK (result IN ('verified','not_verified','contradicted')),
  verified_by STRING NOT NULL CHECK (length(trim(verified_by)) > 0),
  verified_at STRING NOT NULL,
  checked_source_sha256 STRING NOT NULL
    CHECK (rlike(checked_source_sha256, '^[0-9a-f]{64}$')),
  excerpt STRING,
  CONSTRAINT source_verifications_pk PRIMARY KEY (source_ref_id, seq),
  CONSTRAINT source_verifications_excerpt
    CHECK (result <> 'verified' OR (excerpt IS NOT NULL AND length(trim(excerpt)) > 0))
) USING DELTA
  TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS aakr_catalog.open_questions (
  question_id STRING NOT NULL,
  entry_id STRING NOT NULL,
  version INT NOT NULL,
  question STRING NOT NULL CHECK (length(trim(question)) > 0),
  evidence_needed STRING NOT NULL CHECK (length(trim(evidence_needed)) > 0),
  CONSTRAINT open_questions_pk PRIMARY KEY (question_id)
) USING DELTA
  TBLPROPERTIES ('delta.appendOnly' = 'true');

-- Informational foreign keys (added after all tables exist; each references
-- the parent's full primary key). Idempotency note: ALTER ADD CONSTRAINT
-- fails if the constraint already exists — run this file once per schema.

ALTER TABLE aakr_catalog.entry_versions ADD CONSTRAINT entry_versions_entries_fk
  FOREIGN KEY (entry_id) REFERENCES aakr_catalog.entries;
ALTER TABLE aakr_catalog.entry_versions ADD CONSTRAINT entry_versions_supersedes_fk
  FOREIGN KEY (entry_id, supersedes_version) REFERENCES aakr_catalog.entry_versions;
ALTER TABLE aakr_catalog.version_status ADD CONSTRAINT version_status_versions_fk
  FOREIGN KEY (entry_id, version) REFERENCES aakr_catalog.entry_versions;
ALTER TABLE aakr_catalog.match_tags ADD CONSTRAINT match_tags_versions_fk
  FOREIGN KEY (entry_id, version) REFERENCES aakr_catalog.entry_versions;
ALTER TABLE aakr_catalog.tool_contracts ADD CONSTRAINT tool_contracts_versions_fk
  FOREIGN KEY (entry_id, version) REFERENCES aakr_catalog.entry_versions;
ALTER TABLE aakr_catalog.applicability_groups ADD CONSTRAINT applicability_groups_versions_fk
  FOREIGN KEY (entry_id, version) REFERENCES aakr_catalog.entry_versions;
ALTER TABLE aakr_catalog.applicability_conditions ADD CONSTRAINT applicability_conditions_groups_fk
  FOREIGN KEY (group_id, entry_id, version) REFERENCES aakr_catalog.applicability_groups;
ALTER TABLE aakr_catalog.content_sections ADD CONSTRAINT content_sections_versions_fk
  FOREIGN KEY (entry_id, version) REFERENCES aakr_catalog.entry_versions;
ALTER TABLE aakr_catalog.entry_links ADD CONSTRAINT entry_links_from_fk
  FOREIGN KEY (from_entry_id, from_version) REFERENCES aakr_catalog.entry_versions;
ALTER TABLE aakr_catalog.entry_links ADD CONSTRAINT entry_links_to_fk
  FOREIGN KEY (to_entry_id, to_version) REFERENCES aakr_catalog.entry_versions;
ALTER TABLE aakr_catalog.source_refs ADD CONSTRAINT source_refs_versions_fk
  FOREIGN KEY (entry_id, version) REFERENCES aakr_catalog.entry_versions;
ALTER TABLE aakr_catalog.source_refs ADD CONSTRAINT source_refs_conditions_fk
  FOREIGN KEY (target_condition_id) REFERENCES aakr_catalog.applicability_conditions;
ALTER TABLE aakr_catalog.source_refs ADD CONSTRAINT source_refs_sections_fk
  FOREIGN KEY (target_section_id) REFERENCES aakr_catalog.content_sections;
ALTER TABLE aakr_catalog.source_refs ADD CONSTRAINT source_refs_links_fk
  FOREIGN KEY (target_link_id) REFERENCES aakr_catalog.entry_links;
ALTER TABLE aakr_catalog.source_verifications ADD CONSTRAINT source_verifications_refs_fk
  FOREIGN KEY (source_ref_id) REFERENCES aakr_catalog.source_refs;
ALTER TABLE aakr_catalog.open_questions ADD CONSTRAINT open_questions_versions_fk
  FOREIGN KEY (entry_id, version) REFERENCES aakr_catalog.entry_versions;

-- Read-only views queried by DatabricksStore (same semantics as the SQLite views).

CREATE OR REPLACE VIEW aakr_catalog.version_current_status AS
SELECT entry_id, version, status, seq
FROM (
  SELECT entry_id, version, status, seq,
    ROW_NUMBER() OVER (PARTITION BY entry_id, version ORDER BY seq DESC) AS rn
  FROM aakr_catalog.version_status
) WHERE rn = 1;

CREATE OR REPLACE VIEW aakr_catalog.published_versions AS
WITH publication_frontier AS (
  SELECT entry_id, MAX(version) AS version FROM aakr_catalog.version_status
  WHERE status = 'published' GROUP BY entry_id
)
SELECT e.entry_id, e.entry_type, v.version, v.title, v.content_path,
       v.content_sha256, v.manifest_sha256
FROM publication_frontier f
JOIN aakr_catalog.entry_versions v ON v.entry_id = f.entry_id AND v.version = f.version
JOIN aakr_catalog.entries e ON e.entry_id = v.entry_id
JOIN aakr_catalog.version_current_status s ON s.entry_id = v.entry_id AND s.version = v.version
WHERE s.status = 'published';

CREATE OR REPLACE VIEW aakr_catalog.pinnable_versions AS
SELECT v.* FROM aakr_catalog.entry_versions v
JOIN aakr_catalog.version_current_status s ON s.entry_id = v.entry_id AND s.version = v.version
WHERE s.status IN ('published','deprecated')
  AND EXISTS (SELECT 1 FROM aakr_catalog.version_status h WHERE h.entry_id = v.entry_id
   AND h.version = v.version AND h.status = 'published');

CREATE OR REPLACE VIEW aakr_catalog.source_ref_current AS
SELECT r.source_ref_id, r.entry_id, r.version, r.source_type, r.source_uri,
       r.source_version, r.target_condition_id, r.target_section_id, r.target_link_id,
       v.seq, COALESCE(v.result, 'unverified') AS result,
       v.verified_by, v.verified_at, v.checked_source_sha256, v.excerpt
FROM aakr_catalog.source_refs r
LEFT JOIN aakr_catalog.source_verifications v ON v.source_ref_id = r.source_ref_id
  AND v.seq = (SELECT MAX(v2.seq) FROM aakr_catalog.source_verifications v2
   WHERE v2.source_ref_id = r.source_ref_id);

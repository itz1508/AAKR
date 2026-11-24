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
-- * CHECK constraints must be added via ALTER TABLE ADD CONSTRAINT after
--   table creation: this runtime accepts inline CREATE TABLE CHECKs but does
--   NOT enforce them, while ALTER-added CHECKs are enforced and validated
--   against existing rows at add time. Hex-digest checks use rlike instead
--   of SQLite GLOB. ALTER ADD CONSTRAINT is not idempotent — add these
--   before any data is written, and run this file once per schema.

CREATE SCHEMA IF NOT EXISTS aakr_catalog;
USE aakr_catalog;

CREATE TABLE IF NOT EXISTS aakr_catalog.entries (
  entry_id STRING NOT NULL,
  entry_type STRING NOT NULL,
  created_at STRING NOT NULL,
  CONSTRAINT entries_pk PRIMARY KEY (entry_id)
) USING DELTA
  TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS aakr_catalog.entry_versions (
  entry_id STRING NOT NULL,
  version INT NOT NULL,
  title STRING NOT NULL,
  content_path STRING NOT NULL,
  content_sha256 STRING NOT NULL,
  manifest_sha256 STRING NOT NULL,
  created_by STRING NOT NULL,
  created_at STRING NOT NULL,
  supersedes_version INT,
  derived_from_ref STRING,
  derived_from_sha256 STRING,
  CONSTRAINT entry_versions_pk PRIMARY KEY (entry_id, version)
) USING DELTA
  TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS aakr_catalog.version_status (
  entry_id STRING NOT NULL,
  version INT NOT NULL,
  seq BIGINT NOT NULL,
  status STRING NOT NULL,
  actor STRING NOT NULL,
  recorded_at STRING NOT NULL,
  CONSTRAINT version_status_pk PRIMARY KEY (entry_id, version, seq)
) USING DELTA
  TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS aakr_catalog.match_tags (
  tag_id STRING NOT NULL,
  entry_id STRING NOT NULL,
  version INT NOT NULL,
  tag_type STRING NOT NULL,
  value STRING NOT NULL,
  CONSTRAINT match_tags_pk PRIMARY KEY (tag_id)
) USING DELTA
  TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS aakr_catalog.tool_contracts (
  tool_contract_id STRING NOT NULL,
  entry_id STRING NOT NULL,
  version INT NOT NULL,
  input_schema STRING NOT NULL,
  output_schema STRING NOT NULL,
  handler_ref STRING NOT NULL,
  invocation_scope STRING NOT NULL,
  CONSTRAINT tool_contracts_pk PRIMARY KEY (tool_contract_id, entry_id, version)
) USING DELTA
  TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS aakr_catalog.applicability_groups (
  group_id STRING NOT NULL,
  entry_id STRING NOT NULL,
  version INT NOT NULL,
  group_mode STRING NOT NULL,
  CONSTRAINT applicability_groups_pk PRIMARY KEY (group_id, entry_id, version)
) USING DELTA
  TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS aakr_catalog.applicability_conditions (
  condition_id STRING NOT NULL,
  entry_id STRING NOT NULL,
  version INT NOT NULL,
  group_id STRING NOT NULL,
  signal STRING NOT NULL,
  `operator` STRING NOT NULL,
  value STRING,
  required_evidence STRING NOT NULL,
  CONSTRAINT applicability_conditions_pk PRIMARY KEY (condition_id)
) USING DELTA
  TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS aakr_catalog.content_sections (
  section_id STRING NOT NULL,
  entry_id STRING NOT NULL,
  version INT NOT NULL,
  heading STRING NOT NULL,
  summary STRING NOT NULL,
  ordinal INT,
  CONSTRAINT content_sections_pk PRIMARY KEY (section_id, entry_id, version)
) USING DELTA
  TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS aakr_catalog.entry_links (
  link_id STRING NOT NULL,
  from_entry_id STRING NOT NULL,
  from_version INT NOT NULL,
  to_entry_id STRING NOT NULL,
  to_version INT NOT NULL,
  link_type STRING NOT NULL,
  CONSTRAINT entry_links_pk PRIMARY KEY (link_id)
) USING DELTA
  TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS aakr_catalog.source_refs (
  source_ref_id STRING NOT NULL,
  entry_id STRING NOT NULL,
  version INT NOT NULL,
  source_type STRING NOT NULL,
  source_uri STRING NOT NULL,
  source_version STRING NOT NULL,
  retrieved_at STRING NOT NULL,
  target_condition_id STRING,
  target_section_id STRING,
  target_link_id STRING,
  CONSTRAINT source_refs_pk PRIMARY KEY (source_ref_id)
) USING DELTA
  TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS aakr_catalog.source_verifications (
  source_ref_id STRING NOT NULL,
  seq BIGINT NOT NULL,
  result STRING NOT NULL,
  verified_by STRING NOT NULL,
  verified_at STRING NOT NULL,
  checked_source_sha256 STRING NOT NULL,
  excerpt STRING,
  CONSTRAINT source_verifications_pk PRIMARY KEY (source_ref_id, seq)
) USING DELTA
  TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS aakr_catalog.open_questions (
  question_id STRING NOT NULL,
  entry_id STRING NOT NULL,
  version INT NOT NULL,
  question STRING NOT NULL,
  evidence_needed STRING NOT NULL,
  CONSTRAINT open_questions_pk PRIMARY KEY (question_id)
) USING DELTA
  TBLPROPERTIES ('delta.appendOnly' = 'true');

-- Enforced CHECK constraints. ALTER TABLE ADD CONSTRAINT is the only form
-- this runtime enforces (inline CREATE TABLE CHECKs are silently accepted but
-- not enforced). Delta validates existing rows when a constraint is added,
-- so add these before any data is written. Not idempotent — run once.

ALTER TABLE aakr_catalog.entries ADD CONSTRAINT entries_entry_type_nonblank
  CHECK (length(trim(entry_type)) > 0);
ALTER TABLE aakr_catalog.entries ADD CONSTRAINT entries_created_at_nonblank
  CHECK (length(trim(created_at)) > 0);
ALTER TABLE aakr_catalog.entry_versions ADD CONSTRAINT entry_versions_version_gte1
  CHECK (version >= 1);
ALTER TABLE aakr_catalog.entry_versions ADD CONSTRAINT entry_versions_title_nonblank
  CHECK (length(trim(title)) > 0);
ALTER TABLE aakr_catalog.entry_versions ADD CONSTRAINT entry_versions_content_sha256_hex
  CHECK (rlike(content_sha256, '^[0-9a-f]{64}$'));
ALTER TABLE aakr_catalog.entry_versions ADD CONSTRAINT entry_versions_manifest_sha256_hex
  CHECK (rlike(manifest_sha256, '^[0-9a-f]{64}$'));
ALTER TABLE aakr_catalog.entry_versions ADD CONSTRAINT entry_versions_created_by_nonblank
  CHECK (length(trim(created_by)) > 0);
ALTER TABLE aakr_catalog.entry_versions ADD CONSTRAINT entry_versions_derived_sha256_hex
  CHECK (derived_from_sha256 IS NULL OR rlike(derived_from_sha256, '^[0-9a-f]{64}$'));
ALTER TABLE aakr_catalog.entry_versions ADD CONSTRAINT entry_versions_supersedes_lt
  CHECK (supersedes_version IS NULL OR supersedes_version < version);
ALTER TABLE aakr_catalog.entry_versions ADD CONSTRAINT entry_versions_derived_pair
  CHECK ((derived_from_ref IS NULL) = (derived_from_sha256 IS NULL));
ALTER TABLE aakr_catalog.version_status ADD CONSTRAINT version_status_seq_gte1
  CHECK (seq >= 1);
ALTER TABLE aakr_catalog.version_status ADD CONSTRAINT version_status_status_vocab
  CHECK (status IN ('draft','published','deprecated'));
ALTER TABLE aakr_catalog.version_status ADD CONSTRAINT version_status_actor_nonblank
  CHECK (length(trim(actor)) > 0);
ALTER TABLE aakr_catalog.match_tags ADD CONSTRAINT match_tags_tag_type_vocab
  CHECK (tag_type IN ('domain','task_type','project_type','language','framework','intent','file_signal','persona'));
ALTER TABLE aakr_catalog.match_tags ADD CONSTRAINT match_tags_value_nonblank
  CHECK (length(trim(value)) > 0);
ALTER TABLE aakr_catalog.tool_contracts ADD CONSTRAINT tool_contracts_input_schema_nonblank
  CHECK (length(trim(input_schema)) > 0);
ALTER TABLE aakr_catalog.tool_contracts ADD CONSTRAINT tool_contracts_handler_ref_nonblank
  CHECK (length(trim(handler_ref)) > 0);
ALTER TABLE aakr_catalog.tool_contracts ADD CONSTRAINT tool_contracts_invocation_scope_nonblank
  CHECK (length(trim(invocation_scope)) > 0);
ALTER TABLE aakr_catalog.applicability_groups ADD CONSTRAINT applicability_groups_group_mode_vocab
  CHECK (group_mode IN ('all_of','any_of','none_of'));
ALTER TABLE aakr_catalog.applicability_conditions ADD CONSTRAINT applicability_conditions_signal_nonblank
  CHECK (length(trim(signal)) > 0);
ALTER TABLE aakr_catalog.applicability_conditions ADD CONSTRAINT applicability_conditions_operator_vocab
  CHECK (`operator` IN ('eq','ne','contains','exists','absent'));
ALTER TABLE aakr_catalog.applicability_conditions ADD CONSTRAINT applicability_conditions_required_evidence_vocab
  CHECK (required_evidence IN ('file_range','call_path','test_assertion','command_output','runtime_artifact'));
ALTER TABLE aakr_catalog.applicability_conditions ADD CONSTRAINT applicability_conditions_operator_value
  CHECK ((`operator` IN ('exists','absent') AND value IS NULL) OR
         (`operator` NOT IN ('exists','absent') AND value IS NOT NULL));
ALTER TABLE aakr_catalog.content_sections ADD CONSTRAINT content_sections_ordinal_gte1
  CHECK (ordinal IS NULL OR ordinal >= 1);
ALTER TABLE aakr_catalog.entry_links ADD CONSTRAINT entry_links_link_type_vocab
  CHECK (link_type IN ('depends_on','alternative_to','compatible_with','incompatible_with',
     'related','fixture_for','part_of'));
ALTER TABLE aakr_catalog.source_refs ADD CONSTRAINT source_refs_source_type_vocab
  CHECK (source_type IN ('documentation','standard','release_notes','repository','curator_authored','other'));
ALTER TABLE aakr_catalog.source_refs ADD CONSTRAINT source_refs_source_uri_nonblank
  CHECK (length(trim(source_uri)) > 0);
ALTER TABLE aakr_catalog.source_refs ADD CONSTRAINT source_refs_source_version_nonblank
  CHECK (length(trim(source_version)) > 0);
ALTER TABLE aakr_catalog.source_refs ADD CONSTRAINT source_refs_exactly_one_target
  CHECK ((CASE WHEN target_condition_id IS NOT NULL THEN 1 ELSE 0 END
        + CASE WHEN target_section_id IS NOT NULL THEN 1 ELSE 0 END
        + CASE WHEN target_link_id IS NOT NULL THEN 1 ELSE 0 END) = 1);
ALTER TABLE aakr_catalog.source_verifications ADD CONSTRAINT source_verifications_seq_gte1
  CHECK (seq >= 1);
ALTER TABLE aakr_catalog.source_verifications ADD CONSTRAINT source_verifications_result_vocab
  CHECK (result IN ('verified','not_verified','contradicted'));
ALTER TABLE aakr_catalog.source_verifications ADD CONSTRAINT source_verifications_verified_by_nonblank
  CHECK (length(trim(verified_by)) > 0);
ALTER TABLE aakr_catalog.source_verifications ADD CONSTRAINT source_verifications_checked_sha256_hex
  CHECK (rlike(checked_source_sha256, '^[0-9a-f]{64}$'));
ALTER TABLE aakr_catalog.source_verifications ADD CONSTRAINT source_verifications_excerpt
  CHECK (result <> 'verified' OR (excerpt IS NOT NULL AND length(trim(excerpt)) > 0));
ALTER TABLE aakr_catalog.open_questions ADD CONSTRAINT open_questions_question_nonblank
  CHECK (length(trim(question)) > 0);
ALTER TABLE aakr_catalog.open_questions ADD CONSTRAINT open_questions_evidence_needed_nonblank
  CHECK (length(trim(evidence_needed)) > 0);

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
  FOREIGN KEY (target_section_id, entry_id, version) REFERENCES aakr_catalog.content_sections;
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
LEFT JOIN (
  SELECT source_ref_id, seq, result, verified_by, verified_at,
         checked_source_sha256, excerpt,
         ROW_NUMBER() OVER (PARTITION BY source_ref_id ORDER BY seq DESC) AS rn
  FROM aakr_catalog.source_verifications
) v ON v.source_ref_id = r.source_ref_id AND v.rn = 1;

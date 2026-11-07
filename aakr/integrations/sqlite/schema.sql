-- OneShot Catalog: SQLite reference schema, contract version 1
-- Only execute in a dedicated development database. Curator must verify pragmas on every connection.
PRAGMA foreign_keys = ON;
PRAGMA recursive_triggers = ON;

CREATE TABLE entries (
  entry_id TEXT PRIMARY KEY,
  entry_type TEXT NOT NULL CHECK (length(trim(entry_type)) > 0),
  created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0)
);

CREATE TABLE entry_versions (
  entry_id TEXT NOT NULL,
  version INTEGER NOT NULL CHECK (version >= 1),
  title TEXT NOT NULL CHECK (length(trim(title)) > 0),
  content_path TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
  manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
  created_at TEXT NOT NULL,
  supersedes_version INTEGER,
  derived_from_ref TEXT,
  derived_from_sha256 TEXT CHECK (derived_from_sha256 IS NULL OR
    (length(derived_from_sha256) = 64 AND derived_from_sha256 NOT GLOB '*[^0-9a-f]*')),
  PRIMARY KEY (entry_id, version),
  FOREIGN KEY (entry_id) REFERENCES entries(entry_id),
  FOREIGN KEY (entry_id, supersedes_version) REFERENCES entry_versions(entry_id, version),
  CHECK (supersedes_version IS NULL OR supersedes_version < version),
  CHECK ((derived_from_ref IS NULL) = (derived_from_sha256 IS NULL))
);

CREATE TABLE version_status (
  entry_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  seq INTEGER NOT NULL CHECK (seq >= 1),
  status TEXT NOT NULL CHECK (status IN ('draft','published','deprecated')),
  actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
  recorded_at TEXT NOT NULL,
  PRIMARY KEY (entry_id, version, seq),
  FOREIGN KEY (entry_id, version) REFERENCES entry_versions(entry_id, version)
);

CREATE TABLE match_tags (
  tag_id TEXT PRIMARY KEY,
  entry_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  tag_type TEXT NOT NULL CHECK (tag_type IN
    ('domain','task_type','project_type','language','framework','intent','file_signal')),
  value TEXT NOT NULL CHECK (length(trim(value)) > 0),
  UNIQUE (entry_id, version, tag_type, value),
  FOREIGN KEY (entry_id, version) REFERENCES entry_versions(entry_id, version)
);

CREATE TABLE applicability_groups (
  group_id TEXT NOT NULL,
  entry_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  group_mode TEXT NOT NULL CHECK (group_mode IN ('all_of','any_of','none_of')),
  PRIMARY KEY (group_id, entry_id, version),
  FOREIGN KEY (entry_id, version) REFERENCES entry_versions(entry_id, version)
);

CREATE TABLE applicability_conditions (
  condition_id TEXT PRIMARY KEY,
  entry_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  group_id TEXT NOT NULL,
  signal TEXT NOT NULL CHECK (length(trim(signal)) > 0),
  operator TEXT NOT NULL CHECK (operator IN ('eq','ne','contains','exists','absent')),
  value TEXT,
  required_evidence TEXT NOT NULL CHECK (required_evidence IN
    ('file_range','call_path','test_assertion','command_output','runtime_artifact')),
  UNIQUE (condition_id, entry_id, version),
  FOREIGN KEY (group_id, entry_id, version)
    REFERENCES applicability_groups(group_id, entry_id, version),
  CHECK ((operator IN ('exists','absent') AND value IS NULL) OR
         (operator NOT IN ('exists','absent') AND value IS NOT NULL))
);

CREATE TABLE content_sections (
  section_id TEXT NOT NULL,
  entry_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  heading TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (section_id, entry_id, version),
  FOREIGN KEY (entry_id, version) REFERENCES entry_versions(entry_id, version)
);

CREATE TABLE entry_links (
  link_id TEXT PRIMARY KEY,
  from_entry_id TEXT NOT NULL,
  from_version INTEGER NOT NULL,
  to_entry_id TEXT NOT NULL,
  to_version INTEGER NOT NULL,
  link_type TEXT NOT NULL CHECK (link_type IN
    ('depends_on','alternative_to','compatible_with','incompatible_with',
     'related','fixture_for','part_of')),
  UNIQUE (link_id, from_entry_id, from_version),
  FOREIGN KEY (from_entry_id, from_version) REFERENCES entry_versions(entry_id, version),
  FOREIGN KEY (to_entry_id, to_version) REFERENCES entry_versions(entry_id, version)
);

CREATE TABLE source_refs (
  source_ref_id TEXT PRIMARY KEY,
  entry_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  source_type TEXT NOT NULL CHECK (source_type IN
    ('documentation','standard','release_notes','repository','curator_authored','other')),
  source_uri TEXT NOT NULL CHECK (length(trim(source_uri)) > 0),
  source_version TEXT NOT NULL CHECK (length(trim(source_version)) > 0),
  retrieved_at TEXT NOT NULL,
  target_condition_id TEXT,
  target_section_id TEXT,
  target_link_id TEXT,
  CHECK ((target_condition_id IS NOT NULL) + (target_section_id IS NOT NULL) +
         (target_link_id IS NOT NULL) = 1),
  FOREIGN KEY (entry_id, version) REFERENCES entry_versions(entry_id, version),
  FOREIGN KEY (target_condition_id, entry_id, version)
    REFERENCES applicability_conditions(condition_id, entry_id, version),
  FOREIGN KEY (target_section_id, entry_id, version)
    REFERENCES content_sections(section_id, entry_id, version),
  FOREIGN KEY (target_link_id, entry_id, version)
    REFERENCES entry_links(link_id, from_entry_id, from_version)
);

CREATE UNIQUE INDEX uq_source_condition ON source_refs (
  entry_id, version, source_type, source_uri, source_version, target_condition_id
) WHERE target_condition_id IS NOT NULL;
CREATE UNIQUE INDEX uq_source_section ON source_refs (
  entry_id, version, source_type, source_uri, source_version, target_section_id
) WHERE target_section_id IS NOT NULL;
CREATE UNIQUE INDEX uq_source_link ON source_refs (
  entry_id, version, source_type, source_uri, source_version, target_link_id
) WHERE target_link_id IS NOT NULL;

CREATE TABLE source_verifications (
  source_ref_id TEXT NOT NULL REFERENCES source_refs(source_ref_id),
  seq INTEGER NOT NULL CHECK (seq >= 1),
  result TEXT NOT NULL CHECK (result IN ('verified','not_verified','contradicted')),
  verified_by TEXT NOT NULL CHECK (length(trim(verified_by)) > 0),
  verified_at TEXT NOT NULL,
  checked_source_sha256 TEXT NOT NULL CHECK (
    length(checked_source_sha256) = 64 AND checked_source_sha256 NOT GLOB '*[^0-9a-f]*'),
  excerpt TEXT,
  PRIMARY KEY (source_ref_id, seq),
  CHECK (result <> 'verified' OR (excerpt IS NOT NULL AND length(trim(excerpt)) > 0))
);

CREATE TABLE open_questions (
  question_id TEXT PRIMARY KEY,
  entry_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  question TEXT NOT NULL CHECK (length(trim(question)) > 0),
  evidence_needed TEXT NOT NULL CHECK (length(trim(evidence_needed)) > 0),
  FOREIGN KEY (entry_id, version) REFERENCES entry_versions(entry_id, version)
);

CREATE TRIGGER status_valid_insert BEFORE INSERT ON version_status
BEGIN
  SELECT CASE WHEN NEW.seq <> COALESCE((SELECT MAX(seq) FROM version_status
             WHERE entry_id=NEW.entry_id AND version=NEW.version), 0) + 1
     THEN RAISE(ABORT, 'status sequence gap') END;
  SELECT CASE WHEN NEW.seq=1 AND NEW.status <> 'draft'
     THEN RAISE(ABORT, 'initial status must be draft') END;
  SELECT CASE WHEN NEW.seq>1 AND NOT (
    (SELECT status FROM version_status WHERE entry_id=NEW.entry_id AND version=NEW.version
     ORDER BY seq DESC LIMIT 1) = 'draft' AND NEW.status IN ('published','deprecated')
    OR
    (SELECT status FROM version_status WHERE entry_id=NEW.entry_id AND version=NEW.version
     ORDER BY seq DESC LIMIT 1) = 'published' AND NEW.status='deprecated'
  ) THEN RAISE(ABORT, 'invalid status transition') END;
END;

CREATE TRIGGER verification_valid_insert BEFORE INSERT ON source_verifications
BEGIN
  SELECT CASE WHEN NEW.seq <> COALESCE((SELECT MAX(seq) FROM source_verifications
      WHERE source_ref_id=NEW.source_ref_id), 0) + 1
    THEN RAISE(ABORT, 'verification sequence gap') END;
  SELECT CASE WHEN lower(trim(NEW.verified_by)) = (
      SELECT lower(trim(v.created_by)) FROM entry_versions v
      JOIN source_refs r ON r.entry_id=v.entry_id AND r.version=v.version
      WHERE r.source_ref_id=NEW.source_ref_id)
    THEN RAISE(ABORT, 'creator cannot verify own version') END;
END;

CREATE TRIGGER freeze_tags BEFORE INSERT ON match_tags WHEN
  COALESCE((SELECT status FROM version_status WHERE entry_id=NEW.entry_id AND version=NEW.version
    ORDER BY seq DESC LIMIT 1), 'missing') <> 'draft'
BEGIN SELECT RAISE(ABORT, 'manifest is not draft'); END;
CREATE TRIGGER freeze_groups BEFORE INSERT ON applicability_groups WHEN
  COALESCE((SELECT status FROM version_status WHERE entry_id=NEW.entry_id AND version=NEW.version
    ORDER BY seq DESC LIMIT 1), 'missing') <> 'draft'
BEGIN SELECT RAISE(ABORT, 'manifest is not draft'); END;
CREATE TRIGGER freeze_conditions BEFORE INSERT ON applicability_conditions WHEN
  COALESCE((SELECT status FROM version_status WHERE entry_id=NEW.entry_id AND version=NEW.version
    ORDER BY seq DESC LIMIT 1), 'missing') <> 'draft'
BEGIN SELECT RAISE(ABORT, 'manifest is not draft'); END;
CREATE TRIGGER freeze_sections BEFORE INSERT ON content_sections WHEN
  COALESCE((SELECT status FROM version_status WHERE entry_id=NEW.entry_id AND version=NEW.version
    ORDER BY seq DESC LIMIT 1), 'missing') <> 'draft'
BEGIN SELECT RAISE(ABORT, 'manifest is not draft'); END;
CREATE TRIGGER freeze_links BEFORE INSERT ON entry_links WHEN
  COALESCE((SELECT status FROM version_status WHERE entry_id=NEW.from_entry_id AND version=NEW.from_version
    ORDER BY seq DESC LIMIT 1), 'missing') <> 'draft'
BEGIN SELECT RAISE(ABORT, 'manifest is not draft'); END;
CREATE TRIGGER freeze_refs BEFORE INSERT ON source_refs WHEN
  COALESCE((SELECT status FROM version_status WHERE entry_id=NEW.entry_id AND version=NEW.version
    ORDER BY seq DESC LIMIT 1), 'missing') <> 'draft'
BEGIN SELECT RAISE(ABORT, 'manifest is not draft'); END;
CREATE TRIGGER freeze_questions BEFORE INSERT ON open_questions WHEN
  COALESCE((SELECT status FROM version_status WHERE entry_id=NEW.entry_id AND version=NEW.version
    ORDER BY seq DESC LIMIT 1), 'missing') <> 'draft'
BEGIN SELECT RAISE(ABORT, 'manifest is not draft'); END;

CREATE TRIGGER no_update_entries BEFORE UPDATE ON entries BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER no_delete_entries BEFORE DELETE ON entries BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER no_update_entry_versions BEFORE UPDATE ON entry_versions BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER no_delete_entry_versions BEFORE DELETE ON entry_versions BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER no_update_version_status BEFORE UPDATE ON version_status BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER no_delete_version_status BEFORE DELETE ON version_status BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER no_update_match_tags BEFORE UPDATE ON match_tags BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER no_delete_match_tags BEFORE DELETE ON match_tags BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER no_update_applicability_groups BEFORE UPDATE ON applicability_groups BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER no_delete_applicability_groups BEFORE DELETE ON applicability_groups BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER no_update_applicability_conditions BEFORE UPDATE ON applicability_conditions BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER no_delete_applicability_conditions BEFORE DELETE ON applicability_conditions BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER no_update_content_sections BEFORE UPDATE ON content_sections BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER no_delete_content_sections BEFORE DELETE ON content_sections BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER no_update_entry_links BEFORE UPDATE ON entry_links BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER no_delete_entry_links BEFORE DELETE ON entry_links BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER no_update_source_refs BEFORE UPDATE ON source_refs BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER no_delete_source_refs BEFORE DELETE ON source_refs BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER no_update_source_verifications BEFORE UPDATE ON source_verifications BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER no_delete_source_verifications BEFORE DELETE ON source_verifications BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER no_update_open_questions BEFORE UPDATE ON open_questions BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER no_delete_open_questions BEFORE DELETE ON open_questions BEGIN SELECT RAISE(ABORT, 'append-only'); END;

CREATE VIEW version_current_status AS
SELECT entry_id, version, status, seq
FROM (
  SELECT entry_id, version, status, seq,
   ROW_NUMBER() OVER (PARTITION BY entry_id, version ORDER BY seq DESC) AS rn
  FROM version_status
) WHERE rn=1;

CREATE VIEW published_versions AS
WITH publication_frontier AS (
  SELECT entry_id, MAX(version) AS version FROM version_status
  WHERE status='published' GROUP BY entry_id
)
SELECT e.entry_id, e.entry_type, v.version, v.title, v.content_path,
       v.content_sha256, v.manifest_sha256
FROM publication_frontier f
JOIN entry_versions v ON v.entry_id=f.entry_id AND v.version=f.version
JOIN entries e ON e.entry_id=v.entry_id
JOIN version_current_status s ON s.entry_id=v.entry_id AND s.version=v.version
WHERE s.status='published';

CREATE VIEW pinnable_versions AS
SELECT v.* FROM entry_versions v
JOIN version_current_status s ON s.entry_id=v.entry_id AND s.version=v.version
WHERE s.status IN ('published','deprecated')
 AND EXISTS (SELECT 1 FROM version_status h WHERE h.entry_id=v.entry_id
  AND h.version=v.version AND h.status='published');

CREATE VIEW source_ref_current AS
SELECT r.source_ref_id, r.entry_id, r.version, r.source_type, r.source_uri,
       r.source_version, r.target_condition_id, r.target_section_id, r.target_link_id,
       v.seq, COALESCE(v.result,'unverified') AS result,
       v.verified_by, v.verified_at, v.checked_source_sha256, v.excerpt
FROM source_refs r
LEFT JOIN source_verifications v ON v.source_ref_id=r.source_ref_id
 AND v.seq=(SELECT MAX(v2.seq) FROM source_verifications v2 WHERE v2.source_ref_id=r.source_ref_id);
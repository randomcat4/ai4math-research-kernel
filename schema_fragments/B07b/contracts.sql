CREATE TABLE product_contracts(
  contract_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  current_version INTEGER NOT NULL CHECK(current_version>0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(run_id,contract_id)
) STRICT;

CREATE TABLE product_contract_versions(
  contract_id TEXT NOT NULL REFERENCES product_contracts(contract_id) ON DELETE RESTRICT,
  version INTEGER NOT NULL CHECK(version>0),
  state TEXT NOT NULL CHECK(state IN (
    'DRAFT','AMBIGUOUS','CONFIRMED','PENDING_INVALIDATION','SUPERSEDED'
  )),
  content_json TEXT NOT NULL CHECK(json_valid(content_json) AND json_type(content_json)='object'),
  content_digest TEXT NOT NULL CHECK(length(content_digest)=64),
  supersedes_version INTEGER,
  confirmed_by TEXT,
  confirmed_at TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(contract_id,version),
  FOREIGN KEY(contract_id,supersedes_version)
    REFERENCES product_contract_versions(contract_id,version) ON DELETE RESTRICT,
  CHECK(
    (state IN ('CONFIRMED','SUPERSEDED') AND confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL) OR
    (state NOT IN ('CONFIRMED','SUPERSEDED') AND confirmed_by IS NULL AND confirmed_at IS NULL)
  )
) WITHOUT ROWID, STRICT;

CREATE TABLE product_contract_ambiguities(
  ambiguity_id TEXT PRIMARY KEY,
  contract_id TEXT NOT NULL,
  contract_version INTEGER NOT NULL,
  field_path TEXT NOT NULL,
  description TEXT NOT NULL,
  options_json TEXT NOT NULL CHECK(json_valid(options_json) AND json_type(options_json)='array'),
  state TEXT NOT NULL CHECK(state IN ('OPEN','RESOLVED')),
  selected_option TEXT,
  resolved_by TEXT,
  resolved_at TEXT,
  FOREIGN KEY(contract_id,contract_version)
    REFERENCES product_contract_versions(contract_id,version) ON DELETE RESTRICT,
  CHECK(
    (state='OPEN' AND selected_option IS NULL AND resolved_by IS NULL AND resolved_at IS NULL) OR
    (state='RESOLVED' AND selected_option IS NOT NULL AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)
  ),
  UNIQUE(contract_id,contract_version,field_path)
) STRICT;

CREATE TABLE product_contract_material_references(
  reference_id TEXT PRIMARY KEY,
  contract_id TEXT NOT NULL,
  contract_version INTEGER NOT NULL,
  field_path TEXT NOT NULL,
  anchor_id TEXT NOT NULL REFERENCES product_material_anchors(anchor_id) ON DELETE RESTRICT,
  anchor_kind TEXT NOT NULL CHECK(anchor_kind IN ('PAGE_SEGMENT','FORMULA')),
  excerpt_digest TEXT NOT NULL CHECK(length(excerpt_digest)=64),
  acceptance_state TEXT NOT NULL CHECK(acceptance_state IN ('PROPOSED','USER_ACCEPTED')),
  accepted_by TEXT,
  accepted_at TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(contract_id,contract_version)
    REFERENCES product_contract_versions(contract_id,version) ON DELETE RESTRICT,
  CHECK(
    (acceptance_state='PROPOSED' AND accepted_by IS NULL AND accepted_at IS NULL) OR
    (acceptance_state='USER_ACCEPTED' AND accepted_by IS NOT NULL AND accepted_at IS NOT NULL)
  ),
  UNIQUE(contract_id,contract_version,field_path,anchor_id)
) STRICT;

CREATE TABLE product_contract_authority_dependencies(
  contract_id TEXT NOT NULL,
  contract_version INTEGER NOT NULL,
  field_path TEXT NOT NULL,
  object_kind TEXT NOT NULL CHECK(object_kind IN (
    'CHECKPOINT','QUEUE','TOOL_FEEDBACK','COMPOSITION','REVIEW','WITNESS','PUBLICATION'
  )),
  object_id TEXT NOT NULL,
  stable_label TEXT NOT NULL,
  object_digest TEXT NOT NULL CHECK(length(object_digest)=64),
  reopened_obligation_id TEXT NOT NULL,
  PRIMARY KEY(contract_id,contract_version,field_path,object_kind,object_id),
  FOREIGN KEY(contract_id,contract_version)
    REFERENCES product_contract_versions(contract_id,version) ON DELETE RESTRICT
) WITHOUT ROWID, STRICT;

CREATE TABLE product_contract_revision_previews(
  preview_id TEXT PRIMARY KEY,
  contract_id TEXT NOT NULL,
  base_version INTEGER NOT NULL,
  proposed_content_json TEXT NOT NULL
    CHECK(json_valid(proposed_content_json) AND json_type(proposed_content_json)='object'),
  proposed_content_digest TEXT NOT NULL CHECK(length(proposed_content_digest)=64),
  changed_fields_json TEXT NOT NULL
    CHECK(json_valid(changed_fields_json) AND json_type(changed_fields_json)='array'),
  affected_objects_json TEXT NOT NULL
    CHECK(json_valid(affected_objects_json) AND json_type(affected_objects_json)='array'),
  preserved_sibling_ids_json TEXT NOT NULL
    CHECK(json_valid(preserved_sibling_ids_json) AND json_type(preserved_sibling_ids_json)='array'),
  reopened_obligation_ids_json TEXT NOT NULL
    CHECK(json_valid(reopened_obligation_ids_json) AND json_type(reopened_obligation_ids_json)='array'),
  preview_digest TEXT NOT NULL CHECK(length(preview_digest)=64),
  state TEXT NOT NULL CHECK(state IN ('ACTIVE','APPLYING','APPLIED','STALE')),
  created_at TEXT NOT NULL,
  applied_at TEXT,
  FOREIGN KEY(contract_id,base_version)
    REFERENCES product_contract_versions(contract_id,version) ON DELETE RESTRICT
) STRICT;

CREATE TABLE product_contract_revision_invalidations(
  preview_id TEXT PRIMARY KEY
    REFERENCES product_contract_revision_previews(preview_id) ON DELETE RESTRICT,
  contract_id TEXT NOT NULL,
  base_version INTEGER NOT NULL,
  new_version INTEGER NOT NULL,
  kernel_event_id TEXT NOT NULL UNIQUE,
  research_revision INTEGER NOT NULL CHECK(research_revision>0),
  invalidation_intent_json TEXT NOT NULL
    CHECK(json_valid(invalidation_intent_json) AND json_type(invalidation_intent_json)='object'),
  state TEXT NOT NULL CHECK(state IN ('PENDING','APPLIED')),
  created_at TEXT NOT NULL,
  applied_at TEXT,
  FOREIGN KEY(contract_id,base_version)
    REFERENCES product_contract_versions(contract_id,version) ON DELETE RESTRICT,
  FOREIGN KEY(contract_id,new_version)
    REFERENCES product_contract_versions(contract_id,version) ON DELETE RESTRICT
) STRICT;

CREATE INDEX product_contract_versions_state
ON product_contract_versions(contract_id,state,version);
CREATE INDEX product_contract_references_version
ON product_contract_material_references(contract_id,contract_version,field_path);
CREATE INDEX product_contract_revision_pending
ON product_contract_revision_invalidations(state,created_at,preview_id);

CREATE TABLE product_revocation_previews(
  preview_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  target_fact_id TEXT NOT NULL,
  target_fact_digest TEXT NOT NULL CHECK(length(target_fact_digest) = 64),
  preview_revision INTEGER NOT NULL CHECK(preview_revision >= 0),
  contract_version INTEGER NOT NULL CHECK(contract_version >= 1),
  affected_fact_ids_json TEXT NOT NULL
    CHECK(json_valid(affected_fact_ids_json) AND json_type(affected_fact_ids_json) = 'array'),
  preserved_sibling_ids_json TEXT NOT NULL
    CHECK(json_valid(preserved_sibling_ids_json) AND json_type(preserved_sibling_ids_json) = 'array'),
  reopened_obligation_ids_json TEXT NOT NULL
    CHECK(json_valid(reopened_obligation_ids_json) AND json_type(reopened_obligation_ids_json) = 'array'),
  preview_digest TEXT NOT NULL UNIQUE CHECK(length(preview_digest) = 64),
  state TEXT NOT NULL CHECK(state IN ('ACTIVE','STALE','CONSUMED')),
  created_at TEXT NOT NULL,
  stale_at TEXT,
  consumed_at TEXT,
  kernel_event_id TEXT,
  CHECK(
    (state = 'ACTIVE' AND stale_at IS NULL AND consumed_at IS NULL AND kernel_event_id IS NULL) OR
    (state = 'STALE' AND stale_at IS NOT NULL AND consumed_at IS NULL AND kernel_event_id IS NULL) OR
    (state = 'CONSUMED' AND stale_at IS NULL AND consumed_at IS NOT NULL
      AND kernel_event_id IS NOT NULL)
  )
) STRICT;

CREATE TABLE product_revocation_dependencies(
  object_kind TEXT NOT NULL,
  object_id TEXT NOT NULL,
  fact_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(object_kind,object_id,fact_id),
  FOREIGN KEY(object_kind,object_id)
    REFERENCES product_authority_bindings(object_kind,object_id) ON DELETE RESTRICT
) WITHOUT ROWID, STRICT;

CREATE TABLE product_revocation_recoveries(
  recovery_id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  revoked_target_fact_id TEXT NOT NULL,
  replacement_fact_id TEXT NOT NULL,
  replacement_fact_digest TEXT NOT NULL CHECK(length(replacement_fact_digest) = 64),
  restored_object_kind TEXT NOT NULL,
  restored_object_id TEXT NOT NULL,
  kernel_revision INTEGER NOT NULL CHECK(kernel_revision >= 1),
  kernel_receipt_id TEXT NOT NULL,
  kernel_event_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(kernel_event_id,restored_object_kind,restored_object_id),
  FOREIGN KEY(restored_object_kind,restored_object_id)
    REFERENCES product_authority_bindings(object_kind,object_id) ON DELETE RESTRICT
) STRICT;

CREATE INDEX product_revocation_preview_scope
ON product_revocation_previews(run_id,state,preview_revision,preview_id);

CREATE INDEX product_revocation_dependencies_fact
ON product_revocation_dependencies(run_id,fact_id,object_kind,object_id);

CREATE INDEX product_revocation_recoveries_object
ON product_revocation_recoveries(run_id,restored_object_kind,restored_object_id,kernel_revision);

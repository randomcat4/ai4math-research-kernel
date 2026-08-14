CREATE TABLE product_authority_invalidation_ledger(
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  kernel_event_id TEXT NOT NULL UNIQUE,
  run_id TEXT NOT NULL,
  research_revision INTEGER NOT NULL CHECK(research_revision >= 1),
  intent_digest TEXT NOT NULL CHECK(length(intent_digest) = 64),
  intent_json TEXT NOT NULL CHECK(json_valid(intent_json) AND json_type(intent_json) = 'object'),
  recorded_at TEXT NOT NULL,
  UNIQUE(run_id,research_revision)
) STRICT;

CREATE TABLE product_authority_invalidation_watermarks(
  run_id TEXT PRIMARY KEY,
  recorded_sequence INTEGER NOT NULL DEFAULT 0 CHECK(recorded_sequence >= 0),
  recorded_revision INTEGER NOT NULL DEFAULT 0 CHECK(recorded_revision >= 0),
  processed_sequence INTEGER NOT NULL DEFAULT 0 CHECK(processed_sequence >= 0),
  processed_revision INTEGER NOT NULL DEFAULT 0 CHECK(processed_revision >= 0),
  updated_at TEXT NOT NULL,
  CHECK(processed_sequence <= recorded_sequence),
  CHECK(processed_revision <= recorded_revision)
) STRICT;

CREATE TABLE product_authority_bindings(
  object_kind TEXT NOT NULL CHECK(object_kind IN (
    'CHECKPOINT','QUEUE','TOOL_FEEDBACK','COMPOSITION','REVIEW','WITNESS','PUBLICATION'
  )),
  object_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  contract_version INTEGER NOT NULL CHECK(contract_version >= 1),
  bound_revision INTEGER NOT NULL CHECK(bound_revision >= 0),
  stable_label TEXT NOT NULL,
  object_digest TEXT NOT NULL CHECK(length(object_digest) = 64),
  state TEXT NOT NULL CHECK(state IN ('VALID','INVALIDATED')),
  invalidated_by_event_id TEXT REFERENCES product_authority_invalidation_ledger(kernel_event_id)
    ON DELETE RESTRICT,
  invalidated_at TEXT,
  invalidation_reason TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(object_kind,object_id),
  UNIQUE(run_id,object_kind,stable_label),
  CHECK(
    (state = 'VALID' AND invalidated_by_event_id IS NULL AND invalidated_at IS NULL
      AND invalidation_reason IS NULL) OR
    (state = 'INVALIDATED' AND invalidated_by_event_id IS NOT NULL AND invalidated_at IS NOT NULL
      AND invalidation_reason IS NOT NULL)
  )
) WITHOUT ROWID, STRICT;

CREATE TABLE product_authority_invalidation_materializations(
  kernel_event_id TEXT NOT NULL
    REFERENCES product_authority_invalidation_ledger(kernel_event_id) ON DELETE RESTRICT,
  object_kind TEXT NOT NULL,
  object_id TEXT NOT NULL,
  previous_state TEXT NOT NULL CHECK(previous_state IN ('VALID','INVALIDATED')),
  resulting_state TEXT NOT NULL CHECK(resulting_state = 'INVALIDATED'),
  materialized_at TEXT NOT NULL,
  PRIMARY KEY(kernel_event_id,object_kind,object_id),
  FOREIGN KEY(object_kind,object_id)
    REFERENCES product_authority_bindings(object_kind,object_id) ON DELETE RESTRICT
) WITHOUT ROWID, STRICT;

CREATE INDEX product_authority_invalidation_pending
ON product_authority_invalidation_ledger(run_id,sequence,research_revision);

CREATE INDEX product_authority_bindings_run_state
ON product_authority_bindings(run_id,state,object_kind,object_id);

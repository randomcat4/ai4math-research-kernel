CREATE TABLE product_claims(
  claim_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  contract_version INTEGER NOT NULL CHECK(contract_version >= 1),
  kernel_revision_created INTEGER NOT NULL CHECK(kernel_revision_created >= 0),
  kernel_revision_decided INTEGER CHECK(kernel_revision_decided >= kernel_revision_created),
  statement TEXT NOT NULL CHECK(length(statement) > 0),
  statement_digest TEXT NOT NULL CHECK(length(statement_digest) = 64),
  claim_kind TEXT NOT NULL CHECK(claim_kind IN ('ROOT','LEMMA','DEFINITION','COUNTEREXAMPLE','COMPUTATION')),
  lifecycle TEXT NOT NULL CHECK(lifecycle IN ('PENDING_VERIFICATION','ACCEPTED','REJECTED','INVALIDATED','REVOKED')),
  authority_class TEXT NOT NULL CHECK(authority_class IN ('RESEARCH_HISTORY','VERIFIED')),
  promotion_eligible INTEGER NOT NULL CHECK(promotion_eligible IN (0,1)),
  source_binding_json TEXT NOT NULL CHECK(json_valid(source_binding_json) AND json_type(source_binding_json) = 'object'),
  work_item_id TEXT NOT NULL,
  worker_run_id TEXT NOT NULL,
  attempt_id TEXT NOT NULL,
  submitted_by_subject_id TEXT NOT NULL,
  route_id TEXT,
  supersedes_claim_id TEXT REFERENCES product_claims(claim_id) ON DELETE RESTRICT,
  superseded_by_claim_id TEXT REFERENCES product_claims(claim_id) ON DELETE RESTRICT,
  stable_label TEXT NOT NULL,
  public_summary TEXT,
  repair_feedback TEXT,
  validation_id TEXT,
  kernel_receipt_id TEXT,
  kernel_event_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(run_id,worker_run_id,attempt_id),
  UNIQUE(run_id,stable_label),
  UNIQUE(kernel_receipt_id),
  UNIQUE(kernel_event_id),
  CHECK(
    (lifecycle = 'ACCEPTED' AND authority_class = 'VERIFIED' AND promotion_eligible = 1
      AND validation_id IS NOT NULL AND kernel_receipt_id IS NOT NULL AND kernel_event_id IS NOT NULL
      AND repair_feedback IS NULL AND kernel_revision_decided IS NOT NULL) OR
    (lifecycle = 'REJECTED' AND authority_class = 'RESEARCH_HISTORY' AND promotion_eligible = 0
      AND validation_id IS NOT NULL AND kernel_receipt_id IS NOT NULL AND kernel_event_id IS NOT NULL
      AND repair_feedback IS NOT NULL AND kernel_revision_decided IS NOT NULL) OR
    (lifecycle IN ('PENDING_VERIFICATION','INVALIDATED','REVOKED')
      AND authority_class = 'RESEARCH_HISTORY' AND promotion_eligible = 0)
  )
) STRICT;

CREATE TABLE product_claim_predecessors(
  claim_id TEXT NOT NULL REFERENCES product_claims(claim_id) ON DELETE RESTRICT,
  fact_id TEXT NOT NULL REFERENCES product_claims(claim_id) ON DELETE RESTRICT,
  ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
  PRIMARY KEY(claim_id,fact_id),
  UNIQUE(claim_id,ordinal),
  CHECK(claim_id <> fact_id)
) STRICT;

CREATE TABLE product_claim_evidence(
  claim_id TEXT NOT NULL REFERENCES product_claims(claim_id) ON DELETE RESTRICT,
  ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
  binding_json TEXT NOT NULL CHECK(json_valid(binding_json) AND json_type(binding_json) = 'object'),
  PRIMARY KEY(claim_id,ordinal)
) STRICT;

CREATE TABLE product_claim_validations(
  validation_id TEXT PRIMARY KEY,
  claim_id TEXT NOT NULL REFERENCES product_claims(claim_id) ON DELETE RESTRICT,
  backend TEXT NOT NULL CHECK(backend IN ('LEAN','DETERMINISTIC_CHECKER','MANAGED_HUMAN','SOFT_VERIFIER')),
  verdict TEXT NOT NULL CHECK(verdict IN ('ACCEPTED','REJECTED')),
  verifier_reference_id TEXT NOT NULL,
  selected_subgraph_digest TEXT NOT NULL CHECK(length(selected_subgraph_digest) = 64),
  authority_effect TEXT NOT NULL,
  promotion_eligible INTEGER NOT NULL CHECK(promotion_eligible IN (0,1)),
  result_json TEXT NOT NULL CHECK(json_valid(result_json) AND json_type(result_json) = 'object'),
  kernel_receipt_id TEXT,
  kernel_event_id TEXT,
  created_at TEXT NOT NULL,
  CHECK(
    (kernel_receipt_id IS NULL AND kernel_event_id IS NULL) OR
    (kernel_receipt_id IS NOT NULL AND kernel_event_id IS NOT NULL)
  )
) STRICT;

CREATE TABLE product_claim_reuse(
  reuse_id INTEGER PRIMARY KEY,
  claim_id TEXT NOT NULL REFERENCES product_claims(claim_id) ON DELETE RESTRICT,
  reused_by_subject_id TEXT NOT NULL,
  query TEXT NOT NULL,
  created_at TEXT NOT NULL
) STRICT;

CREATE VIRTUAL TABLE product_claim_fts USING fts5(
  statement,
  stable_label,
  content='product_claims',
  content_rowid='rowid',
  tokenize='unicode61'
);

CREATE TRIGGER product_claim_fts_insert AFTER INSERT ON product_claims BEGIN
  INSERT INTO product_claim_fts(rowid,statement,stable_label)
  VALUES(new.rowid,new.statement,new.stable_label);
END;

CREATE TRIGGER product_claim_fts_delete AFTER DELETE ON product_claims BEGIN
  INSERT INTO product_claim_fts(product_claim_fts,rowid,statement,stable_label)
  VALUES('delete',old.rowid,old.statement,old.stable_label);
END;

CREATE TRIGGER product_claim_fts_update AFTER UPDATE OF statement,stable_label ON product_claims BEGIN
  INSERT INTO product_claim_fts(product_claim_fts,rowid,statement,stable_label)
  VALUES('delete',old.rowid,old.statement,old.stable_label);
  INSERT INTO product_claim_fts(rowid,statement,stable_label)
  VALUES(new.rowid,new.statement,new.stable_label);
END;

CREATE INDEX product_claim_scope_lifecycle
ON product_claims(run_id,contract_version,lifecycle,claim_id);

CREATE INDEX product_claim_lineage
ON product_claims(supersedes_claim_id,superseded_by_claim_id);

CREATE INDEX product_claim_reuse_subject
ON product_claim_reuse(reused_by_subject_id,created_at,reuse_id);

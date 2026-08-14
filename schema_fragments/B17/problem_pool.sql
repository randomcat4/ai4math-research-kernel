CREATE TABLE product_problem_pools(
  problem_pool_id TEXT PRIMARY KEY,
  deployment_id TEXT NOT NULL,
  date_from TEXT NOT NULL,
  date_to TEXT NOT NULL,
  subjects_json TEXT NOT NULL CHECK(json_valid(subjects_json) AND json_type(subjects_json)='array'),
  version_rule TEXT NOT NULL CHECK(version_rule IN ('LATEST_VISIBLE','ALL_VERSIONS')),
  withdrawal_rule TEXT NOT NULL CHECK(withdrawal_rule IN ('EXCLUDE_WITHDRAWN','INCLUDE_FLAGGED')),
  exclusion_rules_json TEXT NOT NULL CHECK(json_valid(exclusion_rules_json) AND json_type(exclusion_rules_json)='array'),
  state TEXT NOT NULL CHECK(state IN ('COLLECTING','HUMAN_AUDIT','FROZEN')),
  frozen_by TEXT,
  frozen_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK((state='FROZEN' AND frozen_by IS NOT NULL AND frozen_at IS NOT NULL) OR (state<>'FROZEN' AND frozen_by IS NULL AND frozen_at IS NULL))
) STRICT;

CREATE TABLE product_problem_pool_snapshots(
  problem_pool_id TEXT NOT NULL REFERENCES product_problem_pools(problem_pool_id) ON DELETE RESTRICT,
  snapshot_id TEXT NOT NULL REFERENCES product_source_snapshots(snapshot_id) ON DELETE RESTRICT,
  ordinal INTEGER NOT NULL CHECK(ordinal>=0),
  ingest_status TEXT NOT NULL CHECK(ingest_status IN ('INGESTED','FAILED','BLOCKED')),
  failure_code TEXT,
  PRIMARY KEY(problem_pool_id,snapshot_id),
  UNIQUE(problem_pool_id,ordinal),
  CHECK((ingest_status='INGESTED' AND failure_code IS NULL) OR (ingest_status<>'INGESTED' AND failure_code IS NOT NULL))
) WITHOUT ROWID, STRICT;

CREATE TABLE product_problem_source_records(
  source_record_id TEXT PRIMARY KEY,
  problem_pool_id TEXT NOT NULL REFERENCES product_problem_pools(problem_pool_id) ON DELETE RESTRICT,
  snapshot_id TEXT NOT NULL REFERENCES product_source_snapshots(snapshot_id) ON DELETE RESTRICT,
  arxiv_id TEXT,
  version INTEGER,
  title TEXT,
  summary TEXT,
  published_at TEXT,
  updated_at TEXT,
  subjects_json TEXT NOT NULL CHECK(json_valid(subjects_json) AND json_type(subjects_json)='array'),
  withdrawn INTEGER NOT NULL CHECK(withdrawn IN (0,1)),
  denominator_status TEXT NOT NULL CHECK(denominator_status IN ('INCLUDED','EXCLUDED','FAILED','BLOCKED')),
  reason_code TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(problem_pool_id,snapshot_id,arxiv_id,version,reason_code),
  CHECK((arxiv_id IS NULL AND version IS NULL) OR (arxiv_id IS NOT NULL AND version>0))
) STRICT;

CREATE TABLE product_problem_candidates(
  problem_candidate_id TEXT PRIMARY KEY,
  problem_pool_id TEXT NOT NULL REFERENCES product_problem_pools(problem_pool_id) ON DELETE RESTRICT,
  source_record_id TEXT NOT NULL UNIQUE REFERENCES product_problem_source_records(source_record_id) ON DELETE RESTRICT,
  arxiv_id TEXT NOT NULL,
  version INTEGER NOT NULL CHECK(version>0),
  marker_kind TEXT NOT NULL CHECK(marker_kind IN ('CONJECTURE','PROBLEM','QUESTION')),
  extracted_statement TEXT NOT NULL,
  dedupe_digest TEXT NOT NULL CHECK(length(dedupe_digest)=64),
  audit_status TEXT NOT NULL CHECK(audit_status IN ('HUMAN_AUDIT_PENDING','HUMAN_INCLUDED','HUMAN_EXCLUDED')),
  normalized_statement TEXT,
  definitions_json TEXT NOT NULL CHECK(json_valid(definitions_json) AND json_type(definitions_json)='array'),
  quantifiers_json TEXT NOT NULL CHECK(json_valid(quantifiers_json) AND json_type(quantifiers_json)='array'),
  hypotheses_json TEXT NOT NULL CHECK(json_valid(hypotheses_json) AND json_type(hypotheses_json)='array'),
  audited_by TEXT,
  audited_at TEXT,
  audit_note TEXT,
  importance_score INTEGER CHECK(importance_score BETWEEN 0 AND 100),
  verifiability_score INTEGER CHECK(verifiability_score BETWEEN 0 AND 100),
  bridge_potential_score INTEGER CHECK(bridge_potential_score BETWEEN 0 AND 100),
  estimated_cost_score INTEGER CHECK(estimated_cost_score BETWEEN 0 AND 100),
  recommendation_score INTEGER,
  recommendation_status TEXT NOT NULL CHECK(recommendation_status IN ('UNSCORED','RECOMMENDED','NOT_RECOMMENDED')),
  machine_certificate_status TEXT NOT NULL CHECK(machine_certificate_status IN ('NOT_ATTEMPTED','PRODUCED','FAILED')),
  heterogeneous_review_status TEXT NOT NULL CHECK(heterogeneous_review_status IN ('NOT_REQUESTED','PENDING','ACCEPTED','REJECTED')),
  expert_confirmation_status TEXT NOT NULL CHECK(expert_confirmation_status IN ('EXTERNAL_CONFIRMATION_PENDING','CONFIRMED','REJECTED')),
  author_confirmation_status TEXT NOT NULL CHECK(author_confirmation_status IN ('EXTERNAL_CONFIRMATION_PENDING','CONFIRMED','REJECTED','NOT_APPLICABLE')),
  created_run_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK(
    (audit_status='HUMAN_AUDIT_PENDING' AND normalized_statement IS NULL AND audited_by IS NULL AND audited_at IS NULL) OR
    (audit_status='HUMAN_INCLUDED' AND normalized_statement IS NOT NULL AND json_array_length(definitions_json)>0 AND json_array_length(quantifiers_json)>0 AND json_array_length(hypotheses_json)>0 AND audited_by IS NOT NULL AND audited_at IS NOT NULL) OR
    (audit_status='HUMAN_EXCLUDED' AND normalized_statement IS NULL AND audited_by IS NOT NULL AND audited_at IS NOT NULL AND audit_note IS NOT NULL)
  ),
  CHECK(
    (recommendation_status='UNSCORED' AND importance_score IS NULL AND verifiability_score IS NULL AND bridge_potential_score IS NULL AND estimated_cost_score IS NULL AND recommendation_score IS NULL) OR
    (recommendation_status<>'UNSCORED' AND importance_score IS NOT NULL AND verifiability_score IS NOT NULL AND bridge_potential_score IS NOT NULL AND estimated_cost_score IS NOT NULL AND recommendation_score IS NOT NULL)
  )
) STRICT;

CREATE TABLE product_problem_batch_commands(
  batch_id TEXT PRIMARY KEY,
  problem_pool_id TEXT NOT NULL REFERENCES product_problem_pools(problem_pool_id) ON DELETE RESTRICT,
  request_id TEXT NOT NULL UNIQUE,
  deployment_id TEXT NOT NULL,
  candidate_ids_json TEXT NOT NULL CHECK(json_valid(candidate_ids_json) AND json_type(candidate_ids_json)='array'),
  contract_template_artifact_id TEXT NOT NULL,
  contract_template_sha256 TEXT NOT NULL CHECK(length(contract_template_sha256)=64),
  per_run_budget_json TEXT NOT NULL CHECK(json_valid(per_run_budget_json) AND json_type(per_run_budget_json)='object'),
  labels_json TEXT NOT NULL CHECK(json_valid(labels_json) AND json_type(labels_json)='array'),
  batch_receipt_id TEXT NOT NULL UNIQUE,
  batch_receipt_state TEXT NOT NULL CHECK(batch_receipt_state IN ('PENDING','DECIDED')),
  state TEXT NOT NULL CHECK(state IN ('DISPATCHED','RUNNING','COMPLETED','BLOCKED')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE product_problem_batch_runs(
  batch_id TEXT NOT NULL REFERENCES product_problem_batch_commands(batch_id) ON DELETE RESTRICT,
  problem_candidate_id TEXT NOT NULL REFERENCES product_problem_candidates(problem_candidate_id) ON DELETE RESTRICT,
  create_request_id TEXT NOT NULL UNIQUE,
  create_receipt_id TEXT NOT NULL UNIQUE,
  created_run_id TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  PRIMARY KEY(batch_id,problem_candidate_id)
) WITHOUT ROWID, STRICT;

CREATE INDEX product_problem_source_denominator ON product_problem_source_records(problem_pool_id,denominator_status,reason_code);
CREATE INDEX product_problem_candidate_recommendation ON product_problem_candidates(problem_pool_id,recommendation_status,recommendation_score,problem_candidate_id);
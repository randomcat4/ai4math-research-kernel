CREATE TABLE product_jobs(
  job_id TEXT PRIMARY KEY,
  receipt_id TEXT NOT NULL UNIQUE
    REFERENCES product_receipts(receipt_id) ON DELETE RESTRICT,
  scope_key TEXT NOT NULL,
  scope_kind TEXT NOT NULL CHECK(scope_kind IN ('GLOBAL','RUN','DEPLOYMENT')),
  run_id TEXT,
  deployment_id TEXT,
  kind TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  request_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN (
    'QUEUED','RUNNING','PAUSED','WAITING','CANCEL_REQUESTED',
    'CANCELLED','SUCCEEDED','FAILED','OUTCOME_UNKNOWN','STALE','INVALIDATED'
  )),
  retry_safety TEXT NOT NULL CHECK(retry_safety IN (
    'IDEMPOTENT','READ_ONLY','IDEMPOTENCY_KEY','MANUAL_ONLY'
  )),
  idempotency_key TEXT,
  lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation >= 0),
  current_checkpoint_id TEXT,
  worker_run_ids_json TEXT NOT NULL DEFAULT '[]'
    CHECK(json_valid(worker_run_ids_json) AND json_type(worker_run_ids_json) = 'array'),
  result_refs_json TEXT NOT NULL DEFAULT '[]'
    CHECK(json_valid(result_refs_json) AND json_type(result_refs_json) = 'array'),
  failure_code TEXT,
  authority_effect TEXT NOT NULL DEFAULT 'NONE' CHECK(authority_effect = 'NONE'),
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  CHECK(
    (scope_kind = 'GLOBAL' AND run_id IS NULL AND deployment_id IS NOT NULL) OR
    (scope_kind = 'RUN' AND run_id IS NOT NULL AND deployment_id IS NULL) OR
    (scope_kind = 'DEPLOYMENT' AND run_id IS NULL AND deployment_id IS NOT NULL)
  ),
  CHECK(
    (retry_safety = 'IDEMPOTENCY_KEY' AND idempotency_key IS NOT NULL) OR
    (retry_safety <> 'IDEMPOTENCY_KEY' AND idempotency_key IS NULL)
  ),
  CHECK(
    (state IN ('CANCELLED','SUCCEEDED','FAILED','OUTCOME_UNKNOWN','STALE','INVALIDATED')
      AND finished_at IS NOT NULL) OR
    (state NOT IN ('CANCELLED','SUCCEEDED','FAILED','OUTCOME_UNKNOWN','STALE','INVALIDATED')
      AND finished_at IS NULL)
  ),
  UNIQUE(scope_key, request_id)
) STRICT;

CREATE INDEX product_jobs_claim_queue
ON product_jobs(state, created_at, job_id);

CREATE INDEX product_jobs_scope_state
ON product_jobs(scope_kind, run_id, deployment_id, state, created_at);

CREATE TABLE product_job_checkpoints(
  checkpoint_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES product_jobs(job_id) ON DELETE CASCADE,
  research_revision INTEGER NOT NULL CHECK(research_revision >= 0),
  contract_version INTEGER NOT NULL CHECK(contract_version > 0),
  artifact_id TEXT NOT NULL,
  checkpoint_digest TEXT NOT NULL CHECK(length(checkpoint_digest) = 64),
  state TEXT NOT NULL CHECK(state IN ('ACTIVE','STALE','INVALIDATED')),
  invalidation_reason TEXT,
  created_at TEXT NOT NULL,
  invalidated_at TEXT,
  CHECK(
    (state = 'ACTIVE' AND invalidated_at IS NULL AND invalidation_reason IS NULL) OR
    (state <> 'ACTIVE' AND invalidated_at IS NOT NULL AND invalidation_reason IS NOT NULL)
  )
) STRICT;

CREATE INDEX product_job_checkpoint_binding
ON product_job_checkpoints(job_id, research_revision, contract_version, state);

CREATE TABLE product_job_leases(
  lease_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES product_jobs(job_id) ON DELETE CASCADE,
  lease_generation INTEGER NOT NULL CHECK(lease_generation > 0),
  holder_id TEXT NOT NULL,
  process_token TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('ACTIVE','RELEASED','EXPIRED')),
  claimed_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  released_at TEXT,
  CHECK(
    (state = 'ACTIVE' AND released_at IS NULL) OR
    (state <> 'ACTIVE' AND released_at IS NOT NULL)
  ),
  UNIQUE(job_id, lease_generation)
) STRICT;

CREATE UNIQUE INDEX product_job_one_active_lease
ON product_job_leases(job_id)
WHERE state = 'ACTIVE';

CREATE INDEX product_job_lease_recovery
ON product_job_leases(state, expires_at, job_id);

CREATE TABLE product_job_execution_receipts(
  execution_receipt_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES product_jobs(job_id) ON DELETE RESTRICT,
  lease_id TEXT NOT NULL UNIQUE REFERENCES product_job_leases(lease_id) ON DELETE RESTRICT,
  lease_generation INTEGER NOT NULL CHECK(lease_generation > 0),
  outcome TEXT NOT NULL CHECK(outcome IN (
    'SUCCEEDED','FAILED','CANCELLED','OUTCOME_UNKNOWN'
  )),
  exit_code INTEGER,
  result_refs_json TEXT NOT NULL
    CHECK(json_valid(result_refs_json) AND json_type(result_refs_json) = 'array'),
  failure_code TEXT,
  authority_effect TEXT NOT NULL CHECK(authority_effect = 'NONE'),
  received_at TEXT NOT NULL
) STRICT;

CREATE INDEX product_job_receipts_job
ON product_job_execution_receipts(job_id, received_at);

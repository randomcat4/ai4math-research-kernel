CREATE TABLE product_work_items(
  work_item_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  logical_key TEXT NOT NULL,
  work_kind TEXT NOT NULL,
  route_id TEXT,
  parent_work_item_id TEXT REFERENCES product_work_items(work_item_id) ON DELETE RESTRICT,
  assignment_summary TEXT NOT NULL,
  assignment_artifact_ids_json TEXT NOT NULL DEFAULT '[]'
    CHECK(json_valid(assignment_artifact_ids_json)
      AND json_type(assignment_artifact_ids_json) = 'array'),
  input_artifact_ids_json TEXT NOT NULL DEFAULT '[]'
    CHECK(json_valid(input_artifact_ids_json)
      AND json_type(input_artifact_ids_json) = 'array'),
  created_at TEXT NOT NULL,
  UNIQUE(run_id, logical_key)
) STRICT;

CREATE INDEX product_work_items_run
ON product_work_items(run_id, created_at, work_item_id);

CREATE TABLE product_worker_runs(
  worker_run_id TEXT PRIMARY KEY,
  work_item_id TEXT NOT NULL REFERENCES product_work_items(work_item_id) ON DELETE RESTRICT,
  ordinal INTEGER NOT NULL CHECK(ordinal > 0),
  worker_kind TEXT NOT NULL,
  role_id TEXT NOT NULL,
  parent_worker_run_id TEXT REFERENCES product_worker_runs(worker_run_id) ON DELETE RESTRICT,
  state TEXT NOT NULL CHECK(state IN (
    'QUEUED','RUNNING','WAITING_TOOL','WAITING_REVIEW','PAUSED',
    'CANCEL_REQUESTED','COMPLETED','FAILED','CANCELLED'
  )),
  process_token TEXT NOT NULL UNIQUE,
  budget_plan_json TEXT NOT NULL CHECK(json_valid(budget_plan_json)),
  usage_json TEXT NOT NULL DEFAULT '{}'
    CHECK(json_valid(usage_json) AND json_type(usage_json) = 'object'),
  checkpoint_id TEXT,
  enqueued_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  last_activity_at TEXT NOT NULL,
  stop_reason TEXT,
  UNIQUE(work_item_id, ordinal),
  CHECK(
    (state IN ('COMPLETED','FAILED','CANCELLED') AND finished_at IS NOT NULL) OR
    (state NOT IN ('COMPLETED','FAILED','CANCELLED') AND finished_at IS NULL)
  )
) STRICT;

CREATE INDEX product_worker_runs_work_history
ON product_worker_runs(work_item_id, ordinal, worker_run_id);

CREATE TABLE product_worker_attempts(
  attempt_id TEXT PRIMARY KEY,
  worker_run_id TEXT NOT NULL
    REFERENCES product_worker_runs(worker_run_id) ON DELETE RESTRICT,
  ordinal INTEGER NOT NULL CHECK(ordinal > 0),
  process_token TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK(state IN (
    'RUNNING','SUCCEEDED','FAILED','CANCELLED','OUTCOME_UNKNOWN'
  )),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  exit_code INTEGER,
  diagnostic_code TEXT,
  output_artifact_ids_json TEXT NOT NULL DEFAULT '[]'
    CHECK(json_valid(output_artifact_ids_json)
      AND json_type(output_artifact_ids_json) = 'array'),
  UNIQUE(worker_run_id, ordinal),
  CHECK(
    (state = 'RUNNING' AND finished_at IS NULL) OR
    (state <> 'RUNNING' AND finished_at IS NOT NULL)
  )
) STRICT;

CREATE INDEX product_worker_attempts_run_history
ON product_worker_attempts(worker_run_id, ordinal, attempt_id);

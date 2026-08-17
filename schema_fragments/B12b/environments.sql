CREATE TABLE product_managed_python_profiles(
  profile_id TEXT PRIMARY KEY,
  environment_digest TEXT NOT NULL CHECK(length(environment_digest) = 64),
  interpreter_path TEXT NOT NULL,
  interpreter_sha256 TEXT NOT NULL CHECK(length(interpreter_sha256) = 64),
  lock_artifact_id TEXT NOT NULL,
  lock_artifact_sha256 TEXT NOT NULL CHECK(length(lock_artifact_sha256) = 64),
  lock_artifact_byte_count INTEGER NOT NULL CHECK(lock_artifact_byte_count >= 0),
  lock_artifact_media_type TEXT NOT NULL,
  packages_json TEXT NOT NULL
    CHECK(json_valid(packages_json) AND json_type(packages_json) = 'object'),
  authority_ceiling TEXT NOT NULL CHECK(authority_ceiling IN (
    'NO_FACT_GRAPH_WRITE','SOFT_TOOL_RESULT'
  )),
  availability TEXT NOT NULL CHECK(availability IN ('AVAILABLE','UNAVAILABLE')),
  registered_at TEXT NOT NULL,
  UNIQUE(environment_digest)
) STRICT;

CREATE TABLE product_managed_python_executions(
  execution_id TEXT PRIMARY KEY,
  tool_run_id TEXT NOT NULL REFERENCES product_tool_runs(tool_run_id) ON DELETE RESTRICT,
  attempt_id TEXT NOT NULL UNIQUE
    REFERENCES product_tool_attempts(attempt_id) ON DELETE RESTRICT,
  job_id TEXT NOT NULL UNIQUE REFERENCES product_jobs(job_id) ON DELETE RESTRICT,
  profile_id TEXT NOT NULL
    REFERENCES product_managed_python_profiles(profile_id) ON DELETE RESTRICT,
  script_artifact_id TEXT NOT NULL,
  script_artifact_sha256 TEXT NOT NULL CHECK(length(script_artifact_sha256) = 64),
  script_artifact_byte_count INTEGER NOT NULL CHECK(script_artifact_byte_count >= 0),
  script_artifact_media_type TEXT NOT NULL,
  input_artifacts_json TEXT NOT NULL
    CHECK(json_valid(input_artifacts_json) AND json_type(input_artifacts_json) = 'array'),
  runtime_state TEXT NOT NULL CHECK(runtime_state IN (
    'STARTING','RUNNING','RECEIPT_RECORDED','ABANDONED'
  )),
  pid INTEGER,
  process_start_ticks INTEGER,
  output_artifact_ids_json TEXT NOT NULL DEFAULT '[]'
    CHECK(json_valid(output_artifact_ids_json) AND json_type(output_artifact_ids_json) = 'array'),
  public_log_artifact_id TEXT,
  failure_adjustment_json TEXT
    CHECK(failure_adjustment_json IS NULL OR (
      json_valid(failure_adjustment_json) AND json_type(failure_adjustment_json) = 'object'
    )),
  pending_tool_receipt_json TEXT
    CHECK(pending_tool_receipt_json IS NULL OR (
      json_valid(pending_tool_receipt_json) AND
      json_type(pending_tool_receipt_json) = 'object'
    )),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  CHECK(
    (runtime_state IN ('RECEIPT_RECORDED','ABANDONED') AND finished_at IS NOT NULL) OR
    (runtime_state IN ('STARTING','RUNNING') AND finished_at IS NULL)
  )
) STRICT;

CREATE INDEX product_managed_python_runtime
ON product_managed_python_executions(runtime_state, started_at, execution_id);

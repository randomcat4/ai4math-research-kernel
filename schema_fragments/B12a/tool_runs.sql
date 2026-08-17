CREATE TABLE product_tool_catalog(
  tool_id TEXT NOT NULL,
  tool_version TEXT NOT NULL,
  function_name TEXT NOT NULL,
  provider TEXT NOT NULL,
  build_version TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  function_schema_json TEXT NOT NULL
    CHECK(json_valid(function_schema_json) AND json_type(function_schema_json) = 'object'),
  function_schema_digest TEXT NOT NULL CHECK(length(function_schema_digest) = 64),
  availability TEXT NOT NULL CHECK(availability IN (
    'CONFIGURED_UNPROBED','AVAILABLE','SMOKE_ONLY',
    'PRODUCT_RECEIPT_AVAILABLE','UNAVAILABLE','EXTERNAL_BLOCKED'
  )),
  authority_ceiling TEXT NOT NULL CHECK(authority_ceiling IN (
    'NO_FACT_GRAPH_WRITE','SOFT_TOOL_RESULT','CERTIFICATE_REQUIRES_VALIDATION'
  )),
  registered_at TEXT NOT NULL,
  status_updated_at TEXT NOT NULL,
  PRIMARY KEY(tool_id, tool_version, function_name)
) WITHOUT ROWID, STRICT;

CREATE INDEX product_tool_catalog_status
ON product_tool_catalog(availability, provider, tool_id, tool_version, function_name);

CREATE TABLE product_tool_runs(
  tool_run_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  research_revision INTEGER NOT NULL CHECK(research_revision >= 0),
  contract_version INTEGER NOT NULL CHECK(contract_version > 0),
  request_id TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  tool_id TEXT NOT NULL,
  tool_version TEXT NOT NULL,
  function_name TEXT NOT NULL,
  function_schema_digest TEXT NOT NULL CHECK(length(function_schema_digest) = 64),
  arguments_artifact_id TEXT NOT NULL,
  arguments_artifact_sha256 TEXT NOT NULL CHECK(length(arguments_artifact_sha256) = 64),
  arguments_artifact_byte_count INTEGER NOT NULL CHECK(arguments_artifact_byte_count >= 0),
  arguments_artifact_media_type TEXT NOT NULL,
  input_artifact_ids_json TEXT NOT NULL
    CHECK(json_valid(input_artifact_ids_json) AND json_type(input_artifact_ids_json) = 'array'),
  resource_request_json TEXT NOT NULL
    CHECK(json_valid(resource_request_json) AND json_type(resource_request_json) = 'object'),
  authority_ceiling TEXT NOT NULL CHECK(authority_ceiling IN (
    'NO_FACT_GRAPH_WRITE','SOFT_TOOL_RESULT','CERTIFICATE_REQUIRES_VALIDATION'
  )),
  invocation_digest TEXT NOT NULL CHECK(length(invocation_digest) = 64),
  invocation_status TEXT NOT NULL CHECK(invocation_status IN (
    'QUEUED','RUNNING','WAITING','CANCEL_REQUESTED','CANCELLED',
    'SUCCEEDED','FAILED','OUTCOME_UNKNOWN','STALE','INVALIDATED'
  )),
  validation_status TEXT NOT NULL DEFAULT 'NOT_SUBMITTED' CHECK(validation_status IN (
    'NOT_SUBMITTED','VALIDATION_ACCEPTED','VALIDATION_REJECTED','STALE'
  )),
  validation_receipt_id TEXT,
  current_attempt_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(current_attempt_id) REFERENCES product_tool_attempts(attempt_id) ON DELETE RESTRICT,
  FOREIGN KEY(tool_id, tool_version, function_name)
    REFERENCES product_tool_catalog(tool_id, tool_version, function_name)
    ON DELETE RESTRICT,
  CHECK(
    (validation_status = 'NOT_SUBMITTED' AND validation_receipt_id IS NULL) OR
    (validation_status <> 'NOT_SUBMITTED' AND validation_receipt_id IS NOT NULL)
  ),
  UNIQUE(run_id, request_id)
) STRICT;

CREATE INDEX product_tool_runs_scope
ON product_tool_runs(run_id, invocation_status, created_at, tool_run_id);

CREATE INDEX product_tool_runs_function
ON product_tool_runs(tool_id, tool_version, function_name, created_at, tool_run_id);

CREATE TABLE product_tool_attempts(
  attempt_id TEXT PRIMARY KEY,
  tool_run_id TEXT NOT NULL
    REFERENCES product_tool_runs(tool_run_id) ON DELETE CASCADE,
  attempt_ordinal INTEGER NOT NULL CHECK(attempt_ordinal > 0),
  job_id TEXT NOT NULL UNIQUE
    REFERENCES product_jobs(job_id) ON DELETE RESTRICT,
  status TEXT NOT NULL CHECK(status IN (
    'QUEUED','RUNNING','WAITING','CANCEL_REQUESTED','CANCELLED',
    'SUCCEEDED','FAILED','OUTCOME_UNKNOWN','STALE','INVALIDATED'
  )),
  resource_request_json TEXT NOT NULL
    CHECK(json_valid(resource_request_json) AND json_type(resource_request_json) = 'object'),
  resource_usage_json TEXT
    CHECK(resource_usage_json IS NULL OR (
      json_valid(resource_usage_json) AND json_type(resource_usage_json) = 'object'
    )),
  public_log_artifact_id TEXT,
  output_artifact_ids_json TEXT NOT NULL DEFAULT '[]'
    CHECK(json_valid(output_artifact_ids_json) AND json_type(output_artifact_ids_json) = 'array'),
  public_summary TEXT,
  exit_code INTEGER,
  failure_code TEXT,
  authority_effect TEXT NOT NULL DEFAULT 'NONE' CHECK(authority_effect = 'NONE'),
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  CHECK(
    (status IN ('CANCELLED','SUCCEEDED','FAILED','OUTCOME_UNKNOWN','STALE','INVALIDATED')
      AND finished_at IS NOT NULL) OR
    (status NOT IN ('CANCELLED','SUCCEEDED','FAILED','OUTCOME_UNKNOWN','STALE','INVALIDATED')
      AND finished_at IS NULL)
  ),
  UNIQUE(tool_run_id, attempt_ordinal)
) STRICT;

CREATE INDEX product_tool_attempt_history
ON product_tool_attempts(tool_run_id, attempt_ordinal);

CREATE INDEX product_tool_attempt_status
ON product_tool_attempts(status, created_at, attempt_id);

CREATE TABLE product_source_snapshots(
  snapshot_id TEXT PRIMARY KEY,
  tool_run_id TEXT NOT NULL REFERENCES product_tool_runs(tool_run_id) ON DELETE RESTRICT,
  attempt_id TEXT NOT NULL REFERENCES product_tool_attempts(attempt_id) ON DELETE RESTRICT,
  connector TEXT NOT NULL CHECK(connector IN ('MATLAS','OPENALEX','CROSSREF','ARXIV')),
  connector_version TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('LIVE_QUERY','REPLAYED_SNAPSHOT')),
  parent_snapshot_id TEXT REFERENCES product_source_snapshots(snapshot_id) ON DELETE RESTRICT,
  endpoint TEXT NOT NULL,
  queried_at TEXT NOT NULL,
  request_json TEXT NOT NULL
    CHECK(json_valid(request_json) AND json_type(request_json) = 'object'),
  request_digest TEXT NOT NULL CHECK(length(request_digest) = 64),
  http_status INTEGER,
  raw_kind TEXT NOT NULL CHECK(raw_kind IN ('WIRE_RESPONSE','TRANSPORT_RECEIPT')),
  raw_artifact_id TEXT NOT NULL,
  raw_artifact_sha256 TEXT NOT NULL CHECK(length(raw_artifact_sha256) = 64),
  raw_artifact_byte_count INTEGER NOT NULL CHECK(raw_artifact_byte_count >= 0),
  raw_artifact_media_type TEXT NOT NULL,
  source_visible_version TEXT,
  coverage_json TEXT NOT NULL
    CHECK(json_valid(coverage_json) AND json_type(coverage_json) = 'object'),
  normalized_json TEXT NOT NULL
    CHECK(json_valid(normalized_json) AND json_type(normalized_json) = 'object'),
  result_status TEXT NOT NULL CHECK(result_status IN (
    'SUCCESS','NO_HIT','HTTP_ERROR','TIMEOUT','NETWORK_ERROR','SCHEMA_DRIFT'
  )),
  error_code TEXT,
  error_detail TEXT,
  created_at TEXT NOT NULL,
  CHECK(
    (mode = 'LIVE_QUERY' AND parent_snapshot_id IS NULL) OR
    (mode = 'REPLAYED_SNAPSHOT' AND parent_snapshot_id IS NOT NULL)
  ),
  CHECK(
    (result_status IN ('SUCCESS','NO_HIT') AND error_code IS NULL AND error_detail IS NULL) OR
    (result_status NOT IN ('SUCCESS','NO_HIT') AND error_code IS NOT NULL)
  ),
  UNIQUE(tool_run_id, attempt_id)
) STRICT;

CREATE INDEX product_source_snapshots_connector
ON product_source_snapshots(connector, queried_at, snapshot_id);

CREATE INDEX product_source_snapshots_parent
ON product_source_snapshots(parent_snapshot_id, snapshot_id);

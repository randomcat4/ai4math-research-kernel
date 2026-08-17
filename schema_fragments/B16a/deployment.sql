CREATE TABLE product_deployment_probe_runs(
  probe_run_id TEXT PRIMARY KEY,
  deployment_id TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('AVAILABLE','DEGRADED','UNAVAILABLE','UNCONFIGURED')),
  total_cost_microunits INTEGER NOT NULL CHECK(total_cost_microunits >= 0),
  failure_count INTEGER NOT NULL CHECK(failure_count >= 0),
  CHECK(finished_at >= started_at)
) STRICT;

CREATE INDEX product_deployment_probe_history
ON product_deployment_probe_runs(deployment_id, finished_at, probe_run_id);

CREATE TABLE product_deployment_probe_results(
  probe_run_id TEXT NOT NULL
    REFERENCES product_deployment_probe_runs(probe_run_id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
  capability_key TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN (
    'CPU','RAM','ROCM','GPU','CAS','SQLITE','SERVICE_ENDPOINT','TOOL_CATALOG'
  )),
  status TEXT NOT NULL CHECK(status IN ('AVAILABLE','DEGRADED','UNAVAILABLE','UNCONFIGURED')),
  latency_ms INTEGER NOT NULL CHECK(latency_ms >= 0),
  cost_microunits INTEGER NOT NULL CHECK(cost_microunits >= 0),
  fault_code TEXT,
  public_details_json TEXT NOT NULL CHECK(
    json_valid(public_details_json) AND json_type(public_details_json) = 'object'
  ),
  PRIMARY KEY(probe_run_id, ordinal),
  UNIQUE(probe_run_id, capability_key),
  CHECK(
    (status = 'AVAILABLE' AND fault_code IS NULL) OR
    (status <> 'AVAILABLE' AND fault_code IS NOT NULL)
  )
) WITHOUT ROWID, STRICT;

CREATE INDEX product_deployment_probe_capability
ON product_deployment_probe_results(kind, status, probe_run_id);

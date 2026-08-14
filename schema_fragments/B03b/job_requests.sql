CREATE TABLE product_job_requests(
  job_id TEXT PRIMARY KEY REFERENCES product_jobs(job_id) ON DELETE RESTRICT,
  receipt_id TEXT NOT NULL UNIQUE REFERENCES product_receipts(receipt_id) ON DELETE RESTRICT,
  request_json TEXT NOT NULL
    CHECK(json_valid(request_json) AND json_type(request_json)='object'),
  request_digest TEXT NOT NULL CHECK(length(request_digest)=64),
  created_at TEXT NOT NULL
) STRICT;

CREATE UNIQUE INDEX product_job_request_digest
ON product_job_requests(job_id,request_digest);

CREATE TABLE product_durable_resolutions(
  job_id TEXT PRIMARY KEY REFERENCES product_jobs(job_id) ON DELETE RESTRICT,
  receipt_id TEXT NOT NULL UNIQUE REFERENCES product_receipts(receipt_id) ON DELETE RESTRICT,
  lease_generation INTEGER NOT NULL CHECK(lease_generation>0),
  execution_digest TEXT NOT NULL CHECK(length(execution_digest)=64),
  outcome TEXT NOT NULL CHECK(outcome IN ('SUCCEEDED','FAILED','CANCELLED','OUTCOME_UNKNOWN')),
  decision_json TEXT CHECK(
    decision_json IS NULL OR (json_valid(decision_json) AND json_type(decision_json)='object')
  ),
  resolved_at TEXT NOT NULL,
  CHECK((outcome='OUTCOME_UNKNOWN' AND decision_json IS NULL) OR
        (outcome<>'OUTCOME_UNKNOWN' AND decision_json IS NOT NULL))
) STRICT;

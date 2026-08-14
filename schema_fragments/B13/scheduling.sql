CREATE TABLE product_schedule_plans(
  schedule_plan_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  hardware_profile_json TEXT NOT NULL
    CHECK(json_valid(hardware_profile_json) AND json_type(hardware_profile_json) = 'object'),
  quality_contract_json TEXT NOT NULL
    CHECK(json_valid(quality_contract_json) AND json_type(quality_contract_json) = 'object'),
  plan_digest TEXT NOT NULL CHECK(length(plan_digest) = 64),
  state TEXT NOT NULL CHECK(state IN ('READY','RUNNING','BUDGET_PAUSED','COMPLETED')),
  pause_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(run_id,plan_digest)
) STRICT;

CREATE TABLE product_scheduled_work(
  work_item_id TEXT PRIMARY KEY,
  schedule_plan_id TEXT NOT NULL
    REFERENCES product_schedule_plans(schedule_plan_id) ON DELETE RESTRICT,
  stable_ordinal INTEGER NOT NULL CHECK(stable_ordinal > 0),
  requirement_json TEXT NOT NULL
    CHECK(json_valid(requirement_json) AND json_type(requirement_json) = 'object'),
  placement_json TEXT NOT NULL
    CHECK(json_valid(placement_json) AND json_type(placement_json) = 'object'),
  concurrency_group TEXT NOT NULL,
  group_capacity INTEGER NOT NULL CHECK(group_capacity > 0),
  state TEXT NOT NULL CHECK(state IN (
    'QUEUED','RUNNING','BUDGET_PAUSED','SUCCEEDED','FAILED'
  )),
  promotion_state TEXT NOT NULL CHECK(promotion_state IN (
    'WAITING','CLAIMED','PROMOTED','NOT_ELIGIBLE'
  )),
  execution_receipt_json TEXT
    CHECK(execution_receipt_json IS NULL OR (
      json_valid(execution_receipt_json) AND json_type(execution_receipt_json) = 'object'
    )),
  started_at TEXT,
  finished_at TEXT,
  execution_started_monotonic_ns INTEGER,
  execution_finished_monotonic_ns INTEGER,
  failure_code TEXT,
  promotion_claimed_at TEXT,
  promoted_at TEXT,
  UNIQUE(schedule_plan_id,stable_ordinal),
  CHECK(
    (state IN ('SUCCEEDED','FAILED') AND finished_at IS NOT NULL) OR
    (state NOT IN ('SUCCEEDED','FAILED') AND finished_at IS NULL)
  ),
  CHECK(
    (state IN ('SUCCEEDED','FAILED') AND execution_receipt_json IS NOT NULL AND
      execution_started_monotonic_ns IS NOT NULL AND execution_finished_monotonic_ns IS NOT NULL
      AND execution_finished_monotonic_ns >= execution_started_monotonic_ns) OR
    (state NOT IN ('SUCCEEDED','FAILED') AND execution_receipt_json IS NULL)
  )
) STRICT;

CREATE INDEX product_scheduled_work_queue
ON product_scheduled_work(schedule_plan_id,state,stable_ordinal,work_item_id);

CREATE INDEX product_scheduled_work_concurrency
ON product_scheduled_work(schedule_plan_id,concurrency_group,state,work_item_id);

CREATE INDEX product_scheduled_work_promotion
ON product_scheduled_work(schedule_plan_id,promotion_state,stable_ordinal,work_item_id);

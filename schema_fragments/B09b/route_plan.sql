CREATE TABLE product_route_plans(
  route_plan_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  research_revision INTEGER NOT NULL CHECK(research_revision >= 0),
  contract_version INTEGER NOT NULL CHECK(contract_version > 0),
  plan_digest TEXT NOT NULL CHECK(length(plan_digest) = 64),
  proposal_json TEXT NOT NULL
    CHECK(json_valid(proposal_json) AND json_type(proposal_json) = 'object'),
  state TEXT NOT NULL
    CHECK(state IN ('PROPOSED','APPROVED','ACTIVE','PAUSED','STOPPED')),
  state_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(run_id,plan_digest)
) STRICT;

CREATE INDEX product_route_plans_run_state
ON product_route_plans(run_id,state,updated_at,route_plan_id);

CREATE TABLE product_planned_routes(
  route_id TEXT PRIMARY KEY,
  route_plan_id TEXT NOT NULL
    REFERENCES product_route_plans(route_plan_id) ON DELETE RESTRICT,
  method TEXT NOT NULL,
  target TEXT NOT NULL,
  expected_verifier TEXT NOT NULL,
  milestones_json TEXT NOT NULL
    CHECK(json_valid(milestones_json) AND json_type(milestones_json) = 'array'),
  termination_condition TEXT NOT NULL,
  dependencies_json TEXT NOT NULL
    CHECK(json_valid(dependencies_json) AND json_type(dependencies_json) = 'array'),
  state TEXT NOT NULL
    CHECK(state IN ('PROPOSED','APPROVED','ACTIVE','PAUSED','STOPPED')),
  priority INTEGER NOT NULL CHECK(priority > 0),
  budget_json TEXT NOT NULL
    CHECK(json_valid(budget_json) AND json_type(budget_json) = 'object'),
  stop_reason TEXT
) STRICT;

CREATE INDEX product_planned_routes_plan_state
ON product_planned_routes(route_plan_id,state,priority,route_id);

CREATE TABLE product_route_plan_commands(
  run_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  request_digest TEXT NOT NULL CHECK(length(request_digest) = 64),
  route_plan_id TEXT NOT NULL
    REFERENCES product_route_plans(route_plan_id) ON DELETE RESTRICT,
  action TEXT NOT NULL CHECK(action IN (
    'APPROVE','START','PAUSE','STOP','SET_PRIORITY','SET_BUDGET'
  )),
  expected_revision INTEGER NOT NULL CHECK(expected_revision >= 0),
  contract_version INTEGER NOT NULL CHECK(contract_version > 0),
  result_json TEXT NOT NULL
    CHECK(json_valid(result_json) AND json_type(result_json) = 'object'),
  applied_at TEXT NOT NULL,
  PRIMARY KEY(run_id,request_id)
) STRICT;

CREATE TABLE product_route_hints(
  hint_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  route_plan_id TEXT REFERENCES product_route_plans(route_plan_id) ON DELETE RESTRICT,
  content_artifact_id TEXT NOT NULL,
  created_at TEXT NOT NULL
) STRICT;

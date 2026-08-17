CREATE TABLE product_bridge_opportunities(
  opportunity_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  route_id TEXT REFERENCES product_planned_routes(route_id) ON DELETE RESTRICT,
  source_problem_json TEXT NOT NULL CHECK(json_valid(source_problem_json) AND json_type(source_problem_json)='object'),
  target_domain TEXT NOT NULL,
  domain_distance INTEGER NOT NULL CHECK(domain_distance BETWEEN 0 AND 1000000),
  source_method_maturity INTEGER NOT NULL CHECK(source_method_maturity BETWEEN 0 AND 1000000),
  target_domain_absence INTEGER NOT NULL CHECK(target_domain_absence BETWEEN 0 AND 1000000),
  native_tool_advantage INTEGER NOT NULL CHECK(native_tool_advantage BETWEEN 0 AND 1000000),
  expected_certificate_compression INTEGER NOT NULL CHECK(expected_certificate_compression BETWEEN 0 AND 1000000),
  mapping_loss INTEGER NOT NULL CHECK(mapping_loss BETWEEN 0 AND 1000000),
  assumption_loss INTEGER NOT NULL CHECK(assumption_loss BETWEEN 0 AND 1000000),
  backtranslation_cost INTEGER NOT NULL CHECK(backtranslation_cost BETWEEN 0 AND 1000000),
  ranking_score INTEGER NOT NULL,
  mapping_definition_json TEXT NOT NULL CHECK(json_valid(mapping_definition_json) AND json_type(mapping_definition_json)='object'),
  assumption_audit_json TEXT NOT NULL CHECK(json_valid(assumption_audit_json) AND json_type(assumption_audit_json)='object'),
  backtranslation_plan_json TEXT NOT NULL CHECK(json_valid(backtranslation_plan_json) AND json_type(backtranslation_plan_json)='object'),
  selection_reason TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('EVALUATING','ELIGIBLE','REJECTED','BRIDGE_REGISTERED')),
  rejection_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK((state='REJECTED' AND rejection_reason IS NOT NULL) OR (state<>'REJECTED' AND rejection_reason IS NULL))
) STRICT;

CREATE TABLE product_bridge_death_tests(
  death_test_id TEXT PRIMARY KEY,
  opportunity_id TEXT NOT NULL REFERENCES product_bridge_opportunities(opportunity_id) ON DELETE RESTRICT,
  test_rank INTEGER NOT NULL CHECK(test_rank>0),
  test_kind TEXT NOT NULL CHECK(test_kind IN ('COUNTEREXAMPLE','TYPE_CHECK','ROUNDTRIP','ASSUMPTION_LOSS','SOURCE_REPLAY')),
  specification_json TEXT NOT NULL CHECK(json_valid(specification_json) AND json_type(specification_json)='object'),
  status TEXT NOT NULL CHECK(status IN ('PASSED','FAILED','ERROR')),
  receipt_artifact_id TEXT NOT NULL,
  elapsed_ms INTEGER NOT NULL CHECK(elapsed_ms>=0),
  cost_microunits INTEGER NOT NULL CHECK(cost_microunits>=0),
  failure_code TEXT,
  recorded_at TEXT NOT NULL,
  UNIQUE(opportunity_id,test_rank),
  CHECK((status='PASSED' AND failure_code IS NULL) OR (status<>'PASSED' AND failure_code IS NOT NULL))
) STRICT;

CREATE TABLE product_bridge_opportunity_bindings(
  opportunity_id TEXT PRIMARY KEY REFERENCES product_bridge_opportunities(opportunity_id) ON DELETE RESTRICT,
  bridge_spec_id TEXT NOT NULL REFERENCES bridges(bridge_id) ON DELETE RESTRICT,
  bound_at TEXT NOT NULL
) STRICT;

CREATE TABLE product_ablation_plans(
  ablation_plan_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  problem_pool_digest TEXT NOT NULL CHECK(length(problem_pool_digest)=64),
  problem_ids_json TEXT NOT NULL CHECK(json_valid(problem_ids_json) AND json_type(problem_ids_json)='array'),
  model_identity_json TEXT NOT NULL CHECK(json_valid(model_identity_json) AND json_type(model_identity_json)='object'),
  tool_builds_json TEXT NOT NULL CHECK(json_valid(tool_builds_json) AND json_type(tool_builds_json)='object'),
  candidate_count INTEGER NOT NULL CHECK(candidate_count>0),
  budget_json TEXT NOT NULL CHECK(json_valid(budget_json) AND json_type(budget_json)='object'),
  verifier_identity_json TEXT NOT NULL CHECK(json_valid(verifier_identity_json) AND json_type(verifier_identity_json)='object'),
  verifier_profile_receipt_id TEXT NOT NULL,
  frozen_digest TEXT NOT NULL CHECK(length(frozen_digest)=64),
  state TEXT NOT NULL CHECK(state IN ('FROZEN','RUNNING','COMPLETED')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(run_id,frozen_digest)
) STRICT;

CREATE TABLE product_ablation_groups(
  ablation_plan_id TEXT NOT NULL REFERENCES product_ablation_plans(ablation_plan_id) ON DELETE RESTRICT,
  group_name TEXT NOT NULL CHECK(group_name IN ('direct','near','far-random','far-retrieval','full-RK')),
  frozen_digest TEXT NOT NULL CHECK(length(frozen_digest)=64),
  state TEXT NOT NULL CHECK(state IN ('PENDING','RUNNING','COMPLETED')),
  run_receipt_artifact_id TEXT,
  started_at TEXT,
  completed_at TEXT,
  PRIMARY KEY(ablation_plan_id,group_name)
) STRICT;

CREATE TABLE product_ablation_assignments(
  assignment_id TEXT PRIMARY KEY,
  ablation_plan_id TEXT NOT NULL,
  group_name TEXT NOT NULL,
  problem_id TEXT NOT NULL,
  opportunity_id TEXT REFERENCES product_bridge_opportunities(opportunity_id) ON DELETE RESTRICT,
  rejected_bridge_reason TEXT,
  state TEXT NOT NULL CHECK(state IN ('PENDING','RUNNING','SUCCEEDED','FAILED','REJECTED_BRIDGE')),
  FOREIGN KEY(ablation_plan_id,group_name) REFERENCES product_ablation_groups(ablation_plan_id,group_name) ON DELETE RESTRICT,
  UNIQUE(ablation_plan_id,group_name,problem_id),
  CHECK((state='REJECTED_BRIDGE' AND rejected_bridge_reason IS NOT NULL) OR (state<>'REJECTED_BRIDGE' AND rejected_bridge_reason IS NULL))
) STRICT;

CREATE TABLE product_ablation_results(
  assignment_id TEXT PRIMARY KEY REFERENCES product_ablation_assignments(assignment_id) ON DELETE RESTRICT,
  frozen_digest TEXT NOT NULL CHECK(length(frozen_digest)=64),
  outcome TEXT NOT NULL CHECK(outcome IN ('VERIFIED','REJECTED','INCONCLUSIVE','EXECUTION_FAILED')),
  cost_microunits INTEGER NOT NULL CHECK(cost_microunits>=0),
  certificate_length INTEGER,
  verifier_profile_receipt_id TEXT NOT NULL,
  verifier_receipt_artifact_id TEXT NOT NULL,
  execution_receipt_artifact_id TEXT NOT NULL,
  failure_code TEXT,
  finished_at TEXT NOT NULL,
  CHECK((outcome='EXECUTION_FAILED' AND failure_code IS NOT NULL) OR (outcome<>'EXECUTION_FAILED' AND failure_code IS NULL)),
  CHECK(certificate_length IS NULL OR certificate_length>=0)
) STRICT;

CREATE INDEX product_bridge_opportunities_rank ON product_bridge_opportunities(run_id,state,ranking_score DESC,opportunity_id);
CREATE INDEX product_bridge_death_tests_opportunity ON product_bridge_death_tests(opportunity_id,test_rank);
CREATE INDEX product_ablation_assignments_denominator ON product_ablation_assignments(ablation_plan_id,group_name,state,problem_id);

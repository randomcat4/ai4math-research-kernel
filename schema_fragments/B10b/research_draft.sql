CREATE TABLE product_research_drafts(
  draft_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  contract_version INTEGER NOT NULL CHECK(contract_version>0),
  kernel_revision INTEGER NOT NULL CHECK(kernel_revision>=0),
  source_artifact_json TEXT NOT NULL CHECK(json_valid(source_artifact_json) AND json_type(source_artifact_json)='object'),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  normalized_digest TEXT NOT NULL CHECK(length(normalized_digest)=64),
  defined_symbols_json TEXT NOT NULL CHECK(json_valid(defined_symbols_json) AND json_type(defined_symbols_json)='array'),
  candidate_count INTEGER NOT NULL CHECK(candidate_count>0),
  created_at TEXT NOT NULL,
  UNIQUE(run_id,source_sha256)
) STRICT;

CREATE TABLE product_research_claim_candidates(
  candidate_id TEXT PRIMARY KEY,
  draft_id TEXT NOT NULL REFERENCES product_research_drafts(draft_id) ON DELETE RESTRICT,
  ordinal INTEGER NOT NULL CHECK(ordinal>=0),
  stable_label TEXT NOT NULL,
  statement TEXT NOT NULL,
  statement_digest TEXT NOT NULL CHECK(length(statement_digest)=64),
  claim_kind TEXT NOT NULL CHECK(claim_kind IN ('ROOT','LEMMA','DEFINITION','COUNTEREXAMPLE','COMPUTATION')),
  predecessor_labels_json TEXT NOT NULL CHECK(json_valid(predecessor_labels_json) AND json_type(predecessor_labels_json)='array'),
  predecessor_fact_ids_json TEXT NOT NULL CHECK(json_valid(predecessor_fact_ids_json) AND json_type(predecessor_fact_ids_json)='array'),
  declared_symbols_json TEXT NOT NULL CHECK(json_valid(declared_symbols_json) AND json_type(declared_symbols_json)='array'),
  undefined_symbols_json TEXT NOT NULL CHECK(json_valid(undefined_symbols_json) AND json_type(undefined_symbols_json)='array'),
  proof_text TEXT NOT NULL,
  proof_digest TEXT NOT NULL CHECK(length(proof_digest)=64),
  lifecycle TEXT NOT NULL CHECK(lifecycle IN ('CANDIDATE','SUBMITTED','ACCEPTED','REJECTED')),
  submitted_claim_id TEXT REFERENCES product_claims(claim_id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  UNIQUE(draft_id,ordinal),
  UNIQUE(draft_id,stable_label),
  CHECK((lifecycle='CANDIDATE' AND submitted_claim_id IS NULL) OR (lifecycle<>'CANDIDATE' AND submitted_claim_id IS NOT NULL))
) STRICT;

CREATE TABLE product_research_verifier_plans(
  plan_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES product_research_claim_candidates(candidate_id) ON DELETE RESTRICT,
  claim_id TEXT NOT NULL REFERENCES product_claims(claim_id) ON DELETE RESTRICT,
  selected_subgraph_digest TEXT NOT NULL CHECK(length(selected_subgraph_digest)=64),
  required_backends_json TEXT NOT NULL CHECK(json_valid(required_backends_json) AND json_type(required_backends_json)='array'),
  supplementary_backends_json TEXT NOT NULL CHECK(json_valid(supplementary_backends_json) AND json_type(supplementary_backends_json)='array'),
  plan_digest TEXT NOT NULL CHECK(length(plan_digest)=64),
  status TEXT NOT NULL CHECK(status IN ('PLANNED','PARTIALLY_VERIFIED','READY_FOR_KERNEL','REJECTED','IMPORTED_ACCEPTED','IMPORTED_REJECTED')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(candidate_id),
  UNIQUE(claim_id)
) STRICT;

CREATE TABLE product_research_verifier_results(
  validation_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL REFERENCES product_research_verifier_plans(plan_id) ON DELETE RESTRICT,
  backend TEXT NOT NULL CHECK(backend IN ('LEAN','DETERMINISTIC_CHECKER','MANAGED_HUMAN','SOFT_VERIFIER')),
  verdict TEXT NOT NULL CHECK(verdict IN ('ACCEPTED','REJECTED')),
  result_json TEXT NOT NULL CHECK(json_valid(result_json) AND json_type(result_json)='object'),
  result_digest TEXT NOT NULL CHECK(length(result_digest)=64),
  created_at TEXT NOT NULL,
  UNIQUE(plan_id,backend)
) STRICT;

CREATE TABLE product_research_obligations(
  obligation_id TEXT PRIMARY KEY,
  draft_id TEXT NOT NULL REFERENCES product_research_drafts(draft_id) ON DELETE RESTRICT,
  candidate_id TEXT NOT NULL REFERENCES product_research_claim_candidates(candidate_id) ON DELETE RESTRICT,
  claim_id TEXT REFERENCES product_claims(claim_id) ON DELETE RESTRICT,
  status TEXT NOT NULL CHECK(status IN ('WAITING_SUBMISSION','WAITING_KERNEL','REPAIR_REQUIRED','DISCHARGED_BY_KERNEL')),
  kernel_event_id TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(candidate_id),
  UNIQUE(kernel_event_id),
  CHECK((status IN ('REPAIR_REQUIRED','DISCHARGED_BY_KERNEL') AND kernel_event_id IS NOT NULL) OR (status IN ('WAITING_SUBMISSION','WAITING_KERNEL') AND kernel_event_id IS NULL))
) STRICT;

CREATE TABLE product_research_closure_readiness(
  draft_id TEXT PRIMARY KEY REFERENCES product_research_drafts(draft_id) ON DELETE RESTRICT,
  status TEXT NOT NULL CHECK(status IN ('BLOCKED','READY_FOR_CLOSURE_WITNESS')),
  discharged_count INTEGER NOT NULL CHECK(discharged_count>=0),
  total_count INTEGER NOT NULL CHECK(total_count>0),
  blocking_obligation_ids_json TEXT NOT NULL CHECK(json_valid(blocking_obligation_ids_json) AND json_type(blocking_obligation_ids_json)='array'),
  last_kernel_event_id TEXT,
  updated_at TEXT NOT NULL,
  CHECK((status='READY_FOR_CLOSURE_WITNESS' AND discharged_count=total_count AND json_array_length(blocking_obligation_ids_json)=0) OR (status='BLOCKED' AND discharged_count<total_count AND json_array_length(blocking_obligation_ids_json)>0))
) STRICT;

CREATE INDEX product_research_candidates_draft ON product_research_claim_candidates(draft_id,ordinal);
CREATE INDEX product_research_plans_status ON product_research_verifier_plans(status,plan_id);
CREATE INDEX product_research_obligations_draft ON product_research_obligations(draft_id,status,obligation_id);
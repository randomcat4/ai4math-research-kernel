CREATE TABLE research_catalog_fence (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL CHECK(revision>=0)
);
INSERT INTO research_catalog_fence(singleton,revision) VALUES(1,0);
CREATE TABLE research_catalog (
  run_id TEXT PRIMARY KEY, title TEXT NOT NULL, question_summary TEXT NOT NULL,
  owner TEXT NOT NULL, labels_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE research_summary_projection (
  run_id TEXT PRIMARY KEY REFERENCES research_catalog(run_id) ON DELETE CASCADE,
  outcome_state TEXT NOT NULL, execution_state TEXT NOT NULL,
  authority_state TEXT NOT NULL, publication_state TEXT NOT NULL,
  phase TEXT NOT NULL, blockers_json TEXT NOT NULL, next_actions_json TEXT NOT NULL,
  available_actions_json TEXT NOT NULL, budget_json TEXT NOT NULL,
  recent_activity_at TEXT NOT NULL, recent_activity_summary TEXT NOT NULL,
  research_revision INTEGER NOT NULL CHECK(research_revision>=0),
  contract_version INTEGER NOT NULL CHECK(contract_version>=1),
  last_cursor INTEGER NOT NULL CHECK(last_cursor>=0),
  projection_source_digest TEXT NOT NULL CHECK(length(projection_source_digest)=64)
);
CREATE INDEX research_catalog_owner ON research_catalog(owner,run_id);
CREATE INDEX research_summary_recent ON research_summary_projection(recent_activity_at DESC,run_id);

CREATE TABLE product_problem_pool_artifact_bindings(
  problem_pool_id TEXT NOT NULL REFERENCES product_problem_pools(problem_pool_id) ON DELETE RESTRICT,
  binding_kind TEXT NOT NULL CHECK(binding_kind IN ('SEMANTIC_AUDIT','CONTRACT_TEMPLATE')),
  artifact_id TEXT NOT NULL CHECK(length(artifact_id)>0),
  artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256)=64 AND artifact_sha256 GLOB '[0-9a-f]*'),
  artifact_byte_count INTEGER NOT NULL CHECK(artifact_byte_count>=0),
  artifact_media_type TEXT NOT NULL CHECK(length(artifact_media_type)>0),
  artifact_at_revision INTEGER NOT NULL CHECK(artifact_at_revision>=0),
  bound_by TEXT NOT NULL CHECK(length(bound_by)>0),
  binding_digest TEXT NOT NULL UNIQUE CHECK(length(binding_digest)=64 AND binding_digest GLOB '[0-9a-f]*'),
  authority_effect TEXT NOT NULL CHECK(authority_effect='NO_FACT'),
  created_at TEXT NOT NULL,
  PRIMARY KEY(problem_pool_id,binding_kind)
) WITHOUT ROWID, STRICT;

CREATE INDEX product_problem_pool_artifact_lookup
ON product_problem_pool_artifact_bindings(artifact_id,problem_pool_id,binding_kind);

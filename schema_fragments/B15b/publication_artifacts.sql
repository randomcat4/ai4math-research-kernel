CREATE TABLE product_candidate_renders(
  render_request_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  finalized_revision INTEGER NOT NULL CHECK(finalized_revision > 0),
  terminal_root_id TEXT NOT NULL,
  terminal_root_digest TEXT NOT NULL CHECK(length(terminal_root_digest) = 64),
  closure_witness_id TEXT NOT NULL,
  dependency_closure_digest TEXT NOT NULL CHECK(length(dependency_closure_digest) = 64),
  finalized_snapshot_digest TEXT NOT NULL CHECK(length(finalized_snapshot_digest) = 64),
  abstract_digest TEXT NOT NULL CHECK(length(abstract_digest) = 64),
  candidate_tex_artifact_id TEXT NOT NULL,
  candidate_tex_sha256 TEXT NOT NULL CHECK(length(candidate_tex_sha256) = 64),
  candidate_tex_byte_count INTEGER NOT NULL CHECK(candidate_tex_byte_count >= 0),
  created_at TEXT NOT NULL,
  UNIQUE(run_id,candidate_tex_sha256)
) STRICT;

CREATE TABLE product_publication_review_bindings(
  generation_command_id TEXT PRIMARY KEY
    REFERENCES product_publication_candidates(generation_command_id) ON DELETE RESTRICT,
  review_task_id TEXT NOT NULL UNIQUE
    REFERENCES product_review_tasks(review_task_id) ON DELETE RESTRICT,
  render_request_id TEXT NOT NULL
    REFERENCES product_candidate_renders(render_request_id) ON DELETE RESTRICT,
  candidate_tex_sha256 TEXT NOT NULL CHECK(length(candidate_tex_sha256) = 64),
  abstract_digest TEXT NOT NULL CHECK(length(abstract_digest) = 64),
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE product_compilation_attempts(
  compilation_attempt_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  generation_command_id TEXT NOT NULL
    REFERENCES product_publication_candidates(generation_command_id) ON DELETE RESTRICT,
  paper_review_id TEXT NOT NULL
    REFERENCES product_publication_reviews(paper_review_id) ON DELETE RESTRICT,
  candidate_tex_sha256 TEXT NOT NULL CHECK(length(candidate_tex_sha256) = 64),
  abstract_digest TEXT NOT NULL CHECK(length(abstract_digest) = 64),
  compiler_profile TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK(outcome IN ('SUCCEEDED','FAILED')),
  stdout_log_id TEXT NOT NULL
    REFERENCES product_public_logs(log_id) ON DELETE RESTRICT,
  stderr_log_id TEXT NOT NULL
    REFERENCES product_public_logs(log_id) ON DELETE RESTRICT,
  stdout_log_artifact_id TEXT NOT NULL,
  stderr_log_artifact_id TEXT NOT NULL,
  final_pdf_artifact_id TEXT,
  final_pdf_sha256 TEXT CHECK(final_pdf_sha256 IS NULL OR length(final_pdf_sha256) = 64),
  failure_code TEXT,
  created_at TEXT NOT NULL,
  CHECK(
    (outcome='SUCCEEDED' AND final_pdf_artifact_id IS NOT NULL
      AND final_pdf_sha256 IS NOT NULL AND failure_code IS NULL) OR
    (outcome='FAILED' AND final_pdf_artifact_id IS NULL
      AND final_pdf_sha256 IS NULL AND failure_code IS NOT NULL)
  )
) STRICT;

CREATE INDEX product_compilation_history
ON product_compilation_attempts(run_id,generation_command_id,created_at,compilation_attempt_id);

CREATE TABLE product_dossier_artifacts(
  dossier_request_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  observed_revision INTEGER NOT NULL CHECK(observed_revision >= 0),
  observed_status TEXT NOT NULL,
  snapshot_digest TEXT NOT NULL CHECK(length(snapshot_digest) = 64),
  dossier_artifact_id TEXT NOT NULL,
  dossier_sha256 TEXT NOT NULL CHECK(length(dossier_sha256) = 64),
  created_at TEXT NOT NULL,
  UNIQUE(run_id,observed_revision,snapshot_digest)
) STRICT;

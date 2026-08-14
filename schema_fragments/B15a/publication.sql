CREATE TABLE product_publication_finalizations(
  run_id TEXT PRIMARY KEY,
  finalized_revision INTEGER NOT NULL CHECK(finalized_revision > 0),
  contract_version INTEGER NOT NULL CHECK(contract_version > 0),
  final_outcome TEXT NOT NULL CHECK(final_outcome IN ('PROVED','DISPROVED','UNRESOLVED')),
  terminal_root_id TEXT,
  terminal_root_digest TEXT CHECK(
    terminal_root_digest IS NULL OR length(terminal_root_digest) = 64
  ),
  closure_witness_id TEXT,
  dependency_closure_digest TEXT CHECK(
    dependency_closure_digest IS NULL OR length(dependency_closure_digest) = 64
  ),
  finalize_command_id TEXT NOT NULL UNIQUE,
  finalize_event_id TEXT NOT NULL UNIQUE,
  finalized_at TEXT NOT NULL,
  CHECK(
    (final_outcome IN ('PROVED','DISPROVED')
      AND terminal_root_id IS NOT NULL
      AND terminal_root_digest IS NOT NULL
      AND closure_witness_id IS NOT NULL
      AND dependency_closure_digest IS NOT NULL) OR
    (final_outcome = 'UNRESOLVED'
      AND terminal_root_id IS NULL
      AND terminal_root_digest IS NULL
      AND closure_witness_id IS NULL
      AND dependency_closure_digest IS NULL)
  )
) STRICT;

CREATE TABLE product_publication_candidates(
  generation_command_id TEXT PRIMARY KEY,
  generation_event_id TEXT NOT NULL UNIQUE,
  run_id TEXT NOT NULL
    REFERENCES product_publication_finalizations(run_id) ON DELETE RESTRICT,
  publication_revision INTEGER NOT NULL CHECK(publication_revision > 0),
  finalized_revision INTEGER NOT NULL CHECK(finalized_revision > 0),
  contract_version INTEGER NOT NULL CHECK(contract_version > 0),
  terminal_root_id TEXT NOT NULL,
  terminal_root_digest TEXT NOT NULL CHECK(length(terminal_root_digest) = 64),
  closure_witness_id TEXT NOT NULL,
  dependency_closure_digest TEXT NOT NULL CHECK(length(dependency_closure_digest) = 64),
  candidate_tex_artifact_id TEXT NOT NULL,
  candidate_tex_sha256 TEXT NOT NULL CHECK(length(candidate_tex_sha256) = 64),
  candidate_tex_byte_count INTEGER NOT NULL CHECK(candidate_tex_byte_count >= 0),
  candidate_tex_media_type TEXT NOT NULL CHECK(candidate_tex_media_type = 'application/x-tex'),
  generated_by_subject_id TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  UNIQUE(run_id,candidate_tex_artifact_id,candidate_tex_sha256)
) STRICT;

CREATE INDEX product_publication_candidate_run
ON product_publication_candidates(run_id,publication_revision,generation_command_id);

CREATE TABLE product_publication_reviews(
  paper_review_id TEXT PRIMARY KEY,
  review_command_id TEXT NOT NULL UNIQUE,
  review_event_id TEXT NOT NULL UNIQUE,
  run_id TEXT NOT NULL,
  publication_revision INTEGER NOT NULL CHECK(publication_revision > 0),
  generation_command_id TEXT NOT NULL
    REFERENCES product_publication_candidates(generation_command_id) ON DELETE RESTRICT,
  finalized_revision INTEGER NOT NULL CHECK(finalized_revision > 0),
  terminal_root_id TEXT NOT NULL,
  terminal_root_digest TEXT NOT NULL CHECK(length(terminal_root_digest) = 64),
  closure_witness_id TEXT NOT NULL,
  dependency_closure_digest TEXT NOT NULL CHECK(length(dependency_closure_digest) = 64),
  candidate_tex_artifact_id TEXT NOT NULL,
  candidate_tex_sha256 TEXT NOT NULL CHECK(length(candidate_tex_sha256) = 64),
  signed_review_artifact_id TEXT NOT NULL,
  signed_review_sha256 TEXT NOT NULL CHECK(length(signed_review_sha256) = 64),
  reviewer_subject_id TEXT NOT NULL,
  paper_review_schema_version TEXT NOT NULL,
  verdict TEXT NOT NULL CHECK(verdict IN ('ACCEPT','REJECT')),
  reviewed_at TEXT NOT NULL
) STRICT;

CREATE INDEX product_publication_review_candidate
ON product_publication_reviews(generation_command_id,verdict,paper_review_id);

CREATE TABLE product_publication_compilations(
  compile_command_id TEXT PRIMARY KEY,
  compile_event_id TEXT NOT NULL UNIQUE,
  run_id TEXT NOT NULL,
  publication_revision INTEGER NOT NULL CHECK(publication_revision > 0),
  generation_command_id TEXT NOT NULL
    REFERENCES product_publication_candidates(generation_command_id) ON DELETE RESTRICT,
  paper_review_id TEXT NOT NULL UNIQUE
    REFERENCES product_publication_reviews(paper_review_id) ON DELETE RESTRICT,
  candidate_tex_artifact_id TEXT NOT NULL,
  candidate_tex_sha256 TEXT NOT NULL CHECK(length(candidate_tex_sha256) = 64),
  final_pdf_artifact_id TEXT NOT NULL,
  final_pdf_sha256 TEXT NOT NULL CHECK(length(final_pdf_sha256) = 64),
  compiled_by_subject_id TEXT NOT NULL,
  compiler_profile_id TEXT NOT NULL,
  compiler_profile_version TEXT NOT NULL,
  compiled_at TEXT NOT NULL
) STRICT;

CREATE INDEX product_publication_compilation_run
ON product_publication_compilations(run_id,publication_revision,compile_command_id);

CREATE TABLE product_research_lineage_artifacts(
  lineage_artifact_id TEXT PRIMARY KEY,
  stable_project_id TEXT NOT NULL CHECK(stable_project_id IN ('ZHAO_C61','N2_AJT5')),
  artifact_id TEXT NOT NULL,
  artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256)=64),
  artifact_byte_count INTEGER NOT NULL CHECK(artifact_byte_count>=0),
  artifact_media_type TEXT NOT NULL,
  source_uri TEXT NOT NULL,
  source_version TEXT NOT NULL,
  content_class TEXT NOT NULL CHECK(content_class IN (
    'PROBLEM_STATEMENT','PUBLIC_DEFINITIONS','TOOLCHAIN_LOCK','CONTRACT',
    'HISTORICAL_MATERIAL','HISTORICAL_CONCLUSION','HISTORICAL_PROOF',
    'CERTIFICATE','CERTIFICATE_REPORT'
  )),
  captured_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(stable_project_id,artifact_id,source_version)
) STRICT;

CREATE TABLE product_research_case_lineages(
  lineage_id TEXT PRIMARY KEY,
  stable_project_id TEXT NOT NULL CHECK(stable_project_id IN ('ZHAO_C61','N2_AJT5')),
  mode TEXT NOT NULL CHECK(mode IN (
    'CLEAN_ROOM_REDISCOVERY','IMPORTED_CERTIFICATE_VERIFICATION',
    'HISTORICAL_CANDIDATE_MIGRATION'
  )),
  run_id TEXT NOT NULL UNIQUE,
  contract_version INTEGER NOT NULL CHECK(contract_version>0),
  frozen_tree_digest TEXT NOT NULL CHECK(length(frozen_tree_digest)=64),
  data_root_id TEXT NOT NULL UNIQUE,
  input_manifest_artifact_id TEXT NOT NULL,
  input_manifest_sha256 TEXT NOT NULL CHECK(length(input_manifest_sha256)=64),
  input_manifest_json TEXT NOT NULL CHECK(json_valid(input_manifest_json) AND json_type(input_manifest_json)='object'),
  candidate_authority TEXT NOT NULL CHECK(candidate_authority='CANDIDATE_ONLY'),
  status TEXT NOT NULL CHECK(status IN (
    'RUNNING','COMPLETED_NO_REDISCOVERY','REDISCOVERED_CANDIDATE_ONLY',
    'CERTIFICATES_PENDING','CERTIFICATES_CHECKED','HISTORY_MIGRATED_CANDIDATE_ONLY'
  )),
  created_by_subject_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK(
    (stable_project_id='ZHAO_C61' AND mode IN ('CLEAN_ROOM_REDISCOVERY','IMPORTED_CERTIFICATE_VERIFICATION')) OR
    (stable_project_id='N2_AJT5' AND mode='HISTORICAL_CANDIDATE_MIGRATION')
  )
) STRICT;

CREATE TABLE product_research_lineage_inputs(
  lineage_id TEXT NOT NULL REFERENCES product_research_case_lineages(lineage_id) ON DELETE RESTRICT,
  lineage_artifact_id TEXT NOT NULL REFERENCES product_research_lineage_artifacts(lineage_artifact_id) ON DELETE RESTRICT,
  input_role TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK(ordinal>=0),
  PRIMARY KEY(lineage_id,lineage_artifact_id),
  UNIQUE(lineage_id,ordinal)
) STRICT;

CREATE TABLE product_research_certificate_verifications(
  lineage_id TEXT NOT NULL REFERENCES product_research_case_lineages(lineage_id) ON DELETE RESTRICT,
  certificate_artifact_id TEXT NOT NULL,
  certificate_sha256 TEXT NOT NULL CHECK(length(certificate_sha256)=64),
  verifier_receipt_id TEXT NOT NULL UNIQUE,
  verifier_receipt_json TEXT NOT NULL CHECK(json_valid(verifier_receipt_json) AND json_type(verifier_receipt_json)='object'),
  verdict TEXT NOT NULL CHECK(verdict IN ('ACCEPTED','REJECTED')),
  checked_at TEXT NOT NULL,
  PRIMARY KEY(lineage_id,certificate_artifact_id)
) WITHOUT ROWID, STRICT;

CREATE TABLE product_research_lineage_reports(
  lineage_id TEXT PRIMARY KEY REFERENCES product_research_case_lineages(lineage_id) ON DELETE RESTRICT,
  report_artifact_id TEXT NOT NULL,
  report_sha256 TEXT NOT NULL CHECK(length(report_sha256)=64),
  report_json TEXT NOT NULL CHECK(json_valid(report_json) AND json_type(report_json)='object'),
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE product_research_lineage_candidates(
  lineage_id TEXT NOT NULL REFERENCES product_research_case_lineages(lineage_id) ON DELETE RESTRICT,
  claim_id TEXT NOT NULL REFERENCES product_claims(claim_id) ON DELETE RESTRICT,
  source_material_id TEXT REFERENCES product_materials(material_id) ON DELETE RESTRICT,
  historical_input_id TEXT,
  status TEXT NOT NULL CHECK(status IN ('PENDING_CURRENT_VERIFICATION','ACCEPTED_BY_CURRENT_KERNEL','REJECTED_BY_CURRENT_KERNEL')),
  PRIMARY KEY(lineage_id,claim_id)
) WITHOUT ROWID, STRICT;

CREATE TABLE product_research_lineage_outcomes(
  lineage_id TEXT PRIMARY KEY REFERENCES product_research_case_lineages(lineage_id) ON DELETE RESTRICT,
  outcome TEXT NOT NULL CHECK(outcome IN ('NO_REDISCOVERY','REDISCOVERED_CANDIDATE_ONLY')),
  result_artifact_id TEXT,
  result_sha256 TEXT CHECK(result_sha256 IS NULL OR length(result_sha256)=64),
  recorded_at TEXT NOT NULL
) STRICT;

CREATE INDEX product_research_lineage_project_mode
ON product_research_case_lineages(stable_project_id,mode,created_at,lineage_id);
CREATE INDEX product_research_lineage_candidate_status
ON product_research_lineage_candidates(status,lineage_id,claim_id);
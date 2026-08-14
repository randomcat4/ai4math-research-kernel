CREATE TABLE product_material_profiles(
  profile_id TEXT PRIMARY KEY,
  material_kind TEXT NOT NULL CHECK(material_kind IN ('PDF','TEX','IMAGE','TEXT')),
  parser_name TEXT NOT NULL,
  parser_build TEXT NOT NULL,
  availability TEXT NOT NULL CHECK(availability IN ('AVAILABLE','UNAVAILABLE')),
  unavailable_reason TEXT,
  registered_at TEXT NOT NULL,
  CHECK(
    (availability='AVAILABLE' AND unavailable_reason IS NULL) OR
    (availability='UNAVAILABLE' AND unavailable_reason IS NOT NULL)
  )
) STRICT;

CREATE TABLE product_materials(
  material_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  material_kind TEXT NOT NULL CHECK(material_kind IN ('PDF','TEX','IMAGE','TEXT')),
  original_artifact_id TEXT NOT NULL,
  original_artifact_sha256 TEXT NOT NULL CHECK(length(original_artifact_sha256)=64),
  original_artifact_byte_count INTEGER NOT NULL CHECK(original_artifact_byte_count>=0),
  original_artifact_media_type TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(run_id, original_artifact_id)
) STRICT;

CREATE TABLE product_material_extractions(
  extraction_id TEXT PRIMARY KEY,
  material_id TEXT NOT NULL REFERENCES product_materials(material_id) ON DELETE RESTRICT,
  profile_id TEXT NOT NULL
    REFERENCES product_material_profiles(profile_id) ON DELETE RESTRICT,
  mode TEXT NOT NULL CHECK(mode IN ('MACHINE','HUMAN_REVISION')),
  supersedes_extraction_id TEXT
    REFERENCES product_material_extractions(extraction_id) ON DELETE RESTRICT,
  tool_run_id TEXT REFERENCES product_tool_runs(tool_run_id) ON DELETE RESTRICT,
  attempt_id TEXT REFERENCES product_tool_attempts(attempt_id) ON DELETE RESTRICT,
  status TEXT NOT NULL CHECK(status IN ('SUCCEEDED','PROFILE_UNAVAILABLE','FAILED')),
  parser_build TEXT NOT NULL,
  text_artifact_id TEXT,
  text_artifact_sha256 TEXT CHECK(text_artifact_sha256 IS NULL OR length(text_artifact_sha256)=64),
  text_artifact_byte_count INTEGER CHECK(text_artifact_byte_count IS NULL OR text_artifact_byte_count>=0),
  text_artifact_media_type TEXT,
  layout_artifact_id TEXT,
  formula_artifact_id TEXT,
  difference_artifact_id TEXT,
  revision_reason TEXT,
  revised_by TEXT,
  error_code TEXT,
  error_detail TEXT,
  created_at TEXT NOT NULL,
  CHECK(
    (mode='MACHINE' AND supersedes_extraction_id IS NULL AND revision_reason IS NULL AND revised_by IS NULL) OR
    (mode='HUMAN_REVISION' AND supersedes_extraction_id IS NOT NULL AND revision_reason IS NOT NULL AND revised_by IS NOT NULL)
  ),
  CHECK(
    (mode='MACHINE' AND tool_run_id IS NOT NULL AND attempt_id IS NOT NULL) OR
    (mode='HUMAN_REVISION' AND tool_run_id IS NULL AND attempt_id IS NULL)
  ),
  CHECK(
    (status='SUCCEEDED' AND text_artifact_id IS NOT NULL AND layout_artifact_id IS NOT NULL AND formula_artifact_id IS NOT NULL AND error_code IS NULL) OR
    (status<>'SUCCEEDED' AND text_artifact_id IS NULL AND error_code IS NOT NULL)
  )
) STRICT;

CREATE INDEX product_material_extractions_history
ON product_material_extractions(material_id, created_at, extraction_id);

CREATE TABLE product_material_anchors(
  anchor_id TEXT PRIMARY KEY,
  extraction_id TEXT NOT NULL
    REFERENCES product_material_extractions(extraction_id) ON DELETE RESTRICT,
  anchor_kind TEXT NOT NULL CHECK(anchor_kind IN ('PAGE_SEGMENT','FORMULA')),
  locator_json TEXT NOT NULL CHECK(json_valid(locator_json) AND json_type(locator_json)='object'),
  excerpt TEXT NOT NULL,
  excerpt_digest TEXT NOT NULL CHECK(length(excerpt_digest)=64),
  created_at TEXT NOT NULL,
  UNIQUE(extraction_id, anchor_kind, locator_json)
) STRICT;

CREATE INDEX product_material_anchors_extraction
ON product_material_anchors(extraction_id, anchor_kind, anchor_id);

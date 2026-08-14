CREATE TABLE product_guidance(
  guidance_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  research_revision INTEGER NOT NULL CHECK(research_revision >= 0),
  contract_version INTEGER NOT NULL CHECK(contract_version > 0),
  checkpoint_id TEXT NOT NULL,
  target_kind TEXT NOT NULL CHECK(target_kind IN ('ROUTE','WORK_ITEM')),
  target_id TEXT NOT NULL,
  route_id TEXT NOT NULL
    REFERENCES product_planned_routes(route_id) ON DELETE RESTRICT,
  kind TEXT NOT NULL CHECK(kind IN (
    'CHANGE_REPRESENTATION','PRIORITIZE_LEMMA','STOP_ROUTE_REQUEST'
  )),
  content_artifact_id TEXT NOT NULL,
  submitted_by TEXT NOT NULL,
  supersedes_guidance_id TEXT UNIQUE
    REFERENCES product_guidance(guidance_id) ON DELETE RESTRICT,
  state TEXT NOT NULL CHECK(state IN (
    'QUEUED','APPLIED','REJECTED','SUPERSEDED','CANCELLED'
  )),
  resolution_code TEXT,
  applied_work_item_id TEXT
    REFERENCES product_work_items(work_item_id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  CHECK(
    (state = 'QUEUED' AND resolution_code IS NULL
      AND applied_work_item_id IS NULL AND resolved_at IS NULL) OR
    (state = 'APPLIED' AND resolution_code = 'WORK_INPUT_CHANGED'
      AND applied_work_item_id IS NOT NULL AND resolved_at IS NOT NULL) OR
    (state IN ('REJECTED','SUPERSEDED','CANCELLED')
      AND resolution_code IS NOT NULL
      AND applied_work_item_id IS NULL AND resolved_at IS NOT NULL)
  )
) STRICT;

CREATE INDEX product_guidance_queue
ON product_guidance(run_id,state,route_id,created_at,guidance_id);

CREATE INDEX product_guidance_checkpoint
ON product_guidance(checkpoint_id,state,guidance_id);

CREATE TABLE product_guidance_effects(
  guidance_id TEXT PRIMARY KEY
    REFERENCES product_guidance(guidance_id) ON DELETE RESTRICT,
  work_item_id TEXT NOT NULL UNIQUE
    REFERENCES product_work_items(work_item_id) ON DELETE RESTRICT,
  effect_kind TEXT NOT NULL CHECK(effect_kind IN (
    'REPRESENTATION_INPUT','LEMMA_PRIORITY_INPUT'
  )),
  content_artifact_id TEXT NOT NULL,
  input_artifact_ids_json TEXT NOT NULL CHECK(
    json_valid(input_artifact_ids_json)
    AND json_type(input_artifact_ids_json) = 'array'
  ),
  applied_at TEXT NOT NULL
) WITHOUT ROWID, STRICT;

CREATE TABLE product_activity_events(
  cursor INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  scope_kind TEXT NOT NULL CHECK(scope_kind IN ('GLOBAL','RUN','DEPLOYMENT')),
  run_id TEXT,
  deployment_id TEXT,
  source TEXT NOT NULL,
  research_revision INTEGER CHECK(research_revision IS NULL OR research_revision >= 0),
  kernel_event_id TEXT UNIQUE,
  entity_refs TEXT NOT NULL CHECK(json_valid(entity_refs)),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  recorded_at TEXT NOT NULL,
  CHECK(
    (scope_kind = 'GLOBAL' AND run_id IS NULL AND deployment_id IS NULL) OR
    (scope_kind = 'RUN' AND run_id IS NOT NULL AND deployment_id IS NULL) OR
    (scope_kind = 'DEPLOYMENT' AND run_id IS NULL AND deployment_id IS NOT NULL)
  )
) STRICT;

CREATE INDEX product_activity_run_cursor
ON product_activity_events(run_id, cursor)
WHERE scope_kind = 'RUN';

CREATE INDEX product_activity_deployment_cursor
ON product_activity_events(deployment_id, cursor)
WHERE scope_kind = 'DEPLOYMENT';

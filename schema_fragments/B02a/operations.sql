CREATE TABLE product_receipts(
  receipt_id TEXT PRIMARY KEY,
  receipt_version INTEGER NOT NULL CHECK(receipt_version > 0),
  scope_key TEXT NOT NULL,
  request_id TEXT NOT NULL,
  request_digest TEXT NOT NULL CHECK(length(request_digest) = 64),
  state TEXT NOT NULL CHECK(state IN ('PENDING','DECIDED','OUTCOME_UNKNOWN')),
  receipt_json TEXT NOT NULL CHECK(json_valid(receipt_json)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(scope_key, request_id)
) STRICT;

CREATE INDEX product_receipt_state_updated
ON product_receipts(state, updated_at, receipt_id);

CREATE TABLE product_integration_outbox(
  outbox_id TEXT PRIMARY KEY,
  activity_cursor INTEGER NOT NULL UNIQUE
    REFERENCES product_activity_events(cursor) ON DELETE RESTRICT,
  topic TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('PENDING','DELIVERING','DELIVERED','FAILED')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  available_at TEXT NOT NULL,
  delivered_at TEXT
) STRICT;

CREATE INDEX product_outbox_delivery
ON product_integration_outbox(state, available_at, activity_cursor);

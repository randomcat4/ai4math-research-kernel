CREATE TABLE product_public_logs(
  log_id TEXT PRIMARY KEY,
  scope_kind TEXT NOT NULL CHECK(scope_kind IN ('GLOBAL','RUN','DEPLOYMENT')),
  scope_id TEXT NOT NULL,
  producer_run_id TEXT NOT NULL,
  producer_kind TEXT NOT NULL CHECK(producer_kind = 'MANAGED_PROCESS'),
  stream TEXT NOT NULL CHECK(stream IN ('STDOUT','STDERR')),
  state TEXT NOT NULL CHECK(state IN ('OPEN','SEALING','SEALED')),
  logical_name TEXT NOT NULL,
  byte_count INTEGER NOT NULL DEFAULT 0 CHECK(byte_count >= 0),
  artifact_id TEXT REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  sealed_at TEXT,
  UNIQUE(producer_run_id, stream),
  CHECK(
    (state = 'SEALED' AND artifact_id IS NOT NULL AND sealed_at IS NOT NULL) OR
    (state <> 'SEALED' AND artifact_id IS NULL AND sealed_at IS NULL)
  )
) STRICT;

CREATE INDEX product_public_logs_scope
ON product_public_logs(scope_kind, scope_id, producer_run_id, stream);

CREATE TABLE product_public_log_chunks(
  log_id TEXT NOT NULL REFERENCES product_public_logs(log_id) ON DELETE RESTRICT,
  chunk_offset INTEGER NOT NULL CHECK(chunk_offset >= 0),
  byte_count INTEGER NOT NULL CHECK(byte_count > 0),
  transfer_sha256 TEXT NOT NULL CHECK(
    length(transfer_sha256) = 64 AND transfer_sha256 = lower(transfer_sha256)
  ),
  data BLOB NOT NULL CHECK(typeof(data) = 'blob' AND length(data) = byte_count),
  appended_at TEXT NOT NULL,
  PRIMARY KEY(log_id, chunk_offset)
) WITHOUT ROWID, STRICT;

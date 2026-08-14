CREATE TABLE product_uploads(
  upload_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK(state IN ('OPEN','COMMITTING','COMMITTED','ABORTED')),
  logical_name TEXT NOT NULL,
  media_type TEXT NOT NULL,
  declared_byte_count INTEGER NOT NULL CHECK(declared_byte_count >= 0),
  declared_sha256 TEXT NOT NULL CHECK(
    length(declared_sha256) = 64 AND declared_sha256 = lower(declared_sha256)
  ),
  received_byte_count INTEGER NOT NULL DEFAULT 0 CHECK(
    received_byte_count >= 0 AND received_byte_count <= declared_byte_count
  ),
  spool_name TEXT NOT NULL UNIQUE,
  artifact_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  committed_at TEXT,
  CHECK(
    (state = 'COMMITTED' AND artifact_id IS NOT NULL AND committed_at IS NOT NULL) OR
    (state <> 'COMMITTED' AND artifact_id IS NULL AND committed_at IS NULL)
  )
) STRICT;

CREATE INDEX product_upload_resume
ON product_uploads(state, updated_at, upload_id);

CREATE TABLE product_upload_chunks(
  upload_id TEXT NOT NULL REFERENCES product_uploads(upload_id) ON DELETE CASCADE,
  chunk_offset INTEGER NOT NULL CHECK(chunk_offset >= 0),
  byte_count INTEGER NOT NULL CHECK(byte_count > 0),
  transfer_sha256 TEXT NOT NULL CHECK(
    length(transfer_sha256) = 64 AND transfer_sha256 = lower(transfer_sha256)
  ),
  accepted_at TEXT NOT NULL,
  PRIMARY KEY(upload_id, chunk_offset)
) WITHOUT ROWID, STRICT;

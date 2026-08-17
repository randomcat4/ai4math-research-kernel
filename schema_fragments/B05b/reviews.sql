CREATE TABLE product_review_tasks(
  review_task_id TEXT PRIMARY KEY,
  review_type TEXT NOT NULL CHECK(review_type IN ('ATOMIC','COMPOSITION','PAPER')),
  binding_json TEXT NOT NULL CHECK(json_valid(binding_json) AND json_type(binding_json) = 'object'),
  author_subject_ids_json TEXT NOT NULL CHECK(json_valid(author_subject_ids_json) AND json_type(author_subject_ids_json) = 'array'),
  assignee_identity_id TEXT NOT NULL REFERENCES product_identities(identity_id) ON DELETE RESTRICT,
  independence_required INTEGER NOT NULL DEFAULT 1 CHECK(independence_required = 1),
  independence_status TEXT NOT NULL DEFAULT 'PENDING' CHECK(independence_status IN ('PENDING','VERIFIED','FAILED')),
  status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','CLAIMED','SUBMITTED','EXPIRED','REASSIGNED','INVALIDATED')),
  signed_artifact_id TEXT,
  signed_artifact_sha256 TEXT,
  signed_artifact_byte_count INTEGER,
  signed_artifact_media_type TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  claimed_at TEXT,
  submitted_at TEXT,
  CHECK(expires_at > created_at),
  CHECK(
    (status IN ('OPEN','REASSIGNED') AND claimed_at IS NULL AND submitted_at IS NULL) OR
    (status = 'CLAIMED' AND claimed_at IS NOT NULL AND submitted_at IS NULL) OR
    (status = 'SUBMITTED' AND claimed_at IS NOT NULL AND submitted_at IS NOT NULL) OR
    (status IN ('EXPIRED','INVALIDATED') AND submitted_at IS NULL)
  ),
  CHECK(
    (signed_artifact_id IS NULL AND signed_artifact_sha256 IS NULL AND signed_artifact_byte_count IS NULL AND signed_artifact_media_type IS NULL) OR
    (signed_artifact_id IS NOT NULL AND length(signed_artifact_sha256) = 64 AND signed_artifact_byte_count >= 0 AND length(signed_artifact_media_type) > 0)
  ),
  CHECK(
    (status = 'SUBMITTED' AND independence_status = 'VERIFIED' AND signed_artifact_id IS NOT NULL) OR
    (status <> 'SUBMITTED' AND signed_artifact_id IS NULL)
  )
) STRICT;

CREATE INDEX product_review_task_assignee_status
ON product_review_tasks(assignee_identity_id, status, expires_at, review_task_id);

CREATE INDEX product_review_task_run_status
ON product_review_tasks(json_extract(binding_json, '$.run_id'), status, review_task_id);

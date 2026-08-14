CREATE TABLE product_identities_b05c(
  identity_id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN (
    'MAIN',
    'LITERATURE_REVIEWER',
    'WORKER',
    'MACHINE_VERIFIER',
    'PEER_REVIEWER',
    'PAPER_REVIEWER',
    'PUBLICATION_WORKER',
    'ADMIN'
  )),
  capability_id TEXT NOT NULL UNIQUE,
  credential_salt BLOB NOT NULL CHECK(length(credential_salt) = 16),
  credential_digest BLOB NOT NULL CHECK(length(credential_digest) = 32),
  enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
  created_at TEXT NOT NULL,
  disabled_at TEXT,
  CHECK(
    (enabled = 1 AND disabled_at IS NULL) OR
    (enabled = 0 AND disabled_at IS NOT NULL)
  )
) STRICT;

INSERT INTO product_identities_b05c(
  identity_id,subject_id,display_name,role,capability_id,credential_salt,
  credential_digest,enabled,created_at,disabled_at
)
SELECT
  identity_id,
  subject_id,
  display_name,
  CASE role WHEN 'REVIEWER' THEN 'PEER_REVIEWER' ELSE role END,
  capability_id,
  credential_salt,
  credential_digest,
  enabled,
  created_at,
  disabled_at
FROM product_identities;

CREATE TABLE product_sessions_b05c(
  session_id TEXT PRIMARY KEY,
  organization_id TEXT NOT NULL,
  active_identity_id TEXT NOT NULL
    REFERENCES product_identities_b05c(identity_id) ON DELETE RESTRICT,
  session_version INTEGER NOT NULL CHECK(session_version > 0),
  issued_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT,
  CHECK(expires_at > issued_at)
) STRICT;

INSERT INTO product_sessions_b05c(
  session_id,organization_id,active_identity_id,session_version,issued_at,expires_at,revoked_at
)
SELECT
  session_id,organization_id,active_identity_id,session_version,issued_at,expires_at,revoked_at
FROM product_sessions;

CREATE TABLE product_session_identities_b05c(
  session_id TEXT NOT NULL
    REFERENCES product_sessions_b05c(session_id) ON DELETE CASCADE,
  identity_id TEXT NOT NULL
    REFERENCES product_identities_b05c(identity_id) ON DELETE RESTRICT,
  authenticated_at TEXT NOT NULL,
  PRIMARY KEY(session_id, identity_id)
) STRICT;

INSERT INTO product_session_identities_b05c(session_id,identity_id,authenticated_at)
SELECT session_id,identity_id,authenticated_at
FROM product_session_identities;

CREATE TABLE product_review_tasks_b05c(
  review_task_id TEXT PRIMARY KEY,
  review_type TEXT NOT NULL CHECK(review_type IN ('ATOMIC','COMPOSITION','PAPER')),
  binding_json TEXT NOT NULL CHECK(json_valid(binding_json) AND json_type(binding_json) = 'object'),
  author_subject_ids_json TEXT NOT NULL CHECK(json_valid(author_subject_ids_json) AND json_type(author_subject_ids_json) = 'array'),
  assignee_identity_id TEXT NOT NULL REFERENCES product_identities_b05c(identity_id) ON DELETE RESTRICT,
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

INSERT INTO product_review_tasks_b05c(
  review_task_id,review_type,binding_json,author_subject_ids_json,assignee_identity_id,
  independence_required,independence_status,status,signed_artifact_id,
  signed_artifact_sha256,signed_artifact_byte_count,signed_artifact_media_type,
  created_at,expires_at,claimed_at,submitted_at
)
SELECT
  review_task_id,review_type,binding_json,author_subject_ids_json,assignee_identity_id,
  independence_required,independence_status,status,signed_artifact_id,
  signed_artifact_sha256,signed_artifact_byte_count,signed_artifact_media_type,
  created_at,expires_at,claimed_at,submitted_at
FROM product_review_tasks;

DROP TABLE product_review_tasks;
DROP TABLE product_session_identities;
DROP TABLE product_sessions;
DROP TABLE product_identities;

ALTER TABLE product_identities_b05c RENAME TO product_identities;
ALTER TABLE product_sessions_b05c RENAME TO product_sessions;
ALTER TABLE product_session_identities_b05c RENAME TO product_session_identities;
ALTER TABLE product_review_tasks_b05c RENAME TO product_review_tasks;

CREATE INDEX product_identity_role8_enabled
ON product_identities(role, enabled, identity_id);

CREATE INDEX product_session_role8_expiry
ON product_sessions(revoked_at, expires_at, session_id);

CREATE INDEX product_session_identity_role8_lookup
ON product_session_identities(identity_id, session_id);

CREATE INDEX product_review_role8_assignee_status
ON product_review_tasks(assignee_identity_id, status, expires_at, review_task_id);

CREATE INDEX product_review_role8_run_status
ON product_review_tasks(json_extract(binding_json, '$.run_id'), status, review_task_id);

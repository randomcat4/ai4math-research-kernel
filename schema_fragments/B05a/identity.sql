CREATE TABLE product_identities(
  identity_id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('MAIN','WORKER','REVIEWER','ADMIN')),
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

CREATE INDEX product_identity_role_enabled
ON product_identities(role, enabled, identity_id);

CREATE TABLE product_sessions(
  session_id TEXT PRIMARY KEY,
  organization_id TEXT NOT NULL,
  active_identity_id TEXT NOT NULL
    REFERENCES product_identities(identity_id) ON DELETE RESTRICT,
  session_version INTEGER NOT NULL CHECK(session_version > 0),
  issued_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT,
  CHECK(expires_at > issued_at)
) STRICT;

CREATE INDEX product_session_expiry
ON product_sessions(revoked_at, expires_at, session_id);

CREATE TABLE product_session_identities(
  session_id TEXT NOT NULL
    REFERENCES product_sessions(session_id) ON DELETE CASCADE,
  identity_id TEXT NOT NULL
    REFERENCES product_identities(identity_id) ON DELETE RESTRICT,
  authenticated_at TEXT NOT NULL,
  PRIMARY KEY(session_id, identity_id)
) STRICT;

CREATE INDEX product_session_identity_lookup
ON product_session_identities(identity_id, session_id);
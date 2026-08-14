CREATE TABLE product_managed_sessions(
  session_id TEXT PRIMARY KEY
    REFERENCES product_sessions(session_id) ON DELETE CASCADE,
  identity_id TEXT NOT NULL UNIQUE
    REFERENCES product_identities(identity_id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL
) STRICT;

CREATE INDEX product_managed_session_identity
ON product_managed_sessions(identity_id, session_id);

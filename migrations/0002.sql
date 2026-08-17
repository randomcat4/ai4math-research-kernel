-- migration-name: rk_v1_run_artifacts
-- Associate immutable artifacts with one run without exposing host paths or bytes.
PRAGMA foreign_keys = ON;

BEGIN IMMEDIATE;

CREATE TABLE run_artifacts (
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    artifact_id             TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    logical_name            TEXT NOT NULL,
    role                    TEXT NOT NULL CHECK (role IN
                               ('CONTRACT','CREATE_INPUT','APPLY_INPUT','CLOSURE_GRAPH','DOSSIER')),
    linked_at               TEXT NOT NULL,
    PRIMARY KEY (run_id, artifact_id, logical_name),
    UNIQUE (run_id, logical_name, role)
);

CREATE INDEX ix_run_artifacts_run_role ON run_artifacts(run_id, role, logical_name);

COMMIT;

-- The migration runner records this file's exact digest after the schema commit.

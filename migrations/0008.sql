-- migration-name: rk_fact_and_paper_artifact_roles
PRAGMA foreign_keys = ON;

BEGIN IMMEDIATE;

ALTER TABLE run_artifacts RENAME TO run_artifacts_v02;

CREATE TABLE run_artifacts (
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    artifact_id             TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    logical_name            TEXT NOT NULL,
    role                    TEXT NOT NULL CHECK (role IN
                               ('CONTRACT','CREATE_INPUT','APPLY_INPUT','CLOSURE_GRAPH','DOSSIER',
                                'RESEARCH_MATERIAL','COMPONENT_RESULT','HUMAN_REVIEW',
                                'CLAIM_STATEMENT','PEER_REVIEW','PAPER_REVIEW')),
    linked_at               TEXT NOT NULL,
    PRIMARY KEY (run_id, artifact_id, logical_name),
    UNIQUE (run_id, logical_name, role)
);

INSERT INTO run_artifacts(run_id,artifact_id,logical_name,role,linked_at)
SELECT run_id,artifact_id,logical_name,role,linked_at FROM run_artifacts_v02;

DROP TABLE run_artifacts_v02;

CREATE INDEX ix_run_artifacts_run_role ON run_artifacts(run_id, role, logical_name);

COMMIT;

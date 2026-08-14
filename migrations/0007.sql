CREATE TABLE research_hints (
    hint_id                    TEXT PRIMARY KEY,
    run_id                     TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    contract_version           INTEGER NOT NULL,
    hint_kind                  TEXT NOT NULL CHECK (hint_kind IN
                                  ('CHANGE_REPRESENTATION','STOP_ROUTE','PRIORITIZE_LEMMA',
                                   'CHANGE_STRATEGY','OTHER')),
    hint_text                  TEXT NOT NULL CHECK (length(trim(hint_text)) > 0),
    target_route_id            TEXT REFERENCES routes(route_id) ON DELETE RESTRICT,
    target_claim_id            TEXT REFERENCES claims(claim_id) ON DELETE RESTRICT,
    checkpoint_label           TEXT NOT NULL,
    created_by_capability_id   TEXT NOT NULL REFERENCES capabilities(capability_id),
    created_by_event_id        TEXT NOT NULL REFERENCES events(event_id),
    created_at                 TEXT NOT NULL,
    FOREIGN KEY (run_id, contract_version)
        REFERENCES contract_versions(run_id, version) ON DELETE RESTRICT
);

CREATE INDEX ix_research_hints_run_created ON research_hints(run_id, created_at, hint_id);

CREATE TABLE paper_reviews (
    paper_review_id             TEXT PRIMARY KEY,
    run_id                      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    contract_version            INTEGER NOT NULL,
    final_fact_id               TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE RESTRICT,
    paper_sha256                TEXT NOT NULL CHECK (length(paper_sha256) = 64),
    status                      TEXT NOT NULL CHECK (status IN ('CORRECT','WRONG','PENDING','OVERRIDDEN')),
    review_artifact_id          TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    reviewer_capability_id      TEXT NOT NULL REFERENCES capabilities(capability_id),
    created_by_event_id         TEXT NOT NULL REFERENCES events(event_id),
    created_at                  TEXT NOT NULL,
    FOREIGN KEY (run_id, contract_version)
        REFERENCES contract_versions(run_id, version) ON DELETE RESTRICT
);

CREATE INDEX ix_paper_reviews_final ON paper_reviews(run_id, final_fact_id, created_at);

CREATE TABLE atomic_verifications (
    verification_id             TEXT PRIMARY KEY,
    run_id                      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    contract_version            INTEGER NOT NULL,
    claim_id                    TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE RESTRICT,
    backend                     TEXT NOT NULL CHECK (backend IN
                                  ('LEAN','DETERMINISTIC_CHECKER','MANAGED_PEER','SOFT_VERIFIER')),
    verdict                     TEXT NOT NULL CHECK (verdict IN ('ACCEPTED','REJECTED')),
    verification_ref            TEXT,
    repair_feedback             TEXT NOT NULL DEFAULT '',
    verifier_capability_id      TEXT NOT NULL REFERENCES capabilities(capability_id),
    created_by_event_id         TEXT NOT NULL REFERENCES events(event_id),
    created_at                  TEXT NOT NULL,
    FOREIGN KEY (run_id, contract_version)
        REFERENCES contract_versions(run_id, version) ON DELETE RESTRICT
);

CREATE INDEX ix_atomic_verifications_claim ON atomic_verifications(claim_id, created_at);

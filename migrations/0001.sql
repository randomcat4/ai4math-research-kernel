-- migration-name: rk_v1_initial
-- RK-PRD-2 implementation schema v1
-- SQLite >= 3.45. Execute as one migration on an empty local database.
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA busy_timeout = 5000;

BEGIN IMMEDIATE;

CREATE TABLE schema_migrations (
    version                 INTEGER PRIMARY KEY CHECK (version > 0),
    name                    TEXT NOT NULL UNIQUE,
    sha256                  TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 GLOB '[0-9a-f]*'),
    applied_at              TEXT NOT NULL
);

CREATE TABLE capabilities (
    capability_id           TEXT PRIMARY KEY,
    subject_id              TEXT NOT NULL,
    issuer                  TEXT NOT NULL,
    key_id                  TEXT NOT NULL,
    allowed_actions_json    TEXT NOT NULL CHECK (json_valid(allowed_actions_json)),
    run_scope_json          TEXT NOT NULL CHECK (json_valid(run_scope_json)),
    nonce                   TEXT NOT NULL UNIQUE,
    credential_digest       TEXT NOT NULL UNIQUE CHECK (length(credential_digest) = 64),
    issued_at               TEXT NOT NULL,
    expires_at              TEXT NOT NULL,
    revoked_at              TEXT,
    revocation_reason       TEXT,
    CHECK (expires_at > issued_at),
    CHECK ((revoked_at IS NULL) = (revocation_reason IS NULL))
);

CREATE TABLE runs (
    run_id                  TEXT PRIMARY KEY,
    stable_project_id       TEXT NOT NULL,
    create_issuer           TEXT NOT NULL,
    create_request_id       TEXT NOT NULL,
    create_request_digest   TEXT NOT NULL CHECK (length(create_request_digest) = 64),
    created_by_capability_id TEXT NOT NULL REFERENCES capabilities(capability_id) ON DELETE RESTRICT,
    status                  TEXT NOT NULL CHECK (status IN
                               ('OPEN','RUNNING','PAUSED','CLOSED','CONTRACT_DEFECTIVE')),
    revision                INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    current_contract_version INTEGER,
    root_claim_id           TEXT,
    parent_dossier_artifact_id TEXT,
    final_outcome           TEXT CHECK (final_outcome IS NULL OR final_outcome IN
                               ('PROVED','DISPROVED','ROUTE_LOCAL','PREVIOUSLY_KNOWN',
                                'CONTRACT_DEFECTIVE','UNRESOLVED')),
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    closed_at               TEXT,
    CHECK ((status = 'CLOSED') = (closed_at IS NOT NULL)),
    CHECK (final_outcome IS NULL OR status = 'CLOSED'),
    FOREIGN KEY (run_id, current_contract_version)
        REFERENCES contract_versions(run_id, version) ON DELETE RESTRICT,
    FOREIGN KEY (root_claim_id)
        REFERENCES claims(claim_id) ON DELETE RESTRICT,
    FOREIGN KEY (parent_dossier_artifact_id)
        REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    UNIQUE (run_id, revision),
    UNIQUE (create_issuer, create_request_id)
);

CREATE TABLE artifacts (
    artifact_id             TEXT PRIMARY KEY,
    sha256                  TEXT NOT NULL UNIQUE CHECK (length(sha256) = 64),
    byte_count              INTEGER NOT NULL CHECK (byte_count >= 0),
    media_type              TEXT NOT NULL,
    cas_relpath             TEXT NOT NULL UNIQUE,
    ingest_state            TEXT NOT NULL CHECK (ingest_state IN
                               ('STAGED','COMMITTED','QUARANTINED','ORPHANED')),
    quarantine_code         TEXT,
    source_name             TEXT,
    original_path           TEXT,
    line_count              INTEGER CHECK (line_count IS NULL OR line_count >= 0),
    created_at              TEXT NOT NULL,
    committed_at            TEXT,
    CHECK ((ingest_state = 'QUARANTINED') <= (quarantine_code IS NOT NULL)),
    CHECK ((ingest_state = 'COMMITTED') <= (committed_at IS NOT NULL))
);

CREATE TABLE contract_versions (
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    version                 INTEGER NOT NULL CHECK (version > 0),
    status                  TEXT NOT NULL CHECK (status IN
                               ('DRAFT','FROZEN','DEFECT_PROPOSED','SUPERSEDED')),
    contract_artifact_id    TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    contract_json           TEXT NOT NULL CHECK (json_valid(contract_json)),
    statement_hash          TEXT NOT NULL CHECK (length(statement_hash) = 64),
    supersedes_version      INTEGER,
    defect_type             TEXT,
    defect_evidence_json    TEXT CHECK (defect_evidence_json IS NULL OR json_valid(defect_evidence_json)),
    impact_analysis_json    TEXT CHECK (impact_analysis_json IS NULL OR json_valid(impact_analysis_json)),
    created_by_capability_id TEXT NOT NULL REFERENCES capabilities(capability_id) ON DELETE RESTRICT,
    approved_by_json        TEXT NOT NULL CHECK (json_valid(approved_by_json)),
    created_at              TEXT NOT NULL,
    frozen_at               TEXT,
    PRIMARY KEY (run_id, version),
    FOREIGN KEY (run_id, supersedes_version)
        REFERENCES contract_versions(run_id, version) ON DELETE RESTRICT,
    CHECK ((status = 'DRAFT') OR frozen_at IS NOT NULL),
    CHECK ((version = 1 AND supersedes_version IS NULL) OR
           (version > 1 AND supersedes_version = version - 1))
);

CREATE TABLE commands (
    command_id              TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    request_id              TEXT NOT NULL,
    command_type            TEXT NOT NULL,
    request_digest          TEXT NOT NULL CHECK (length(request_digest) = 64),
    expected_revision       INTEGER NOT NULL CHECK (expected_revision >= 0),
    capability_id           TEXT NOT NULL REFERENCES capabilities(capability_id) ON DELETE RESTRICT,
    accepted                INTEGER NOT NULL CHECK (accepted IN (0,1)),
    revision_before         INTEGER NOT NULL CHECK (revision_before >= 0),
    revision_after          INTEGER NOT NULL CHECK (revision_after >= 0),
    rejection_code          TEXT,
    missing_conditions_json TEXT NOT NULL CHECK (json_valid(missing_conditions_json)),
    receipt_json            TEXT NOT NULL CHECK (json_valid(receipt_json)),
    trace_id                TEXT NOT NULL,
    decided_at              TEXT NOT NULL,
    UNIQUE (run_id, request_id),
    CHECK ((accepted = 1 AND revision_after = revision_before + 1 AND rejection_code IS NULL) OR
           (accepted = 0 AND revision_after = revision_before AND rejection_code IS NOT NULL))
);

CREATE TABLE events (
    event_seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id                TEXT NOT NULL UNIQUE,
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    command_id              TEXT NOT NULL REFERENCES commands(command_id) ON DELETE RESTRICT,
    revision                INTEGER NOT NULL CHECK (revision > 0),
    event_type              TEXT NOT NULL,
    payload_json            TEXT NOT NULL CHECK (json_valid(payload_json)),
    contract_version        INTEGER,
    route_id                TEXT,
    claim_id                TEXT,
    attempt_id              TEXT,
    recorded_at             TEXT NOT NULL,
    UNIQUE (run_id, revision, event_id),
    FOREIGN KEY (run_id, contract_version)
        REFERENCES contract_versions(run_id, version) ON DELETE RESTRICT
);

CREATE TABLE claims (
    claim_id                TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    contract_version        INTEGER NOT NULL,
    claim_kind              TEXT NOT NULL CHECK (claim_kind IN
                               ('ROOT','LEMMA','AUXILIARY','COUNTEREXAMPLE','SIDE_FINDING','BRIDGE')),
    stable_label            TEXT NOT NULL,
    statement_revision      INTEGER NOT NULL DEFAULT 1 CHECK (statement_revision > 0),
    statement_artifact_id   TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    statement_hash          TEXT NOT NULL CHECK (length(statement_hash) = 64),
    normalized_statement_json TEXT NOT NULL CHECK (json_valid(normalized_statement_json)),
    lifecycle_status        TEXT NOT NULL CHECK (lifecycle_status IN
                               ('ACTIVE','INVALIDATED','SUPERSEDED','RETIRED')),
    route_result            TEXT NOT NULL CHECK (route_result IN
                               ('UNASSESSED','CANDIDATE','LOCAL_LEMMAS_VERIFIED','ROUTE_LOCAL',
                                'ROUTE_PROVED','REFUTED','PREVIOUSLY_KNOWN')),
    machine_verdict         TEXT NOT NULL CHECK (machine_verdict IN
                               ('UNVERIFIED','KERNEL_VERIFIED','CERTIFICATE_VERIFIED','REPLAY_FAILED')),
    semantic_verdict        TEXT NOT NULL CHECK (semantic_verdict IN
                               ('UNREVIEWED','TESTED','HUMAN_ATTESTED','REFUTED')),
    peer_verdict            TEXT NOT NULL CHECK (peer_verdict IN
                               ('UNREVIEWED','ACCEPTED','REJECTED','NEEDS_REVISION')),
    quality_verdict         TEXT NOT NULL CHECK (quality_verdict IN
                               ('UNREVIEWED','ACCEPTED','REJECTED','NEEDS_REVISION')),
    closure_state           TEXT NOT NULL CHECK (closure_state IN
                               ('NOT_REQUIRED','OPEN','CLOSED_MACHINE','CLOSED_HUMAN','CLOSED_HYBRID','INVALIDATED')),
    supersedes_claim_id     TEXT REFERENCES claims(claim_id) ON DELETE RESTRICT,
    invalidated_by_event_id TEXT REFERENCES events(event_id) ON DELETE RESTRICT,
    created_by_event_id     TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    FOREIGN KEY (run_id, contract_version)
        REFERENCES contract_versions(run_id, version) ON DELETE RESTRICT,
    UNIQUE (run_id, stable_label, statement_revision),
    UNIQUE (claim_id, run_id, contract_version, statement_hash)
);

CREATE TABLE claim_edges (
    edge_id                 TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    contract_version        INTEGER NOT NULL,
    from_claim_id           TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE RESTRICT,
    to_claim_id             TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE RESTRICT,
    edge_kind               TEXT NOT NULL CHECK (edge_kind IN
                               ('IMPLIES','EQUIVALENT_TO','DEPENDS_ON','SPECIALIZES','GENERALIZES',
                                'CONTRADICTS','SUPERSEDES')),
    direction               TEXT NOT NULL CHECK (direction IN ('FORWARD','REVERSE','BIDIRECTIONAL')),
    justification_kind      TEXT NOT NULL CHECK (justification_kind IN
                               ('LEAN_DECLARATION','CHECKER_PROFILE','HUMAN_ARGUMENT','BRIDGE','DEFINITIONAL')),
    justification_ref       TEXT NOT NULL,
    status                  TEXT NOT NULL CHECK (status IN ('ACTIVE','INVALIDATED','REJECTED')),
    created_by_event_id     TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    invalidated_by_event_id TEXT REFERENCES events(event_id) ON DELETE RESTRICT,
    created_at              TEXT NOT NULL,
    FOREIGN KEY (run_id, contract_version)
        REFERENCES contract_versions(run_id, version) ON DELETE RESTRICT,
    CHECK (from_claim_id <> to_claim_id),
    UNIQUE (run_id, contract_version, from_claim_id, to_claim_id, edge_kind, direction)
);

CREATE TABLE composition_obligations (
    obligation_id           TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    contract_version        INTEGER NOT NULL,
    parent_claim_id         TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE RESTRICT,
    child_claim_ids_json    TEXT NOT NULL CHECK (json_valid(child_claim_ids_json)),
    local_domain_json       TEXT NOT NULL CHECK (json_valid(local_domain_json)),
    coverage_ref            TEXT NOT NULL,
    coverage_status         TEXT NOT NULL CHECK (coverage_status IN
                               ('MACHINE_CHECKED','HUMAN_ATTESTED','OPEN','NOT_APPLICABLE')),
    compatibility_ref       TEXT NOT NULL,
    compatibility_status    TEXT NOT NULL CHECK (compatibility_status IN
                               ('MACHINE_CHECKED','HUMAN_ATTESTED','OPEN','NOT_APPLICABLE')),
    invariant_ref           TEXT NOT NULL,
    invariant_status        TEXT NOT NULL CHECK (invariant_status IN
                               ('MACHINE_CHECKED','HUMAN_ATTESTED','OPEN','NOT_APPLICABLE')),
    progress_ref            TEXT NOT NULL,
    progress_status         TEXT NOT NULL CHECK (progress_status IN
                               ('MACHINE_CHECKED','HUMAN_ATTESTED','OPEN','NOT_APPLICABLE')),
    boundary_ref            TEXT NOT NULL,
    boundary_status         TEXT NOT NULL CHECK (boundary_status IN
                               ('MACHINE_CHECKED','HUMAN_ATTESTED','OPEN','NOT_APPLICABLE')),
    simultaneous_choice_ref TEXT NOT NULL,
    simultaneous_choice_status TEXT NOT NULL CHECK (simultaneous_choice_status IN
                               ('MACHINE_CHECKED','HUMAN_ATTESTED','OPEN','NOT_APPLICABLE')),
    composition_rule        TEXT NOT NULL CHECK (composition_rule IN
                               ('LEAN_DECLARATION','CHECKER_PROFILE','HUMAN_ARGUMENT',
                                'HYBRID_CUTS','DIRECT_EDGE')),
    closure_theorem_ref     TEXT NOT NULL,
    missing_conditions_json TEXT NOT NULL CHECK (json_valid(missing_conditions_json)),
    displacement_status     TEXT NOT NULL CHECK (displacement_status IN
                               ('NOT_ASSESSED','NO_DISPLACEMENT','OBLIGATION_DISPLACEMENT')),
    status                  TEXT NOT NULL CHECK (status IN
                               ('OPEN','DISCHARGED_MACHINE','DISCHARGED_HUMAN',
                                'DISCHARGED_HYBRID','INVALIDATED')),
    created_by_event_id     TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    updated_by_event_id     TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    FOREIGN KEY (run_id, contract_version)
        REFERENCES contract_versions(run_id, version) ON DELETE RESTRICT
);

CREATE TABLE closure_witnesses (
    witness_id              TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    parent_claim_id         TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE RESTRICT,
    contract_version        INTEGER NOT NULL,
    selected_subgraph_digest TEXT NOT NULL CHECK (length(selected_subgraph_digest) = 64),
    selected_subgraph_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    discharged_obligations_json TEXT NOT NULL CHECK (json_valid(discharged_obligations_json)),
    open_obligations_json   TEXT NOT NULL CHECK (json_valid(open_obligations_json)),
    edge_justifications_json TEXT NOT NULL CHECK (json_valid(edge_justifications_json)),
    bridge_dependencies_json TEXT NOT NULL CHECK (json_valid(bridge_dependencies_json)),
    composition_mode        TEXT NOT NULL CHECK (composition_mode IN
                               ('MACHINE','PEER','HYBRID')),
    verification_refs_json  TEXT NOT NULL CHECK (json_valid(verification_refs_json)),
    human_attestation_review_ids_json TEXT NOT NULL CHECK (json_valid(human_attestation_review_ids_json)),
    status                  TEXT NOT NULL CHECK (status IN
                               ('DRAFT','ACCEPTED','REJECTED','INVALIDATED')),
    created_by_event_id     TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    accepted_by_event_id    TEXT REFERENCES events(event_id) ON DELETE RESTRICT,
    created_at              TEXT NOT NULL,
    FOREIGN KEY (run_id, contract_version)
        REFERENCES contract_versions(run_id, version) ON DELETE RESTRICT,
    UNIQUE (parent_claim_id, contract_version, selected_subgraph_digest)
);

CREATE TABLE approach_roots (
    approach_root_id        TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    label                   TEXT NOT NULL,
    origin_artifact_id      TEXT REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    origin_event_id         TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    parent_root_ids_json    TEXT NOT NULL CHECK (json_valid(parent_root_ids_json)),
    contact_epoch           INTEGER NOT NULL CHECK (contact_epoch >= 0),
    contamination_json      TEXT NOT NULL CHECK (json_valid(contamination_json)),
    created_at              TEXT NOT NULL,
    UNIQUE (run_id, label, contact_epoch)
);

CREATE TABLE evidence_roots (
    evidence_root_id        TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    root_kind               TEXT NOT NULL CHECK (root_kind IN
                               ('LEAN_KERNEL','CHECKER','ENUMERATION','HUMAN','MODEL','EXTERNAL_SOURCE')),
    origin_artifact_id      TEXT REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    verifier_profile_id     TEXT,
    ancestor_root_ids_json  TEXT NOT NULL CHECK (json_valid(ancestor_root_ids_json)),
    source_graph_json       TEXT NOT NULL CHECK (json_valid(source_graph_json)),
    created_by_event_id     TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    created_at              TEXT NOT NULL
);

CREATE TABLE routes (
    route_id                TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    contract_version        INTEGER NOT NULL,
    target_claim_id         TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE RESTRICT,
    label                   TEXT NOT NULL,
    status                  TEXT NOT NULL CHECK (status IN
                               ('SCOUT','ACTIVE','BLOCKED','PAUSED','RETIRED','PROVED','REFUTED')),
    representation         TEXT NOT NULL,
    tool_family             TEXT NOT NULL,
    approach_root_id        TEXT NOT NULL REFERENCES approach_roots(approach_root_id) ON DELETE RESTRICT,
    budget_policy_json      TEXT NOT NULL CHECK (json_valid(budget_policy_json)),
    novelty_zero_streak     INTEGER NOT NULL DEFAULT 0 CHECK (novelty_zero_streak >= 0),
    first_failed_obligation_id TEXT,
    created_by_event_id     TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    retired_by_event_id     TEXT REFERENCES events(event_id) ON DELETE RESTRICT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    FOREIGN KEY (run_id, contract_version)
        REFERENCES contract_versions(run_id, version) ON DELETE RESTRICT,
    UNIQUE (run_id, label)
);

CREATE TABLE attempts (
    attempt_id              TEXT PRIMARY KEY,
    route_id                TEXT NOT NULL REFERENCES routes(route_id) ON DELETE RESTRICT,
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    ordinal                 INTEGER NOT NULL CHECK (ordinal > 0),
    status                  TEXT NOT NULL CHECK (status IN
                               ('QUEUED','RUNNING','PAUSED','SUCCEEDED','FAILED','ABORTED','ENVIRONMENT_ERROR')),
    isolation_epoch         INTEGER NOT NULL CHECK (isolation_epoch >= 0),
    work_relpath            TEXT NOT NULL UNIQUE,
    allowed_write_set_json  TEXT NOT NULL CHECK (json_valid(allowed_write_set_json)),
    input_snapshot_digest   TEXT NOT NULL CHECK (length(input_snapshot_digest) = 64),
    started_at              TEXT,
    ended_at                TEXT,
    created_by_event_id     TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    UNIQUE (route_id, ordinal),
    CHECK ((status IN ('RUNNING','PAUSED','SUCCEEDED','FAILED','ABORTED','ENVIRONMENT_ERROR'))
            <= (started_at IS NOT NULL)),
    CHECK ((status IN ('SUCCEEDED','FAILED','ABORTED','ENVIRONMENT_ERROR'))
            <= (ended_at IS NOT NULL))
);

CREATE TABLE leases (
    lease_id                TEXT PRIMARY KEY,
    attempt_id              TEXT NOT NULL REFERENCES attempts(attempt_id) ON DELETE RESTRICT,
    holder_id               TEXT NOT NULL,
    status                  TEXT NOT NULL CHECK (status IN ('ACTIVE','RELEASED','EXPIRED','REVOKED')),
    acquired_at             TEXT NOT NULL,
    heartbeat_at            TEXT NOT NULL,
    expires_at              TEXT NOT NULL,
    released_at             TEXT,
    created_by_event_id     TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    CHECK (expires_at > acquired_at),
    CHECK ((status = 'ACTIVE') = (released_at IS NULL))
);

CREATE UNIQUE INDEX ux_leases_one_active_attempt
    ON leases(attempt_id) WHERE status = 'ACTIVE';

CREATE TABLE evidence (
    evidence_id             TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    claim_id                TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE RESTRICT,
    contract_version        INTEGER NOT NULL,
    statement_hash          TEXT NOT NULL CHECK (length(statement_hash) = 64),
    artifact_id             TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    evidence_type           TEXT NOT NULL CHECK (evidence_type IN
                               ('LEAN_REPLAY','CHECKER_CERTIFICATE','EXACT_ENUMERATION','COUNTEREXAMPLE',
                                'NATURAL_LANGUAGE_PROOF','MODEL_JUDGE','PEER_SIGNATURE',
                                'SEMANTIC_AUDIT','LITERATURE_SOURCE','EXECUTION_LOG')),
    evidence_strength       TEXT NOT NULL CHECK (evidence_strength IN
                               ('HARD_MACHINE','HUMAN_ATTESTED','SOFT_MODEL','PROVENANCE_ONLY')),
    evidence_root_id        TEXT NOT NULL REFERENCES evidence_roots(evidence_root_id) ON DELETE RESTRICT,
    scope_json              TEXT NOT NULL CHECK (json_valid(scope_json)),
    provenance_json         TEXT NOT NULL CHECK (json_valid(provenance_json)),
    ingest_schema_version   INTEGER NOT NULL CHECK (ingest_schema_version > 0),
    ingest_status           TEXT NOT NULL CHECK (ingest_status IN
                               ('ACCEPTED','REJECTED','QUARANTINED','INVALIDATED')),
    submitted_by_command_id TEXT NOT NULL REFERENCES commands(command_id) ON DELETE RESTRICT,
    created_at              TEXT NOT NULL,
    invalidated_by_event_id TEXT REFERENCES events(event_id) ON DELETE RESTRICT,
    FOREIGN KEY (run_id, contract_version)
        REFERENCES contract_versions(run_id, version) ON DELETE RESTRICT,
    UNIQUE (run_id, claim_id, contract_version, statement_hash, artifact_id, evidence_type,
            evidence_root_id)
);

CREATE TABLE verdict_events (
    verdict_event_id        TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    claim_id                TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE RESTRICT,
    command_id              TEXT NOT NULL REFERENCES commands(command_id) ON DELETE RESTRICT,
    revision                INTEGER NOT NULL CHECK (revision > 0),
    axis                    TEXT NOT NULL CHECK (axis IN ('ROUTE','MACHINE','SEMANTIC','PEER','QUALITY','CLOSURE')),
    value_before            TEXT NOT NULL,
    value_after             TEXT NOT NULL,
    evidence_ids_json       TEXT NOT NULL CHECK (json_valid(evidence_ids_json)),
    closure_witness_id      TEXT REFERENCES closure_witnesses(witness_id) ON DELETE RESTRICT,
    capability_id           TEXT NOT NULL REFERENCES capabilities(capability_id) ON DELETE RESTRICT,
    reason_code             TEXT NOT NULL,
    recorded_at             TEXT NOT NULL,
    CHECK (value_before <> value_after)
);

CREATE TABLE peer_reviews (
    review_id               TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    claim_id                TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE RESTRICT,
    contract_version        INTEGER NOT NULL,
    statement_hash          TEXT NOT NULL CHECK (length(statement_hash) = 64),
    selected_subgraph_digest TEXT CHECK (selected_subgraph_digest IS NULL OR length(selected_subgraph_digest) = 64),
    reviewer_capability_id  TEXT NOT NULL REFERENCES capabilities(capability_id) ON DELETE RESTRICT,
    independence_profile_json TEXT NOT NULL CHECK (json_valid(independence_profile_json)),
    verdict                 TEXT NOT NULL CHECK (verdict IN ('ACCEPT','REJECT','NEEDS_REVISION')),
    checklist_json          TEXT NOT NULL CHECK (json_valid(checklist_json)),
    review_artifact_id      TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    source_graph_json       TEXT NOT NULL CHECK (json_valid(source_graph_json)),
    created_by_event_id     TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    created_at              TEXT NOT NULL,
    FOREIGN KEY (run_id, contract_version)
        REFERENCES contract_versions(run_id, version) ON DELETE RESTRICT,
    UNIQUE (claim_id, contract_version, statement_hash, reviewer_capability_id, review_artifact_id)
);

CREATE TABLE quality_reviews (
    quality_review_id       TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    claim_id                TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE RESTRICT,
    contract_version        INTEGER NOT NULL,
    reviewer_capability_id  TEXT NOT NULL REFERENCES capabilities(capability_id) ON DELETE RESTRICT,
    verdict                 TEXT NOT NULL CHECK (verdict IN ('ACCEPT','REJECT','NEEDS_REVISION')),
    dimensions_json         TEXT NOT NULL CHECK (json_valid(dimensions_json)),
    review_artifact_id      TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    training_pool           TEXT NOT NULL CHECK (training_pool IN ('HUMAN_SOFT_LABELS','EXCLUDED')),
    created_by_event_id     TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    created_at              TEXT NOT NULL,
    FOREIGN KEY (run_id, contract_version)
        REFERENCES contract_versions(run_id, version) ON DELETE RESTRICT
);

CREATE TABLE literature_records (
    literature_record_id   TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    contract_version        INTEGER NOT NULL,
    claim_id                TEXT REFERENCES claims(claim_id) ON DELETE RESTRICT,
    status                  TEXT NOT NULL CHECK (status IN
                               ('LITERATURE_HIT','NO_HIT_AFTER_SEARCH','SEARCH_INCOMPLETE')),
    relation                TEXT CHECK (relation IS NULL OR relation IN
                               ('EQUIVALENT','STRICTLY_STRONGER','STRICTLY_WEAKER','OVERLAP',
                                'CONTRADICTS','INCOMPARABLE')),
    scope_json              TEXT NOT NULL CHECK (json_valid(scope_json)),
    cutoff_date             TEXT NOT NULL,
    query_families_json     TEXT NOT NULL CHECK (json_valid(query_families_json)),
    query_log_artifact_id   TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    reference_artifact_id   TEXT REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    assessment_artifact_id  TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    created_by_event_id     TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    created_at              TEXT NOT NULL,
    FOREIGN KEY (run_id, contract_version)
        REFERENCES contract_versions(run_id, version) ON DELETE RESTRICT,
    CHECK ((status = 'LITERATURE_HIT') = (reference_artifact_id IS NOT NULL)),
    CHECK ((status = 'LITERATURE_HIT') OR relation IS NULL)
);

CREATE TABLE bridges (
    bridge_id               TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    contract_version        INTEGER NOT NULL,
    source_claim_id         TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE RESTRICT,
    target_claim_id         TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE RESTRICT,
    directionality          TEXT NOT NULL CHECK (directionality IN
                               ('CANDIDATE','ONE_WAY_VALID','EQUIVALENT_VALID','REFUTED')),
    term_mapping_json       TEXT NOT NULL CHECK (json_valid(term_mapping_json)),
    forward_obligations_json TEXT NOT NULL CHECK (json_valid(forward_obligations_json)),
    reverse_obligations_json TEXT NOT NULL CHECK (json_valid(reverse_obligations_json)),
    loss_accounting_json    TEXT NOT NULL CHECK (json_valid(loss_accounting_json)),
    target_audit_review_id  TEXT REFERENCES peer_reviews(review_id) ON DELETE RESTRICT,
    backtranslation_artifact_id TEXT REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    created_by_event_id     TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    updated_by_event_id     TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    FOREIGN KEY (run_id, contract_version)
        REFERENCES contract_versions(run_id, version) ON DELETE RESTRICT,
    CHECK (source_claim_id <> target_claim_id)
);

CREATE TABLE lean_feedback_events (
    lean_feedback_id        TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    claim_id                TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE RESTRICT,
    attempt_id              TEXT REFERENCES attempts(attempt_id) ON DELETE RESTRICT,
    contract_version        INTEGER NOT NULL,
    environment_profile_id  TEXT NOT NULL,
    toolchain               TEXT NOT NULL,
    mathlib_commit          TEXT,
    source_artifact_id      TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    output_artifact_id      TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    feedback_kind           TEXT NOT NULL CHECK (feedback_kind IN
                               ('MISSING_PREMISE','TYPE_MISMATCH','FAILED_GOAL','SEMANTIC_DRIFT',
                                'ENVIRONMENT_ERROR','REPLAY_PASS','SORRY_FOUND','AXIOM_VIOLATION',
                                'NATIVE_DECIDE_VIOLATION')),
    first_failed_obligation_id TEXT,
    diagnostic_json         TEXT NOT NULL CHECK (json_valid(diagnostic_json)),
    created_by_event_id     TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    created_at              TEXT NOT NULL,
    FOREIGN KEY (run_id, contract_version)
        REFERENCES contract_versions(run_id, version) ON DELETE RESTRICT
);

CREATE TABLE failure_records (
    failure_record_id       TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    contract_version        INTEGER NOT NULL,
    route_id                TEXT REFERENCES routes(route_id) ON DELETE RESTRICT,
    claim_id                TEXT REFERENCES claims(claim_id) ON DELETE RESTRICT,
    failure_kind            TEXT NOT NULL CHECK (failure_kind IN
                               ('COUNTEREXAMPLE','FALSE_LEMMA','BRIDGE_FAILURE','COMPOSITION_GAP',
                                'ENVIRONMENT_FAILURE','BUDGET_STOP','NO_NOVELTY','SCOPE_MISMATCH')),
    normalized_fingerprint  TEXT NOT NULL CHECK (length(normalized_fingerprint) = 64),
    equivalence_key         TEXT NOT NULL,
    first_failed_obligation_id TEXT,
    evidence_artifact_id    TEXT REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    applicability_json      TEXT NOT NULL CHECK (json_valid(applicability_json)),
    novelty_delta_json      TEXT NOT NULL CHECK (json_valid(novelty_delta_json)),
    status                  TEXT NOT NULL CHECK (status IN ('ACTIVE','REVIEW_REQUIRED','SUPERSEDED')),
    created_by_event_id     TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    created_at              TEXT NOT NULL,
    FOREIGN KEY (run_id, contract_version)
        REFERENCES contract_versions(run_id, version) ON DELETE RESTRICT,
    UNIQUE (run_id, contract_version, normalized_fingerprint)
);

CREATE TABLE budget_events (
    budget_event_id         TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    route_id                TEXT REFERENCES routes(route_id) ON DELETE RESTRICT,
    attempt_id              TEXT REFERENCES attempts(attempt_id) ON DELETE RESTRICT,
    command_id              TEXT NOT NULL REFERENCES commands(command_id) ON DELETE RESTRICT,
    revision                INTEGER NOT NULL CHECK (revision > 0),
    event_kind              TEXT NOT NULL CHECK (event_kind IN
                               ('RESERVATION','ACTUAL','REFUND','UNKNOWN_COST','DENIAL','FUSE_TRIP')),
    resource_kind           TEXT NOT NULL CHECK (resource_kind IN
                               ('INPUT_TOKEN','OUTPUT_TOKEN','CPU_SECOND','GPU_SECOND','EXPERT_MINUTE',
                                'API_MICRO_CURRENCY','DISK_BYTE','WALL_SECOND')),
    amount_microunits       INTEGER CHECK (amount_microunits IS NULL OR amount_microunits >= 0),
    unit                    TEXT NOT NULL,
    currency                TEXT,
    provider_usage_json     TEXT NOT NULL CHECK (json_valid(provider_usage_json)),
    recorded_at             TEXT NOT NULL,
    CHECK ((event_kind = 'UNKNOWN_COST') OR amount_microunits IS NOT NULL)
);

CREATE TABLE execution_bindings (
    binding_id              TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    route_id                TEXT NOT NULL REFERENCES routes(route_id) ON DELETE RESTRICT,
    attempt_id              TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id) ON DELETE RESTRICT,
    adapter_name            TEXT NOT NULL,
    adapter_version         TEXT NOT NULL,
    source_commit           TEXT,
    external_run_id         TEXT,
    external_task_id        TEXT,
    external_session_ids_json TEXT NOT NULL CHECK (json_valid(external_session_ids_json)),
    workspace_commit        TEXT,
    environment_profile_id  TEXT NOT NULL,
    invocation_artifact_id  TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    result_artifact_id      TEXT REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    created_by_event_id     TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    created_at              TEXT NOT NULL,
    completed_at            TEXT
);

CREATE TABLE integration_outbox (
    outbox_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    event_id                TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    destination             TEXT NOT NULL CHECK (destination IN ('ARCHON_GIT','DOSSIER_SNAPSHOT','METRICS')),
    payload_digest          TEXT NOT NULL CHECK (length(payload_digest) = 64),
    payload_json            TEXT NOT NULL CHECK (json_valid(payload_json)),
    delivery_status         TEXT NOT NULL CHECK (delivery_status IN
                               ('PENDING','DELIVERING','DELIVERED','FAILED')),
    attempt_count           INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at         TEXT,
    last_error_code         TEXT,
    delivered_at            TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    UNIQUE (event_id, destination, payload_digest),
    CHECK ((delivery_status = 'DELIVERED') = (delivered_at IS NOT NULL))
);

CREATE INDEX ix_commands_run_revision ON commands(run_id, revision_after);
CREATE INDEX ix_events_run_cursor ON events(run_id, event_seq);
CREATE INDEX ix_events_run_revision ON events(run_id, revision);
CREATE INDEX ix_contract_versions_run_status ON contract_versions(run_id, status);
CREATE INDEX ix_claims_run_contract ON claims(run_id, contract_version, lifecycle_status);
CREATE INDEX ix_claim_edges_to_active ON claim_edges(to_claim_id, status);
CREATE INDEX ix_claim_edges_from_active ON claim_edges(from_claim_id, status);
CREATE INDEX ix_obligations_parent_status ON composition_obligations(parent_claim_id, status);
CREATE INDEX ix_witness_parent_status ON closure_witnesses(parent_claim_id, status);
CREATE INDEX ix_routes_run_status ON routes(run_id, status);
CREATE INDEX ix_attempts_route_status ON attempts(route_id, status);
CREATE INDEX ix_artifacts_ingest_state ON artifacts(ingest_state);
CREATE INDEX ix_evidence_claim_status ON evidence(claim_id, ingest_status);
CREATE INDEX ix_verdict_claim_axis ON verdict_events(claim_id, axis, revision);
CREATE INDEX ix_peer_claim ON peer_reviews(claim_id, contract_version, verdict);
CREATE INDEX ix_literature_run_status ON literature_records(run_id, status);
CREATE INDEX ix_failures_equivalence ON failure_records(run_id, equivalence_key, status);
CREATE INDEX ix_budget_run_kind ON budget_events(run_id, resource_kind, event_kind);
CREATE INDEX ix_outbox_delivery ON integration_outbox(delivery_status, next_attempt_at);

-- Append-only evidence of decisions. Current projections live elsewhere.
CREATE TRIGGER no_update_commands BEFORE UPDATE ON commands
BEGIN SELECT RAISE(ABORT, 'commands are append-only'); END;
CREATE TRIGGER no_delete_commands BEFORE DELETE ON commands
BEGIN SELECT RAISE(ABORT, 'commands are append-only'); END;
CREATE TRIGGER no_update_events BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER no_delete_events BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER no_update_verdict_events BEFORE UPDATE ON verdict_events
BEGIN SELECT RAISE(ABORT, 'verdict_events are append-only'); END;
CREATE TRIGGER no_delete_verdict_events BEFORE DELETE ON verdict_events
BEGIN SELECT RAISE(ABORT, 'verdict_events are append-only'); END;
CREATE TRIGGER no_update_budget_events BEFORE UPDATE ON budget_events
BEGIN SELECT RAISE(ABORT, 'budget_events are append-only'); END;
CREATE TRIGGER no_delete_budget_events BEFORE DELETE ON budget_events
BEGIN SELECT RAISE(ABORT, 'budget_events are append-only'); END;

-- Prevent a claim-edge cycle on active dependency/implication edges.
CREATE TRIGGER claim_edges_reject_cycle
BEFORE INSERT ON claim_edges
WHEN NEW.status = 'ACTIVE' AND NEW.edge_kind IN ('IMPLIES','DEPENDS_ON','SPECIALIZES','GENERALIZES')
BEGIN
    WITH RECURSIVE reachable(id) AS (
        SELECT NEW.to_claim_id
        UNION
        SELECT ce.to_claim_id
          FROM claim_edges ce JOIN reachable r ON ce.from_claim_id = r.id
         WHERE ce.run_id = NEW.run_id
           AND ce.status = 'ACTIVE'
           AND ce.edge_kind IN ('IMPLIES','DEPENDS_ON','SPECIALIZES','GENERALIZES')
    )
    SELECT CASE WHEN EXISTS(SELECT 1 FROM reachable WHERE id = NEW.from_claim_id)
        THEN RAISE(ABORT, 'claim graph cycle') END;
END;

COMMIT;

-- The migration runner computes SHA-256 over this exact file before execution and,
-- after COMMIT succeeds, records (1, 'rk_v1_initial', digest, injected_now_utc)
-- in a second BEGIN IMMEDIATE transaction. A startup check refuses a nonempty DB
-- whose recorded digest differs. The migration must not contain its own hash.

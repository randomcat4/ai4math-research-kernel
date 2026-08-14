-- migration-name: rk_v02_host_execution_receipts
-- Host-owned, claim-scoped execution receipts.  No public command writes this table.
BEGIN IMMEDIATE;

CREATE TABLE host_execution_receipts (
    receipt_id              TEXT PRIMARY KEY,
    receipt_nonce           TEXT NOT NULL UNIQUE,
    service_instance_id     TEXT NOT NULL,
    run_id                  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    route_id                TEXT NOT NULL REFERENCES routes(route_id) ON DELETE RESTRICT,
    attempt_id              TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id) ON DELETE RESTRICT,
    binding_id              TEXT NOT NULL UNIQUE REFERENCES execution_bindings(binding_id) ON DELETE RESTRICT,
    claim_id                TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE RESTRICT,
    contract_version        INTEGER NOT NULL,
    statement_hash          TEXT NOT NULL CHECK (length(statement_hash) = 64),
    environment_profile_id  TEXT NOT NULL,
    adapter_name            TEXT NOT NULL,
    adapter_version         TEXT NOT NULL,
    source_commit           TEXT,
    toolchain               TEXT,
    binary_sha256           TEXT CHECK (binary_sha256 IS NULL OR length(binary_sha256) = 64),
    request_hash            TEXT NOT NULL CHECK (length(request_hash) = 64),
    result_hash             TEXT NOT NULL CHECK (length(result_hash) = 64),
    source_sha256           TEXT CHECK (source_sha256 IS NULL OR length(source_sha256) = 64),
    output_sha256           TEXT CHECK (output_sha256 IS NULL OR length(output_sha256) = 64),
    input_snapshot_digest   TEXT NOT NULL CHECK (length(input_snapshot_digest) = 64),
    environment_digest      TEXT NOT NULL CHECK (length(environment_digest) = 64),
    mount_digest            TEXT NOT NULL CHECK (length(mount_digest) = 64),
    dependency_closure_digest TEXT CHECK (
        dependency_closure_digest IS NULL OR length(dependency_closure_digest)=64),
    process_digest          TEXT NOT NULL CHECK (length(process_digest) = 64),
    tool_digest             TEXT NOT NULL CHECK (length(tool_digest) = 64),
    status                  TEXT NOT NULL,
    exit_code               INTEGER,
    wall_time_ms            INTEGER NOT NULL CHECK (wall_time_ms >= 0),
    provider_usage_json     TEXT NOT NULL CHECK (json_valid(provider_usage_json)),
    payload_json            TEXT NOT NULL CHECK (json_valid(payload_json)),
    signature               TEXT NOT NULL CHECK (length(signature) = 64),
    authority_eligible      INTEGER NOT NULL CHECK (authority_eligible IN (0,1)),
    block_reasons_json      TEXT NOT NULL CHECK (json_valid(block_reasons_json)),
    recorded_by_command_id  TEXT NOT NULL REFERENCES commands(command_id) ON DELETE RESTRICT,
    consumed_by_feedback_id TEXT UNIQUE REFERENCES lean_feedback_events(lean_feedback_id) ON DELETE RESTRICT,
    created_at              TEXT NOT NULL,
    consumed_at             TEXT,
    FOREIGN KEY (run_id, contract_version)
        REFERENCES contract_versions(run_id, version) ON DELETE RESTRICT,
    CHECK ((consumed_by_feedback_id IS NULL) = (consumed_at IS NULL))
);

CREATE TABLE host_execution_claims (
    attempt_id     TEXT PRIMARY KEY REFERENCES attempts(attempt_id) ON DELETE RESTRICT,
    binding_id     TEXT NOT NULL UNIQUE REFERENCES execution_bindings(binding_id) ON DELETE RESTRICT,
    claim_token    TEXT NOT NULL UNIQUE,
    run_id         TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    route_id       TEXT NOT NULL REFERENCES routes(route_id) ON DELETE RESTRICT,
    request_hash   TEXT NOT NULL CHECK (length(request_hash)=64),
    component      TEXT NOT NULL,
    service_instance_id TEXT NOT NULL,
    claimed_at     TEXT NOT NULL,
    heartbeat_at   TEXT NOT NULL,
    recover_after  TEXT NOT NULL,
    completed_at   TEXT,
    recovery_state TEXT NOT NULL DEFAULT 'PENDING'
                   CHECK (recovery_state IN ('PENDING','COMPLETED','UNKNOWN_FUSED'))
);

CREATE INDEX ix_host_receipts_scope
    ON host_execution_receipts(run_id, claim_id, contract_version, statement_hash);
CREATE UNIQUE INDEX ux_host_receipts_authority_once
    ON host_execution_receipts(receipt_id) WHERE consumed_by_feedback_id IS NOT NULL;

COMMIT;

-- migration-name: rk_v02_revalidate_authority
-- v0.1 did not revalidate every materialized verdict against a scoped one-shot host receipt.
-- Preserve all immutable evidence/history, but require v0.2 runtime re-promotion.
PRAGMA foreign_keys = ON;

BEGIN IMMEDIATE;

CREATE TEMP TABLE affected_v01_authority_runs(
    run_id TEXT PRIMARY KEY,
    old_revision INTEGER NOT NULL,
    migration_request_id TEXT,
    migration_command_id TEXT,
    migration_event_id TEXT
);
INSERT INTO affected_v01_authority_runs(run_id, old_revision)
SELECT DISTINCT c.run_id, r.revision
FROM claims c JOIN runs r ON r.run_id = c.run_id
WHERE route_result IN (
        'LOCAL_LEMMAS_VERIFIED','ROUTE_LOCAL','ROUTE_PROVED','REFUTED','PREVIOUSLY_KNOWN'
      )
   OR machine_verdict IN ('KERNEL_VERIFIED','CERTIFICATE_VERIFIED')
   OR semantic_verdict IN ('HUMAN_ATTESTED','REFUTED')
   OR peer_verdict = 'ACCEPTED'
   OR quality_verdict IN ('ACCEPTED','REJECTED','NEEDS_REVISION')
   OR closure_state IN ('CLOSED_MACHINE','CLOSED_HUMAN','CLOSED_HYBRID');

-- RegisterClaim in v0.1 did not prove that the stored statement artifact was the
-- canonical contract root and allowed multiple ROOT rows.  No historical ROOT may be
-- used as a scope anchor for a new host receipt without explicit reconstruction.
INSERT OR IGNORE INTO affected_v01_authority_runs(run_id, old_revision)
SELECT DISTINCT c.run_id, r.revision
FROM claims c JOIN runs r ON r.run_id = c.run_id
WHERE c.claim_kind = 'ROOT';

INSERT OR IGNORE INTO affected_v01_authority_runs(run_id, old_revision)
SELECT run_id, revision FROM runs
WHERE final_outcome IN ('PROVED','DISPROVED','ROUTE_LOCAL','PREVIOUSLY_KNOWN');

-- The migration runner records its ledger row after this transaction commits.  If the
-- process dies in that narrow window, startup may execute this file once more.  A persisted
-- system command is the per-run idempotency marker: never create a second synthetic revision.
DELETE FROM affected_v01_authority_runs
WHERE run_id IN (
    SELECT run_id FROM commands WHERE command_type = 'SystemRevalidateAuthority'
);

UPDATE affected_v01_authority_runs
SET migration_request_id =
        '019b4c00-' || lower(hex(randomblob(2))) || '-7' ||
        substr(lower(hex(randomblob(2))),2) || '-8' ||
        substr(lower(hex(randomblob(2))),2) || '-' || lower(hex(randomblob(6))),
    migration_command_id =
        '019b4c01-' || lower(hex(randomblob(2))) || '-7' ||
        substr(lower(hex(randomblob(2))),2) || '-8' ||
        substr(lower(hex(randomblob(2))),2) || '-' || lower(hex(randomblob(6))),
    migration_event_id =
        '019b4c02-' || lower(hex(randomblob(2))) || '-7' ||
        substr(lower(hex(randomblob(2))),2) || '-8' ||
        substr(lower(hex(randomblob(2))),2) || '-' || lower(hex(randomblob(6)));

-- Revoke every pre-v0.2 execution context before reopening the research record.  OPEN is
-- deliberate: migrated runs generally have no trusted Interrupt checkpoint and therefore
-- cannot use Resume.  They must rebuild the canonical ROOT and start a fresh route/attempt.
UPDATE leases
SET status = 'REVOKED',
    released_at = COALESCE(released_at, CURRENT_TIMESTAMP)
WHERE status = 'ACTIVE'
  AND attempt_id IN (
      SELECT attempt_id FROM attempts
      WHERE run_id IN (SELECT run_id FROM affected_v01_authority_runs)
  );

UPDATE attempts
SET status = 'ABORTED',
    started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
    ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP)
WHERE run_id IN (SELECT run_id FROM affected_v01_authority_runs)
  AND status IN ('QUEUED','RUNNING','PAUSED');

UPDATE routes
SET status = 'RETIRED',
    updated_at = CURRENT_TIMESTAMP
WHERE run_id IN (SELECT run_id FROM affected_v01_authority_runs)
  AND status <> 'RETIRED';

-- Preserve historical budget bytes, but quarantine the entire v0.1 budget generation.
-- Reservations, refunds and fuse trips must not constrain a freshly reconstructed run;
-- actual/unknown usage must not appear as host-observed consumption.
UPDATE budget_events
SET provider_usage_json = json_set(
        provider_usage_json,
        '$._rk_trust',
        'LEGACY_UNTRUSTED'
    )
WHERE run_id IN (SELECT run_id FROM affected_v01_authority_runs)
  AND event_kind IN (
      'RESERVATION','ACTUAL','REFUND','UNKNOWN_COST','DENIAL','FUSE_TRIP'
  );

UPDATE runs
SET status = 'OPEN',
    final_outcome = NULL,
    closed_at = NULL,
    parent_dossier_artifact_id = NULL
WHERE run_id IN (SELECT run_id FROM affected_v01_authority_runs);

UPDATE claims
SET route_result = CASE
        WHEN route_result IN (
            'LOCAL_LEMMAS_VERIFIED','ROUTE_LOCAL','ROUTE_PROVED','REFUTED','PREVIOUSLY_KNOWN'
        )
        THEN 'CANDIDATE'
        ELSE route_result
    END,
    machine_verdict = CASE
        WHEN machine_verdict IN ('KERNEL_VERIFIED','CERTIFICATE_VERIFIED') THEN 'UNVERIFIED'
        ELSE machine_verdict
    END,
    semantic_verdict = CASE
        WHEN semantic_verdict IN ('HUMAN_ATTESTED','REFUTED') THEN 'UNREVIEWED'
        ELSE semantic_verdict
    END,
    peer_verdict = CASE WHEN peer_verdict = 'ACCEPTED' THEN 'UNREVIEWED' ELSE peer_verdict END,
    quality_verdict = CASE
        WHEN quality_verdict IN ('ACCEPTED','REJECTED','NEEDS_REVISION') THEN 'UNREVIEWED'
        ELSE quality_verdict
    END,
    closure_state = CASE
        WHEN closure_state IN ('CLOSED_MACHINE','CLOSED_HUMAN','CLOSED_HYBRID') THEN 'OPEN'
        ELSE closure_state
    END,
    lifecycle_status = CASE WHEN claim_kind = 'ROOT' THEN 'INVALIDATED' ELSE lifecycle_status END
WHERE run_id IN (SELECT run_id FROM affected_v01_authority_runs);

UPDATE closure_witnesses
SET status = 'INVALIDATED'
WHERE status = 'ACCEPTED';

UPDATE composition_obligations
SET status = 'INVALIDATED'
WHERE status IN ('DISCHARGED_MACHINE','DISCHARGED_HUMAN','DISCHARGED_HYBRID');

UPDATE claim_edges
SET status = 'INVALIDATED'
WHERE justification_kind IN ('LEAN_DECLARATION','CHECKER_PROFILE','HUMAN_ARGUMENT')
  AND status = 'ACTIVE';

UPDATE bridges
SET directionality = 'CANDIDATE'
WHERE directionality IN ('ONE_WAY_VALID','EQUIVALENT_VALID');

-- Authority revalidation changes the public mathematical projection.  Represent that change
-- as an accepted, auditable system command/event and advance the run revision exactly once.
-- This prevents a pre-migration expected_revision or dossier identity from being reused.
INSERT OR IGNORE INTO capabilities(
    capability_id,subject_id,issuer,key_id,allowed_actions_json,run_scope_json,nonce,
    credential_digest,issued_at,expires_at
) VALUES (
    '019b4c03-0000-7000-8000-000000000001','rk-host-migration','rk-migration','rk-v02',
    '["SystemRevalidateAuthority"]','[]','rk-v02-migration-authority-nonce',
    '8db52f563cadb16549c44606b381379d42dbca7a2fbd99d75ee34e2bc7628c47',
    '1970-01-01T00:00:00.000Z','9999-12-31T23:59:59.999Z'
);

INSERT INTO commands(
    command_id,run_id,request_id,command_type,request_digest,expected_revision,
    capability_id,accepted,revision_before,revision_after,rejection_code,
    missing_conditions_json,receipt_json,trace_id,decided_at
)
SELECT
    migration_command_id,
    run_id,
    migration_request_id,
    'SystemRevalidateAuthority',
    lower(hex(randomblob(32))),
    old_revision,
    '019b4c03-0000-7000-8000-000000000001',
    1,
    old_revision,
    old_revision + 1,
    NULL,
    '[]',
    json_object(
        'schema_version','rk.receipt.v1',
        'request_id',migration_request_id,
        'command_id',migration_command_id,
        'run_id',run_id,
        'accepted',json('true'),
        'revision_before',old_revision,
        'revision_after',old_revision + 1,
        'event_ids',json_array(migration_event_id),
        'artifact_ids',json_array(),
        'rejection_code',NULL,
        'missing_conditions',json_array(),
        'decided_at',strftime('%Y-%m-%dT%H:%M:%fZ','now')
    ),
    'rk-v02-revalidate:' || run_id,
    strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM affected_v01_authority_runs;

INSERT INTO events(
    event_id,run_id,command_id,revision,event_type,payload_json,recorded_at
)
SELECT
    migration_event_id,
    run_id,
    migration_command_id,
    old_revision + 1,
    'AUTHORITY_REVALIDATED',
    json_object(
        'migration','rk_v02_revalidate_authority',
        'legacy_authority','INVALIDATED',
        'execution_generation','REQUIRES_FRESH_ROOT_ROUTE_ATTEMPT'
    ),
    strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM affected_v01_authority_runs;

UPDATE runs
SET revision = revision + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE run_id IN (SELECT run_id FROM affected_v01_authority_runs);

DROP TABLE affected_v01_authority_runs;

COMMIT;

-- The migration runner records this file's exact digest after the schema commit.

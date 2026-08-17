-- migration-name: rk_bridge_contract_closure_migration
PRAGMA foreign_keys = ON;

BEGIN IMMEDIATE;

UPDATE bridges
SET contract_version = (
    SELECT source.contract_version
    FROM claims AS source
    JOIN claims AS target ON target.claim_id = bridges.target_claim_id
    WHERE source.claim_id = bridges.source_claim_id
      AND source.contract_version = target.contract_version
)
WHERE EXISTS (
    SELECT 1
    FROM claims AS source
    JOIN claims AS target ON target.claim_id = bridges.target_claim_id
    WHERE source.claim_id = bridges.source_claim_id
      AND source.contract_version = target.contract_version
      AND source.contract_version <> bridges.contract_version
);

COMMIT;

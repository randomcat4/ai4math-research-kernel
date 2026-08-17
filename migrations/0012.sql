-- migration-name: rk_bridge_semantic_retry_dedup
PRAGMA foreign_keys = ON;

BEGIN IMMEDIATE;

DELETE FROM bridges
WHERE bridge_id IN (
    SELECT later.bridge_id
    FROM bridges AS later
    JOIN bridges AS earlier
      ON earlier.run_id = later.run_id
     AND earlier.contract_version = later.contract_version
     AND earlier.source_claim_id = later.source_claim_id
     AND earlier.target_claim_id = later.target_claim_id
     AND earlier.directionality = later.directionality
     AND earlier.term_mapping_json = later.term_mapping_json
     AND earlier.forward_obligations_json = later.forward_obligations_json
     AND earlier.reverse_obligations_json = later.reverse_obligations_json
     AND earlier.loss_accounting_json = later.loss_accounting_json
     AND earlier.bridge_spec_json = later.bridge_spec_json
     AND earlier.bridge_id < later.bridge_id
);

COMMIT;

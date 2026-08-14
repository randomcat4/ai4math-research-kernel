-- migration-name: rk_host_checker_receipt_consumption
PRAGMA foreign_keys = ON;

BEGIN IMMEDIATE;

ALTER TABLE host_execution_receipts ADD COLUMN consumed_by_verification_id TEXT
    REFERENCES atomic_verifications(verification_id) ON DELETE RESTRICT;
ALTER TABLE host_execution_receipts ADD COLUMN checker_consumed_at TEXT;

CREATE UNIQUE INDEX ux_host_checker_receipt_once
    ON host_execution_receipts(consumed_by_verification_id)
    WHERE consumed_by_verification_id IS NOT NULL;

COMMIT;

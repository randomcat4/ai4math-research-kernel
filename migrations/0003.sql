-- migration-name: rk_v1_execution_receipt_nonce
-- Bind trusted execution receipts to one kernel-issued invocation and consume once.
PRAGMA foreign_keys = ON;

BEGIN IMMEDIATE;

ALTER TABLE execution_bindings ADD COLUMN invocation_nonce TEXT;
UPDATE execution_bindings
SET invocation_nonce = lower(hex(randomblob(16)))
WHERE invocation_nonce IS NULL;
CREATE UNIQUE INDEX ux_execution_bindings_invocation_nonce
    ON execution_bindings(invocation_nonce);

ALTER TABLE lean_feedback_events ADD COLUMN receipt_nonce TEXT;
CREATE UNIQUE INDEX ux_lean_feedback_receipt_nonce
    ON lean_feedback_events(receipt_nonce)
    WHERE receipt_nonce IS NOT NULL;

COMMIT;

-- The migration runner records this file's exact digest after the schema commit.

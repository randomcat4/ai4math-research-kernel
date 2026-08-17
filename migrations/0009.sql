-- migration-name: rk_bridge_spec_product
PRAGMA foreign_keys = ON;

BEGIN IMMEDIATE;

ALTER TABLE bridges ADD COLUMN bridge_spec_json TEXT NOT NULL DEFAULT '{}'
    CHECK (json_valid(bridge_spec_json));
ALTER TABLE bridges ADD COLUMN forward_status TEXT NOT NULL DEFAULT 'CANDIDATE'
    CHECK (forward_status IN ('UNSTATED','CANDIDATE','CHECKED','REFUTED'));
ALTER TABLE bridges ADD COLUMN reverse_status TEXT NOT NULL DEFAULT 'UNSTATED'
    CHECK (reverse_status IN ('UNSTATED','CANDIDATE','CHECKED','REFUTED'));

COMMIT;

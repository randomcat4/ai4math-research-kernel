CREATE TABLE product_backups(
  backup_id TEXT PRIMARY KEY,
  deployment_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state = 'SUCCEEDED'),
  include_cas INTEGER NOT NULL CHECK(include_cas IN (0,1)),
  include_configuration INTEGER NOT NULL CHECK(include_configuration IN (0,1)),
  backup_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
  backup_digest TEXT NOT NULL CHECK(length(backup_digest) = 64),
  manifest_digest TEXT NOT NULL CHECK(length(manifest_digest) = 64),
  activity_cursor INTEGER NOT NULL CHECK(activity_cursor >= 0),
  job_count INTEGER NOT NULL CHECK(job_count >= 0),
  checkpoint_count INTEGER NOT NULL CHECK(checkpoint_count >= 0),
  terminal_job_count INTEGER NOT NULL CHECK(terminal_job_count >= 0 AND terminal_job_count <= job_count),
  created_at TEXT NOT NULL,
  completed_at TEXT NOT NULL,
  UNIQUE(deployment_id, request_id),
  UNIQUE(backup_artifact_id),
  CHECK(completed_at >= created_at)
) STRICT;

CREATE INDEX product_backups_deployment_history
ON product_backups(deployment_id, completed_at, backup_id);

CREATE TABLE product_deployment_upgrades(
  upgrade_id TEXT PRIMARY KEY,
  deployment_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  backup_id TEXT NOT NULL REFERENCES product_backups(backup_id) ON DELETE RESTRICT,
  release_id TEXT NOT NULL,
  release_manifest_digest TEXT NOT NULL CHECK(length(release_manifest_digest) = 64),
  state TEXT NOT NULL CHECK(state IN ('SUCCEEDED','FAILED')),
  fragments_before INTEGER NOT NULL CHECK(fragments_before >= 0),
  fragments_after INTEGER CHECK(fragments_after IS NULL OR fragments_after >= fragments_before),
  failure_code TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  UNIQUE(deployment_id, request_id),
  CHECK(
    (state = 'SUCCEEDED' AND fragments_after IS NOT NULL AND failure_code IS NULL) OR
    (state = 'FAILED' AND fragments_after IS NULL AND failure_code IS NOT NULL)
  ),
  CHECK(finished_at >= started_at)
) STRICT;

CREATE INDEX product_deployment_upgrades_history
ON product_deployment_upgrades(deployment_id, finished_at, upgrade_id);

CREATE TABLE product_deployment_restores(
  restore_id TEXT PRIMARY KEY,
  source_backup_id TEXT NOT NULL,
  deployment_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  target_root_digest TEXT NOT NULL CHECK(length(target_root_digest) = 64),
  state TEXT NOT NULL CHECK(state IN ('RUNNING','SUCCEEDED','FAILED')),
  restored_database_digest TEXT,
  restored_activity_cursor INTEGER,
  restored_job_count INTEGER,
  restored_checkpoint_count INTEGER,
  failure_code TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  UNIQUE(deployment_id, request_id),
  CHECK(
    (state = 'RUNNING' AND restored_database_digest IS NULL
      AND restored_activity_cursor IS NULL AND restored_job_count IS NULL
      AND restored_checkpoint_count IS NULL AND failure_code IS NULL) OR
    (state = 'SUCCEEDED' AND restored_database_digest IS NOT NULL
      AND restored_activity_cursor IS NOT NULL AND restored_job_count IS NOT NULL
      AND restored_checkpoint_count IS NOT NULL AND failure_code IS NULL) OR
    (state = 'FAILED' AND restored_database_digest IS NULL
      AND restored_activity_cursor IS NULL AND restored_job_count IS NULL
      AND restored_checkpoint_count IS NULL AND failure_code IS NOT NULL)
  ),
  CHECK(finished_at >= started_at)
) STRICT;

CREATE INDEX product_deployment_restores_history
ON product_deployment_restores(deployment_id, finished_at, restore_id);

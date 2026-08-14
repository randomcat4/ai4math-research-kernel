import type {JsonObject} from "../../../../sdk/typescript/src/client.js";

export interface ArtifactBinding {
  artifact_id: string;
  sha256: string;
  byte_count: number;
  media_type: string;
}

export interface DeploymentStatus {
  deploymentId: string;
  state: string;
  probeRunId: string;
  capabilityKeys: string[];
  faultCodes: string[];
  revision: number;
  lastCursor: number;
}

export interface AdminHealth {
  reportId: string;
  deploymentId: string;
  state: string;
  probeRunId: string;
  faultCodes: string[];
  revision: number;
  lastCursor: number;
}

export interface DeploymentJob {
  id: string;
  deploymentId: string;
  type: string;
  state: string;
  executionReceiptId: string;
  revision: number;
}

export interface BackupStatus {
  id: string;
  deploymentId: string;
  state: string;
  artifactId: string;
  digest: string;
  revision: number;
}

export interface OperationReceipt {
  receiptId: string;
  state: string;
  jobId?: string;
  revisionAfter?: number;
}

export interface AdminFailure {
  code: string;
  status: number;
  title: string;
  detail: string;
  action: string;
  unavailable: boolean;
  rethlasBlocked: boolean;
}

export function deploymentStatus(value: JsonObject): DeploymentStatus {
  const entity = projection(value);
  const domain = object(entity.domain, "domain");
  return {
    deploymentId: string(domain.deployment_id, "deployment_id"),
    state: string(domain.deployment_state, "deployment_state"),
    probeRunId: string(domain.probe_run_id, "probe_run_id"),
    capabilityKeys: strings(domain.capability_keys, "capability_keys"),
    faultCodes: strings(domain.fault_codes, "fault_codes"),
    revision: integer(value.deployment_revision, "deployment_revision"),
    lastCursor: integer(value.last_cursor, "last_cursor"),
  };
}

export function adminHealth(value: JsonObject): AdminHealth {
  const entity = projection(value);
  const domain = object(entity.domain, "domain");
  return {
    reportId: string(domain.health_report_id, "health_report_id"),
    deploymentId: string(domain.deployment_id, "deployment_id"),
    state: string(domain.overall_state, "overall_state"),
    probeRunId: string(domain.probe_run_id, "probe_run_id"),
    faultCodes: strings(domain.fault_codes, "fault_codes"),
    revision: integer(value.deployment_revision, "deployment_revision"),
    lastCursor: integer(value.last_cursor, "last_cursor"),
  };
}

export function deploymentJob(value: JsonObject): DeploymentJob {
  const entity = projection(value);
  const domain = object(entity.domain, "domain");
  return {
    id: string(domain.deployment_job_id, "deployment_job_id"),
    deploymentId: string(domain.deployment_id, "deployment_id"),
    type: string(domain.job_type, "job_type"),
    state: string(domain.job_state, "job_state"),
    executionReceiptId: string(domain.execution_receipt_id, "execution_receipt_id"),
    revision: integer(value.deployment_revision, "deployment_revision"),
  };
}

export function backupStatus(value: JsonObject): BackupStatus {
  const entity = projection(value);
  const domain = object(entity.domain, "domain");
  return {
    id: string(domain.backup_id, "backup_id"),
    deploymentId: string(domain.deployment_id, "deployment_id"),
    state: string(domain.backup_state, "backup_state"),
    artifactId: string(domain.backup_artifact_id, "backup_artifact_id"),
    digest: string(domain.backup_digest, "backup_digest"),
    revision: integer(value.deployment_revision, "deployment_revision"),
  };
}

export function operationReceipt(value: JsonObject): OperationReceipt {
  return {
    receiptId: string(value.receipt_id, "receipt_id"),
    state: string(value.state, "state"),
    jobId: optionalString(value.job_id),
    revisionAfter: optionalInteger(value.revision_after),
  };
}

export function failure(code: string, status: number): AdminFailure {
  const rethlasBlocked =
    status === 504 || code.includes("RETHLAS") && code.includes("TIMEOUT");
  const unavailable =
    rethlasBlocked ||
    status === 404 ||
    status === 501 ||
    status === 503 ||
    code.includes("UNAVAILABLE") ||
    code.includes("UNKNOWN_VARIANT");
  if (rethlasBlocked) {
    return {
      code,
      status,
      title: "Rethlas ??????????",
      detail: "HTTP 504 ??????????????????????????",
      action: "??????? receipt???????????????????",
      unavailable: true,
      rethlasBlocked: true,
    };
  }
  return {
    code,
    status,
    title: unavailable ? "????????" : "???????",
    detail: code,
    action: unavailable
      ? "???????? query/command variant????????????????"
      : "???? Admin session??? revision ???????????",
    unavailable,
    rethlasBlocked: false,
  };
}

function projection(value: JsonObject): JsonObject {
  const result = object(value.result, "result");
  return object(result.entity, "result.entity");
}

function object(value: unknown, label: string): JsonObject {
  if (value === null || Array.isArray(value) || typeof value !== "object") {
    throw new Error(`${label} is not an object`);
  }
  return value as JsonObject;
}

function string(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) throw new Error(`${label} is invalid`);
  return value;
}

function optionalString(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function integer(value: unknown, label: string): number {
  if (!Number.isSafeInteger(value) || Number(value) < 0) throw new Error(`${label} is invalid`);
  return Number(value);
}

function optionalInteger(value: unknown): number | undefined {
  return Number.isSafeInteger(value) && Number(value) >= 0 ? Number(value) : undefined;
}

function strings(value: unknown, label: string): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new Error(`${label} is invalid`);
  }
  return value as string[];
}

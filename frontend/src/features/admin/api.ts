import {
  ResearchProductClient,
  type JsonObject,
  type ProductTransport,
} from "../../../../sdk/typescript/src/client.js";
import type {CommandPayloadMap} from "../../../../sdk/typescript/src/types.js";
import {
  adminHealth,
  backupStatus,
  deploymentJob,
  deploymentStatus,
  failure,
  operationReceipt,
  type AdminFailure,
  type ArtifactBinding,
} from "./model.js";

export type DeploymentOperation =
  | {action: "BOOTSTRAP"; data_root: string; configuration_artifact: ArtifactBinding}
  | {action: "PROBE_CAPABILITY"; capability_profile_id: string}
  | {
      action: "BACKUP";
      backup_target: string;
      include_cas: boolean;
      include_configuration: boolean;
    }
  | {action: "RESTORE"; backup_artifact: ArtifactBinding; new_data_root: string}
  | {action: "UPGRADE_PREFLIGHT"; release_manifest: ArtifactBinding}
  | {action: "MIGRATE_SCHEMA"; release_manifest: ArtifactBinding; backup_id: string}
  | {action: "EXPORT_DIAGNOSTICS"; redact_credentials: boolean};

export class AdminApiError extends Error {
  readonly failure: AdminFailure;

  constructor(readonly status: number, readonly code: string) {
    super(`${code} (HTTP ${status})`);
    this.failure = failure(code, status);
  }
}

class AdminTransport implements ProductTransport {
  constructor(private readonly baseUrl: string) {}

  async request(
    operation: "command" | "query" | "subscribe" | "artifact",
    body: JsonObject,
  ): Promise<JsonObject> {
    if (operation !== "command" && operation !== "query") {
      throw new AdminApiError(501, "ADMIN_VARIANT_UNAVAILABLE");
    }
    const path = operation === "command"
      ? "/v1/deployment/operations"
      : "/v1/deployment/queries";
    let response: Response;
    try {
      response = await fetch(this.baseUrl + path, {
        method: "POST",
        credentials: "include",
        headers: {"content-type": "application/json"},
        body: JSON.stringify(body),
      });
    } catch {
      throw new AdminApiError(0, "NETWORK_UNAVAILABLE");
    }
    const value: unknown = await response.json().catch(() => null);
    if (value === null || Array.isArray(value) || typeof value !== "object") {
      throw new AdminApiError(response.status, "INVALID_SERVER_ENVELOPE");
    }
    const result = value as JsonObject;
    if (!response.ok) {
      const fallback = response.status === 504
        ? "RETHLAS_GATEWAY_TIMEOUT"
        : "ADMIN_REQUEST_FAILED";
      throw new AdminApiError(
        response.status,
        typeof result.code === "string" ? result.code : fallback,
      );
    }
    return result;
  }
}

export class AdminGateway {
  private readonly client: ResearchProductClient;

  constructor(
    readonly deploymentId: string,
    private deploymentRevision: number | undefined,
    baseUrl = "",
  ) {
    this.client = new ResearchProductClient(new AdminTransport(baseUrl));
  }

  setRevision(revision: number | undefined): void {
    this.deploymentRevision = revision;
  }

  async status() {
    return deploymentStatus(await this.client.query({
      scope: this.queryScope(),
      type: "DEPLOYMENT_STATUS",
      payload: {},
    }));
  }

  async health() {
    return adminHealth(await this.client.query({
      scope: this.queryScope(),
      type: "ADMIN_HEALTH",
      payload: {},
    }));
  }

  async job(id: string) {
    return deploymentJob(await this.client.query({
      scope: this.queryScope(),
      type: "DEPLOYMENT_JOB",
      payload: {deployment_job_id: id},
    }));
  }

  async backup(id: string) {
    return backupStatus(await this.client.query({
      scope: this.queryScope(),
      type: "BACKUP_STATUS",
      payload: {backup_id: id},
    }));
  }

  async operate(payload: DeploymentOperation) {
    if (this.deploymentRevision === undefined) {
      throw new AdminApiError(409, "DEPLOYMENT_REVISION_REQUIRED");
    }
    const value = await this.client.command({
      request_id: crypto.randomUUID(),
      scope: {
        kind: "DEPLOYMENT",
        deployment_id: this.deploymentId,
        expected_deployment_revision: this.deploymentRevision,
      },
      type: "DEPLOYMENT_OPERATION",
      payload: payload as CommandPayloadMap["DEPLOYMENT_OPERATION"],
    });
    return operationReceipt(value);
  }

  private queryScope() {
    return this.deploymentRevision === undefined
      ? {
          kind: "DEPLOYMENT" as const,
          deployment_id: this.deploymentId,
        }
      : {
          kind: "DEPLOYMENT" as const,
          deployment_id: this.deploymentId,
          at_deployment_revision: this.deploymentRevision,
        };
  }
}

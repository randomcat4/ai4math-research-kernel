import {
  ResearchProductClient,
  type JsonObject,
  type ProductTransport,
} from "../../../../sdk/typescript/src/client.js";
import type {CommandPayloadMap, QueryPayloadMap} from "../../../../sdk/typescript/src/types.js";
import type {ArtifactRef, CommandOutcome, CreateResearchDraft} from "./model.js";

export class ProductRouteError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly unavailable: boolean,
  ) {
    super(`${code} (HTTP ${status})`);
  }
}

class BrowserTransport implements ProductTransport {
  constructor(private readonly baseUrl: string) {}

  async request(operation: "command" | "query" | "subscribe" | "artifact", body: JsonObject): Promise<JsonObject> {
    const scope = body.scope as JsonObject | undefined;
    let path: string;
    if (operation === "command") {
      path = scope?.kind === "RUN"
        ? `/v1/research/${encodeURIComponent(String(scope.run_id))}/commands`
        : "/v1/deployment/operations";
    } else if (operation === "query") {
      path = scope?.kind === "RUN"
        ? `/v1/research/${encodeURIComponent(String(scope.run_id))}/queries`
        : "/v1/deployment/queries";
    } else {
      throw new Error(`${operation} uses its dedicated adapter`);
    }
    return requestJson(this.baseUrl + path, {method: "POST", body: JSON.stringify(body)});
  }
}

async function requestJson(url: string, init: RequestInit): Promise<JsonObject> {
  const response = await fetch(url, {
    ...init,
    credentials: "include",
    headers: {"content-type": "application/json", ...init.headers},
  });
  const value: unknown = await response.json();
  if (value === null || Array.isArray(value) || typeof value !== "object") {
    throw new ProductRouteError(response.status, "INVALID_SERVER_ENVELOPE", false);
  }
  const body = value as JsonObject;
  if (!response.ok) {
    const code = typeof body.code === "string" ? body.code : "PRODUCT_REQUEST_FAILED";
    throw new ProductRouteError(
      response.status,
      code,
      response.status === 404 || response.status === 501 || response.status === 503 || code.includes("UNAVAILABLE"),
    );
  }
  return body;
}

function commandOutcome(value: JsonObject): CommandOutcome {
  return {
    receiptId: typeof value.receipt_id === "string" ? value.receipt_id : undefined,
    runId: typeof value.created_run_id === "string" ? value.created_run_id : undefined,
    state: typeof value.state === "string" ? value.state : "DECIDED",
    raw: value,
  };
}

export class ResearchGateway {
  private readonly client: ResearchProductClient;

  constructor(
    private readonly deploymentId: string,
    private readonly baseUrl = "",
  ) {
    this.client = new ResearchProductClient(new BrowserTransport(baseUrl));
  }

  async createResearch(requestId: string, draft: CreateResearchDraft): Promise<CommandOutcome> {
    const value = await this.client.command({
      request_id: requestId,
      scope: {kind: "GLOBAL", deployment_id: this.deploymentId},
      type: "CREATE_RESEARCH",
      payload: {
        question: draft.question,
        owner: draft.owner,
        labels: draft.labels,
        contract_draft: draft.contractDraft as JsonObject,
        initial_budget: draft.initialBudget,
        title: draft.title,
        material_artifacts: draft.materialArtifacts as unknown as JsonObject[],
      },
    });
    return commandOutcome(value);
  }

  async runCommand<T extends F01CommandType>(
    runId: string,
    revision: number,
    contractVersion: number,
    requestId: string,
    type: T,
    payload: CommandPayloadMap[T],
  ): Promise<CommandOutcome> {
    const value = await this.client.command({
      request_id: requestId,
      scope: {kind: "RUN", run_id: runId, expected_revision: revision, expected_contract_version: contractVersion},
      type,
      payload,
    });
    return commandOutcome(value);
  }

  query<TResult extends JsonObject, T extends F01QueryType>(
    runId: string,
    type: T,
    payload: QueryPayloadMap[T],
  ): Promise<TResult> {
    return this.client.query({scope: {kind: "RUN", run_id: runId}, type, payload}) as Promise<TResult>;
  }

  async upload(file: File, onProgress: (received: number, total: number) => void, signal?: AbortSignal): Promise<ArtifactRef> {
    const sha256 = await digestHex(await file.arrayBuffer());
    const resumeKey = `rk-upload:${sha256}:${file.size}`;
    const stored = localStorage.getItem(resumeKey);
    const requestId = stored ? JSON.parse(stored).requestId as string : crypto.randomUUID();
    const begin = await requestJson(this.baseUrl + "/v1/artifacts/operations", {
      method: "POST",
      signal,
      body: JSON.stringify({
        type: "BEGIN_UPLOAD",
        payload: {request_id: requestId, logical_name: file.name, media_type: file.type || mediaType(file.name), byte_count: file.size, sha256},
      }),
    });
    const upload = begin.upload as JsonObject;
    const uploadId = String(upload.upload_id);
    let offset = Number(upload.received_byte_count);
    localStorage.setItem(resumeKey, JSON.stringify({requestId, uploadId}));
    const chunkBytes = 1024 * 1024;
    while (offset < file.size) {
      const bytes = await file.slice(offset, Math.min(offset + chunkBytes, file.size)).arrayBuffer();
      const response = await fetch(this.baseUrl + "/v1/artifacts/operations", {
        method: "POST",
        credentials: "include",
        signal,
        headers: {
          "content-type": "application/octet-stream",
          "x-rk-artifact-operation": "APPEND_CHUNK",
          "x-rk-upload-id": uploadId,
          "x-rk-upload-offset": String(offset),
          "x-rk-chunk-sha256": await digestHex(bytes),
        },
        body: bytes,
      });
      const result = await response.json() as JsonObject;
      if (!response.ok) {
        throw new ProductRouteError(response.status, String(result.code ?? "ARTIFACT_UPLOAD_FAILED"), response.status === 503);
      }
      offset = Number((result.upload as JsonObject).received_byte_count);
      onProgress(offset, file.size);
    }
    const committed = await requestJson(this.baseUrl + "/v1/artifacts/operations", {
      method: "POST",
      signal,
      body: JSON.stringify({type: "COMMIT_UPLOAD", payload: {upload_id: uploadId}}),
    });
    localStorage.removeItem(resumeKey);
    return committed.artifact_ref as unknown as ArtifactRef;
  }

  artifactUrl(artifactId: string): string {
    return `${this.baseUrl}/v1/artifacts/${encodeURIComponent(artifactId)}`;
  }
}

type F01CommandType = "CONFIRM_CONTRACT" | "AMEND_CONTRACT" | "CONFIRM_MATERIAL_EXTRACTION";

type F01QueryType = "CONTRACT" | "CONTRACT_IMPACT" | "MATERIAL" | "MATERIAL_EXTRACTION" | "EXTRACTION_DIFF";

async function digestHex(value: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", value);
  return [...new Uint8Array(digest)].map((part) => part.toString(16).padStart(2, "0")).join("");
}

function mediaType(name: string): string {
  const suffix = name.toLowerCase().split(".").pop();
  return suffix === "pdf" ? "application/pdf" : suffix === "tex" ? "application/x-tex" : suffix === "txt" ? "text/plain" : "application/octet-stream";
}

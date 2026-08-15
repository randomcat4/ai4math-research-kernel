import {
  ResearchProductClient,
  type JsonObject,
  type JsonValue,
  type ProductTransport,
} from "../../../../sdk/typescript/src/client.js";
import {
  apiAction,
  type ArtifactBinding,
  type ArtifactChoice,
  type ClaimDraft,
  type ClaimRevision,
  type ClaimView,
  type CommandReceipt,
  type FeatureFailure,
  type GraphEdge,
  type GraphNode,
  type GraphView,
  type LineageView,
  type ReviewTaskChoice,
  type RunFence,
  type WorkContext,
  type WorkflowView,
} from "./model.js";
import type { RevokeConfirmation, RevokePreview } from "../revocation/model.js";

export class ClaimApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly unavailable: boolean,
    message = code,
  ) {
    super(message);
  }
  toFailure(): FeatureFailure {
    return {
      code: this.code,
      message: this.message,
      unavailable: this.unavailable,
      action: apiAction(this.code),
    };
  }
}
class BrowserTransport implements ProductTransport {
  constructor(private readonly baseUrl: string) {}
  async request(
    operation: "command" | "query" | "subscribe" | "artifact",
    body: JsonObject,
  ): Promise<JsonObject> {
    if (operation !== "command" && operation !== "query")
      throw new ClaimApiError(501, "FEATURE_ROUTE_UNAVAILABLE", true);
    const scope = obj(body.scope, "scope"),
      runId = str(scope.run_id, "scope.run_id");
    const suffix = operation === "command" ? "commands" : "queries";
    return requestJson(
      this.baseUrl + "/v1/research/" + encodeURIComponent(runId) + "/" + suffix,
      { method: "POST", body: JSON.stringify(body) },
    );
  }
}
async function requestJson(
  url: string,
  init: RequestInit,
): Promise<JsonObject> {
  let response: Response;
  try {
    response = await fetch(url, {
      ...init,
      credentials: "include",
      headers: { "content-type": "application/json", ...init.headers },
    });
  } catch (error) {
    throw new ClaimApiError(0, "NETWORK_UNAVAILABLE", true, String(error));
  }
  const value: unknown = await response.json().catch(() => null);
  if (value === null || Array.isArray(value) || typeof value !== "object")
    throw new ClaimApiError(response.status, "INVALID_SERVER_ENVELOPE", false);
  const result = value as JsonObject;
  if (!response.ok) {
    const code =
      typeof result.code === "string" ? result.code : "PRODUCT_REQUEST_FAILED";
    throw new ClaimApiError(
      response.status,
      code,
      response.status === 404 ||
        response.status === 501 ||
        response.status === 503 ||
        code.includes("UNKNOWN") ||
        code.includes("UNAVAILABLE") ||
        code.includes("NOT_FOUND"),
    );
  }
  return result;
}
export class ClaimGateway {
  private readonly client: ResearchProductClient;
  constructor(private readonly baseUrl = "") {
    this.client = new ResearchProductClient(new BrowserTransport(baseUrl));
  }
  async queryClaim(runId: string, claimId: string): Promise<ClaimView> {
    const value = await this.client.query({
      scope: qscope(runId),
      type: "CLAIM",
      payload: { claim_id: claimId },
    });
    return claimView(result(value, "CLAIM"));
  }
  async queryHistory(runId: string, claimId: string): Promise<ClaimRevision[]> {
    const value = await this.client.query({
      scope: qscope(runId),
      type: "CLAIM_HISTORY",
      payload: { claim_id: claimId, page: { limit: 100 } },
    });
    return array(
      result(value, "CLAIM_HISTORY").items,
      "CLAIM_HISTORY.items",
    ).map((item, index) =>
      historyView(obj(item, "CLAIM_HISTORY[" + index + "]")),
    );
  }
  async queryWorkflow(runId: string): Promise<WorkflowView> {
    const value = await this.client.query({
      scope: qscope(runId),
      type: "WORKFLOW",
      payload: {},
    });
    const domain = result(value, "WORKFLOW"),
      contexts = workContexts(domain),
      states = obj(domain.state_counts, "state_counts");
    const active = contexts.filter((item) => !isTerminal(item.state));
    return {
      id: "workflow:" + runId,
      phase: "执行工作流",
      state:
        Object.entries(states)
          .map(([state, count]) => `${state} ${count}`)
          .join(" · ") || "无状态",
      activeWorkItemIds: [...new Set(active.map((item) => item.workItemId))],
      digest: "以当前研究修订与活动游标绑定",
    };
  }
  async queryWorkContexts(runId: string): Promise<WorkContext[]> {
    const value = await this.client.query({
      scope: qscope(runId),
      type: "WORKFLOW",
      payload: {},
    });
    return workContexts(result(value, "WORKFLOW"));
  }
  async queryLineage(runId: string, lineageId: string): Promise<LineageView> {
    const value = await this.client.query({
      scope: qscope(runId),
      type: "RESEARCH_CASE_LINEAGE",
      payload: { lineage_id: lineageId },
    });
    const domain = result(value, "RESEARCH_CASE_LINEAGE");
    return {
      id: str(domain.lineage_id, "lineage_id"),
      mode: str(domain.lineage_mode, "lineage_mode"),
      sourceVersion: str(domain.source_version, "source_version"),
      state: str(domain.lineage_state, "lineage_state"),
      digest: str(domain.lineage_digest, "lineage_digest"),
      evidenceClass: "CANDIDATE_SOURCE",
      authorityEffect: "NO_FACT",
    };
  }
  async queryGraph(
    runId: string,
    revision: number,
    mode: "VERIFIED" | "RESEARCH_HISTORY",
    seedIds: string[],
    cursor?: string,
  ): Promise<GraphView> {
    const value = await this.client.query({
      scope: qscope(runId),
      type: "GRAPH_SLICE",
      payload: {
        at_revision: revision,
        depth: 4,
        direction: "BOTH",
        filters: {},
        mode,
        node_limit: 120,
        seed_ids: seedIds,
        ...(cursor ? { continuation_cursor: cursor } : {}),
      },
    });
    return graphView(result(value, "GRAPH_SLICE"));
  }
  async queryArtifacts(runId: string): Promise<ArtifactChoice[]> {
    const value = await this.client.query({
      scope: qscope(runId),
      type: "ARTIFACT_INDEX",
      payload: { page: { limit: 200 } },
    });
    return array(
      result(value, "ARTIFACT_INDEX").items,
      "ARTIFACT_INDEX.items",
    ).map((raw, index) => {
      const item = obj(raw, "ARTIFACT_INDEX[" + index + "]");
      return {
        artifact_id: str(item.artifact_id, "artifact_id"),
        sha256: str(item.sha256, "sha256"),
        label:
          optional(item.logical_name) ||
          optional(item.source_name) ||
          "研究工件",
        role: optional(item.role) || "未分类",
        mediaType: str(item.media_type, "media_type"),
        byteCount: num(item.byte_count, "byte_count"),
      };
    });
  }
  async queryReviewTasks(runId: string): Promise<ReviewTaskChoice[]> {
    const value = await this.client.query({
      scope: qscope(runId),
      type: "REVIEW_INBOX",
      payload: { page: { limit: 100 } },
    });
    return array(result(value, "REVIEW_INBOX").items, "REVIEW_INBOX.items").map(
      (raw, index) => {
        const item = obj(raw, "REVIEW_INBOX[" + index + "]"),
          binding = obj(item.binding_json, "binding_json");
        const signedId = optional(item.signed_artifact_id),
          signedSha = optional(item.signed_artifact_sha256);
        return {
          id: str(item.review_task_id, "review_task_id"),
          label: optional(item.review_type) || "独立审查任务",
          description: `${optional(item.status) || "OPEN"} · ${optional(item.expires_at) || "无到期时间"}`,
          targetDigest: optional(binding.target_digest),
          signedArtifact:
            signedId && signedSha
              ? { artifact_id: signedId, sha256: signedSha }
              : undefined,
          verifierReceiptIds: stringArray(item.verifier_receipt_ids_json),
        };
      },
    );
  }
  async upload(
    file: File,
    onProgress: (received: number, total: number) => void = () => undefined,
  ): Promise<ArtifactBinding> {
    const bytes = await file.arrayBuffer(),
      sha256 = await digestHex(bytes),
      requestId = crypto.randomUUID();
    const begin = await requestJson(this.baseUrl + "/v1/artifacts/operations", {
      method: "POST",
      body: JSON.stringify({
        type: "BEGIN_UPLOAD",
        payload: {
          request_id: requestId,
          logical_name: file.name,
          media_type: file.type || "application/octet-stream",
          byte_count: file.size,
          sha256,
        },
      }),
    });
    const upload = obj(begin.upload, "upload"),
      uploadId = str(upload.upload_id, "upload_id");
    let offset = num(upload.received_byte_count, "received_byte_count");
    while (offset < file.size) {
      const chunk = await file
        .slice(offset, Math.min(offset + 1024 * 1024, file.size))
        .arrayBuffer();
      const response = await fetch(this.baseUrl + "/v1/artifacts/operations", {
        method: "POST",
        credentials: "include",
        headers: {
          "content-type": "application/octet-stream",
          "x-rk-artifact-operation": "APPEND_CHUNK",
          "x-rk-upload-id": uploadId,
          "x-rk-upload-offset": String(offset),
          "x-rk-chunk-sha256": await digestHex(chunk),
        },
        body: chunk,
      });
      const payload = (await response.json()) as JsonObject;
      if (!response.ok)
        throw new ClaimApiError(
          response.status,
          optional(payload.code) || "ARTIFACT_UPLOAD_FAILED",
          false,
        );
      offset = num(
        obj(payload.upload, "upload").received_byte_count,
        "received_byte_count",
      );
      onProgress(offset, file.size);
    }
    const committed = await requestJson(
        this.baseUrl + "/v1/artifacts/operations",
        {
          method: "POST",
          body: JSON.stringify({
            type: "COMMIT_UPLOAD",
            payload: { upload_id: uploadId },
          }),
        },
      ),
      ref = obj(committed.artifact_ref, "artifact_ref");
    return {
      artifact_id: str(ref.artifact_id, "artifact_id"),
      sha256: str(ref.sha256, "sha256"),
    };
  }
  async submitClaim(
    fence: RunFence,
    requestId: string,
    draft: ClaimDraft,
  ): Promise<CommandReceipt> {
    const value = await this.client.command({
      request_id: requestId,
      scope: cscope(fence),
      type: "SUBMIT_CLAIM",
      payload: {
        statement: draft.statement,
        claim_kind: draft.claimKind,
        predecessor_fact_ids: draft.predecessorFactIds,
        work_item_id: draft.workItemId,
        worker_run_id: draft.workerRunId,
        attempt_id: draft.attemptId,
        proof_or_evidence_artifacts:
          draft.proofArtifacts as unknown as JsonObject[],
        source_binding_artifact:
          draft.sourceBindingArtifact as unknown as JsonObject,
        ...(draft.supersedesClaimId
          ? { supersedes_claim_id: draft.supersedesClaimId }
          : {}),
      },
    });
    return receipt(value);
  }
  async importVerification(
    fence: RunFence,
    requestId: string,
    input: {
      reviewTaskId: string;
      signedReviewArtifact: ArtifactBinding;
      targetDigest: string;
      verifierReceiptIds: string[];
    },
  ): Promise<CommandReceipt> {
    const value = await this.client.command({
      request_id: requestId,
      scope: cscope(fence),
      type: "IMPORT_VERIFICATION",
      payload: {
        review_task_id: input.reviewTaskId,
        signed_review_artifact:
          input.signedReviewArtifact as unknown as JsonObject,
        target_digest: input.targetDigest,
        verifier_receipt_ids: input.verifierReceiptIds,
      },
    });
    return receipt(value);
  }
  async queryRevokePreview(
    fence: RunFence,
    claimId: string,
    targetDigest: string,
  ): Promise<RevokePreview> {
    const value = await this.client.query({
        scope: qscope(fence.runId),
        type: "REVOKE_PREVIEW",
        payload: {
          at_revision: fence.revision,
          claim_id: claimId,
          target_digest: targetDigest,
        },
      }),
      domain = result(value, "REVOKE_PREVIEW");
    return {
      id: str(domain.preview_id, "preview_id"),
      targetClaimId: str(domain.target_fact_id, "target_fact_id"),
      targetDigest: str(domain.target_fact_digest, "target_fact_digest"),
      closureDigest: str(domain.closure_digest, "closure_digest"),
      affectedClaimIds: stringArray(domain.affected_fact_ids_json),
      preservedSiblingIds: stringArray(domain.preserved_sibling_ids_json),
      reopenedObligationIds: stringArray(domain.reopened_obligation_ids_json),
      previewRevision: num(domain.preview_revision, "preview_revision"),
    };
  }
  async confirmRevoke(
    fence: RunFence,
    requestId: string,
    preview: RevokePreview,
    input: RevokeConfirmation,
  ): Promise<CommandReceipt> {
    const value = await this.client.command({
      request_id: requestId,
      scope: cscope(fence),
      type: "CONFIRM_REVOKE",
      payload: {
        fact_id: preview.targetClaimId,
        target_fact_digest: preview.targetDigest,
        preview_revision: preview.previewRevision,
        contract_version: fence.contractVersion,
        affected_fact_ids: input.affectedFactIds,
        preserved_sibling_ids: input.preservedSiblingIds,
        reopened_obligation_ids: input.reopenedObligationIds,
        reason_artifact: input.reasonArtifact as unknown as JsonObject,
      },
    });
    return receipt(value);
  }
}
function qscope(runId: string) {
  return { kind: "RUN" as const, run_id: runId };
}
function cscope(fence: RunFence) {
  return {
    kind: "RUN" as const,
    run_id: fence.runId,
    expected_revision: fence.revision,
    expected_contract_version: fence.contractVersion,
  };
}
function receipt(value: JsonObject): CommandReceipt {
  return {
    receiptId: str(value.receipt_id, "receipt_id"),
    state: str(value.state, "state"),
  };
}
function result(value: JsonObject, label: string) {
  return obj(value.result, label + ".result");
}
function claimView(domain: JsonObject): ClaimView {
  return {
    id: str(domain.claim_id, "claim_id"),
    stableLabel: str(domain.stable_label, "stable_label"),
    lifecycle: str(domain.lifecycle, "lifecycle"),
    machineState: str(domain.machine_state, "machine_state"),
    semanticState: str(domain.semantic_state, "semantic_state"),
    statementDigest: str(domain.statement_digest, "statement_digest"),
    artifactIds: stringArray(domain.artifact_ids_json),
  };
}
function historyView(domain: JsonObject): ClaimRevision {
  return {
    id: str(domain.claim_id, "claim_id"),
    revision: num(domain.claim_revision, "claim_revision"),
    lifecycle: str(domain.lifecycle, "lifecycle"),
    statementDigest: str(domain.statement_digest, "statement_digest"),
    supersedesClaimId: optional(domain.supersedes_claim_id),
  };
}
function workContexts(domain: JsonObject): WorkContext[] {
  return array(domain.work_items, "work_items").flatMap((raw, index) => {
    const item = obj(raw, "work_items[" + index + "]"),
      workItemId = str(item.work_item_id, "work_item_id"),
      summary = str(item.assignment_summary, "assignment_summary"),
      runs = array(item.worker_runs, "worker_runs");
    return runs.flatMap((workerRaw, workerIndex) => {
      const worker = obj(workerRaw, `worker_runs[${workerIndex}]`),
        workerRunId = str(worker.worker_run_id, "worker_run_id"),
        role =
          optional(worker.role_id) ||
          optional(worker.worker_kind) ||
          "研究执行",
        attempts = array(worker.attempts, "attempts");
      return attempts.map((attemptRaw, attemptIndex) => {
        const attempt = obj(attemptRaw, `attempts[${attemptIndex}]`),
          attemptId = str(attempt.attempt_id, "attempt_id"),
          state = str(attempt.state, "state");
        return {
          id: `${workItemId}:${workerRunId}:${attemptId}`,
          workItemId,
          workerRunId,
          attemptId,
          label: summary,
          description: `${role} · 第 ${num(attempt.ordinal, "ordinal")} 次运行`,
          state,
        };
      });
    });
  });
}
function graphView(value: JsonObject): GraphView {
  const rawNodes = array(value.nodes, "nodes"),
    rawEdges = array(value.edges, "edges");
  const nodes: GraphNode[] = rawNodes.map((raw, index) => {
    const node = obj(raw, "node[" + index + "]");
    return {
      claimId: str(node.claim_id, "claim_id"),
      stableLabel: str(node.stable_label, "stable_label"),
      statement: str(node.statement, "statement"),
      lifecycle: str(node.lifecycle, "lifecycle"),
      dependable: bool(node.dependable, "dependable"),
      claimType: str(node.claim_type, "claim_type"),
      authorityAxes: obj(node.authority_axes, "authority_axes"),
      contractVersion: num(node.contract_version, "contract_version"),
      verificationMethod: str(node.verification_method, "verification_method"),
    };
  });
  const edges: GraphEdge[] = rawEdges.map((raw, index) => {
    const edge = obj(raw, "edge[" + index + "]");
    return {
      id: str(edge.edge_id, "edge_id"),
      from: str(edge.from_claim_id, "from_claim_id"),
      to: str(edge.to_claim_id, "to_claim_id"),
      direction: str(edge.logical_direction, "logical_direction"),
      obligationStatus: str(edge.obligation_status, "obligation_status"),
    };
  });
  const mode = str(value.mode, "mode");
  if (mode !== "VERIFIED" && mode !== "RESEARCH_HISTORY")
    throw new ClaimApiError(200, "UNKNOWN_GRAPH_MODE", false);
  return {
    mode,
    atRevision: num(value.at_revision, "at_revision"),
    contractVersion: num(value.contract_version, "contract_version"),
    nodes,
    edges,
    total: num(value.total_matches, "total_matches"),
    truncated: bool(value.truncated, "truncated"),
    continuationCursor: optional(value.continuation_cursor),
  };
}
function isTerminal(state: string) {
  return ["SUCCEEDED", "FAILED", "CANCELLED", "OUTCOME_UNKNOWN"].includes(
    state,
  );
}
async function digestHex(value: ArrayBuffer) {
  const digest = await crypto.subtle.digest("SHA-256", value);
  return [...new Uint8Array(digest)]
    .map((part) => part.toString(16).padStart(2, "0"))
    .join("");
}
function obj(value: JsonValue | undefined, path: string): JsonObject {
  if (
    value === null ||
    value === undefined ||
    Array.isArray(value) ||
    typeof value !== "object"
  )
    throw new ClaimApiError(200, "INVALID_SERVER_ENVELOPE", false, path);
  return value;
}
function array(value: JsonValue | undefined, path: string): JsonValue[] {
  if (!Array.isArray(value))
    throw new ClaimApiError(200, "INVALID_SERVER_ENVELOPE", false, path);
  return value;
}
function str(value: JsonValue | undefined, path: string) {
  if (typeof value !== "string" || !value)
    throw new ClaimApiError(200, "INVALID_SERVER_ENVELOPE", false, path);
  return value;
}
function optional(value: JsonValue | undefined) {
  return typeof value === "string" ? value : "";
}
function num(value: JsonValue | undefined, path: string) {
  if (typeof value !== "number" || !Number.isSafeInteger(value))
    throw new ClaimApiError(200, "INVALID_SERVER_ENVELOPE", false, path);
  return value;
}
function bool(value: JsonValue | undefined, path: string) {
  if (typeof value !== "boolean")
    throw new ClaimApiError(200, "INVALID_SERVER_ENVELOPE", false, path);
  return value;
}
function stringArray(value: JsonValue | undefined) {
  return Array.isArray(value) && value.every((item) => typeof item === "string")
    ? (value as string[])
    : [];
}

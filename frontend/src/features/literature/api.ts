import {
  ResearchProductClient,
  type JsonObject,
  type JsonValue,
  type ProductTransport,
} from "../../../../sdk/typescript/src/client.js";
import type {
  ApplicabilityView,
  FeatureFailure,
  LiteratureGraphView,
  LiteratureQueryDraft,
  LiteratureQueryView,
  LiteratureSourceView,
  NoveltyReviewView,
  PriorArtView,
  QueryReceipt,
  ReviewTaskView,
  RunFence,
  SourceMode,
  SourceSnapshotView,
} from "./model.js";
import {failureAction} from "./model.js";

export class LiteratureApiError extends Error {
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
      action: failureAction(this.code),
    };
  }
}

class BrowserProductTransport implements ProductTransport {
  constructor(private readonly baseUrl: string) {}

  async request(
    operation: "command" | "query" | "subscribe" | "artifact",
    body: JsonObject,
  ): Promise<JsonObject> {
    if (operation !== "command" && operation !== "query") {
      throw new LiteratureApiError(501, "FEATURE_ROUTE_UNAVAILABLE", true);
    }
    const scope = object(body.scope, "scope");
    const runId = scope.kind === "RUN" ? string(scope.run_id, "scope.run_id") : undefined;
    const path = operation === "command"
      ? runId
        ? `/v1/research/${encodeURIComponent(runId)}/commands`
        : "/v1/deployment/operations"
      : runId
        ? `/v1/research/${encodeURIComponent(runId)}/queries`
        : "/v1/deployment/queries";
    return requestJson(this.baseUrl + path, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }
}

async function requestJson(url: string, init: RequestInit): Promise<JsonObject> {
  let response: Response;
  try {
    response = await fetch(url, {
      ...init,
      credentials: "include",
      headers: {"content-type": "application/json", ...init.headers},
    });
  } catch (error) {
    throw new LiteratureApiError(0, "NETWORK_UNAVAILABLE", true, String(error));
  }
  const value: unknown = await response.json().catch(() => null);
  if (value === null || Array.isArray(value) || typeof value !== "object") {
    throw new LiteratureApiError(response.status, "INVALID_SERVER_ENVELOPE", false);
  }
  const result = value as JsonObject;
  if (!response.ok) {
    const code = typeof result.code === "string" ? result.code : "PRODUCT_REQUEST_FAILED";
    throw new LiteratureApiError(
      response.status,
      code,
      response.status === 404 ||
        response.status === 501 ||
        response.status === 503 ||
        code.includes("UNAVAILABLE") ||
        code.includes("UNKNOWN_VARIANT"),
    );
  }
  return result;
}

export class LiteratureGateway {
  private readonly client: ResearchProductClient;

  constructor(baseUrl = "") {
    this.client = new ResearchProductClient(new BrowserProductTransport(baseUrl));
  }

  async runLiteratureQuery(
    fence: RunFence,
    requestId: string,
    draft: LiteratureQueryDraft,
  ): Promise<QueryReceipt> {
    const value = await this.client.command({
      request_id: requestId,
      scope: commandScope(fence),
      type: "RUN_LITERATURE_QUERY",
      payload: {
        research_question: draft.researchQuestion,
        connector_profile_ids: draft.connectors,
        query_text: draft.queryText,
        coverage_boundary: draft.coverageBoundary,
        target_entity_ids: draft.targetEntityIds,
        capture_raw_response: true,
      },
    });
    return receipt(value);
  }

  async replaySnapshot(
    fence: RunFence,
    requestId: string,
    snapshotId: string,
    expectedResponseSha256: string,
  ): Promise<QueryReceipt> {
    const value = await this.client.command({
      request_id: requestId,
      scope: commandScope(fence),
      type: "REPLAY_SOURCE_SNAPSHOT",
      payload: {
        source_snapshot_id: snapshotId,
        expected_response_sha256: expectedResponseSha256,
        reconfirm_external_index: false,
      },
    });
    return receipt(value);
  }

  async createLiteratureReviewTask(
    fence: RunFence,
    requestId: string,
    input: {
      targetEntityIds: string[];
      targetDigest: string;
      authorSubjectIds: string[];
      assigneeSubjectId: string;
      expiresAt: string;
    },
  ): Promise<QueryReceipt> {
    const value = await this.client.command({
      request_id: requestId,
      scope: commandScope(fence),
      type: "CREATE_REVIEW_TASK",
      payload: {
        review_type: "LITERATURE",
        target_entity_ids: input.targetEntityIds,
        target_digest: input.targetDigest,
        author_subject_ids: input.authorSubjectIds,
        assignee_subject_id: input.assigneeSubjectId,
        independence_constraints: {
          required_capability: "LITERATURE_REVIEWER",
          reject_author_overlap: true,
          no_second_math_truth: true,
        },
        expires_at: input.expiresAt,
      },
    });
    return receipt(value);
  }

  queryLiteratureQuery(runId: string, id: string): Promise<LiteratureQueryView> {
    return this.entity(runId, "LITERATURE_QUERY", {literature_query_id: id}, queryView);
  }

  querySnapshot(runId: string, id: string): Promise<SourceSnapshotView> {
    return this.entity(runId, "SOURCE_SNAPSHOT", {source_snapshot_id: id}, snapshotView);
  }

  querySource(runId: string, id: string): Promise<LiteratureSourceView> {
    return this.entity(runId, "LITERATURE_SOURCE", {literature_source_id: id}, sourceView);
  }

  async queryGraph(runId: string, id: string): Promise<LiteratureGraphView> {
    const value = await this.client.query({
      scope: queryScope(runId),
      type: "LITERATURE_GRAPH",
      payload: {literature_graph_id: id, page: {limit: 100}},
    });
    const projection = firstListProjection(value, "LITERATURE_GRAPH");
    return graphView(projection);
  }

  queryApplicability(runId: string, id: string): Promise<ApplicabilityView> {
    return this.entity(
      runId,
      "THEOREM_APPLICABILITY",
      {applicability_review_id: id},
      applicabilityView,
    );
  }

  queryPriorArt(runId: string, id: string): Promise<PriorArtView> {
    return this.entity(runId, "PRIOR_ART_COMPARISON", {comparison_id: id}, priorArtView);
  }

  queryNovelty(runId: string, id: string): Promise<NoveltyReviewView> {
    return this.entity(runId, "NOVELTY_REVIEW", {novelty_review_id: id}, noveltyView);
  }

  queryReviewTask(runId: string, id: string): Promise<ReviewTaskView> {
    return this.entity(runId, "REVIEW_TASK", {review_task_id: id}, reviewTaskView);
  }

  private async entity<T>(
    runId: string,
    type:
      | "LITERATURE_QUERY"
      | "SOURCE_SNAPSHOT"
      | "LITERATURE_SOURCE"
      | "THEOREM_APPLICABILITY"
      | "PRIOR_ART_COMPARISON"
      | "NOVELTY_REVIEW"
      | "REVIEW_TASK",
    payload: JsonObject,
    parse: (projection: JsonObject) => T,
  ): Promise<T> {
    const value = await this.client.query({scope: queryScope(runId), type, payload} as never);
    return parse(entityProjection(value, type));
  }
}

function commandScope(fence: RunFence) {
  return {
    kind: "RUN" as const,
    run_id: fence.runId,
    expected_revision: fence.revision,
    expected_contract_version: fence.contractVersion,
  };
}

function queryScope(runId: string) {
  return {kind: "RUN" as const, run_id: runId};
}

function receipt(value: JsonObject): QueryReceipt {
  return {
    receiptId: string(value.receipt_id, "receipt_id"),
    state: string(value.state, "state"),
    jobId: optionalString(value.job_id),
  };
}

function entityProjection(value: JsonObject, type: string): JsonObject {
  const result = object(value.result, `${type}.result`);
  return object(result.entity, `${type}.result.entity`);
}

function firstListProjection(value: JsonObject, type: string): JsonObject {
  const result = object(value.result, `${type}.result`);
  const items = result.items;
  if (!Array.isArray(items) || items.length === 0) {
    throw new LiteratureApiError(200, `${type}_EMPTY`, false);
  }
  return object(items[0], `${type}.result.items[0]`);
}

function projectionDomain(value: JsonObject): JsonObject {
  return object(value.domain, "projection.domain");
}

function queryView(value: JsonObject): LiteratureQueryView {
  const domain = projectionDomain(value);
  return {
    id: string(domain.literature_query_id, "literature_query_id"),
    status: string(value.status, "status"),
    sourceMode: sourceMode(domain.source_mode),
    snapshotId: string(domain.snapshot_id, "snapshot_id"),
    queryDigest: string(domain.query_digest, "query_digest"),
    coverageBoundary: string(domain.coverage_boundary, "coverage_boundary"),
  };
}

function snapshotView(value: JsonObject): SourceSnapshotView {
  const domain = projectionDomain(value);
  return {
    id: string(domain.snapshot_id, "snapshot_id"),
    status: string(value.status, "status"),
    sourceMode: sourceMode(domain.source_mode),
    corpusDigest: string(domain.corpus_digest, "corpus_digest"),
    retrievedAt: string(domain.retrieved_at, "retrieved_at"),
    coverageBoundary: string(domain.coverage_boundary, "coverage_boundary"),
    evidenceClass: string(value.evidence_class, "evidence_class"),
    authorityEffect: string(value.authority_effect, "authority_effect"),
  };
}

function sourceView(value: JsonObject): LiteratureSourceView {
  const domain = projectionDomain(value);
  return {
    id: string(domain.literature_source_id, "literature_source_id"),
    status: string(value.status, "status"),
    stableSourceId: string(domain.stable_source_id, "stable_source_id"),
    sourceVersion: string(domain.source_version, "source_version"),
    sourceKind: string(domain.source_kind, "source_kind"),
    contentDigest: string(domain.content_digest, "content_digest"),
    sourceArtifactIds: strings(value.source_artifact_ids, "source_artifact_ids"),
  };
}

function graphView(value: JsonObject): LiteratureGraphView {
  const domain = projectionDomain(value);
  return {
    id: string(domain.literature_graph_id, "literature_graph_id"),
    status: string(value.status, "status"),
    nodeCount: integer(domain.node_count, "node_count"),
    edgeCount: integer(domain.edge_count, "edge_count"),
    sourceKinds: strings(domain.source_kinds, "source_kinds"),
    graphDigest: string(domain.graph_digest, "graph_digest"),
    sourceArtifactIds: strings(value.source_artifact_ids, "source_artifact_ids"),
  };
}

function applicabilityView(value: JsonObject): ApplicabilityView {
  const domain = projectionDomain(value);
  return {
    id: string(domain.applicability_review_id, "applicability_review_id"),
    status: string(value.status, "status"),
    theoremId: string(domain.theorem_id, "theorem_id"),
    claimId: string(domain.claim_id, "claim_id"),
    verdict: string(domain.verdict, "verdict"),
    reviewArtifactId: string(domain.review_artifact_id, "review_artifact_id"),
  };
}

function priorArtView(value: JsonObject): PriorArtView {
  const domain = projectionDomain(value);
  return {
    id: string(domain.comparison_id, "comparison_id"),
    status: optionalString(value.status) ?? "AVAILABLE",
    claimId: string(domain.claim_id, "claim_id"),
    literatureSourceId: string(domain.literature_source_id, "literature_source_id"),
    relationship: string(domain.relationship, "relationship"),
    comparisonDigest: string(domain.comparison_digest, "comparison_digest"),
  };
}

function noveltyView(value: JsonObject): NoveltyReviewView {
  const domain = projectionDomain(value);
  return {
    id: string(domain.novelty_review_id, "novelty_review_id"),
    status: string(value.status, "status"),
    claimId: string(domain.claim_id, "claim_id"),
    verdict: string(domain.verdict, "verdict"),
    coverageSnapshotIds: strings(domain.coverage_snapshot_ids, "coverage_snapshot_ids"),
    reviewArtifactId: string(domain.review_artifact_id, "review_artifact_id"),
  };
}

function reviewTaskView(value: JsonObject): ReviewTaskView {
  const domain = projectionDomain(value);
  return {
    id: string(domain.review_task_id, "review_task_id"),
    status: string(value.status, "status"),
    reviewType: string(domain.review_type, "review_type"),
    reviewState: string(domain.review_state, "review_state"),
    targetId: string(domain.target_id, "target_id"),
    signedReviewArtifactId: string(
      domain.signed_review_artifact_id,
      "signed_review_artifact_id",
    ),
  };
}

function sourceMode(value: JsonValue | undefined): SourceMode {
  const mode = string(value, "source_mode");
  if (mode !== "LIVE_QUERY" && mode !== "REPLAYED_SNAPSHOT") {
    throw new LiteratureApiError(200, "UNKNOWN_SOURCE_MODE", false);
  }
  return mode;
}

function object(value: JsonValue | undefined, path: string): JsonObject {
  if (value === null || value === undefined || Array.isArray(value) || typeof value !== "object") {
    throw new LiteratureApiError(200, "INVALID_SERVER_ENVELOPE", false, `${path} is not an object`);
  }
  return value;
}

function string(value: JsonValue | undefined, path: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new LiteratureApiError(200, "INVALID_SERVER_ENVELOPE", false, `${path} is not a string`);
  }
  return value;
}

function optionalString(value: JsonValue | undefined): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function integer(value: JsonValue | undefined, path: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
    throw new LiteratureApiError(200, "INVALID_SERVER_ENVELOPE", false, `${path} is not an integer`);
  }
  return value;
}

function strings(value: JsonValue | undefined, path: string): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new LiteratureApiError(200, "INVALID_SERVER_ENVELOPE", false, `${path} is not a string array`);
  }
  return value as string[];
}

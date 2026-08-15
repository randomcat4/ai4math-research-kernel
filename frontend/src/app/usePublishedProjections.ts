import { useEffect, useMemo, useState } from "react";

import type { Capability, Engine, RunView } from "../features/compute/model";
import type { ProblemPoolView } from "../features/problem-pool/model";
import type { PublicationView } from "../features/publication/model";
import type { LineageCase } from "../features/research-lineage/model";
import type { ToolRunView, ToolView } from "../features/tools/model";
import type { RouteChoice, WorkItemView } from "../features/work/model";
import type { ProductMeta, ResearchSummary } from "./api";

export type QueryPhase =
  | "loading"
  | "ready"
  | "empty"
  | "not_found"
  | "unpublished"
  | "error";

type QueryEnvelope = {
  result_type: string;
  stable_entity_id: string;
  research_revision?: number;
  contract_version?: number;
  deployment_revision?: number;
  last_cursor?: number;
  result: Record<string, unknown>;
};

export type AreaState = { phase: QueryPhase; detail: string };

export type PublishedProjectionModel = {
  work: AreaState;
  compute: AreaState;
  publication: AreaState;
  pool: AreaState;
  routePlanId: string;
  planDigest: string;
  routes: RouteChoice[];
  activeItems: WorkItemView[];
  historyItems: WorkItemView[];
  capabilities: Capability[];
  computeRuns: RunView[];
  tools: ToolView[];
  toolRuns: ToolRunView[];
  publicationView?: PublicationView;
  problemPool?: ProblemPoolView;
  lineages: LineageCase[];
  deploymentRevision?: number;
};

class ProjectionError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super(code);
  }
}

const initialArea: AreaState = {
  phase: "loading",
  detail: "正在读取真实服务端投影",
};

const initialModel: PublishedProjectionModel = {
  work: initialArea,
  compute: initialArea,
  publication: initialArea,
  pool: initialArea,
  routePlanId: "",
  planDigest: "",
  routes: [],
  activeItems: [],
  historyItems: [],
  capabilities: [],
  computeRuns: [],
  tools: [],
  toolRuns: [],
  lineages: [],
};

function object(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.map(object).filter((item) => item !== undefined)
    : [];
}

function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function number(value: unknown): number {
  return typeof value === "number" ? value : 0;
}

async function request(
  path: string,
  scope: Record<string, unknown>,
  type: string,
  payload: Record<string, unknown>,
): Promise<QueryEnvelope> {
  const response = await fetch(path, {
    method: "POST",
    credentials: "include",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      schema_version: "rk.product.query.v1",
      scope,
      query: { type, payload },
    }),
  });
  const value: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const body = object(value);
    throw new ProjectionError(
      response.status,
      text(body?.code) || `HTTP_${response.status}`,
    );
  }
  const body = object(value);
  const result = object(body?.result);
  if (!body || !result || text(body.result_type) !== type) {
    throw new ProjectionError(502, "INVALID_QUERY_RESULT");
  }
  return { ...body, result } as QueryEnvelope;
}

function area(error: unknown): AreaState {
  if (error instanceof ProjectionError && error.status === 404) {
    return {
      phase: "not_found",
      detail:
        "服务端没有返回所请求的对象；这不代表当前研究中该类对象数量为零。",
    };
  }
  if (error instanceof ProjectionError && error.status === 503) {
    return { phase: "unpublished", detail: "服务端尚未发布此查询投影" };
  }
  return {
    phase: "error",
    detail:
      error instanceof ProjectionError
        ? `查询失败 · ${error.code}`
        : "查询连接失败",
  };
}

function collectIds(
  value: unknown,
  key: string,
  found = new Set<string>(),
): Set<string> {
  if (Array.isArray(value)) {
    value.forEach((item) => collectIds(item, key, found));
    return found;
  }
  const item = object(value);
  if (!item) return found;
  const candidate = item[key];
  if (typeof candidate === "string" && candidate) found.add(candidate);
  Object.values(item).forEach((child) => collectIds(child, key, found));
  return found;
}

function mapAttempt(value: Record<string, unknown>) {
  const attempts = records(value.attempts);
  const latest = attempts.at(-1);
  return {
    attemptId: text(latest?.attempt_id) || text(value.worker_run_id),
    workerRunId: text(value.worker_run_id),
    workerLabel: text(value.role_id) || text(value.worker_kind),
    state: text(latest?.state) || text(value.state),
    startedAt:
      text(latest?.started_at) ||
      text(value.started_at) ||
      text(value.enqueued_at),
    publicSummary:
      text(latest?.diagnostic_code) ||
      text(value.stop_reason) ||
      "无公开诊断摘要",
  };
}

function mapWorkItems(value: unknown): WorkItemView[] {
  return records(value).map((item, index) => ({
    workItemId: text(item.work_item_id),
    stableLabel: text(item.logical_key),
    title: text(item.assignment_summary),
    state: text(item.aggregate_state),
    routeId: text(item.route_id),
    position: index + 1,
    attempts: records(item.worker_runs).map(mapAttempt),
  }));
}

function mapRoutes(value: unknown): RouteChoice[] {
  return records(value).map((route) => ({
    routeId: text(route.route_id),
    label: text(route.method),
    thesis: `${text(route.target)} · verifier ${text(route.expected_verifier)}`,
    state: text(route.state) as RouteChoice["state"],
    priority: number(route.priority),
    budget: (object(route.budget) as Record<string, number>) ?? {},
    stopReason: text(route.stop_reason) || undefined,
  }));
}

function engineFor(tool: Record<string, unknown>): Engine | undefined {
  const value =
    `${text(tool.tool_id)} ${text(tool.function_name)}`.toUpperCase();
  if (value.includes("LEAN")) return "LEAN";
  if (value.includes("Z3")) return "Z3";
  if (value.includes("SYMPY") || value.includes("CAS")) return "CAS";
  if (value.includes("ENUM")) return "ENUMERATION";
  if (value.includes("PYTHON")) return "PYTHON";
  return undefined;
}

function mapTools(value: unknown): {
  capabilities: Capability[];
  tools: ToolView[];
} {
  const source = records(value);
  const tools = source.map((tool) => ({
    toolId: text(tool.tool_id),
    version: text(tool.tool_version) || text(tool.profile_version),
    functionName: text(tool.function_name),
    schemaDigest: text(tool.function_schema_digest),
    state: text(tool.availability) as ToolView["state"],
    placement: text(tool.provider) || text(tool.profile_id),
    authorityCeiling: text(tool.authority_ceiling),
    description: text(tool.public_summary) || text(tool.build),
  }));
  const capabilities = source.flatMap((tool): Capability[] => {
    const engine = engineFor(tool);
    if (!engine) return [];
    return [
      {
        engine,
        state: text(tool.availability) as Capability["state"],
        version: text(tool.tool_version) || text(tool.profile_version),
        placement: text(tool.provider) || text(tool.profile_id),
        limits: {},
        detail: `${text(tool.function_name)} · authority ${text(tool.authority_ceiling)}`,
      },
    ];
  });
  return { capabilities, tools };
}

function mapToolRun(value: Record<string, unknown>): ToolRunView {
  const attempts = records(value.attempts);
  const latest = attempts.at(-1);
  return {
    toolRunId: text(value.tool_run_id),
    toolId: strings(value.tool_key).join(" / "),
    state: text(value.invocation_status),
    validationState:
      text(value.validation_status) === "NOT_SUBMITTED"
        ? "NOT_REVIEWED"
        : (text(value.validation_status) as ToolRunView["validationState"]),
    authorityState:
      text(value.authority_ceiling) === "MATHEMATICAL_AUTHORITY"
        ? "PROMOTED_BY_KERNEL"
        : "NO_FACT_GRAPH_WRITE",
    outputArtifactIds: strings(latest?.output_artifact_ids),
  };
}

function mapComputeRun(value: Record<string, unknown>): RunView | undefined {
  const engine = text(value.engine) as Engine;
  if (!["PYTHON", "LEAN", "Z3", "CAS", "ENUMERATION"].includes(engine))
    return undefined;
  return {
    computeTaskId: text(value.compute_task_id),
    jobId: text(value.job_id),
    engine,
    state: text(value.state),
    placement: text(value.placement_provider) || text(value.placement),
    parameters: object(value.parameters) ?? {},
    resources: (object(value.resources) as Record<string, number>) ?? {},
    logId: text(value.public_log_artifact_id) || undefined,
    artifacts: records(value.artifacts).flatMap((item) => {
      const ref = artifact(item);
      if (!ref) return [];
      return [
        {
          ...ref,
          name: text(item.name) || ref.artifact_id,
          view: text(item.view) as RunView["artifacts"][number]["view"],
        },
      ];
    }),
    receiptId: text(value.receipt_id) || undefined,
    externalCallRef: text(value.external_call_ref) || undefined,
    validationState: text(value.validation_state) as RunView["validationState"],
    authorityState: text(value.authority_state) as RunView["authorityState"],
  };
}

function artifact(value: unknown) {
  const item = object(value);
  if (!item || !text(item.artifact_id) || !text(item.sha256)) return undefined;
  return {
    artifact_id: text(item.artifact_id),
    sha256: text(item.sha256),
    byte_count: number(item.byte_count),
    media_type: text(item.media_type),
  };
}

function mapPublication(
  value: Record<string, unknown>,
): PublicationView | undefined {
  const finalization = object(value.finalization);
  if (!finalization) return undefined;
  const candidate = object(value.candidate);
  const review = object(value.paper_review);
  const compilation = object(value.compilation);
  const candidateTex = artifact(candidate?.candidate_tex_artifact_json);
  const signedArtifact = artifact(review?.signed_review_artifact_json);
  const pdf = artifact(compilation?.pdf_artifact_json);
  return {
    state: text(finalization.state),
    finalizedRevision: number(finalization.finalized_revision),
    terminalRootId: text(finalization.terminal_root_id),
    closureDigest: text(finalization.dependency_closure_digest),
    candidateTex,
    generationCommandDigest:
      text(candidate?.generation_command_digest) || undefined,
    paperReview:
      review && signedArtifact
        ? {
            paperReviewId: text(review.paper_review_id),
            reviewTaskId: text(review.review_task_id),
            candidateTexDigest: text(review.candidate_tex_digest),
            verdict: text(review.verdict),
            signedArtifact,
          }
        : undefined,
    pdf,
    compileLogId: text(compilation?.compile_log_artifact_id) || undefined,
    compileState: text(compilation?.state) || undefined,
    abstractDigest: text(candidate?.abstract_digest) || undefined,
    reviewedAbstractDigest: text(review?.abstract_digest) || undefined,
  };
}

function mapLineage(value: Record<string, unknown>): LineageCase {
  return {
    lineageId: text(value.lineage_id),
    projectId: text(value.source_project_id),
    runId: text(value.run_id),
    mode: text(value.mode) as LineageCase["mode"],
    manifestId: text(value.manifest_id),
    manifestDigest: text(value.manifest_digest),
    state: text(value.state),
    candidateArtifacts: records(value.artifacts)
      .map(artifact)
      .filter((item) => item !== undefined),
    conclusionState: text(
      value.conclusion_state,
    ) as LineageCase["conclusionState"],
    noRediscovery: value.historical_conclusions_injected === false,
    reportId: text(value.report_id) || undefined,
  };
}

function mapPool(value: Record<string, unknown>): ProblemPoolView | undefined {
  const semanticAuditArtifact = artifact(value.semantic_audit_artifact);
  if (!semanticAuditArtifact) return undefined;
  return {
    poolId: text(value.problem_pool_id),
    name: text(value.pool_name) || text(value.problem_pool_id),
    rules: {
      dateFrom: text(value.date_from),
      dateTo: text(value.date_to),
      subjectClasses: strings(value.subjects),
      versionRule:
        text(value.version_rule) === "ALL_VERSIONS"
          ? "ALL_VERSIONS"
          : "LATEST_VERSION_AS_OF_CUTOFF",
      withdrawnRule:
        text(value.withdrawal_rule) === "INCLUDE_FLAGGED"
          ? "INCLUDE_FLAGGED"
          : "EXCLUDE",
      semanticSampleSize: number(value.semantic_sample_size),
      inclusionRules: strings(value.inclusion_rules),
      exclusionRules: strings(value.exclusion_rules),
    },
    candidates: records(value.candidates).map((candidate) => ({
      candidateId: text(candidate.problem_candidate_id),
      arxivId: text(candidate.arxiv_id),
      version: String(number(candidate.version)),
      date: text(candidate.published_at),
      subjects: strings(candidate.subjects),
      title: text(candidate.extracted_statement),
      state: text(
        candidate.recommendation_status,
      ) as ProblemPoolView["candidates"][number]["state"],
      reason: text(candidate.audit_status),
      semanticAudit: text(
        candidate.audit_status,
      ) as ProblemPoolView["candidates"][number]["semanticAudit"],
    })),
    sourceSnapshotIds: strings(value.source_snapshot_ids),
    semanticAuditArtifact,
    contractTemplateArtifact: artifact(value.contract_template_artifact),
  };
}

export function usePublishedProjections(
  research: ResearchSummary | undefined,
  meta: ProductMeta | null,
  onReload: () => Promise<void>,
): PublishedProjectionModel {
  const [model, setModel] = useState<PublishedProjectionModel>(initialModel);
  const runScope = useMemo(
    () =>
      research
        ? {
            kind: "RUN",
            run_id: research.run_id,
            at_revision: research.research_revision,
            at_contract_version: research.contract_version,
          }
        : undefined,
    [research],
  );

  useEffect(() => {
    if (!research || !meta || !runScope) {
      setModel(initialModel);
      return;
    }
    let active = true;
    const runPath = `/v1/research/${encodeURIComponent(research.run_id)}/queries`;
    const runQuery = (type: string, payload: Record<string, unknown>) =>
      request(runPath, runScope, type, payload);
    const globalQuery = (type: string, payload: Record<string, unknown>) =>
      request(
        "/v1/deployment/queries",
        {
          kind: "GLOBAL",
          deployment_id: meta.deployment_id,
        },
        type,
        payload,
      );
    const deploymentQuery = (type: string, payload: Record<string, unknown>) =>
      request(
        "/v1/deployment/queries",
        {
          kind: "DEPLOYMENT",
          deployment_id: meta.deployment_id,
        },
        type,
        payload,
      );

    async function load() {
      setModel(initialModel);
      const [
        overviewResult,
        workflowResult,
        publicationResult,
        toolsResult,
        adminResult,
      ] = await Promise.allSettled([
        runQuery("RESEARCH_OVERVIEW", {}),
        runQuery("WORKFLOW", {}),
        runQuery("PUBLICATION_STATUS", {}),
        deploymentQuery("TOOL_CATALOG", { page: { limit: 200 } }),
        deploymentQuery("DEPLOYMENT_STATUS", {}),
      ]);
      if (!active) return;
      const failures = [
        overviewResult,
        workflowResult,
        publicationResult,
        toolsResult,
        adminResult,
      ]
        .filter(
          (item): item is PromiseRejectedResult => item.status === "rejected",
        )
        .map((item) => item.reason);
      if (
        failures.some(
          (error) => error instanceof ProjectionError && error.status === 409,
        )
      ) {
        await onReload();
        return;
      }
      const overview =
        overviewResult.status === "fulfilled"
          ? overviewResult.value.result
          : {};
      const workflow =
        workflowResult.status === "fulfilled"
          ? workflowResult.value.result
          : {};
      const idsSource = [
        overview,
        workflow,
        publicationResult.status === "fulfilled"
          ? publicationResult.value.result
          : {},
      ];
      const first = (key: string) => [...collectIds(idsSource, key)][0];
      const routePlanId = first("route_plan_id");
      const detailRequests: Promise<QueryEnvelope>[] = [];
      const labels: string[] = [];
      if (routePlanId) {
        labels.push("route");
        detailRequests.push(
          runQuery("ROUTE_PLAN", { route_plan_id: routePlanId }),
        );
      }
      for (const toolRunId of collectIds(idsSource, "tool_run_id")) {
        labels.push("toolRun");
        detailRequests.push(runQuery("TOOL_RUN", { tool_run_id: toolRunId }));
      }
      for (const computeTaskId of collectIds(idsSource, "compute_task_id")) {
        labels.push("compute");
        detailRequests.push(
          runQuery("COMPUTE_TASK", { compute_task_id: computeTaskId }),
        );
      }
      for (const lineageId of collectIds(idsSource, "lineage_id")) {
        labels.push("lineage");
        detailRequests.push(
          runQuery("RESEARCH_CASE_LINEAGE", { lineage_id: lineageId }),
        );
      }
      const poolId = first("problem_pool_id");
      if (poolId) {
        labels.push("pool");
        detailRequests.push(
          globalQuery("PROBLEM_POOL", { problem_pool_id: poolId }),
        );
      }
      for (const dossierId of collectIds(idsSource, "dossier_id")) {
        labels.push("dossier");
        detailRequests.push(runQuery("DOSSIER", { dossier_id: dossierId }));
      }
      for (const batchJobId of collectIds(idsSource, "batch_job_id")) {
        labels.push("batch");
        detailRequests.push(
          globalQuery("BATCH_RESEARCH_JOB", { batch_job_id: batchJobId }),
        );
      }
      const details = await Promise.allSettled(detailRequests);
      if (!active) return;
      let route: QueryEnvelope | undefined;
      let routeFailure: unknown;
      const toolRuns: ToolRunView[] = [];
      const lineages: LineageCase[] = [];
      const computeRuns: RunView[] = [];
      let pool: ProblemPoolView | undefined;
      details.forEach((result, index) => {
        if (result.status !== "fulfilled") {
          if (labels[index] === "route") routeFailure = result.reason;
          return;
        }
        if (labels[index] === "route") route = result.value;
        if (labels[index] === "toolRun")
          toolRuns.push(mapToolRun(result.value.result));
        if (labels[index] === "lineage")
          lineages.push(mapLineage(result.value.result));
        if (labels[index] === "pool") pool = mapPool(result.value.result);
        if (labels[index] === "compute") {
          const compute = mapComputeRun(result.value.result);
          if (compute) computeRuns.push(compute);
        }
      });
      const workItems = mapWorkItems(workflow.work_items);
      const activeItems = workItems.filter(
        (item) => !["COMPLETED", "FAILED", "CANCELLED"].includes(item.state),
      );
      const historyItems = workItems.filter((item) =>
        ["COMPLETED", "FAILED", "CANCELLED"].includes(item.state),
      );
      const toolCatalog =
        toolsResult.status === "fulfilled"
          ? mapTools(toolsResult.value.result.items)
          : { capabilities: [], tools: [] };
      const publicationView =
        publicationResult.status === "fulfilled"
          ? mapPublication(publicationResult.value.result)
          : undefined;
      setModel({
        work:
          workflowResult.status !== "fulfilled"
            ? area(workflowResult.reason)
            : routePlanId && !route
              ? area(
                  routeFailure ??
                    new ProjectionError(404, "ROUTE_PLAN_NOT_FOUND"),
                )
              : route || workItems.length > 0
                ? { phase: "ready", detail: "已绑定真实工作流投影" }
                : {
                    phase: "empty",
                    detail: "当前研究尚未创建路线计划或工作项。",
                  },
        compute:
          toolsResult.status !== "fulfilled"
            ? area(toolsResult.reason)
            : toolCatalog.tools.length > 0
              ? { phase: "ready", detail: "已绑定真实工具目录与运行回执" }
              : { phase: "empty", detail: "当前部署尚未登记可用工具。" },
        publication:
          publicationResult.status !== "fulfilled"
            ? area(publicationResult.reason)
            : publicationView
              ? { phase: "ready", detail: "已绑定真实发布状态" }
              : { phase: "empty", detail: "研究尚未进入终态发布链。" },
        pool: pool
          ? { phase: "ready", detail: "已绑定真实题池与科研谱系" }
          : poolId
            ? { phase: "error", detail: "题池投影缺少精确语义审计工件绑定" }
            : { phase: "empty", detail: "当前研究未关联题池或科研谱系" },
        routePlanId: text(route?.result.route_plan_id),
        planDigest: text(route?.result.plan_digest),
        routes: mapRoutes(route?.result.routes),
        activeItems,
        historyItems,
        capabilities: toolCatalog.capabilities,
        computeRuns,
        tools: toolCatalog.tools,
        toolRuns,
        publicationView,
        problemPool: pool,
        lineages,
        deploymentRevision:
          adminResult.status === "fulfilled"
            ? adminResult.value.deployment_revision
            : undefined,
      });
    }
    void load().catch((error) => {
      if (!active) return;
      const failed = area(error);
      setModel({
        ...initialModel,
        work: failed,
        compute: failed,
        publication: failed,
        pool: failed,
      });
    });
    return () => {
      active = false;
    };
  }, [meta, onReload, research, runScope]);

  return model;
}

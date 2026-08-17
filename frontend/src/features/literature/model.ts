export type Availability = "AVAILABLE" | "UNAVAILABLE" | "LOADING" | "FAILED";
export type SourceMode = "LIVE_QUERY" | "REPLAYED_SNAPSHOT";
export type ConnectorId = "OPENALEX" | "CROSSREF" | "ARXIV" | "MATLAS";

export interface RunFence {
  runId: string;
  revision: number;
  contractVersion: number;
}

export interface LiteratureQueryDraft {
  researchQuestion: string;
  queryText: string;
  coverageBoundary: string;
  connectors: ConnectorId[];
  targetEntityIds: string[];
}

export interface QueryReceipt {
  receiptId: string;
  state: string;
  jobId?: string;
}

export interface LiteratureQueryView {
  id: string;
  status: string;
  sourceMode: SourceMode;
  snapshotId: string;
  queryDigest: string;
  coverageBoundary: string;
}

export interface SourceSnapshotView {
  id: string;
  status: string;
  sourceMode: SourceMode;
  corpusDigest: string;
  retrievedAt: string;
  coverageBoundary: string;
  evidenceClass: string;
  authorityEffect: string;
}

export interface LiteratureSourceView {
  id: string;
  status: string;
  stableSourceId: string;
  sourceVersion: string;
  sourceKind: string;
  contentDigest: string;
  sourceArtifactIds: string[];
}

export interface LiteratureGraphView {
  id: string;
  status: string;
  nodeCount: number;
  edgeCount: number;
  sourceKinds: string[];
  graphDigest: string;
  sourceArtifactIds: string[];
}

export interface ApplicabilityView {
  id: string;
  status: string;
  theoremId: string;
  claimId: string;
  verdict: string;
  reviewArtifactId: string;
}

export interface PriorArtView {
  id: string;
  status: string;
  claimId: string;
  literatureSourceId: string;
  relationship: string;
  comparisonDigest: string;
}

export interface NoveltyReviewView {
  id: string;
  status: string;
  claimId: string;
  verdict: string;
  coverageSnapshotIds: string[];
  reviewArtifactId: string;
}

export interface ReviewTaskView {
  id: string;
  status: string;
  reviewType: string;
  reviewState: string;
  targetId: string;
  signedReviewArtifactId: string;
}

export interface FeatureFailure {
  code: string;
  message: string;
  unavailable: boolean;
  action: string;
}

export interface NoveltyPresentation {
  tone: "NEUTRAL" | "WARNING" | "CONFIRMED";
  label: string;
  detail: string;
}

const CONNECTOR_LABELS: Record<ConnectorId, string> = {
  OPENALEX: "OpenAlex",
  CROSSREF: "Crossref",
  ARXIV: "arXiv",
  MATLAS: "Matlas",
};

export function connectorLabel(value: ConnectorId): string {
  return CONNECTOR_LABELS[value];
}

export function sourceModeLabel(mode: SourceMode): string {
  return mode === "LIVE_QUERY" ? "在线查询 · LIVE" : "快照重放 · REPLAY";
}

export function noveltyPresentation(
  snapshot: SourceSnapshotView | undefined,
  review: NoveltyReviewView | undefined,
  task: ReviewTaskView | undefined,
): NoveltyPresentation {
  if (!snapshot) {
    return {
      tone: "NEUTRAL",
      label: "尚无新颖性结论",
      detail: "先保存带覆盖边界的来源快照，再创建独立文献审查任务。",
    };
  }
  if (snapshot.status === "NO_HIT") {
    return {
      tone: "WARNING",
      label: "未命中不等于新颖",
      detail: "扩展检索式、数据源与时间边界；NO_HIT 只能说明本次覆盖范围内未返回记录。",
    };
  }
  if (snapshot.status !== "SUCCESS") {
    return {
      tone: "WARNING",
      label: "检索失败，不能判断新颖性",
      detail: "查看来源回执，修复连接器或覆盖边界后重新在线查询。",
    };
  }
  if (!review || !task) {
    return {
      tone: "NEUTRAL",
      label: "等待独立文献审查",
      detail: "机器检索、Matlas 命中和文献图都不能直接给出新颖性。",
    };
  }
  const signedIndependentReview =
    task.reviewType === "LITERATURE" &&
    task.reviewState === "SUBMITTED" &&
    task.signedReviewArtifactId.length > 0 &&
    review.reviewArtifactId === task.signedReviewArtifactId;
  if (review.verdict === "NOVEL" && signedIndependentReview) {
    return {
      tone: "CONFIRMED",
      label: "独立审查记录为 NOVEL",
      detail: "该结论仅在所列覆盖快照与签名审查范围内成立，不产生数学事实。",
    };
  }
  return {
    tone: "WARNING",
    label: "没有可显示的新颖性确认",
    detail: signedIndependentReview
      ? `独立审查结论：${review.verdict}。`
      : "审查尚未由独立 LITERATURE_REVIEWER 签名提交。",
  };
}

export function failureAction(code: string): string {
  if (code.includes("UNKNOWN") || code.includes("VARIANT") || code.includes("UNAVAILABLE")) {
    return "当前服务尚未发布该契约 variant；升级后端并刷新产品元信息。";
  }
  if (code.includes("TIMEOUT")) return "缩小查询范围或稍后重试在线查询；已保存的快照仍可重放。";
  if (code.includes("CURSOR")) return "结果分页游标已过期，请从固定快照重新打开。";
  return "检查回执与覆盖边界，修复后使用新的 request_id 重试。";
}

export interface RunFence {
  runId: string;
  revision: number;
  contractVersion: number;
  lastCursor?: number;
}
export interface ArtifactBinding {
  artifact_id: string;
  sha256: string;
}
export interface ArtifactChoice extends ArtifactBinding {
  label: string;
  role: string;
  mediaType: string;
  byteCount: number;
}
export interface ClaimView {
  id: string;
  stableLabel: string;
  lifecycle: string;
  machineState: string;
  semanticState: string;
  statementDigest: string;
  artifactIds: string[];
}
export interface ClaimRevision {
  id: string;
  revision: number;
  lifecycle: string;
  statementDigest: string;
  supersedesClaimId: string;
}
export interface WorkflowView {
  id: string;
  phase: string;
  state: string;
  activeWorkItemIds: string[];
  digest: string;
}
export interface WorkContext {
  id: string;
  workItemId: string;
  workerRunId: string;
  attemptId: string;
  label: string;
  description: string;
  state: string;
}
export interface ReviewTaskChoice {
  id: string;
  label: string;
  description: string;
  targetDigest: string;
  signedArtifact?: ArtifactBinding;
  verifierReceiptIds: string[];
}
export interface LineageView {
  id: string;
  mode: string;
  sourceVersion: string;
  state: string;
  digest: string;
  evidenceClass: string;
  authorityEffect: string;
}
export interface GraphNode {
  claimId: string;
  stableLabel: string;
  statement: string;
  lifecycle: string;
  dependable: boolean;
  claimType: string;
  authorityAxes: Record<string, unknown>;
  contractVersion: number;
  verificationMethod: string;
}
export interface GraphEdge {
  id: string;
  from: string;
  to: string;
  direction: string;
  obligationStatus: string;
}
export interface GraphView {
  mode: "VERIFIED" | "RESEARCH_HISTORY";
  atRevision: number;
  contractVersion: number;
  nodes: GraphNode[];
  edges: GraphEdge[];
  total: number;
  truncated: boolean;
  continuationCursor?: string;
}
export interface ClaimDraft {
  statement: string;
  claimKind: string;
  predecessorFactIds: string[];
  workItemId: string;
  workerRunId: string;
  attemptId: string;
  proofArtifacts: ArtifactBinding[];
  sourceBindingArtifact: ArtifactBinding;
  supersedesClaimId?: string;
}
export interface CommandReceipt {
  receiptId: string;
  state: string;
}
export interface FeatureFailure {
  code: string;
  message: string;
  unavailable: boolean;
  action: string;
}
export function authorityLabel(claim: ClaimView): {
  tone: string;
  title: string;
  detail: string;
} {
  if (claim.semanticState === "VERIFIED" && claim.machineState === "SUCCEEDED")
    return {
      tone: "verified",
      title: "验证已接受",
      detail: "工具执行成功与数学权威晋级均有独立记录。",
    };
  if (claim.machineState === "SUCCEEDED")
    return {
      tone: "warning",
      title: "工具成功，尚未晋级",
      detail: "SUCCEEDED 只表示执行完成；等待内核接受验证事件。",
    };
  return {
    tone: "neutral",
    title: "候选 Claim",
    detail: "当前对象不可作为可依赖数学事实。",
  };
}
export function apiAction(code: string) {
  if (code === "REVOCATION_PREVIEW_STALE" || code === "STALE_QUERY")
    return "研究修订号已变化，请重新读取对象并生成预览。";
  if (
    code.includes("UNKNOWN") ||
    code.includes("UNAVAILABLE") ||
    code.includes("NOT_FOUND")
  )
    return "当前部署未发布该对象源，或当前研究中没有可选择对象。";
  return "查看动作回执与当前研究修订，修复后重新提交。";
}

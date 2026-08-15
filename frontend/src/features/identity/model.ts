export type ProductRole =
  | "VIEWER"
  | "MAIN"
  | "LITERATURE_REVIEWER"
  | "WORKER"
  | "MACHINE_VERIFIER"
  | "PEER_REVIEWER"
  | "PAPER_REVIEWER"
  | "PUBLICATION_WORKER"
  | "ADMIN";
export interface SessionView {
  sessionId: string;
  principalSubjectId: string;
  identityId: string;
  displayName: string;
  role: ProductRole;
  linkedIdentityIds: string[];
  sessionVersion: number;
  issuedAt: string;
  expiresAt: string;
  accessMode: "SHARED_READ_ONLY" | "MANAGED";
}
export interface SessionOption {
  id: string;
  label: string;
  description: string;
}
const ACTIONS: Record<ProductRole, string[]> = {
  VIEWER: [],
  MAIN: [
    "CreateResearch",
    "FreezeContract",
    "ConfirmRevoke",
    "CreateReviewTask",
    "Finalize",
  ],
  LITERATURE_REVIEWER: [
    "ReviewTheoremApplicability",
    "ReviewMaterialExtraction",
  ],
  WORKER: ["RegisterClaim", "ReviseClaim", "RunTool", "CreateComputeTask"],
  MACHINE_VERIFIER: ["RunVerification"],
  PEER_REVIEWER: [
    "ClaimReviewTask",
    "SubmitAtomicReview",
    "SubmitCompositionReview",
  ],
  PAPER_REVIEWER: ["ClaimReviewTask", "SubmitPaperReview"],
  PUBLICATION_WORKER: ["GenerateCandidateTex", "CompileFinalPdf"],
  ADMIN: [
    "DeploymentOperation",
    "BackupDeployment",
    "RestoreDeployment",
    "ReadDiagnostics",
  ],
};
export function narrowActions(role: ProductRole) {
  return ACTIONS[role];
}

const ROLE_LABELS: Record<ProductRole, string> = {
  VIEWER: "共享浏览者",
  MAIN: "数学家",
  LITERATURE_REVIEWER: "文献审查者",
  WORKER: "研究执行者",
  MACHINE_VERIFIER: "机器验证者",
  PEER_REVIEWER: "同行审查者",
  PAPER_REVIEWER: "整篇审查者",
  PUBLICATION_WORKER: "出版执行者",
  ADMIN: "管理员",
};

export function roleLabel(role: ProductRole): string {
  return ROLE_LABELS[role];
}

export function opaqueSuffix(value: string): string {
  return value.length <= 6 ? value : value.slice(-6);
}

export function identityLabel(session: SessionView): string {
  return `${roleLabel(session.role)} · ${session.displayName} · ${opaqueSuffix(session.identityId)}`;
}

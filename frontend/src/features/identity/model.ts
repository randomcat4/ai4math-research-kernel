export type ProductRole="MAIN"|"WORKER"|"REVIEWER"|"ADMIN";
export interface SessionView{sessionId:string;principalSubjectId:string;identityId:string;displayName:string;role:ProductRole;linkedIdentityIds:string[];sessionVersion:number;issuedAt:string;expiresAt:string}
const ACTIONS:Record<ProductRole,string[]>={MAIN:["CreateResearch","FreezeContract","ConfirmRevoke","CreateReviewTask","Finalize"],WORKER:["RegisterClaim","ReviseClaim","RunTool","CreateComputeTask"],REVIEWER:["ClaimReviewTask","SubmitAtomicReview","SubmitCompositionReview","SubmitPaperReview"],ADMIN:["DeploymentOperation","BackupDeployment","RestoreDeployment","ReadDiagnostics"]};
export function narrowActions(role:ProductRole){return ACTIONS[role]}

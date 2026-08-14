import type {SessionView} from "../identity/model.js";
export type ReviewType="ATOMIC"|"COMPOSITION"|"PAPER";
export interface SignedArtifactRef{artifact_id:string;sha256:string;byte_count:number;media_type:string}
export interface ReviewTask{id:string;type:ReviewType;runId:string;assigneeSubjectId:string;authorSubjectIds:string[];targetDigest:string;contractVersion:number;researchRevision:number;independenceRequired:boolean;state:string;createdAt:string;expiresAt:string;independenceStatus:string;signedArtifactRef?:SignedArtifactRef}
export type CheckDecision=null|true|false;
export interface DraftCheck{key:string;label:string;passed:CheckDecision;conclusion:string;evidenceRefs:string}
export const CHECKS:Record<ReviewType,{key:string;label:string}[]>={ATOMIC:[
 {key:"statement_correct",label:"陈述正确"},{key:"proof_valid",label:"证明有效"},{key:"dependency_scope_valid",label:"依赖范围有效"},{key:"evidence_sufficient",label:"证据充分"}],
 COMPOSITION:[{key:"proof_checked",label:"证明已检查"},{key:"scope_checked",label:"范围已检查"},{key:"coverage",label:"覆盖"},
 {key:"compatibility",label:"兼容性"},{key:"invariant",label:"不变量"},{key:"progress",label:"进展性"},{key:"boundary",label:"边界"},
 {key:"simultaneous_choice",label:"同时选择"}],
 PAPER:[{key:"statement_alignment",label:"陈述一致"},{key:"proof_completeness",label:"证明完整"},{key:"citation_accuracy",label:"引用准确"},
 {key:"novelty_boundary",label:"新颖性边界"},{key:"artifact_binding",label:"工件绑定"},{key:"outcome_alignment",label:"结果一致"}]};
export interface FeatureFailure{code:string;title:string;detail:string;action:string;unavailable:boolean}
export function canReview(session:SessionView|undefined){return session?.role==="PEER_REVIEWER"||session?.role==="PAPER_REVIEWER"}
export function canReviewTask(session:SessionView|undefined,type:ReviewType){return session?.role==="PEER_REVIEWER"?type==="ATOMIC"||type==="COMPOSITION":session?.role==="PAPER_REVIEWER"&&type==="PAPER"}
export function explainError(code:string):FeatureFailure{const messages:Record<string,[string,string,string]>={
 REVIEWER_IS_TASK_AUTHOR:["独立性拒绝","当前审查人与任务作者同源。","切换到已认证且不在作者集合中的 Reviewer 身份。"],
 AUTHOR_BINDING_MISMATCH:["作者绑定不一致","签名工件中的作者集合与任务不同。","重新读取任务并按精确 author_subject_ids 生成签名。"],
 TASK_BINDING_MISMATCH:["任务绑定不一致","run、revision、contract、target 或 digest 不匹配。","丢弃旧草稿，按当前任务绑定重新生成并签名。"],
 ASSIGNEE_BINDING_MISMATCH:["审查人绑定不一致","签名身份不是当前任务 assignee。","切换到任务指定身份并重新签名。"],
 REVIEW_AUTHORITY_INELIGIBLE:["审查权威不合格","签名密钥是 UNMANAGED_REVIEW 或 authority effect 为 NONE。","使用受管 Reviewer 密钥重新签名。"],
 REVIEWER_ROLE_REQUIRED:["需要 Reviewer 身份","Main/Worker/Admin 不能领取或提交独立审查。","在同一 session 中认证并切换到独立 Reviewer。"]};
 const [title,detail,action]=messages[code]??["审查请求被拒绝",code,"查看任务绑定与签名回执后修复；不要把拒绝当作提交成功。"];
 return{code,title,detail,action,unavailable:code.includes("UNAVAILABLE")||code.includes("UNKNOWN")}}

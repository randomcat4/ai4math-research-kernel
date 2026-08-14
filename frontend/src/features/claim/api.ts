import {ResearchProductClient,type JsonObject,type JsonValue,type ProductTransport} from "../../../../sdk/typescript/src/client.js";
import {apiAction,type ArtifactBinding,type ClaimDraft,type ClaimRevision,type ClaimView,type CommandReceipt,type FeatureFailure,type GraphEdge,type GraphNode,type GraphView,type LineageView,type RunFence,type WorkflowView} from "./model.js";
import type {RevokeConfirmation,RevokePreview} from "../revocation/model.js";

export class ClaimApiError extends Error{
 constructor(readonly status:number,readonly code:string,readonly unavailable:boolean,message=code){super(message)}
 toFailure():FeatureFailure{return{code:this.code,message:this.message,unavailable:this.unavailable,action:apiAction(this.code)}}
}
class BrowserTransport implements ProductTransport{
 constructor(private readonly baseUrl:string){}
 async request(operation:"command"|"query"|"subscribe"|"artifact",body:JsonObject):Promise<JsonObject>{
  if(operation!=="command"&&operation!=="query")throw new ClaimApiError(501,"FEATURE_ROUTE_UNAVAILABLE",true);
  const scope=obj(body.scope,"scope"),runId=str(scope.run_id,"scope.run_id");
  const suffix=operation==="command"?"commands":"queries";
  return requestJson(this.baseUrl+"/v1/research/"+encodeURIComponent(runId)+"/"+suffix,{method:"POST",body:JSON.stringify(body)});
 }
}
async function requestJson(url:string,init:RequestInit):Promise<JsonObject>{
 let response:Response;try{response=await fetch(url,{...init,credentials:"include",headers:{"content-type":"application/json",...init.headers}})}
 catch(error){throw new ClaimApiError(0,"NETWORK_UNAVAILABLE",true,String(error))}
 const value:unknown=await response.json().catch(()=>null);
 if(value===null||Array.isArray(value)||typeof value!=="object")throw new ClaimApiError(response.status,"INVALID_SERVER_ENVELOPE",false);
 const result=value as JsonObject;if(!response.ok){const code=typeof result.code==="string"?result.code:"PRODUCT_REQUEST_FAILED";
  throw new ClaimApiError(response.status,code,response.status===404||response.status===501||response.status===503||code.includes("UNKNOWN")||code.includes("UNAVAILABLE"))}
 return result;
}
export class ClaimGateway{
 private readonly client:ResearchProductClient;
 constructor(baseUrl=""){this.client=new ResearchProductClient(new BrowserTransport(baseUrl))}
 async queryClaim(runId:string,claimId:string):Promise<ClaimView>{
  const value=await this.client.query({scope:qscope(runId),type:"CLAIM",payload:{claim_id:claimId}});
  return claimView(entity(value,"CLAIM"));
 }
 async queryHistory(runId:string,claimId:string):Promise<ClaimRevision[]>{
  const value=await this.client.query({scope:qscope(runId),type:"CLAIM_HISTORY",payload:{claim_id:claimId,page:{limit:100}}});
  return items(value,"CLAIM_HISTORY").map(historyView);
 }
 async queryWorkflow(runId:string):Promise<WorkflowView>{
  const value=await this.client.query({scope:qscope(runId),type:"WORKFLOW",payload:{}});
  const projection=entity(value,"WORKFLOW"),domain=obj(projection.domain,"domain");
  return{id:str(domain.workflow_id,"workflow_id"),phase:str(domain.phase,"phase"),state:str(domain.workflow_state,"workflow_state"),activeWorkItemIds:strs(domain.active_work_item_ids,"active_work_item_ids"),digest:str(domain.workflow_digest,"workflow_digest")};
 }
 async queryLineage(runId:string,lineageId:string):Promise<LineageView>{
  const value=await this.client.query({scope:qscope(runId),type:"RESEARCH_CASE_LINEAGE",payload:{lineage_id:lineageId}});
  const projection=entity(value,"RESEARCH_CASE_LINEAGE"),domain=obj(projection.domain,"domain");
  return{id:str(domain.lineage_id,"lineage_id"),mode:str(domain.lineage_mode,"lineage_mode"),sourceVersion:str(domain.source_version,"source_version"),state:str(domain.lineage_state,"lineage_state"),digest:str(domain.lineage_digest,"lineage_digest"),evidenceClass:str(projection.evidence_class,"evidence_class"),authorityEffect:str(projection.authority_effect,"authority_effect")};
 }
 async queryGraph(runId:string,revision:number,mode:"VERIFIED"|"RESEARCH_HISTORY",seedIds:string[],cursor?:string):Promise<GraphView>{
  const value=await this.client.query({scope:qscope(runId),type:"GRAPH_SLICE",payload:{at_revision:revision,depth:4,direction:"BOTH",filters:{},mode,node_limit:120,seed_ids:seedIds,...(cursor?{continuation_cursor:cursor}:{})}});
  return graphView(obj(value.result,"GRAPH_SLICE.result"));
 }
 async submitClaim(fence:RunFence,requestId:string,draft:ClaimDraft):Promise<CommandReceipt>{
  const value=await this.client.command({request_id:requestId,scope:cscope(fence),type:"SUBMIT_CLAIM",payload:{
   statement:draft.statement,claim_kind:draft.claimKind,predecessor_fact_ids:draft.predecessorFactIds,
   work_item_id:draft.workItemId,worker_run_id:draft.workerRunId,attempt_id:draft.attemptId,
   proof_or_evidence_artifacts:draft.proofArtifacts as unknown as JsonObject[],
   source_binding_artifact:draft.sourceBindingArtifact as unknown as JsonObject,
   ...(draft.supersedesClaimId?{supersedes_claim_id:draft.supersedesClaimId}:{})
  }});return receipt(value);
 }
 async importVerification(fence:RunFence,requestId:string,input:{reviewTaskId:string;signedReviewArtifact:ArtifactBinding;targetDigest:string;verifierReceiptIds:string[]}):Promise<CommandReceipt>{
  const value=await this.client.command({request_id:requestId,scope:cscope(fence),type:"IMPORT_VERIFICATION",payload:{
   review_task_id:input.reviewTaskId,signed_review_artifact:input.signedReviewArtifact as unknown as JsonObject,
   target_digest:input.targetDigest,verifier_receipt_ids:input.verifierReceiptIds
  }});return receipt(value);
 }
 async queryRevokePreview(fence:RunFence,claimId:string,targetDigest:string):Promise<RevokePreview>{
  const value=await this.client.query({scope:qscope(fence.runId),type:"REVOKE_PREVIEW",payload:{at_revision:fence.revision,claim_id:claimId,target_digest:targetDigest}});
  const projection=entity(value,"REVOKE_PREVIEW"),domain=obj(projection.domain,"domain");
  return{id:str(domain.revoke_preview_id,"revoke_preview_id"),targetClaimId:str(domain.target_claim_id,"target_claim_id"),targetDigest:str(domain.target_digest,"target_digest"),closureDigest:str(domain.closure_digest,"closure_digest"),affectedClaimIds:strs(domain.affected_claim_ids,"affected_claim_ids"),previewRevision:fence.revision};
 }
 async confirmRevoke(fence:RunFence,requestId:string,preview:RevokePreview,input:RevokeConfirmation):Promise<CommandReceipt>{
  const value=await this.client.command({request_id:requestId,scope:cscope(fence),type:"CONFIRM_REVOKE",payload:{
   fact_id:preview.targetClaimId,target_fact_digest:preview.targetDigest,preview_revision:preview.previewRevision,
   contract_version:fence.contractVersion,affected_fact_ids:input.affectedFactIds,
   preserved_sibling_ids:input.preservedSiblingIds,reopened_obligation_ids:input.reopenedObligationIds,
   reason_artifact:input.reasonArtifact as unknown as JsonObject
  }});return receipt(value);
 }
}
function qscope(runId:string){return{kind:"RUN" as const,run_id:runId}}
function cscope(fence:RunFence){return{kind:"RUN" as const,run_id:fence.runId,expected_revision:fence.revision,expected_contract_version:fence.contractVersion}}
function receipt(value:JsonObject):CommandReceipt{return{receiptId:str(value.receipt_id,"receipt_id"),state:str(value.state,"state")}}
function entity(value:JsonObject,label:string){const result=obj(value.result,label+".result");return obj(result.entity,label+".entity")}
function items(value:JsonObject,label:string){const result=obj(value.result,label+".result");if(!Array.isArray(result.items))throw new ClaimApiError(200,"INVALID_SERVER_ENVELOPE",false);return result.items.map((item,index)=>obj(item,label+"["+index+"]"))}
function claimView(projection:JsonObject):ClaimView{const domain=obj(projection.domain,"domain");return{id:str(domain.claim_id,"claim_id"),stableLabel:str(domain.stable_label,"stable_label"),lifecycle:str(domain.lifecycle,"lifecycle"),machineState:str(domain.machine_state,"machine_state"),semanticState:str(domain.semantic_state,"semantic_state"),statementDigest:str(domain.statement_digest,"statement_digest"),artifactIds:strs(projection.artifact_ids,"artifact_ids")}}
function historyView(projection:JsonObject):ClaimRevision{const domain=obj(projection.domain,"domain");return{id:str(domain.claim_id,"claim_id"),revision:num(domain.claim_revision,"claim_revision"),lifecycle:str(domain.lifecycle,"lifecycle"),statementDigest:str(domain.statement_digest,"statement_digest"),supersedesClaimId:opt(domain.supersedes_claim_id)}}
function graphView(value:JsonObject):GraphView{const rawNodes=value.nodes,rawEdges=value.edges;if(!Array.isArray(rawNodes)||!Array.isArray(rawEdges))throw new ClaimApiError(200,"INVALID_GRAPH_SLICE",false);
 const nodes:GraphNode[]=rawNodes.map((raw,index)=>{const node=obj(raw,"node["+index+"]");return{claimId:str(node.claim_id,"claim_id"),stableLabel:str(node.stable_label,"stable_label"),statement:str(node.statement,"statement"),lifecycle:str(node.lifecycle,"lifecycle"),dependable:bool(node.dependable,"dependable"),claimType:str(node.claim_type,"claim_type"),authorityAxes:obj(node.authority_axes,"authority_axes"),contractVersion:num(node.contract_version,"contract_version"),verificationMethod:str(node.verification_method,"verification_method")}});
 const edges:GraphEdge[]=rawEdges.map((raw,index)=>{const edge=obj(raw,"edge["+index+"]");return{id:str(edge.edge_id,"edge_id"),from:str(edge.from_claim_id,"from_claim_id"),to:str(edge.to_claim_id,"to_claim_id"),direction:str(edge.logical_direction,"logical_direction"),obligationStatus:str(edge.obligation_status,"obligation_status")}});
 const mode=str(value.mode,"mode");if(mode!=="VERIFIED"&&mode!=="RESEARCH_HISTORY")throw new ClaimApiError(200,"UNKNOWN_GRAPH_MODE",false);
 return{mode,atRevision:num(value.at_revision,"at_revision"),contractVersion:num(value.contract_version,"contract_version"),nodes,edges,total:num(value.total_matches,"total_matches"),truncated:bool(value.truncated,"truncated"),continuationCursor:opt(value.continuation_cursor)}}
function obj(value:JsonValue|undefined,path:string):JsonObject{if(value===null||value===undefined||Array.isArray(value)||typeof value!=="object")throw new ClaimApiError(200,"INVALID_SERVER_ENVELOPE",false,path);return value}
function str(value:JsonValue|undefined,path:string){if(typeof value!=="string"||!value)throw new ClaimApiError(200,"INVALID_SERVER_ENVELOPE",false,path);return value}
function opt(value:JsonValue|undefined){return typeof value==="string"?value:""}
function num(value:JsonValue|undefined,path:string){if(typeof value!=="number"||!Number.isSafeInteger(value))throw new ClaimApiError(200,"INVALID_SERVER_ENVELOPE",false,path);return value}
function bool(value:JsonValue|undefined,path:string){if(typeof value!=="boolean")throw new ClaimApiError(200,"INVALID_SERVER_ENVELOPE",false,path);return value}
function strs(value:JsonValue|undefined,path:string){if(!Array.isArray(value)||value.some((item)=>typeof item!=="string"))throw new ClaimApiError(200,"INVALID_SERVER_ENVELOPE",false,path);return value as string[]}

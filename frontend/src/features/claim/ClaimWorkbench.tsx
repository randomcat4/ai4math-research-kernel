import {type FormEvent,useMemo,useState} from "react";
import {ClaimApiError,ClaimGateway} from "./api.js";
import {authorityLabel,type ArtifactBinding,type ClaimRevision,type ClaimView,type CommandReceipt,type FeatureFailure,type GraphView,type LineageView,type RunFence,type WorkflowView} from "./model.js";
import "./claim.css";

export interface ClaimWorkbenchProps{run:RunFence;baseUrl?:string}
export function ClaimWorkbench({run,baseUrl=""}:ClaimWorkbenchProps){
 const gateway=useMemo(()=>new ClaimGateway(baseUrl),[baseUrl]);
 const [claimId,setClaimId]=useState(""),[lineageId,setLineageId]=useState("");
 const [claim,setClaim]=useState<ClaimView>(),[history,setHistory]=useState<ClaimRevision[]>([]);
 const [workflow,setWorkflow]=useState<WorkflowView>(),[lineage,setLineage]=useState<LineageView>();
 const [graph,setGraph]=useState<GraphView>(),[mode,setMode]=useState<"VERIFIED"|"RESEARCH_HISTORY">("VERIFIED");
 const [failure,setFailure]=useState<FeatureFailure>(),[receipt,setReceipt]=useState<CommandReceipt>(),[busy,setBusy]=useState(false);
 async function act(action:()=>Promise<void>){setBusy(true);setFailure(undefined);try{await action()}catch(error){setFailure(toFailure(error))}finally{setBusy(false)}}
 async function load(){await act(async()=>{if(!claimId)throw new ClaimApiError(0,"CLAIM_ID_REQUIRED",false);
  const [nextClaim,nextHistory,nextWorkflow,nextGraph]=await Promise.all([
   gateway.queryClaim(run.runId,claimId),gateway.queryHistory(run.runId,claimId),gateway.queryWorkflow(run.runId),
   gateway.queryGraph(run.runId,run.revision,mode,[claimId])
  ]);setClaim(nextClaim);setHistory(nextHistory);setWorkflow(nextWorkflow);setGraph(nextGraph);
  if(lineageId)setLineage(await gateway.queryLineage(run.runId,lineageId));})}
 async function switchGraph(next:"VERIFIED"|"RESEARCH_HISTORY"){setMode(next);if(claimId)await act(async()=>setGraph(await gateway.queryGraph(run.runId,run.revision,next,[claimId])))}
 const authority=claim?authorityLabel(claim):undefined;
 return <main className="claim-shell" aria-busy={busy}>
  <header className="claim-hero"><div><p>CLAIM CONTROL / 验证控制台</p><h1>Claim 检查、修复与谱系</h1>
   <span>候选、工具执行、验证接受与权威晋级严格分栏。</span></div>
   <div className="claim-fence"><b>r{run.revision}</b><small>contract {run.contractVersion}</small></div></header>
  {failure&&<Failure failure={failure}/>} {receipt&&<div className="claim-receipt"><b>{receipt.state}</b><code>{receipt.receiptId}</code></div>}
  <section className="claim-panel claim-loader"><Title n="01" title="打开 Claim" note="真实稳定 ID 与 revision fence"/>
   <div className="claim-input-row"><label>Claim ID<input value={claimId} onChange={(e)=>setClaimId(e.target.value)}/></label>
    <label>Lineage ID<input value={lineageId} onChange={(e)=>setLineageId(e.target.value)}/></label>
    <button onClick={load} disabled={busy}>读取当前状态</button></div></section>
  <section className="claim-grid claim-grid--top">
   <article className="claim-panel"><Title n="02" title="原子 Claim" note="机器状态 ≠ 数学权威"/>
    {claim?<><div className={"claim-authority is-"+authority?.tone}><b>{authority?.title}</b><span>{authority?.detail}</span></div>
     <Rows rows={[["Stable label",claim.stableLabel],["Lifecycle",claim.lifecycle],["Machine",claim.machineState],["Semantic",claim.semanticState],["Statement digest",claim.statementDigest]]}/>
    </>:<Empty text="输入 Claim ID 后读取。研究稿自动原子化尚无公开 Query variant，不显示模型臆测预览。"/>}</article>
   <article className="claim-panel"><Title n="03" title="研究稿原子化" note="前驱 / 类型 / 未定义符号"/>
    <Unavailable text="当前 C00 未发布 RESEARCH_DRAFT_PREVIEW query variant。候选原子 Claim、前驱、类型、未定义符号与 verifier plan 必须由后端 B10b 产出后再展示；本界面不会本地解析论文冒充结果。"/></article>
   <article className="claim-panel"><Title n="04" title="义务 readiness" note="只读 Workflow + edge receipts"/>
    {workflow?<Rows rows={[["Phase",workflow.phase],["Workflow",workflow.state],["Active work",workflow.activeWorkItemIds.join(", ")],["Digest",workflow.digest]]}/>:<Empty text="读取 Workflow 后显示。"/>}
    {graph&&<div className="claim-obligations">{graph.edges.map((edge)=><span key={edge.id} className={"is-"+edge.obligationStatus.toLowerCase()}>{edge.obligationStatus}</span>)}</div>}</article>
  </section>
  <section className="claim-grid claim-grid--main">
   <article className="claim-panel claim-graph"><Title n="05" title="有效图 / 研究谱系" note="两图绝不混合"/>
    <div className="claim-tabs"><button className={mode==="VERIFIED"?"active":""} onClick={()=>switchGraph("VERIFIED")}>有效依赖图</button>
     <button className={mode==="RESEARCH_HISTORY"?"active":""} onClick={()=>switchGraph("RESEARCH_HISTORY")}>研究历史图</button></div>
    {graph?<><div className="claim-metrics"><b>{graph.nodes.length}<small>nodes</small></b><b>{graph.edges.length}<small>edges</small></b><b>{graph.atRevision}<small>revision</small></b></div>
     <div className="claim-node-list">{graph.nodes.map((node)=><div key={node.claimId}><header><b>{node.stableLabel}</b><span>{node.claimType}</span></header>
      <p>{node.statement}</p><footer><span>{node.verificationMethod}</span><strong>{node.dependable?"DEPENDABLE":"NOT DEPENDABLE"}</strong></footer></div>)}</div>
     {graph.truncated&&<div className="claim-stale">图被 node_limit 截断；使用服务返回 opaque cursor 继续，不能猜测游标。</div>}</>:<Empty text="选择 Claim 后按固定 revision 查询 GraphSlice。"/>}</article>
   <aside className="claim-stack">
    <article className="claim-panel"><Title n="06" title="Claim 历史" note="拒绝 → 修复 → supersede"/>
     {history.length?<ol className="claim-history">{history.map((item)=><li key={item.id+item.revision}><b>r{item.revision}</b><span>{item.lifecycle}</span><code>{item.statementDigest}</code>{item.supersedesClaimId&&<small>supersedes {item.supersedesClaimId}</small>}</li>)}</ol>:<Empty text="暂无已发布历史。"/>}</article>
    <article className="claim-panel"><Title n="07" title="科研谱系" note="当前 run 的来源模式"/>
     {lineage?<Rows rows={[["Mode",lineage.mode],["Source version",lineage.sourceVersion],["State",lineage.state],["Evidence",lineage.evidenceClass],["Authority",lineage.authorityEffect],["Digest",lineage.digest]]}/>:<Empty text="输入 lineage_id；谱系来源不自动晋级 Claim。"/>}</article>
   </aside>
  </section>
  <section className="claim-grid claim-grid--forms">
   <SubmitClaimPanel run={run} gateway={gateway} busy={busy} onFailure={setFailure} onReceipt={setReceipt}/>
   <VerificationPanel run={run} gateway={gateway} busy={busy} onFailure={setFailure} onReceipt={setReceipt}/>
  </section>
 </main>
}
function SubmitClaimPanel({run,gateway,busy,onFailure,onReceipt}:PanelProps){
 const [statement,setStatement]=useState(""),[kind,setKind]=useState("THEOREM"),[predecessors,setPredecessors]=useState("");
 const [workItem,setWorkItem]=useState(""),[workerRun,setWorkerRun]=useState(""),[attempt,setAttempt]=useState("");
 const [source,setSource]=useState(""),[proofs,setProofs]=useState(""),[supersedes,setSupersedes]=useState("");
 async function submit(e:FormEvent){e.preventDefault();try{onReceipt(await gateway.submitClaim(run,crypto.randomUUID(),{
  statement,claimKind:kind,predecessorFactIds:csv(predecessors),workItemId:workItem,workerRunId:workerRun,attemptId:attempt,
  sourceBindingArtifact:binding(source),proofArtifacts:csv(proofs).map(binding),...(supersedes?{supersedesClaimId:supersedes}:{})
 }))}catch(error){onFailure(toFailure(error))}}
 return <form className="claim-panel" onSubmit={submit}><Title n="08" title="提交修复 Claim" note="正式 SUBMIT_CLAIM 路由"/>
  <label>Statement<textarea required value={statement} onChange={(e)=>setStatement(e.target.value)}/></label>
  <div className="claim-pair"><label>Claim type<input required value={kind} onChange={(e)=>setKind(e.target.value)}/></label>
   <label>Predecessor fact IDs<input value={predecessors} onChange={(e)=>setPredecessors(e.target.value)}/></label></div>
  <div className="claim-pair"><label>Work item ID<input required value={workItem} onChange={(e)=>setWorkItem(e.target.value)}/></label>
   <label>Worker run ID<input required value={workerRun} onChange={(e)=>setWorkerRun(e.target.value)}/></label></div>
  <label>Attempt ID<input required value={attempt} onChange={(e)=>setAttempt(e.target.value)}/></label>
  <label>Source binding artifact_id:sha256<input required value={source} onChange={(e)=>setSource(e.target.value)}/></label>
  <label>Proof artifacts（逗号分隔 id:sha256）<input required value={proofs} onChange={(e)=>setProofs(e.target.value)}/></label>
  <label>Supersedes rejected Claim ID<input value={supersedes} onChange={(e)=>setSupersedes(e.target.value)}/></label>
  <button disabled={busy}>提交候选并进入实际 verifier 路由</button></form>
}
function VerificationPanel({run,gateway,busy,onFailure,onReceipt}:PanelProps){
 const [task,setTask]=useState(""),[artifact,setArtifact]=useState(""),[digest,setDigest]=useState(""),[receipts,setReceipts]=useState("");
 async function submit(e:FormEvent){e.preventDefault();try{onReceipt(await gateway.importVerification(run,crypto.randomUUID(),{
  reviewTaskId:task,signedReviewArtifact:binding(artifact),targetDigest:digest,verifierReceiptIds:csv(receipts)
 }))}catch(error){onFailure(toFailure(error))}}
 return <form className="claim-panel" onSubmit={submit}><Title n="09" title="导入验证回执" note="IMPORT_VERIFICATION · kernel gate"/>
  <label>Review task ID<input required value={task} onChange={(e)=>setTask(e.target.value)}/></label>
  <label>Signed review artifact_id:sha256<input required value={artifact} onChange={(e)=>setArtifact(e.target.value)}/></label>
  <label>Target digest<input required value={digest} onChange={(e)=>setDigest(e.target.value)}/></label>
  <label>Verifier receipt IDs<input required value={receipts} onChange={(e)=>setReceipts(e.target.value)}/></label>
  <p className="claim-callout">工具 SUCCEEDED 不在这里自动变成 VALIDATION_ACCEPTED；只有内核接受事件才改变 semantic authority。</p>
  <button disabled={busy}>提交签名验证</button></form>
}
interface PanelProps{run:RunFence;gateway:ClaimGateway;busy:boolean;onFailure:(v:FeatureFailure)=>void;onReceipt:(v:CommandReceipt)=>void}
function Title({n,title,note}:{n:string;title:string;note:string}){return <header className="claim-title"><span>{n}</span><div><h2>{title}</h2><p>{note}</p></div></header>}
function Rows({rows}:{rows:[string,string][]}){return <dl className="claim-rows">{rows.map(([k,v])=><div key={k}><dt>{k}</dt><dd>{v||"—"}</dd></div>)}</dl>}
function Empty({text}:{text:string}){return <p className="claim-empty">{text}</p>}
function Unavailable({text}:{text:string}){return <div className="claim-unavailable"><b>未发布 variant</b><p>{text}</p></div>}
function Failure({failure}:{failure:FeatureFailure}){return <div className="claim-failure" role="alert"><b>{failure.unavailable?"能力不可用":"请求失败"}</b><code>{failure.code}</code><p>{failure.action}</p></div>}
function csv(value:string){return value.split(",").map((v)=>v.trim()).filter(Boolean)}
function binding(value:string):ArtifactBinding{const [artifact_id,sha256,...rest]=value.split(":");if(!artifact_id||!sha256||rest.length||sha256.length!==64)throw new ClaimApiError(0,"INVALID_ARTIFACT_BINDING",false);return{artifact_id,sha256}}
function toFailure(error:unknown):FeatureFailure{return error instanceof ClaimApiError?error.toFailure():{code:"CLIENT_FAILURE",message:String(error),unavailable:false,action:"检查输入，不要把客户端失败当作验证结果。"}}

import {type FormEvent,useEffect,useMemo,useState} from "react";
import type {SessionView} from "../identity/model.js";
import {ReviewApiError,ReviewGateway} from "./api.js";
import {CHECKS,canReview,explainError,type DraftCheck,type FeatureFailure,type ReviewTask,type ReviewType,type SignedArtifactRef} from "./model.js";
import "./review.css";
export interface ReviewWorkbenchProps{session?:SessionView;baseUrl?:string}
export function ReviewWorkbench({session,baseUrl=""}:ReviewWorkbenchProps){
 const gateway=useMemo(()=>new ReviewGateway(baseUrl),[baseUrl]);const [tasks,setTasks]=useState<ReviewTask[]>([]);
 const [selected,setSelected]=useState<ReviewTask>(),[failure,setFailure]=useState<FeatureFailure>(),[busy,setBusy]=useState(false);
 useEffect(()=>{if(canReview(session))void load()},[session]);
 async function act<T>(fn:()=>Promise<T>){setBusy(true);setFailure(undefined);try{return await fn()}catch(error){setFailure(error instanceof ReviewApiError?error.toFailure():explainError(String(error)));return undefined}finally{setBusy(false)}}
 async function load(){const result=await act(()=>gateway.inbox());if(result)setTasks(result)}
 async function claim(task:ReviewTask){const result=await act(()=>gateway.claim(task.id));if(result){setSelected(result);setTasks((all)=>all.map((item)=>item.id===result.id?result:item))}}
 if(session?.role!=="REVIEWER")return <main className="review-shell"><header className="review-hero"><p>INDEPENDENT REVIEW</p><h1>独立审查台</h1></header>
  <section className="review-denied"><b>当前 principal：{session?.role??"NO SESSION"}</b><p>Main、Worker 与 Admin 没有构造、领取或提交签名审查的入口。</p>
   <span>请在同一 session 中认证并切换到独立 Reviewer 身份。</span></section></main>;
 return <main className="review-shell"><header className="review-hero"><div><p>INDEPENDENT REVIEW</p><h1>审查收件箱与签名提交</h1>
  <span>当前 Reviewer：{session?.principalSubjectId}</span></div><button onClick={load} disabled={busy}>刷新真实收件箱</button></header>
  {failure&&<Failure failure={failure}/>}
  <section className="review-grid"><aside className="review-panel"><Title n="01" title="Review inbox" note="只显示任务绑定，不显示 verdict"/>
   <div className="review-inbox">{tasks.length?tasks.map((task)=><button className={selected?.id===task.id?"active":""} key={task.id} onClick={()=>setSelected(task)}>
    <header><b>{task.type}</b><span>{task.state}</span></header><code>{task.id}</code><small>expires {task.expiresAt}</small></button>):<p>当前 Reviewer 没有任务。</p>}</div></aside>
   <section className="review-main">{selected?<><TaskBinding task={selected}/>
    {selected.state==="OPEN"&&<button className="review-claim" onClick={()=>claim(selected)} disabled={busy}>以当前 Reviewer 领取</button>}
    {selected.state==="CLAIMED"&&<DraftEditor task={selected} session={session} gateway={gateway} onFailure={setFailure} onSubmitted={(task)=>{setSelected(task);setTasks((all)=>all.map((item)=>item.id===task.id?task:item))}}/>}
    {selected.state==="SUBMITTED"&&<div className="review-complete"><b>已由 B05b 验签提交</b><code>{selected.signedArtifactRef?.artifact_id}</code></div>}
   </>:<section className="review-panel"><Title n="02" title="选择任务" note="所有检查默认未选择"/><p>从收件箱选择一个任务后查看精确 binding。</p></section>}</section>
  </section></main>
}
function TaskBinding({task}:{task:ReviewTask}){return <section className="review-panel"><Title n="02" title={task.type+" binding"} note="错 binding 与作者同源均由服务拒绝"/>
 <dl className="review-rows"><div><dt>Run</dt><dd>{task.runId}</dd></div><div><dt>Target digest</dt><dd>{task.targetDigest}</dd></div>
  <div><dt>Fence</dt><dd>r{task.researchRevision} · contract {task.contractVersion}</dd></div><div><dt>Assignee</dt><dd>{task.assigneeSubjectId}</dd></div>
  <div><dt>Authors</dt><dd>{task.authorSubjectIds.join(", ")}</dd></div><div><dt>Independence</dt><dd>{task.independenceStatus}</dd></div></dl></section>}
function DraftEditor({task,session,gateway,onFailure,onSubmitted}:{task:ReviewTask;session:SessionView;gateway:ReviewGateway;onFailure:(v:FeatureFailure)=>void;onSubmitted:(v:ReviewTask)=>void}){
 const [checks,setChecks]=useState<DraftCheck[]>(()=>CHECKS[task.type].map((item)=>({...item,passed:null,conclusion:"",evidenceRefs:""})));
 const [verdict,setVerdict]=useState(""),[targetId,setTargetId]=useState(""),[artifactId,setArtifactId]=useState(""),[sha,setSha]=useState("");
 const [bytes,setBytes]=useState(""),[media,setMedia]=useState("application/json");
 function update(index:number,patch:Partial<DraftCheck>){setChecks((current)=>current.map((item,i)=>i===index?{...item,...patch}:item))}
 const draft={schema_version:"rk.product.review.draft.v1",unsigned:true,review_type:task.type,review_task_id:task.id,
  reviewer_subject_id:session.principalSubjectId,binding:{run_id:task.runId,kernel_revision:task.researchRevision,contract_version:task.contractVersion,target_id:targetId,target_digest:task.targetDigest},
  verdict:verdict||null,checks:Object.fromEntries(checks.map((check)=>[check.key,{passed:check.passed,status:check.passed===null?"UNSELECTED":"HUMAN_ATTESTED",conclusion:check.conclusion,evidence_refs:csv(check.evidenceRefs)}]))};
 function download(){const blob=new Blob([JSON.stringify(draft,null,2)],{type:"application/json"});const anchor=document.createElement("a");anchor.href=URL.createObjectURL(blob);anchor.download="review-draft-"+task.id+".json";anchor.click();URL.revokeObjectURL(anchor.href)}
 async function submit(e:FormEvent){e.preventDefault();try{const ref:SignedArtifactRef={artifact_id:artifactId,sha256:sha,byte_count:Number(bytes),media_type:media};
  onSubmitted(await gateway.submit(task.id,ref))}catch(error){onFailure(error instanceof ReviewApiError?error.toFailure():explainError(String(error)))}}
 return <><section className="review-panel"><Title n="03" title="未签审查草稿" note="null ≠ false；无默认 true"/>
  <div className="review-draft-meta"><label>Target ID<input required value={targetId} onChange={(e)=>setTargetId(e.target.value)}/></label>
   <label>Verdict<select value={verdict} onChange={(e)=>setVerdict(e.target.value)}><option value="">未选择</option><option>ACCEPT</option><option>REJECT</option><option>NEEDS_REVISION</option></select></label></div>
  <div className="review-checks">{checks.map((check,index)=><article key={check.key}><header><b>{check.label}</b>
   <select value={check.passed===null?"":String(check.passed)} onChange={(e)=>update(index,{passed:e.target.value===""?null:e.target.value==="true"})}>
    <option value="">未选择</option><option value="true">通过</option><option value="false">不通过</option></select></header>
   <label>结论<textarea value={check.conclusion} onChange={(e)=>update(index,{conclusion:e.target.value})}/></label>
   <label>证据 refs<input value={check.evidenceRefs} onChange={(e)=>update(index,{evidenceRefs:e.target.value})}/></label></article>)}</div>
  <button onClick={download} disabled={checks.some((check)=>check.passed===null)}>下载未签草稿 JSON</button>
  <pre>{JSON.stringify(draft,null,2)}</pre><p className="review-warning">下载不等于签名或提交。签名必须由受管 Reviewer 密钥在外部流程完成；UNMANAGED_REVIEW / NONE 会被拒绝。</p></section>
  <form className="review-panel" onSubmit={submit}><Title n="04" title="提交签名 ArtifactRef" note="正文不接收 verdict / checks"/>
   <label>Artifact ID<input required value={artifactId} onChange={(e)=>setArtifactId(e.target.value)}/></label>
   <label>SHA-256<input required minLength={64} maxLength={64} value={sha} onChange={(e)=>setSha(e.target.value)}/></label>
   <div className="review-draft-meta"><label>Byte count<input required type="number" min="0" value={bytes} onChange={(e)=>setBytes(e.target.value)}/></label>
    <label>Media type<input required value={media} onChange={(e)=>setMedia(e.target.value)}/></label></div>
   <button>提交到 B05b 验签门</button></form></>
}
function Title({n,title,note}:{n:string;title:string;note:string}){return <header className="review-title"><span>{n}</span><div><h2>{title}</h2><p>{note}</p></div></header>}
function Failure({failure}:{failure:FeatureFailure}){return <div className="review-failure" role="alert"><b>{failure.title}</b><code>{failure.code}</code><span>{failure.detail}</span><p>{failure.action}</p></div>}
function csv(value:string){return value.split(",").map((item)=>item.trim()).filter(Boolean)}

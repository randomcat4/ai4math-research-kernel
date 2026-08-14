import {type FormEvent,useMemo,useState} from "react";
import {ClaimApiError,ClaimGateway} from "../claim/api.js";
import type {CommandReceipt,FeatureFailure,RunFence} from "../claim/model.js";
import type {RevokePreview} from "./model.js";
import "./revocation.css";

export interface RevocationWorkbenchProps{run:RunFence;baseUrl?:string}
export function RevocationWorkbench({run,baseUrl=""}:RevocationWorkbenchProps){
 const gateway=useMemo(()=>new ClaimGateway(baseUrl),[baseUrl]);
 const [claimId,setClaimId]=useState(""),[digest,setDigest]=useState(""),[preview,setPreview]=useState<RevokePreview>();
 const [preserved,setPreserved]=useState(""),[reopened,setReopened]=useState(""),[reason,setReason]=useState("");
 const [failure,setFailure]=useState<FeatureFailure>(),[receipt,setReceipt]=useState<CommandReceipt>(),[busy,setBusy]=useState(false);
 async function act(action:()=>Promise<void>){setBusy(true);setFailure(undefined);try{await action()}catch(error){setFailure(toFailure(error))}finally{setBusy(false)}}
 async function inspect(e:FormEvent){e.preventDefault();await act(async()=>setPreview(await gateway.queryRevokePreview(run,claimId,digest)))}
 async function confirm(){if(!preview)return;await act(async()=>{
  if(preview.previewRevision!==run.revision)throw new ClaimApiError(409,"REVOCATION_PREVIEW_STALE",false);
  setReceipt(await gateway.confirmRevoke(run,crypto.randomUUID(),preview,{
   affectedFactIds:preview.affectedClaimIds,preservedSiblingIds:csv(preserved),reopenedObligationIds:csv(reopened),reasonArtifact:binding(reason)
  }));})}
 return <main className="revoke-shell">
  <header className="revoke-hero"><div><p>REVOCATION CONTROL</p><h1>撤销影响预览与恢复</h1>
   <span>先绑定 target digest 与 revision，确认后只失效受影响闭包；无关 sibling 明确保留。</span></div>
   <div><b>r{run.revision}</b><small>contract {run.contractVersion}</small></div></header>
  {failure&&<div className="revoke-failure" role="alert"><b>{failure.code}</b><span>{failure.action}</span>
   {(failure.code==="REVOCATION_PREVIEW_STALE"||failure.code==="STALE_QUERY")&&<button onClick={()=>setPreview(undefined)}>清除旧预览并重做</button>}</div>}
  {receipt&&<div className="revoke-receipt"><b>{receipt.state}</b><code>{receipt.receiptId}</code></div>}
  <section className="revoke-grid">
   <form className="revoke-panel" onSubmit={inspect}><Title n="01" title="绑定撤销目标" note="REVOKE_PREVIEW query"/>
    <label>Claim ID<input required value={claimId} onChange={(e)=>setClaimId(e.target.value)}/></label>
    <label>Target statement digest<input required value={digest} onChange={(e)=>setDigest(e.target.value)}/></label>
    <button disabled={busy}>生成 revision {run.revision} 影响预览</button>
    <p className="revoke-note">目标摘要或 revision 任一变化，旧预览都必须作废。</p></form>
   <article className="revoke-panel"><Title n="02" title="影响闭包" note="preview digest / affected facts"/>
    {preview?<><dl className="revoke-rows"><div><dt>Preview</dt><dd>{preview.id}</dd></div>
      <div><dt>Closure digest</dt><dd>{preview.closureDigest}</dd></div><div><dt>Revision</dt><dd>{preview.previewRevision}</dd></div></dl>
      <ol className="revoke-affected">{preview.affectedClaimIds.map((id)=><li key={id}><span>INVALIDATE</span><code>{id}</code></li>)}</ol>
     </>:<Empty text="先查询真实 RevokePreview；界面不会本地计算依赖闭包。"/>}</article>
   <article className="revoke-panel"><Title n="03" title="Sibling 保留" note="无关事实不可误伤"/>
    <label>Preserved sibling IDs<input value={preserved} onChange={(e)=>setPreserved(e.target.value)} placeholder="逗号分隔"/></label>
    <p className="revoke-safe">这些 ID 会进入 CONFIRM_REVOKE 的 preserved_sibling_ids，由后端再次核对。</p>
    <label>Reopened obligation IDs<input value={reopened} onChange={(e)=>setReopened(e.target.value)} /></label>
    <label>Reason artifact_id:sha256<input required value={reason} onChange={(e)=>setReason(e.target.value)} /></label>
    <button disabled={busy||!preview} onClick={confirm}>按预览确认撤销</button></article>
  </section>
  <section className="revoke-panel revoke-recovery"><Title n="04" title="重新证明与恢复" note="新 Claim，不复活旧事实"/>
   <div className="revoke-flow"><div><b>1</b><span>受影响 Claim 标记 INVALIDATED</span></div><i>→</i>
    <div><b>2</b><span>义务重新打开</span></div><i>→</i><div><b>3</b><span>在 Claim 工作台提交 supersedes 修复</span></div><i>→</i>
    <div><b>4</b><span>新验证接受后恢复可依赖图</span></div></div>
   <p>恢复必须经过新的 SUBMIT_CLAIM 与 IMPORT_VERIFICATION。工具重跑成功不能直接复活已撤销事实。</p>
  </section>
 </main>
}
function Title({n,title,note}:{n:string;title:string;note:string}){return <header className="revoke-title"><span>{n}</span><div><h2>{title}</h2><p>{note}</p></div></header>}
function Empty({text}:{text:string}){return <p className="revoke-empty">{text}</p>}
function csv(v:string){return v.split(",").map((x)=>x.trim()).filter(Boolean)}
function binding(value:string){const [artifact_id,sha256,...rest]=value.split(":");if(!artifact_id||!sha256||rest.length||sha256.length!==64)throw new ClaimApiError(0,"INVALID_ARTIFACT_BINDING",false);return{artifact_id,sha256}}
function toFailure(error:unknown):FeatureFailure{return error instanceof ClaimApiError?error.toFailure():{code:"CLIENT_FAILURE",message:String(error),unavailable:false,action:"检查输入并重新预览。"}}

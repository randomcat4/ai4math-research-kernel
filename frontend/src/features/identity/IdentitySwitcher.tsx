import {type FormEvent,useEffect,useMemo,useState} from "react";
import {IdentityApiError,IdentityGateway} from "./api.js";
import {narrowActions,type SessionView} from "./model.js";
import "./identity.css";
export interface IdentitySwitcherProps{baseUrl?:string;onSessionChange?:(session:SessionView|undefined)=>void}
export function IdentitySwitcher({baseUrl="",onSessionChange}:IdentitySwitcherProps){
 const gateway=useMemo(()=>new IdentityGateway(baseUrl),[baseUrl]);const [session,setSession]=useState<SessionView>();
 const [identity,setIdentity]=useState(""),[secret,setSecret]=useState(""),[error,setError]=useState(""),[busy,setBusy]=useState(false);
 useEffect(()=>{gateway.me().then(update).catch(()=>undefined)},[gateway]);
 function update(value:SessionView|undefined){setSession(value);onSessionChange?.(value)}
 async function login(e:FormEvent){e.preventDefault();setBusy(true);setError("");try{update(await gateway.login(identity,secret));setSecret("")}catch(err){setError(err instanceof IdentityApiError?err.code:String(err))}finally{setBusy(false)}}
 async function switchTo(id:string){setBusy(true);setError("");try{update(await gateway.switchIdentity(id))}catch(err){setError(err instanceof IdentityApiError?err.code:String(err))}finally{setBusy(false)}}
 async function logout(){setBusy(true);try{await gateway.logout();update(undefined)}catch(err){setError(err instanceof IdentityApiError?err.code:String(err))}finally{setBusy(false)}}
 return <section className="identity-panel">
  <header><div><p>SESSION PRINCIPAL</p><h2>同一会话的独立身份</h2></div>{session&&<span className={"identity-role is-"+session.role.toLowerCase()}>{session.role}</span>}</header>
  {error&&<div className="identity-error" role="alert">{error}</div>}
  {session?<><div className="identity-current"><strong>{session.displayName}</strong><code>{session.principalSubjectId}</code>
   <small>session v{session.sessionVersion} · {session.identityId}</small></div>
   <div className="identity-links"><span>已认证身份</span>{session.linkedIdentityIds.map((id)=><button key={id} disabled={busy||id===session.identityId} onClick={()=>switchTo(id)}>{id===session.identityId?"当前 · ":"切换 · "}{id}</button>)}</div>
   <div className="identity-actions"><span>当前窄能力</span>{narrowActions(session.role).map((item)=><code key={item}>{item}</code>)}</div>
   <button className="identity-logout" onClick={logout}>退出整个 session</button>
  </>:<p className="identity-empty">尚未建立产品 session。登录 Main 后仍可在同一 cookie session 中认证独立 Reviewer。</p>}
  <form onSubmit={login}><h3>{session?"认证第二身份":"登录身份"}</h3>
   <label>Identity ID<input required value={identity} onChange={(e)=>setIdentity(e.target.value)}/></label>
   <label>Login secret<input required type="password" autoComplete="current-password" value={secret} onChange={(e)=>setSecret(e.target.value)}/></label>
   <button disabled={busy}>{session?"认证并切换":"建立 session"}</button>
   <p>角色、principal 与 capability 不从正文传入；服务端只从 HttpOnly session 派生。</p></form>
 </section>
}

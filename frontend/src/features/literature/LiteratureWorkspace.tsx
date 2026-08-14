import {type FormEvent, useMemo, useState} from "react";
import {LiteratureApiError, LiteratureGateway} from "./api.js";
import {
  connectorLabel,
  noveltyPresentation,
  sourceModeLabel,
  type ApplicabilityView,
  type ConnectorId,
  type FeatureFailure,
  type LiteratureGraphView,
  type LiteratureQueryDraft,
  type LiteratureQueryView,
  type LiteratureSourceView,
  type NoveltyReviewView,
  type PriorArtView,
  type QueryReceipt,
  type ReviewTaskView,
  type RunFence,
  type SourceSnapshotView,
} from "./model.js";
import "./literature.css";

const CONNECTORS: ConnectorId[] = ["OPENALEX", "CROSSREF", "ARXIV", "MATLAS"];
type EntityKey = "query" | "snapshot" | "source" | "graph" | "applicability" | "priorArt" | "novelty" | "reviewTask";
const ID_LABELS: Record<EntityKey, string> = {
  query: "Literature Query", snapshot: "Source Snapshot", source: "Literature Source",
  graph: "Literature Graph", applicability: "Applicability", priorArt: "Prior-art",
  novelty: "Novelty Review", reviewTask: "Review Task",
};
const EMPTY_IDS: Record<EntityKey, string> = {
  query: "", snapshot: "", source: "", graph: "", applicability: "", priorArt: "",
  novelty: "", reviewTask: "",
};

export interface LiteratureWorkspaceProps {run: RunFence; baseUrl?: string}

export function LiteratureWorkspace({run, baseUrl = ""}: LiteratureWorkspaceProps) {
  const gateway = useMemo(() => new LiteratureGateway(baseUrl), [baseUrl]);
  const [draft, setDraft] = useState<LiteratureQueryDraft>({
    researchQuestion: "", queryText: "", coverageBoundary: "",
    connectors: ["OPENALEX", "CROSSREF", "ARXIV"], targetEntityIds: [],
  });
  const [ids, setIds] = useState(EMPTY_IDS);
  const [query, setQuery] = useState<LiteratureQueryView>();
  const [snapshot, setSnapshot] = useState<SourceSnapshotView>();
  const [source, setSource] = useState<LiteratureSourceView>();
  const [graph, setGraph] = useState<LiteratureGraphView>();
  const [applicability, setApplicability] = useState<ApplicabilityView>();
  const [priorArt, setPriorArt] = useState<PriorArtView>();
  const [novelty, setNovelty] = useState<NoveltyReviewView>();
  const [reviewTask, setReviewTask] = useState<ReviewTaskView>();
  const [receipt, setReceipt] = useState<QueryReceipt>();
  const [failure, setFailure] = useState<FeatureFailure>();
  const [busy, setBusy] = useState(false);
  const noveltyState = noveltyPresentation(snapshot, novelty, reviewTask);

  function toggleConnector(connector: ConnectorId) {
    setDraft((current) => ({
      ...current,
      connectors: current.connectors.includes(connector)
        ? current.connectors.filter((item) => item !== connector)
        : [...current.connectors, connector],
    }));
  }
  async function act(action: () => Promise<void>) {
    setBusy(true); setFailure(undefined);
    try { await action(); } catch (error) { setFailure(toFailure(error)); } finally { setBusy(false); }
  }
  async function runSearch(event: FormEvent) {
    event.preventDefault();
    await act(async () => setReceipt(await gateway.runLiteratureQuery(run, crypto.randomUUID(), draft)));
  }
  async function loadEvidence() {
    await act(async () => {
      const requests = [
        ids.query ? gateway.queryLiteratureQuery(run.runId, ids.query).then(setQuery) : undefined,
        ids.snapshot ? gateway.querySnapshot(run.runId, ids.snapshot).then(setSnapshot) : undefined,
        ids.source ? gateway.querySource(run.runId, ids.source).then(setSource) : undefined,
        ids.graph ? gateway.queryGraph(run.runId, ids.graph).then(setGraph) : undefined,
        ids.applicability ? gateway.queryApplicability(run.runId, ids.applicability).then(setApplicability) : undefined,
        ids.priorArt ? gateway.queryPriorArt(run.runId, ids.priorArt).then(setPriorArt) : undefined,
        ids.novelty ? gateway.queryNovelty(run.runId, ids.novelty).then(setNovelty) : undefined,
        ids.reviewTask ? gateway.queryReviewTask(run.runId, ids.reviewTask).then(setReviewTask) : undefined,
      ].filter((item): item is Promise<void> => item !== undefined);
      if (!requests.length) throw new LiteratureApiError(0, "NO_ENTITY_ID", false);
      await Promise.all(requests);
    });
  }

  return <main className="lit-shell" aria-busy={busy}>
    <header className="lit-hero">
      <div><p className="lit-eyebrow">Literature intelligence / 文献证据台</p>
        <h1>多源检索与新颖性审查</h1>
        <p className="lit-lede">在线查询、固定快照、逐来源版本与独立审查分栏。任何搜索命中都不会直接写入数学事实。</p>
      </div>
      <div className="lit-fence"><span>RUN</span><strong>{run.runId}</strong>
        <small>r{run.revision} · contract {run.contractVersion}</small></div>
    </header>
    {failure && <FailureBanner failure={failure} />}
    {receipt && <div className="lit-receipt" role="status"><span>已提交真实产品命令</span>
      <code>{receipt.receiptId}</code><strong>{receipt.state}</strong>
      {receipt.jobId && <small>job {receipt.jobId}</small>}</div>}

    <section className="lit-grid lit-grid--top">
      <form className="lit-panel" onSubmit={runSearch}>
        <PanelTitle number="01" title="多源检索" note="原始响应强制入快照" />
        <label>研究问题<textarea required value={draft.researchQuestion}
          onChange={(event) => setDraft({...draft, researchQuestion: event.target.value})}
          placeholder="写清要排查的新结论及其数学对象" /></label>
        <label>检索式<input required value={draft.queryText}
          onChange={(event) => setDraft({...draft, queryText: event.target.value})}
          placeholder="关键词、作者、定理名、符号变体" /></label>
        <label>覆盖边界<input required value={draft.coverageBoundary}
          onChange={(event) => setDraft({...draft, coverageBoundary: event.target.value})}
          placeholder="数据库、时间、语言、学科与版本边界" /></label>
        <fieldset><legend>连接器</legend><div className="lit-connectors">
          {CONNECTORS.map((connector) => <label
            className={"lit-connector " + (draft.connectors.includes(connector) ? "is-on" : "")}
            key={connector}><input type="checkbox" checked={draft.connectors.includes(connector)}
              onChange={() => toggleConnector(connector)} />
            <span>{connectorLabel(connector)}</span><small>{connectorHint(connector)}</small></label>)}
        </div></fieldset>
        <button className="lit-primary" disabled={busy || !draft.connectors.length}>
          {busy ? "提交中…" : "运行在线检索"}</button>
      </form>

      <aside className="lit-panel">
        <PanelTitle number="02" title="Matlas 边界" note="外部薄客户端" />
        <div className="lit-boundary"><span className="lit-boundary__mark">M</span><div>
          <strong>仅调用外部 Matlas 服务</strong><p>当前产品不部署 Matlas 服务端、语料或索引。</p>
        </div></div>
        <EvidenceRows rows={[["来源","FrenzyMath / Danus 适配"],["归属","Apache-2.0 attribution"],
          ["覆盖","每次 SourceSnapshot 回执"],["权威","NO_MATH_OR_NOVELTY_WRITE"]]} />
        <p className="lit-warning">Matlas 命中只能成为候选文献边；服务不可用、NO_HIT 或覆盖不完整时绝不显示“新颖”。</p>
      </aside>
    </section>

    <section className="lit-panel lit-loader">
      <PanelTitle number="03" title="打开证据对象" note="只读真实 QueryResult" />
      <div className="lit-id-grid">{(Object.keys(ids) as EntityKey[]).map((key) =>
        <label key={key}>{ID_LABELS[key]}<input value={ids[key]}
          onChange={(event) => setIds({...ids, [key]: event.target.value})}
          placeholder={key + "_id"} /></label>)}</div>
      <button className="lit-secondary" onClick={loadEvidence} disabled={busy}>读取当前服务</button>
    </section>

    <section className="lit-grid lit-grid--evidence">
      <article className="lit-panel"><PanelTitle number="04" title="查询与快照" note="在线 / 重放严格分开" />
        {query || snapshot ? <><div className={"lit-mode " + (snapshot?.sourceMode === "REPLAYED_SNAPSHOT" ? "is-replay" : "")}>
          {sourceModeLabel(snapshot?.sourceMode ?? query?.sourceMode ?? "LIVE_QUERY")}</div>
          <EvidenceRows rows={[["状态",snapshot?.status ?? query?.status],["快照",snapshot?.id ?? query?.snapshotId],
            ["检索时间",snapshot?.retrievedAt],["覆盖边界",snapshot?.coverageBoundary ?? query?.coverageBoundary],
            ["语料摘要",snapshot?.corpusDigest],["证据等级",snapshot?.evidenceClass],["权威效果",snapshot?.authorityEffect]]} />
        </> : <EmptyState text="输入 literature_query_id 或 source_snapshot_id 查看真实回执。" />}</article>

      <article className={"lit-panel lit-novelty is-" + noveltyState.tone.toLowerCase()}>
        <PanelTitle number="05" title="新颖性闸门" note="独立审查，不是第二数学真值" />
        <h3>{noveltyState.label}</h3><p>{noveltyState.detail}</p>
        {novelty && <EvidenceRows rows={[["Claim",novelty.claimId],["审查 verdict",novelty.verdict],
          ["覆盖快照",novelty.coverageSnapshotIds.join(", ")],["签名工件",novelty.reviewArtifactId]]} />}
      </article>

      <article className="lit-panel"><PanelTitle number="06" title="精确来源上下文" note="arXiv ID / version / digest" />
        {source ? <EvidenceRows rows={[["稳定来源 ID",source.stableSourceId],["精确版本",source.sourceVersion],
          ["来源类型",source.sourceKind],["内容摘要",source.contentDigest],
          ["原文锚点工件",source.sourceArtifactIds.join(", ")]]} />
          : <EmptyState text="输入 literature_source_id。精确版本缺失时不会显示可引用上下文。" />}</article>

      <article className="lit-panel"><PanelTitle number="07" title="多源文献图" note="每条边必须可回溯" />
        {graph ? <><div className="lit-metrics"><Metric value={graph.nodeCount} label="nodes" />
          <Metric value={graph.edgeCount} label="edges" /><Metric value={graph.sourceKinds.length} label="sources" /></div>
          <EvidenceRows rows={[["图摘要",graph.graphDigest],["来源类别",graph.sourceKinds.join(", ")],
            ["来源工件",graph.sourceArtifactIds.join(", ")]]} />
          <UnavailableInset text="当前 LITERATURE_GRAPH variant 只发布图级摘要，尚未发布逐边 endpoint/version/anchor 字段；因此本界面不伪造边列表。" />
        </> : <EmptyState text="输入 literature_graph_id 查看已发布图摘要与来源类别。" />}</article>
    </section>

    <section className="lit-grid lit-grid--analysis">
      <article className="lit-panel"><PanelTitle number="08" title="定理适用性" note="量词、假设、符号逐项核对" />
        {applicability ? <><EvidenceRows rows={[["Theorem",applicability.theoremId],["目标 Claim",applicability.claimId],
          ["Verdict",applicability.verdict],["审查工件",applicability.reviewArtifactId]]} />
          <div className="lit-checklist"><span>∀ 量词映射</span><span>H 假设覆盖</span><span>Σ 符号绑定</span></div>
          <UnavailableInset text="逐项映射正文保存在签名审查工件中；当前 query variant 未发布行级字段，需打开工件核验。" />
        </> : <EmptyState text="输入 applicability_review_id，未审查时保持 INSUFFICIENT_CONTEXT。" />}</article>
      <article className="lit-panel"><PanelTitle number="09" title="Prior-art 对照" note="关系判断绑定摘要" />
        {priorArt ? <EvidenceRows rows={[["目标 Claim",priorArt.claimId],["来源",priorArt.literatureSourceId],
          ["关系",priorArt.relationship],["对照摘要",priorArt.comparisonDigest]]} />
          : <EmptyState text="输入 comparison_id；检索结果本身不是 prior-art 关系判断。" />}</article>
      <ReviewTaskPanel run={run} gateway={gateway} task={reviewTask} busy={busy}
        onFailure={setFailure} onReceipt={setReceipt} />
    </section>
  </main>;
}

function ReviewTaskPanel({run,gateway,task,busy,onFailure,onReceipt}: {
  run:RunFence; gateway:LiteratureGateway; task?:ReviewTaskView; busy:boolean;
  onFailure:(value:FeatureFailure|undefined)=>void; onReceipt:(value:QueryReceipt)=>void;
}) {
  const [targets,setTargets]=useState(""), [digest,setDigest]=useState(""), [authors,setAuthors]=useState("");
  const [assignee,setAssignee]=useState(""), [expires,setExpires]=useState("");
  async function submit(event:FormEvent) {event.preventDefault();onFailure(undefined);
    try {onReceipt(await gateway.createLiteratureReviewTask(run,crypto.randomUUID(),{
      targetEntityIds:csv(targets),targetDigest:digest,authorSubjectIds:csv(authors),
      assigneeSubjectId:assignee,expiresAt:expires,
    }));} catch(error){onFailure(toFailure(error));}}
  return <form className="lit-panel" onSubmit={submit}><PanelTitle number="10" title="独立文献审查" note="LITERATURE_REVIEWER" />
    <label>目标实体 IDs<input required value={targets} onChange={(e)=>setTargets(e.target.value)} /></label>
    <label>目标摘要<input required value={digest} onChange={(e)=>setDigest(e.target.value)} /></label>
    <div className="lit-pair"><label>作者 subject IDs<input required value={authors} onChange={(e)=>setAuthors(e.target.value)} /></label>
      <label>审查人 subject ID<input required value={assignee} onChange={(e)=>setAssignee(e.target.value)} /></label></div>
    <label>截止时间<input required type="datetime-local" value={expires} onChange={(e)=>setExpires(e.target.value)} /></label>
    <button className="lit-primary" disabled={busy}>创建独立任务</button>
    {task && <EvidenceRows rows={[["任务",task.id],["类型",task.reviewType],["状态",task.reviewState],
      ["签名工件",task.signedReviewArtifactId]]} />}</form>;
}
function PanelTitle({number,title,note}:{number:string;title:string;note:string}) {
  return <header className="lit-panel-title"><span>{number}</span><div><h2>{title}</h2><p>{note}</p></div></header>;
}
function EvidenceRows({rows}:{rows:[string,string|undefined][]}) {
  return <dl className="lit-rows">{rows.map(([label,value])=><div key={label}><dt>{label}</dt><dd>{value||"—"}</dd></div>)}</dl>;
}
function Metric({value,label}:{value:number;label:string}) {return <div><strong>{value.toLocaleString()}</strong><span>{label}</span></div>;}
function EmptyState({text}:{text:string}) {return <p className="lit-empty">{text}</p>;}
function UnavailableInset({text}:{text:string}) {return <div className="lit-unavailable"><strong>未发布字段</strong><p>{text}</p></div>;}
function FailureBanner({failure}:{failure:FeatureFailure}) {return <div className={"lit-failure "+(failure.unavailable?"is-unavailable":"")} role="alert">
  <strong>{failure.unavailable?"能力暂不可用":"请求未完成"}</strong><code>{failure.code}</code>
  <span>{failure.message}</span><p>{failure.action}</p></div>;}
function connectorHint(value:ConnectorId){return value==="ARXIV"?"精确 ID + 版本":value==="MATLAS"?"外部薄客户端":"元数据与引用";}
function csv(value:string){return value.split(",").map((item)=>item.trim()).filter(Boolean);}
function toFailure(error:unknown):FeatureFailure {if(error instanceof LiteratureApiError)return error.toFailure();
  return {code:"CLIENT_FAILURE",message:error instanceof Error?error.message:String(error),unavailable:false,
    action:"检查输入与浏览器控制台；不要把失败状态解释为检索完成。"};}

import {useMemo, useState} from "react";
import {ContractWorkbench} from "../contract/ContractWorkbench.js";
import type {ContractImpactPreview, ContractProjection} from "../contract/model.js";
import {MaterialsWorkbench} from "../materials/MaterialsWorkbench.js";
import type {ExtractionView} from "../materials/model.js";
import {ProductRouteError, ResearchGateway} from "./api.js";
import type {ArtifactRef, CreateResearchDraft, StatusMessage, TruthState} from "./model.js";
import "./research.css";

interface Props {
  deploymentId: string;
  baseUrl?: string;
  initialRunId?: string;
  researchRevision?: number;
  contractVersion?: number;
  contract?: ContractProjection;
  extraction?: ExtractionView;
  impactPreview?: ContractImpactPreview;
}

const emptyDraft: CreateResearchDraft = {
  title: "",
  question: "",
  owner: "",
  labels: [],
  contractDraft: {
    objects: [], definitions: [], quantifiers: [], exact_negation: "", boundary_rules: {}, success_conditions: [],
  },
  initialBudget: {INPUT_TOKEN: 100000, OUTPUT_TOKEN: 30000},
  materialArtifacts: [],
};

export function ResearchWorkspace(props: Props) {
  const gateway = useMemo(() => new ResearchGateway(props.deploymentId, props.baseUrl), [props.deploymentId, props.baseUrl]);
  const [draft, setDraft] = useState(emptyDraft);
  const [labels, setLabels] = useState("");
  const [contractJson, setContractJson] = useState(JSON.stringify(emptyDraft.contractDraft, null, 2));
  const [runId, setRunId] = useState(props.initialRunId);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<StatusMessage>({state: "CANDIDATE", title: "尚未创建研究", detail: "表单内容仅在本地；服务器返回 Receipt 前没有研究状态。"});

  async function createResearch() {
    setBusy(true);
    try {
      const contractDraft: unknown = JSON.parse(contractJson);
      if (contractDraft === null || Array.isArray(contractDraft) || typeof contractDraft !== "object") throw new Error("合同草稿必须是 JSON 对象");
      const request = {...draft, contractDraft: contractDraft as Record<string, unknown>, labels: labels.split(",").map((item) => item.trim()).filter(Boolean)};
      const outcome = await gateway.createResearch(crypto.randomUUID(), request);
      if (outcome.runId) setRunId(outcome.runId);
      setStatus({state: "CANDIDATE", title: "创建命令已返回", detail: `Receipt ${outcome.receiptId ?? "已返回"}，状态 ${outcome.state}。只有后续内核投影可标记已验证。`});
    } catch (error) {
      setStatus(toStatus(error));
    } finally { setBusy(false); }
  }

  function artifactsChanged(materialArtifacts: ArtifactRef[]) {
    setDraft((current) => ({...current, materialArtifacts}));
  }

  return <main className="rk-research-workspace">
    <header className="rk-research-hero"><div><p className="rk-kicker">ResearchProduct · F01</p><h1>建立一项可审计的数学研究</h1><p>先冻结题目、材料与合同边界，再进入路线和 Claim。任何 OCR、模型建议或外部命中都不会在这里被包装成数学事实。</p></div><StatusCard message={status}/></header>
    <nav aria-label="本页旅程"><a href="#create">01 创建</a><a href="#materials">02 材料</a><a href="#contract">03 合同</a><span>Run {runId ?? "未分配"}</span></nav>
    <section id="create" className="rk-create-research"><div className="rk-section-heading"><span>01</span><div><p className="rk-kicker">真实 CREATE_RESEARCH 命令</p><h2>研究题目与初始边界</h2></div></div><div className="rk-create-grid"><label>标题<input value={draft.title} onChange={(event) => setDraft({...draft, title: event.target.value})} placeholder="例如：有限群上的……"/></label><label>负责人<input value={draft.owner} onChange={(event) => setDraft({...draft, owner: event.target.value})} placeholder="当前组织内主体"/></label><label className="wide">研究问题<textarea value={draft.question} onChange={(event) => setDraft({...draft, question: event.target.value})} placeholder="写明需要证明、反驳或分类的精确问题"/></label><label>标签<input value={labels} onChange={(event) => setLabels(event.target.value)} placeholder="群论, 开放问题"/></label><label>输入预算<input type="number" min={1} value={draft.initialBudget.INPUT_TOKEN} onChange={(event) => setDraft({...draft, initialBudget: {...draft.initialBudget, INPUT_TOKEN: Number(event.target.value)}})}/></label><label className="wide">合同草稿 JSON<textarea className="rk-code" value={contractJson} onChange={(event) => setContractJson(event.target.value)} spellCheck={false}/></label></div><footer><p>{draft.materialArtifacts.length} 个已提交材料原件将绑定创建命令。</p><button type="button" onClick={createResearch} disabled={busy || !draft.question.trim() || !draft.owner.trim()}>{busy ? "等待服务器决定…" : "提交创建研究"}</button></footer></section>
    <section id="materials" className="rk-feature-shell"><div className="rk-section-heading"><span>02</span><div><p className="rk-kicker">分段续传与提取审查</p><h2>附件、OCR 与公式</h2></div></div><MaterialsWorkbench gateway={gateway} runId={runId} researchRevision={props.researchRevision} contractVersion={props.contractVersion} extraction={props.extraction} onArtifactsChange={artifactsChanged} onStatus={setStatus}/></section>
    <section id="contract" className="rk-feature-shell"><div className="rk-section-heading"><span>03</span><div><p className="rk-kicker">逐项歧义确认与局部失效</p><h2>合同冻结及修订</h2></div></div><ContractWorkbench gateway={gateway} runId={runId} researchRevision={props.researchRevision} contract={props.contract} impact={props.impactPreview} onStatus={setStatus}/></section>
  </main>;
}

function StatusCard({message}: {message: StatusMessage}) {
  const labels: Record<TruthState, string> = {CANDIDATE: "候选 / 待内核确认", VERIFIED: "已验证", INVALIDATED: "已失效", EXTERNAL_BLOCKED: "外部阻塞", UNAVAILABLE: "功能当前不可用"};
  return <aside className="rk-status-card" data-state={message.state}><span>{labels[message.state]}</span><strong>{message.title}</strong><p>{message.detail}</p></aside>;
}

function toStatus(error: unknown): StatusMessage {
  if (error instanceof ProductRouteError && error.unavailable) return {state: "UNAVAILABLE", title: "后端尚未发布此命令", detail: `${error.code}。界面不会生成本地假成功。`};
  return {state: "EXTERNAL_BLOCKED", title: "创建未完成", detail: error instanceof Error ? error.message : "未知服务器错误"};
}

import {useMemo, useState} from "react";
import type {ResearchGateway} from "../research/api.js";
import type {ArtifactRef, StatusMessage} from "../research/model.js";
import type {ContractImpactPreview, ContractProjection} from "./model.js";
import "./contract.css";

interface Props {
  gateway: ResearchGateway;
  runId?: string;
  researchRevision?: number;
  contract?: ContractProjection;
  impact?: ContractImpactPreview;
  onStatus(message: StatusMessage): void;
}

export function ContractWorkbench({gateway, runId, researchRevision, contract, impact, onStatus}: Props) {
  const [resolutions, setResolutions] = useState<Record<string, string>>({});
  const [note, setNote] = useState("");
  const [amendment, setAmendment] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const allResolved = useMemo(() => contract?.ambiguities.every((item) => item.state === "CONFIRMED" || resolutions[item.ambiguityId]?.trim()) ?? false, [contract, resolutions]);

  async function confirmContract() {
    if (!runId || researchRevision === undefined || !contract || !allResolved || !note.trim()) return;
    setBusy(true);
    try {
      const result = await gateway.runCommand(runId, researchRevision, contract.version, crypto.randomUUID(), "CONFIRM_CONTRACT", {
        contract_digest: contract.digest,
        material_anchor_ids: contract.materialAnchorIds,
        confirmation_note: JSON.stringify({note, ambiguity_resolutions: resolutions}),
      });
      onStatus({state: "CANDIDATE", title: "合同确认命令已决定", detail: `Receipt ${result.receiptId ?? "已返回"}；是否冻结以最新合同查询为准。`});
    } catch (error) {
      onStatus(errorStatus(error, "合同确认当前不可用"));
    } finally { setBusy(false); }
  }

  async function submitAmendment() {
    if (!runId || researchRevision === undefined || !contract || !impact || !acknowledged || !amendment.trim()) return;
    setBusy(true);
    try {
      const file = new File([JSON.stringify({schema_version: "rk.contract.amendment.v1", base_version: contract.version, amendment, impact_preview_id: impact.previewId, impact_preview_digest: impact.previewDigest})], `contract-amendment-v${contract.version}.json`, {type: "application/json"});
      let artifact: ArtifactRef | undefined;
      artifact = await gateway.upload(file, () => undefined);
      const result = await gateway.runCommand(runId, researchRevision, contract.version, crypto.randomUUID(), "AMEND_CONTRACT", {
        amendment_artifact: {
          artifact_id: artifact.artifact_id,
          sha256: artifact.sha256,
          byte_count: artifact.byte_count,
          media_type: artifact.media_type,
        },
        base_contract_version: contract.version,
        impact_acknowledgement: {preview_id: impact.previewId, preview_digest: impact.previewDigest, acknowledged: true},
      });
      onStatus({state: "CANDIDATE", title: "局部修订已提交", detail: `Receipt ${result.receiptId ?? "已返回"}。失效引擎追平前，旧队列、审查与见证不可消费。`});
    } catch (error) {
      onStatus(errorStatus(error, "合同局部修订当前不可用"));
    } finally { setBusy(false); }
  }

  if (!contract) return <section className="rk-contract rk-empty-contract"><p className="rk-kicker">合同与数学边界</p><h2>等待真实合同投影</h2><p>创建研究成功后，以 CONTRACT 查询返回的修订、摘要和歧义为准；本地表单不会自行冻结合同。</p></section>;
  return <section className="rk-contract" aria-labelledby="contract-title">
    <header><div><p className="rk-kicker">版本 {contract.version} · {contract.state}</p><h2 id="contract-title">数学合同</h2></div><code>{contract.digest.slice(0, 12)}…</code></header>
    <div className="rk-contract-grid"><article><h3>目标陈述</h3><p>{contract.statement}</p><h3>精确否定</h3><p>{contract.exactNegation}</p></article><article><h3>对象与量词</h3><ul>{[...contract.objects, ...contract.quantifiers].map((item) => <li key={item}>{item}</li>)}</ul><h3>边界规则</h3><ul>{contract.boundaryRules.map((item) => <li key={item}>{item}</li>)}</ul></article></div>
    <div className="rk-ambiguities"><div><p className="rk-kicker">逐项确认</p><h3>OCR / 合同歧义</h3></div>{contract.ambiguities.length === 0 ? <p>服务器未报告开放歧义。</p> : contract.ambiguities.map((item) => <label key={item.ambiguityId} data-confirmed={item.state === "CONFIRMED"}><span><strong>{item.field}</strong><small>{item.question}</small><q>{item.currentText}</q></span><textarea disabled={item.state === "CONFIRMED"} value={item.state === "CONFIRMED" ? item.resolution : resolutions[item.ambiguityId] ?? ""} onChange={(event) => setResolutions({...resolutions, [item.ambiguityId]: event.target.value})} placeholder="明确对象、定义域、量词或符号含义"/></label>)}</div>
    <div className="rk-confirm-contract"><label>确认说明<textarea value={note} onChange={(event) => setNote(event.target.value)} placeholder="记录逐项核对范围与仍存在的外部阻塞"/></label><button type="button" disabled={!allResolved || !note.trim() || busy} onClick={confirmContract}>提交正式合同确认</button></div>
    <div className="rk-amendment"><div><p className="rk-kicker">AuthorityInvalidation 预览</p><h3>局部修订及完整失效差异</h3></div><textarea value={amendment} onChange={(event) => setAmendment(event.target.value)} placeholder="只描述要修改的合同局部；先由服务器生成绑定修订的影响预览"/>{impact ? <><table><thead><tr><th>对象</th><th>稳定标签</th><th>之前</th><th>之后</th><th>理由</th></tr></thead><tbody>{impact.differences.map((item) => <tr key={`${item.objectType}:${item.objectId}`}><td>{item.objectType}</td><td>{item.stableLabel}</td><td>{item.beforeState}</td><td data-after={item.afterState}>{item.afterState}</td><td>{item.reason}</td></tr>)}</tbody></table><div className="rk-impact-summary"><span>保留 sibling：{impact.preservedSiblingIds.length}</span><span>重开义务：{impact.reopenedObligationIds.length}</span><code>{impact.previewDigest.slice(0, 12)}…</code></div><label className="rk-ack"><input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)}/>我已逐项核对全部 INVALIDATED / PRESERVED / REOPENED 差异</label><button type="button" disabled={!acknowledged || !amendment.trim() || busy} onClick={submitAmendment}>按此预览提交局部修订</button></> : <div className="rk-unavailable"><strong>尚无真实影响预览</strong><p>CONTRACT_IMPACT 后端未发布或尚未执行时，不提供“确认修订”假按钮。</p></div>}</div>
  </section>;
}

function errorStatus(error: unknown, title: string): StatusMessage {
  const detail = error instanceof Error ? error.message : "未知服务错误";
  return {state: detail.includes("UNAVAILABLE") || detail.includes("503") ? "UNAVAILABLE" : "EXTERNAL_BLOCKED", title, detail};
}

import {useMemo, useRef, useState} from "react";
import type {ResearchGateway} from "../research/api.js";
import type {ArtifactRef, StatusMessage} from "../research/model.js";
import type {ExtractionCorrection, ExtractionView, UploadItem} from "./model.js";
import "./materials.css";

interface Props {
  gateway: ResearchGateway;
  runId?: string;
  researchRevision?: number;
  contractVersion?: number;
  extraction?: ExtractionView;
  onArtifactsChange(artifacts: ArtifactRef[]): void;
  onStatus(message: StatusMessage): void;
}

const accepted = ".pdf,.tex,.txt,.md,image/png,image/jpeg,image/webp";

export function MaterialsWorkbench(props: Props) {
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const [selectedPage, setSelectedPage] = useState(1);
  const [correction, setCorrection] = useState<ExtractionCorrection | null>(null);
  const [busy, setBusy] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const committed = useMemo(() => uploads.flatMap((item) => item.artifact ? [item.artifact] : []), [uploads]);

  function choose(files: FileList | null) {
    if (!files) return;
    setUploads((current) => [
      ...current,
      ...[...files].map((file) => ({id: crypto.randomUUID(), file, received: 0, state: "QUEUED" as const})),
    ]);
  }

  async function uploadAll() {
    setBusy(true);
    abort.current = new AbortController();
    try {
      for (const item of uploads.filter((candidate) => candidate.state !== "COMMITTED")) {
        setUploads((current) => current.map((candidate) => candidate.id === item.id ? {...candidate, state: "HASHING"} : candidate));
        try {
          const artifact = await props.gateway.upload(
            item.file,
            (received) => setUploads((current) => current.map((candidate) => candidate.id === item.id ? {...candidate, state: "UPLOADING", received} : candidate)),
            abort.current.signal,
          );
          setUploads((current) => {
            const next = current.map((candidate) => candidate.id === item.id ? {...candidate, state: "COMMITTED" as const, received: item.file.size, artifact} : candidate);
            props.onArtifactsChange(next.flatMap((candidate) => candidate.artifact ? [candidate.artifact] : []));
            return next;
          });
        } catch (error) {
          if (abort.current.signal.aborted) throw error;
          setUploads((current) => current.map((candidate) => candidate.id === item.id ? {...candidate, state: "FAILED", error: error instanceof Error ? error.message : "上传失败"} : candidate));
        }
      }
    } finally {
      setBusy(false);
    }
  }

  async function confirmCorrection() {
    const extraction = props.extraction;
    if (!correction || !extraction || !props.runId || props.researchRevision === undefined || props.contractVersion === undefined) return;
    setBusy(true);
    try {
      const correctionFile = new File([JSON.stringify({schema_version: "rk.material.corrections.v1", extraction_id: extraction.extractionId, corrections: [correction]})], `correction-${extraction.extractionId}.json`, {type: "application/json"});
      const correctionArtifact = await props.gateway.upload(correctionFile, () => undefined);
      const sourceArtifact = {artifact_id: extraction.originalArtifactId};
      const outcome = await props.gateway.runCommand(
        props.runId,
        props.researchRevision,
        props.contractVersion,
        crypto.randomUUID(),
        "CONFIRM_MATERIAL_EXTRACTION",
        {
          material_extraction_id: extraction.extractionId,
          extraction_digest: extraction.extractionDigest,
          corrections_artifact: {
            artifact_id: correctionArtifact.artifact_id,
            sha256: correctionArtifact.sha256,
            byte_count: correctionArtifact.byte_count,
            media_type: correctionArtifact.media_type,
          },
          source_artifact: sourceArtifact,
          reviewed_page_numbers: [correction.page],
        },
      );
      props.onStatus({state: "CANDIDATE", title: "解析修订已提交", detail: `Receipt ${outcome.receiptId ?? "已返回"}。材料确认不会直接写入数学事实图。`});
      setCorrection(null);
    } catch (error) {
      props.onStatus({state: error instanceof Error && error.message.includes("UNAVAILABLE") ? "UNAVAILABLE" : "EXTERNAL_BLOCKED", title: "解析修订未生效", detail: error instanceof Error ? error.message : "服务不可用"});
    } finally {
      setBusy(false);
    }
  }

  const page = props.extraction?.pages.find((item) => item.page === selectedPage);
  return <section className="rk-materials" aria-labelledby="materials-title">
    <header><div><p className="rk-kicker">不可变原件 · 可审查提取</p><h2 id="materials-title">研究材料</h2></div><span className="rk-boundary">OCR / 解析结果不是数学事实</span></header>
    <div className="rk-upload-zone">
      <label><span>选择 PDF、TeX、图片或文本</span><input type="file" accept={accepted} multiple onChange={(event) => choose(event.target.files)} /></label>
      <div className="rk-upload-actions"><button type="button" onClick={uploadAll} disabled={busy || uploads.length === 0}>分段上传 / 续传</button>{busy && <button type="button" className="rk-secondary" onClick={() => abort.current?.abort()}>暂停</button>}</div>
    </div>
    <ul className="rk-upload-list">{uploads.map((item) => <li key={item.id}><div><strong>{item.file.name}</strong><small>{item.file.type || "未知媒体类型"} · {item.file.size.toLocaleString()} bytes</small></div><progress value={item.received} max={item.file.size}/><span data-state={item.state}>{item.state}</span>{item.error && <small className="rk-error">{item.error}</small>}</li>)}</ul>
    {committed.length > 0 && <p className="rk-notice">已提交 {committed.length} 个 CAS 原件；是否可解析仍以服务器回执为准。</p>}

    {props.extraction ? <div className="rk-extraction-grid">
      <aside><h3>页段与公式锚点</h3>{props.extraction.pages.map((item) => <button type="button" className={selectedPage === item.page ? "active" : ""} key={item.page} onClick={() => setSelectedPage(item.page)}><span>第 {item.page} 页</span><small>{item.formulaCount} 个公式 · {item.confirmed ? "已逐项确认" : "待确认"}</small></button>)}</aside>
      <div className="rk-original"><h3>原件</h3><object data={`${props.gateway.artifactUrl(props.extraction.originalArtifactId)}#page=${selectedPage}`} aria-label={`原件第 ${selectedPage} 页`}/><p>{page?.originalLabel}</p></div>
      <div className="rk-extracted"><h3>提取文本与公式</h3><pre>{page?.extractedText}</pre><p>公式对象：{page?.formulaCount ?? 0}；请核对上下标、量词、集合符号与边界。</p><button type="button" className="rk-secondary" onClick={() => setCorrection({page: selectedPage, locator: `page:${selectedPage}`, originalText: page?.extractedText ?? "", correctedText: page?.extractedText ?? "", reason: ""})}>修订这一处解析错误</button></div>
    </div> : <div className="rk-empty"><h3>尚无真实提取投影</h3><p>上传成功不等于 OCR 成功。服务器发布 MATERIAL_EXTRACTION 查询后，这里才显示原件—提取差异。</p></div>}

    {correction && <div className="rk-correction" role="dialog" aria-modal="true" aria-label="解析错误修订"><h3>精确修订页段锚点</h3><label>定位<input value={correction.locator} onChange={(event) => setCorrection({...correction, locator: event.target.value})}/></label><label>原提取<textarea value={correction.originalText} readOnly/></label><label>修订后<textarea value={correction.correctedText} onChange={(event) => setCorrection({...correction, correctedText: event.target.value})}/></label><label>理由<textarea value={correction.reason} onChange={(event) => setCorrection({...correction, reason: event.target.value})}/></label><div><button type="button" onClick={confirmCorrection} disabled={!correction.reason.trim() || correction.correctedText === correction.originalText || busy}>提交修订并逐页确认</button><button type="button" className="rk-secondary" onClick={() => setCorrection(null)}>取消</button></div></div>}
  </section>;
}

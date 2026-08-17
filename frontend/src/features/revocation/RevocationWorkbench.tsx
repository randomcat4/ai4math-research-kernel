import { type FormEvent, useMemo, useState } from "react";
import {
  ObjectPicker,
  type ObjectPickerOption,
} from "../../design/ObjectPicker.js";
import { GraphGateway } from "../graph/api.js";
import type { GraphSearchHit } from "../graph/model.js";
import { ClaimApiError, ClaimGateway } from "../claim/api.js";
import type {
  ArtifactBinding,
  ClaimView,
  CommandReceipt,
  FeatureFailure,
  RunFence,
} from "../claim/model.js";
import type { RevokePreview } from "./model.js";
import "./revocation.css";
import "./revocation-object-picker.css";

export interface RevocationWorkbenchProps {
  run: RunFence;
  baseUrl?: string;
}
type PickerState = "READY" | "LOADING" | "NOT_PUBLISHED" | "ERROR";
export function RevocationWorkbench({
  run,
  baseUrl = "",
}: RevocationWorkbenchProps) {
  const gateway = useMemo(() => new ClaimGateway(baseUrl), [baseUrl]),
    graphGateway = useMemo(() => new GraphGateway(baseUrl), [baseUrl]);
  const [target, setTarget] = useState<ClaimView>(),
    [preview, setPreview] = useState<RevokePreview>();
  const [reasonText, setReasonText] = useState(""),
    [reasonFile, setReasonFile] = useState<File>(),
    [failure, setFailure] = useState<FeatureFailure>(),
    [receipt, setReceipt] = useState<CommandReceipt>(),
    [busy, setBusy] = useState(false);
  async function act(action: () => Promise<void>) {
    setBusy(true);
    setFailure(undefined);
    try {
      await action();
    } catch (error) {
      setFailure(toFailure(error));
    } finally {
      setBusy(false);
    }
  }
  async function selectTarget(id: string) {
    setPreview(undefined);
    if (!id) {
      setTarget(undefined);
      return;
    }
    await act(async () => setTarget(await gateway.queryClaim(run.runId, id)));
  }
  async function inspect(event: FormEvent) {
    event.preventDefault();
    if (!target) return;
    await act(async () =>
      setPreview(
        await gateway.queryRevokePreview(
          run,
          target.id,
          target.statementDigest,
        ),
      ),
    );
  }
  async function confirm() {
    if (!preview || (!reasonFile && !reasonText.trim())) return;
    await act(async () => {
      if (preview.previewRevision !== run.revision)
        throw new ClaimApiError(409, "REVOCATION_PREVIEW_STALE", false);
      const reason = await uploadReason(
        gateway,
        reasonFile,
        reasonText,
        preview,
      );
      setReceipt(
        await gateway.confirmRevoke(run, crypto.randomUUID(), preview, {
          affectedFactIds: preview.affectedClaimIds,
          preservedSiblingIds: preview.preservedSiblingIds,
          reopenedObligationIds: preview.reopenedObligationIds,
          reasonArtifact: reason,
        }),
      );
    });
  }
  return (
    <main className="revoke-shell">
      <header className="revoke-hero">
        <div>
          <p>撤销与恢复</p>
          <h1>预览数学影响后再确认</h1>
          <span>
            目标摘要从所选命题自动读取；受影响闭包、保留项与重开义务全部以服务器预览为准。
          </span>
        </div>
        <div>
          <b>研究修订 {run.revision}</b>
          <small>合同版本 {run.contractVersion}</small>
        </div>
      </header>
      {failure ? (
        <div className="revoke-failure" role="alert">
          <b>{failure.unavailable ? "能力不可用" : "请求失败"}</b>
          <span>{failure.action}</span>
          <details>
            <summary>技术详情</summary>
            <code>{failure.code}</code>
          </details>
          {failure.code === "REVOCATION_PREVIEW_STALE" ||
          failure.code === "STALE_QUERY" ? (
            <button onClick={() => setPreview(undefined)}>清除旧预览</button>
          ) : null}
        </div>
      ) : null}
      {receipt ? (
        <div className="revoke-receipt">
          <b>{receipt.state}</b>
          <span>撤销动作回执已返回；最终状态以新研究投影为准。</span>
          <details>
            <summary>技术标识</summary>
            <code>{receipt.receiptId}</code>
          </details>
        </div>
      ) : null}
      <section className="revoke-grid">
        <form className="revoke-panel" onSubmit={inspect}>
          <Title title="选择撤销目标" note="从当前研究历史图选择" />
          <RevocationTargetPicker
            graphGateway={graphGateway}
            run={run}
            selectedId={target?.id ?? ""}
            onSelect={(id) => void selectTarget(id)}
          />
          {target ? (
            <div className="revoke-target">
              <strong>{target.stableLabel}</strong>
              <span>
                {target.lifecycle} · {target.semanticState}
              </span>
              <details>
                <summary>查看绑定摘要</summary>
                <code>{target.statementDigest}</code>
              </details>
            </div>
          ) : null}
          <button disabled={busy || !target}>
            生成研究修订 {run.revision} 的影响预览
          </button>
          <p className="revoke-note">
            所选命题或研究修订变化后，旧预览会立即清除。
          </p>
        </form>
        <article className="revoke-panel">
          <Title title="影响闭包" note="由服务端依赖图计算" />
          {preview ? (
            <>
              <dl className="revoke-rows">
                <div>
                  <dt>受影响命题</dt>
                  <dd>{preview.affectedClaimIds.length}</dd>
                </div>
                <div>
                  <dt>保留 sibling</dt>
                  <dd>{preview.preservedSiblingIds.length}</dd>
                </div>
                <div>
                  <dt>重开义务</dt>
                  <dd>{preview.reopenedObligationIds.length}</dd>
                </div>
                <div>
                  <dt>研究修订</dt>
                  <dd>{preview.previewRevision}</dd>
                </div>
              </dl>
              <ol className="revoke-affected">
                {preview.affectedClaimIds.map((id) => (
                  <li key={id}>
                    <span>将失效</span>
                    <code>{id}</code>
                  </li>
                ))}
              </ol>
              <details className="revoke-technical">
                <summary>查看预览技术绑定</summary>
                <code>{preview.closureDigest}</code>
              </details>
            </>
          ) : (
            <Empty text="选择命题并查询真实撤销预览；界面不会自行计算依赖闭包。" />
          )}
        </article>
        <article className="revoke-panel">
          <Title title="撤销理由与确认" note="上传原件或生成结构化说明" />
          {preview ? (
            <div className="revoke-boundary">
              <strong>服务器预览已绑定</strong>
              <span>
                {preview.preservedSiblingIds.length} 个无关事实保留，
                {preview.reopenedObligationIds.length}{" "}
                个义务重开。确认时不会由用户重填。
              </span>
            </div>
          ) : (
            <Empty text="生成影响预览后才能准备确认。" />
          )}
          <label>
            结构化说明
            <textarea
              value={reasonText}
              onChange={(event) => setReasonText(event.target.value)}
              placeholder="说明为何撤销、发现方式、数学影响和建议恢复路径"
            />
          </label>
          <label>
            或上传撤销理由原件
            <input
              type="file"
              accept=".json,.md,.txt,application/json,text/markdown,text/plain"
              onChange={(event) => setReasonFile(event.target.files?.[0])}
            />
          </label>
          {reasonFile ? (
            <p className="revoke-file">已选择：{reasonFile.name}</p>
          ) : null}
          <button
            type="button"
            disabled={busy || !preview || (!reasonFile && !reasonText.trim())}
            onClick={() => void confirm()}
          >
            按服务器预览确认撤销
          </button>
        </article>
      </section>
      <section className="revoke-panel revoke-recovery">
        <Title title="重新证明与恢复" note="创建新命题，不复活旧事实" />
        <div className="revoke-flow">
          <div>
            <b>1</b>
            <span>受影响命题失效</span>
          </div>
          <i>→</i>
          <div>
            <b>2</b>
            <span>义务重新打开</span>
          </div>
          <i>→</i>
          <div>
            <b>3</b>
            <span>提交替代命题</span>
          </div>
          <i>→</i>
          <div>
            <b>4</b>
            <span>新验证接受后恢复</span>
          </div>
        </div>
        <p>
          恢复必须经过新的候选提交与验证导入。工具重跑完成不能直接复活已撤销事实。
        </p>
      </section>
    </main>
  );
}

function RevocationTargetPicker({
  graphGateway,
  run,
  selectedId,
  onSelect,
}: {
  graphGateway: GraphGateway;
  run: RunFence;
  selectedId: string;
  onSelect(id: string): void;
}) {
  const [query, setQuery] = useState(""),
    [hits, setHits] = useState<GraphSearchHit[]>([]),
    [status, setStatus] = useState<PickerState>("READY"),
    [message, setMessage] = useState("");
  async function search() {
    setStatus("LOADING");
    setMessage("");
    try {
      const page = await graphGateway.search(
        { ...run, lastCursor: run.lastCursor ?? 0 },
        "RESEARCH_HISTORY",
        query.trim(),
      );
      setHits(page.items);
      setStatus("READY");
      setMessage(
        page.total > page.items.length
          ? `显示 ${page.items.length} / ${page.total} 条，请缩小关键词。`
          : "",
      );
    } catch (error) {
      setHits([]);
      setStatus(
        error instanceof Error && error.message.includes("UNAVAILABLE")
          ? "NOT_PUBLISHED"
          : "ERROR",
      );
      setMessage(error instanceof Error ? error.message : "事实图搜索失败");
    }
  }
  const options: ObjectPickerOption[] = hits.map((item) => ({
    id: item.claim_id,
    label: item.stable_label,
    description: item.statement,
    meta: item.lifecycle,
  }));
  return (
    <ObjectPicker
      label="当前研究中的命题"
      description="按稳定标签或陈述关键词搜索。选择后自动读取完整 statement digest，不再手填。"
      options={options}
      selectedIds={selectedId ? [selectedId] : []}
      onChange={(ids) => onSelect(ids[0] ?? "")}
      status={status}
      statusText={message}
      emptyText={query ? "没有匹配命题。" : "输入关键词后搜索当前研究。"}
      search={{
        value: query,
        placeholder: "稳定标签或陈述关键词",
        onChange: setQuery,
        onSubmit: () => void search(),
      }}
    />
  );
}
async function uploadReason(
  gateway: ClaimGateway,
  file: File | undefined,
  text: string,
  preview: RevokePreview,
): Promise<ArtifactBinding> {
  if (file) return gateway.upload(file);
  const body = JSON.stringify(
    {
      schema_version: "rk.revocation.reason.v1",
      preview_id: preview.id,
      target_claim_id: preview.targetClaimId,
      reason: text.trim(),
      recorded_at: new Date().toISOString(),
    },
    null,
    2,
  );
  return gateway.upload(
    new File([body], `revocation-reason-${preview.targetClaimId}.json`, {
      type: "application/json",
    }),
  );
}
function Title({ title, note }: { title: string; note: string }) {
  return (
    <header className="revoke-title">
      <div>
        <h2>{title}</h2>
        <p>{note}</p>
      </div>
    </header>
  );
}
function Empty({ text }: { text: string }) {
  return <p className="revoke-empty">{text}</p>;
}
function toFailure(error: unknown): FeatureFailure {
  return error instanceof ClaimApiError
    ? error.toFailure()
    : {
        code: "CLIENT_FAILURE",
        message: String(error),
        unavailable: false,
        action: "重新选择当前研究中的命题并生成新预览。",
      };
}

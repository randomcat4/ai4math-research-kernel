import { type FormEvent, useEffect, useMemo, useState } from "react";
import {
  ObjectPicker,
  type ObjectPickerOption,
} from "../../design/ObjectPicker.js";
import { GraphGateway } from "../graph/api.js";
import type { GraphMode, GraphSearchHit } from "../graph/model.js";
import { ClaimApiError, ClaimGateway } from "./api.js";
import {
  authorityLabel,
  type ArtifactBinding,
  type ArtifactChoice,
  type ClaimRevision,
  type ClaimView,
  type CommandReceipt,
  type FeatureFailure,
  type GraphView,
  type ReviewTaskChoice,
  type RunFence,
  type WorkContext,
  type WorkflowView,
} from "./model.js";
import "./claim.css";
import "./claim-object-picker.css";

export interface ClaimWorkbenchProps {
  run: RunFence;
  baseUrl?: string;
}
type SourceState = "READY" | "LOADING" | "NOT_PUBLISHED" | "ERROR";
interface ObjectSources {
  contexts: WorkContext[];
  artifacts: ArtifactChoice[];
  reviews: ReviewTaskChoice[];
  contextState: SourceState;
  artifactState: SourceState;
  reviewState: SourceState;
}

export function ClaimWorkbench({ run, baseUrl = "" }: ClaimWorkbenchProps) {
  const gateway = useMemo(() => new ClaimGateway(baseUrl), [baseUrl]),
    graphGateway = useMemo(() => new GraphGateway(baseUrl), [baseUrl]);
  const sources = useObjectSources(gateway, run);
  const [claimId, setClaimId] = useState("");
  const [claim, setClaim] = useState<ClaimView>(),
    [history, setHistory] = useState<ClaimRevision[]>([]);
  const [workflow, setWorkflow] = useState<WorkflowView>(),
    [graph, setGraph] = useState<GraphView>(),
    [mode, setMode] = useState<"VERIFIED" | "RESEARCH_HISTORY">("VERIFIED");
  const [failure, setFailure] = useState<FeatureFailure>(),
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
  async function openClaim(id: string) {
    setClaimId(id);
    if (!id) {
      setClaim(undefined);
      setHistory([]);
      setGraph(undefined);
      return;
    }
    await act(async () => {
      const [nextClaim, nextHistory, nextWorkflow, nextGraph] =
        await Promise.all([
          gateway.queryClaim(run.runId, id),
          gateway.queryHistory(run.runId, id),
          gateway.queryWorkflow(run.runId),
          gateway.queryGraph(run.runId, run.revision, mode, [id]),
        ]);
      setClaim(nextClaim);
      setHistory(nextHistory);
      setWorkflow(nextWorkflow);
      setGraph(nextGraph);
    });
  }
  async function switchGraph(next: "VERIFIED" | "RESEARCH_HISTORY") {
    setMode(next);
    if (claimId)
      await act(async () =>
        setGraph(
          await gateway.queryGraph(run.runId, run.revision, next, [claimId]),
        ),
      );
  }
  const authority = claim ? authorityLabel(claim) : undefined;
  return (
    <main className="claim-shell" aria-busy={busy}>
      <header className="claim-hero">
        <div>
          <p>候选命题与验证</p>
          <h1>检查、修复与谱系</h1>
          <span>候选、工具执行、验证接受与权威晋级严格分栏。</span>
        </div>
        <div className="claim-fence">
          <b>研究修订 {run.revision}</b>
          <small>合同版本 {run.contractVersion}</small>
        </div>
      </header>
      {failure ? <Failure failure={failure} /> : null}
      {receipt ? (
        <div className="claim-receipt">
          <b>{receipt.state}</b>
          <span>动作回执已返回</span>
          <details>
            <summary>技术标识</summary>
            <code>{receipt.receiptId}</code>
          </details>
        </div>
      ) : null}
      <section className="claim-panel claim-loader">
        <Title title="打开待核验命题" note="从当前研究的真实事实图搜索" />
        <ClaimSearchPicker
          graphGateway={graphGateway}
          run={run}
          mode="RESEARCH_HISTORY"
          selectedId={claimId}
          onSelect={(id) => void openClaim(id)}
          label="当前研究中的命题"
          description="按稳定标签或陈述关键词搜索；选择后自动读取命题、历史、工作流与局部图。"
        />
      </section>
      <section className="claim-grid claim-grid--top">
        <article className="claim-panel">
          <Title title="原子命题" note="机器状态不等于数学权威" />
          {claim ? (
            <>
              <div className={"claim-authority is-" + authority?.tone}>
                <b>{authority?.title}</b>
                <span>{authority?.detail}</span>
              </div>
              <Rows
                rows={[
                  ["稳定标签", claim.stableLabel],
                  ["生命周期", claim.lifecycle],
                  ["执行状态", claim.machineState],
                  ["数学语义", claim.semanticState],
                ]}
              />
              <details className="claim-technical">
                <summary>查看摘要与内部标识</summary>
                <code>{claim.statementDigest}</code>
              </details>
            </>
          ) : (
            <Empty text="从当前研究中选择一个待核验命题。" />
          )}
        </article>
        <article className="claim-panel">
          <Title title="研究稿原子化" note="前驱、类型、未定义符号" />
          <Unavailable text="当前部署尚未发布研究稿预览对象源。请先在研究稿页面完成原子化；这里不会本地解析论文并冒充服务端结果。" />
        </article>
        <article className="claim-panel">
          <Title title="义务就绪度" note="只读取工作流与边回执" />
          {workflow ? (
            <Rows
              rows={[
                ["阶段", workflow.phase],
                ["工作流", workflow.state],
                ["活跃研究任务", String(workflow.activeWorkItemIds.length)],
              ]}
            />
          ) : (
            <Empty text="选择命题后读取当前工作流。" />
          )}
          {graph ? (
            <div className="claim-obligations">
              {graph.edges.map((edge) => (
                <span
                  key={edge.id}
                  className={"is-" + edge.obligationStatus.toLowerCase()}
                >
                  {edge.obligationStatus}
                </span>
              ))}
            </div>
          ) : null}
        </article>
      </section>
      <section className="claim-grid claim-grid--main">
        <article className="claim-panel claim-graph">
          <Title title="有效图与研究谱系" note="两种图模式绝不混合" />
          <div className="claim-tabs">
            <button
              className={mode === "VERIFIED" ? "active" : ""}
              onClick={() => void switchGraph("VERIFIED")}
            >
              有效依赖图
            </button>
            <button
              className={mode === "RESEARCH_HISTORY" ? "active" : ""}
              onClick={() => void switchGraph("RESEARCH_HISTORY")}
            >
              研究历史图
            </button>
          </div>
          {graph ? (
            <>
              <div className="claim-metrics">
                <b>
                  {graph.nodes.length}
                  <small>命题</small>
                </b>
                <b>
                  {graph.edges.length}
                  <small>依赖</small>
                </b>
                <b>
                  {graph.atRevision}
                  <small>研究修订</small>
                </b>
              </div>
              <div className="claim-node-list">
                {graph.nodes.map((node) => (
                  <div key={node.claimId}>
                    <header>
                      <b>{node.stableLabel}</b>
                      <span>{node.claimType}</span>
                    </header>
                    <p>{node.statement}</p>
                    <footer>
                      <span>{node.verificationMethod}</span>
                      <strong>{node.dependable ? "可依赖" : "不可依赖"}</strong>
                    </footer>
                  </div>
                ))}
              </div>
              {graph.truncated ? (
                <div className="claim-stale">
                  当前局部图已截断；请在事实图页面继续浏览。
                </div>
              ) : null}
            </>
          ) : (
            <Empty text="选择命题后按当前研究修订读取局部图。" />
          )}
        </article>
        <aside className="claim-stack">
          <article className="claim-panel">
            <Title title="命题历史" note="拒绝、修复与替代关系" />
            {history.length ? (
              <ol className="claim-history">
                {history.map((item) => (
                  <li key={item.id + item.revision}>
                    <b>修订 {item.revision}</b>
                    <span>{item.lifecycle}</span>
                    <details>
                      <summary>技术详情</summary>
                      <code>{item.statementDigest}</code>
                      {item.supersedesClaimId ? (
                        <small>替代 {item.supersedesClaimId}</small>
                      ) : null}
                    </details>
                  </li>
                ))}
              </ol>
            ) : (
              <Empty text="当前没有已发布的历史记录。" />
            )}
          </article>
          <article className="claim-panel">
            <Title title="科研谱系" note="来源不会自动晋级为数学事实" />
            <Unavailable text="当前部署没有按研究列出科研谱系的对象选择源。请从“科研谱系”页面选择已导入记录；在列表接口发布前，这里不提供手填入口。" />
          </article>
        </aside>
      </section>
      <section className="claim-grid claim-grid--forms">
        <SubmitClaimPanel
          run={run}
          gateway={gateway}
          graphGateway={graphGateway}
          sources={sources}
          busy={busy}
          onFailure={setFailure}
          onReceipt={setReceipt}
        />
        <VerificationPanel
          run={run}
          gateway={gateway}
          sources={sources}
          busy={busy}
          onFailure={setFailure}
          onReceipt={setReceipt}
        />
      </section>
    </main>
  );
}

function useObjectSources(gateway: ClaimGateway, run: RunFence): ObjectSources {
  const [state, setState] = useState<ObjectSources>({
    contexts: [],
    artifacts: [],
    reviews: [],
    contextState: "LOADING",
    artifactState: "LOADING",
    reviewState: "LOADING",
  });
  useEffect(() => {
    let active = true;
    void Promise.allSettled([
      gateway.queryWorkContexts(run.runId),
      gateway.queryArtifacts(run.runId),
      gateway.queryReviewTasks(run.runId),
    ]).then(([contexts, artifacts, reviews]) => {
      if (!active) return;
      setState({
        contexts: contexts.status === "fulfilled" ? contexts.value : [],
        artifacts: artifacts.status === "fulfilled" ? artifacts.value : [],
        reviews: reviews.status === "fulfilled" ? reviews.value : [],
        contextState: sourceState(contexts),
        artifactState: sourceState(artifacts),
        reviewState: sourceState(reviews),
      });
    });
    return () => {
      active = false;
    };
  }, [gateway, run.runId, run.revision]);
  return state;
}
function sourceState(result: PromiseSettledResult<unknown>): SourceState {
  if (result.status === "fulfilled") return "READY";
  return result.reason instanceof ClaimApiError && result.reason.unavailable
    ? "NOT_PUBLISHED"
    : "ERROR";
}

interface ClaimPickerProps {
  graphGateway: GraphGateway;
  run: RunFence;
  mode: GraphMode;
  selectedId: string;
  onSelect(id: string): void;
  label: string;
  description: string;
  multiple?: false;
}
function ClaimSearchPicker({
  graphGateway,
  run,
  mode,
  selectedId,
  onSelect,
  label,
  description,
}: ClaimPickerProps) {
  const [query, setQuery] = useState(""),
    [hits, setHits] = useState<GraphSearchHit[]>([]),
    [status, setStatus] = useState<SourceState>("READY"),
    [message, setMessage] = useState("");
  async function search() {
    setStatus("LOADING");
    setMessage("");
    try {
      const page = await graphGateway.search(
        { ...run, lastCursor: run.lastCursor ?? 0 },
        mode,
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
  const options = hits.map(claimOption);
  return (
    <ObjectPicker
      label={label}
      description={description}
      options={options}
      selectedIds={selectedId ? [selectedId] : []}
      onChange={(ids) => onSelect(ids[0] ?? "")}
      status={status}
      statusText={message}
      emptyText={
        query
          ? "没有匹配命题；可换一个陈述关键词。"
          : "输入关键词后搜索当前研究。"
      }
      search={{
        value: query,
        placeholder: "稳定标签或陈述关键词",
        onChange: setQuery,
        onSubmit: () => void search(),
      }}
    />
  );
}

interface PanelProps {
  run: RunFence;
  gateway: ClaimGateway;
  sources: ObjectSources;
  busy: boolean;
  onFailure: (v: FeatureFailure) => void;
  onReceipt: (v: CommandReceipt) => void;
}
function SubmitClaimPanel({
  run,
  gateway,
  graphGateway,
  sources,
  busy,
  onFailure,
  onReceipt,
}: PanelProps & { graphGateway: GraphGateway }) {
  const [statement, setStatement] = useState(""),
    [kind, setKind] = useState("THEOREM"),
    [predecessors, setPredecessors] = useState<string[]>([]),
    [supersedes, setSupersedes] = useState("");
  const [workContext, setWorkContext] = useState(""),
    [sourceId, setSourceId] = useState(""),
    [proofIds, setProofIds] = useState<string[]>([]),
    [uploaded, setUploaded] = useState<ArtifactChoice[]>([]),
    [uploading, setUploading] = useState(false);
  const artifacts = [...uploaded, ...sources.artifacts],
    artifactMap = new Map(artifacts.map((item) => [item.artifact_id, item]));
  const context = sources.contexts.find((item) => item.id === workContext),
    source = artifactMap.get(sourceId),
    proofs = proofIds.flatMap((id) => {
      const item = artifactMap.get(id);
      return item ? [item] : [];
    });
  async function upload(files: FileList | null, kind: "source" | "proof") {
    if (!files?.length) return;
    setUploading(true);
    try {
      const refs = await Promise.all(
        [...files].map(async (file) => ({
          file,
          ref: await gateway.upload(file),
        })),
      );
      const choices = refs.map(({ file, ref }) => ({
        ...ref,
        label: file.name,
        role: "本次上传",
        mediaType: file.type || "application/octet-stream",
        byteCount: file.size,
      }));
      setUploaded((current) => [...choices, ...current]);
      if (kind === "source") setSourceId(choices[0].artifact_id);
      else
        setProofIds((current) => [
          ...current,
          ...choices.map((item) => item.artifact_id),
        ]);
    } catch (error) {
      onFailure(toFailure(error));
    } finally {
      setUploading(false);
    }
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!context || !source || !proofs.length) return;
    try {
      onReceipt(
        await gateway.submitClaim(run, crypto.randomUUID(), {
          statement,
          claimKind: kind,
          predecessorFactIds: predecessors,
          workItemId: context.workItemId,
          workerRunId: context.workerRunId,
          attemptId: context.attemptId,
          sourceBindingArtifact: source,
          proofArtifacts: proofs,
          ...(supersedes ? { supersedesClaimId: supersedes } : {}),
        }),
      );
    } catch (error) {
      onFailure(toFailure(error));
    }
  }
  return (
    <form className="claim-panel" onSubmit={submit}>
      <Title title="提交修复命题" note="正式进入统一验证路由" />
      <label>
        数学陈述
        <textarea
          required
          value={statement}
          onChange={(event) => setStatement(event.target.value)}
        />
      </label>
      <label>
        命题类型
        <select
          required
          value={kind}
          onChange={(event) => setKind(event.target.value)}
        >
          <option value="THEOREM">定理</option>
          <option value="LEMMA">引理</option>
          <option value="DEFINITION">定义</option>
          <option value="COUNTEREXAMPLE">反例</option>
          <option value="ROOT">最终结论</option>
        </select>
      </label>
      <GraphMultiPicker
        graphGateway={graphGateway}
        run={run}
        mode="VERIFIED"
        selectedIds={predecessors}
        onChange={setPredecessors}
        label="前驱已验证事实"
        description="搜索并选择当前研究中可依赖的前驱；内部标识仅在技术详情可见。"
      />
      <ObjectPicker
        label="本次执行上下文"
        description="一次选择同时绑定研究任务、一次执行和第 N 次运行。"
        options={sources.contexts.map((item) => ({
          id: item.id,
          label: item.label,
          description: item.description,
          meta: item.state,
        }))}
        selectedIds={workContext ? [workContext] : []}
        onChange={(ids) => setWorkContext(ids[0] ?? "")}
        status={sources.contextState}
        statusText="WORKFLOW 尚未发布结构化执行对象；请先到路线与执行页面启动真实工作。"
      />
      <ObjectPicker
        label="来源工件"
        description="从当前研究的工件索引选择，或上传本次来源文件。"
        options={artifacts.map(artifactOption)}
        selectedIds={sourceId ? [sourceId] : []}
        onChange={(ids) => setSourceId(ids[0] ?? "")}
        status={sources.artifactState}
        statusText="当前研究尚未发布工件索引；可上传真实来源文件。"
      />
      <label className="claim-file">
        上传来源文件
        <input
          type="file"
          onChange={(event) => void upload(event.target.files, "source")}
        />
      </label>
      <ObjectPicker
        label="证明或证据工件"
        description="可多选已提交工件，或上传新的证明与证据文件。"
        options={artifacts.map(artifactOption)}
        selectedIds={proofIds}
        onChange={setProofIds}
        multiple
        status={sources.artifactState}
        statusText="当前研究尚未发布工件索引；可上传真实证明文件。"
      />
      <label className="claim-file">
        上传证明或证据
        <input
          type="file"
          multiple
          onChange={(event) => void upload(event.target.files, "proof")}
        />
      </label>
      <ClaimSearchPicker
        graphGateway={graphGateway}
        run={run}
        mode="RESEARCH_HISTORY"
        selectedId={supersedes}
        onSelect={setSupersedes}
        label="替代被拒命题（可选）"
        description="只有确实修复旧命题时才选择；不会通过手填建立替代关系。"
      />
      <button
        disabled={
          busy ||
          uploading ||
          !statement.trim() ||
          !context ||
          !source ||
          proofs.length === 0
        }
      >
        {uploading ? "正在提交工件…" : "提交候选并进入验证路由"}
      </button>
    </form>
  );
}

function VerificationPanel({
  run,
  gateway,
  sources,
  busy,
  onFailure,
  onReceipt,
}: PanelProps) {
  const [taskId, setTaskId] = useState("");
  const task = sources.reviews.find((item) => item.id === taskId),
    ready = Boolean(
      task?.signedArtifact &&
        task.targetDigest &&
        task.verifierReceiptIds.length,
    );
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (
      !task?.signedArtifact ||
      !task.targetDigest ||
      !task.verifierReceiptIds.length
    )
      return;
    try {
      onReceipt(
        await gateway.importVerification(run, crypto.randomUUID(), {
          reviewTaskId: task.id,
          signedReviewArtifact: task.signedArtifact,
          targetDigest: task.targetDigest,
          verifierReceiptIds: task.verifierReceiptIds,
        }),
      );
    } catch (error) {
      onFailure(toFailure(error));
    }
  }
  return (
    <form className="claim-panel" onSubmit={submit}>
      <Title title="导入验证回执" note="只有完整的独立审查绑定才能提交" />
      <ObjectPicker
        label="可导入的独立审查"
        description="选择审查任务后自动绑定目标摘要、签名工件与 verifier 回执。"
        options={sources.reviews.map((item) => ({
          id: item.id,
          label: item.label,
          description: item.description,
          meta:
            item.signedArtifact && item.verifierReceiptIds.length
              ? "绑定完整"
              : "等待完整回执",
        }))}
        selectedIds={taskId ? [taskId] : []}
        onChange={(ids) => setTaskId(ids[0] ?? "")}
        status={sources.reviewState}
        statusText="当前身份没有可导入审查，或 REVIEW_INBOX 对象源尚未发布。请在“人工审查”页面完成独立审查。"
      />
      {task && !ready ? (
        <Unavailable text="该审查任务尚未同时发布签名工件与 verifier 回执。这里不会让用户手填摘要或回执标识来绕过绑定。" />
      ) : null}
      <p className="claim-callout">
        工具调用完成不会自动变成验证接受；只有内核接受事件才改变数学权威。
      </p>
      <button disabled={busy || !ready}>提交已绑定的签名验证</button>
    </form>
  );
}

function GraphMultiPicker({
  graphGateway,
  run,
  mode,
  selectedIds,
  onChange,
  label,
  description,
}: {
  graphGateway: GraphGateway;
  run: RunFence;
  mode: GraphMode;
  selectedIds: string[];
  onChange(ids: string[]): void;
  label: string;
  description: string;
}) {
  const [query, setQuery] = useState(""),
    [hits, setHits] = useState<GraphSearchHit[]>([]),
    [status, setStatus] = useState<SourceState>("READY"),
    [message, setMessage] = useState("");
  async function search() {
    setStatus("LOADING");
    try {
      const page = await graphGateway.search(
        { ...run, lastCursor: run.lastCursor ?? 0 },
        mode,
        query.trim(),
      );
      setHits(page.items);
      setStatus("READY");
      setMessage(
        page.total > page.items.length
          ? `显示 ${page.items.length} / ${page.total} 条`
          : "",
      );
    } catch (error) {
      setStatus("ERROR");
      setMessage(error instanceof Error ? error.message : "事实图搜索失败");
    }
  }
  return (
    <ObjectPicker
      label={label}
      description={description}
      options={hits.map(claimOption)}
      selectedIds={selectedIds}
      onChange={onChange}
      multiple
      status={status}
      statusText={message}
      emptyText={query ? "没有匹配事实。" : "输入关键词后搜索当前研究。"}
      search={{
        value: query,
        placeholder: "稳定标签或陈述关键词",
        onChange: setQuery,
        onSubmit: () => void search(),
      }}
    />
  );
}
function claimOption(item: GraphSearchHit): ObjectPickerOption {
  return {
    id: item.claim_id,
    label: item.stable_label,
    description: item.statement,
    meta: item.dependable ? "可依赖" : item.lifecycle,
  };
}
function artifactOption(item: ArtifactChoice): ObjectPickerOption {
  return {
    id: item.artifact_id,
    label: item.label,
    description: `${item.role} · ${item.mediaType}`,
    meta: formatBytes(item.byteCount),
  };
}
function formatBytes(value: number) {
  return value < 1024
    ? `${value} B`
    : value < 1024 * 1024
      ? `${(value / 1024).toFixed(1)} KB`
      : `${(value / 1024 / 1024).toFixed(1)} MB`;
}
function Title({ title, note }: { title: string; note: string }) {
  return (
    <header className="claim-title">
      <div>
        <h2>{title}</h2>
        <p>{note}</p>
      </div>
    </header>
  );
}
function Rows({ rows }: { rows: [string, string][] }) {
  return (
    <dl className="claim-rows">
      {rows.map(([key, value]) => (
        <div key={key}>
          <dt>{key}</dt>
          <dd>{value || "—"}</dd>
        </div>
      ))}
    </dl>
  );
}
function Empty({ text }: { text: string }) {
  return <p className="claim-empty">{text}</p>;
}
function Unavailable({ text }: { text: string }) {
  return (
    <div className="claim-unavailable">
      <b>对象源尚未发布</b>
      <p>{text}</p>
    </div>
  );
}
function Failure({ failure }: { failure: FeatureFailure }) {
  return (
    <div className="claim-failure" role="alert">
      <b>{failure.unavailable ? "能力不可用" : "请求失败"}</b>
      <span>{failure.action}</span>
      <details>
        <summary>技术详情</summary>
        <code>{failure.code}</code>
        <p>{failure.message}</p>
      </details>
    </div>
  );
}
function toFailure(error: unknown): FeatureFailure {
  return error instanceof ClaimApiError
    ? error.toFailure()
    : {
        code: "CLIENT_FAILURE",
        message: String(error),
        unavailable: false,
        action: "检查选择对象与当前研究修订后重试。",
      };
}

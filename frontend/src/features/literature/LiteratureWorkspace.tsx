import { type FormEvent, useMemo, useState } from "react";
import { LiteratureApiError, LiteratureGateway } from "./api.js";
import {
  connectorLabel,
  type ConnectorId,
  type FeatureFailure,
  type LiteratureQueryDraft,
  type QueryReceipt,
  type RunFence,
} from "./model.js";
import "./literature.css";

const CONNECTORS: ConnectorId[] = ["OPENALEX", "CROSSREF", "ARXIV", "MATLAS"];
type Tab = "SEARCH" | "SOURCES" | "NOVELTY";
export interface LiteratureWorkspaceProps {
  run: RunFence;
  baseUrl?: string;
}

export function LiteratureWorkspace({
  run,
  baseUrl = "",
}: LiteratureWorkspaceProps) {
  const gateway = useMemo(() => new LiteratureGateway(baseUrl), [baseUrl]);
  const [tab, setTab] = useState<Tab>("SEARCH");
  const [draft, setDraft] = useState<LiteratureQueryDraft>({
    researchQuestion: "",
    queryText: "",
    coverageBoundary: "",
    connectors: ["OPENALEX", "CROSSREF", "ARXIV"],
    targetEntityIds: [],
  });
  const [receipt, setReceipt] = useState<QueryReceipt>();
  const [failure, setFailure] = useState<FeatureFailure>();
  const [busy, setBusy] = useState(false);
  function toggleConnector(connector: ConnectorId) {
    setDraft((current) => ({
      ...current,
      connectors: current.connectors.includes(connector)
        ? current.connectors.filter((item) => item !== connector)
        : [...current.connectors, connector],
    }));
  }
  async function runSearch(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setFailure(undefined);
    try {
      setReceipt(
        await gateway.runLiteratureQuery(run, crypto.randomUUID(), draft),
      );
    } catch (error) {
      setFailure(toFailure(error));
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="lit-shell" aria-busy={busy}>
      <header className="lit-hero">
        <div>
          <p>文献与新颖性</p>
          <h1>多源文献工作台</h1>
          <span>
            检索用于定位来源；命中、无命中和工具成功都不会产生数学事实或新颖性结论。
          </span>
        </div>
        <small>
          研究 · {shortId(run.runId)} · r{run.revision} · 合同 v
          {run.contractVersion}
        </small>
      </header>
      <nav className="lit-tabs" aria-label="文献工作台">
        <TabButton value="SEARCH" current={tab} onSelect={setTab}>
          检索
        </TabButton>
        <TabButton value="SOURCES" current={tab} onSelect={setTab}>
          来源库
        </TabButton>
        <TabButton value="NOVELTY" current={tab} onSelect={setTab}>
          新颖性审查
        </TabButton>
      </nav>
      {failure && <FailureBanner failure={failure} />}
      {tab === "SEARCH" && (
        <SearchTab
          draft={draft}
          busy={busy}
          receipt={receipt}
          setDraft={setDraft}
          toggleConnector={toggleConnector}
          onSubmit={runSearch}
        />
      )}
      {tab === "SOURCES" && <SourceLibrary receipt={receipt} />}
      {tab === "NOVELTY" && <NoveltyReview />}
    </main>
  );
}

function SearchTab({
  draft,
  busy,
  receipt,
  setDraft,
  toggleConnector,
  onSubmit,
}: {
  draft: LiteratureQueryDraft;
  busy: boolean;
  receipt?: QueryReceipt;
  setDraft: (value: LiteratureQueryDraft) => void;
  toggleConnector: (value: ConnectorId) => void;
  onSubmit: (event: FormEvent) => void;
}) {
  return (
    <section className="lit-workspace">
      <form className="lit-panel lit-search" onSubmit={onSubmit}>
        <header>
          <h2>检索问题</h2>
          <p>每次检索都必须声明覆盖边界，并保存原始响应快照。</p>
        </header>
        <label>
          研究问题
          <textarea
            required
            value={draft.researchQuestion}
            onChange={(event) =>
              setDraft({ ...draft, researchQuestion: event.target.value })
            }
            placeholder="要排查的结论、数学对象和关键假设"
          />
        </label>
        <label>
          检索式
          <input
            required
            value={draft.queryText}
            onChange={(event) =>
              setDraft({ ...draft, queryText: event.target.value })
            }
            placeholder="关键词、作者、定理名或符号变体"
          />
        </label>
        <label>
          覆盖边界
          <input
            required
            value={draft.coverageBoundary}
            onChange={(event) =>
              setDraft({ ...draft, coverageBoundary: event.target.value })
            }
            placeholder="数据库、时间、语言、学科和版本范围"
          />
        </label>
        <fieldset>
          <legend>来源服务</legend>
          <div className="lit-connectors">
            {CONNECTORS.map((connector) => (
              <label
                className={draft.connectors.includes(connector) ? "is-on" : ""}
                key={connector}
              >
                <input
                  type="checkbox"
                  checked={draft.connectors.includes(connector)}
                  onChange={() => toggleConnector(connector)}
                />
                <span>{connectorLabel(connector)}</span>
                <small>{connectorSource(connector)}</small>
              </label>
            ))}
          </div>
        </fieldset>
        <button
          className="lit-primary"
          disabled={busy || !draft.connectors.length}
        >
          {busy ? "提交中…" : "运行在线检索"}
        </button>
      </form>
      <aside className="lit-panel lit-boundary">
        <header>
          <h2>来源与权威边界</h2>
          <p>外部标识符必须连同来源系统解释。</p>
        </header>
        <dl>
          <div>
            <dt>arXiv 标识符</dt>
            <dd>来自 arXiv，必须包含精确版本。</dd>
          </div>
          <div>
            <dt>DOI</dt>
            <dd>由 Crossref 返回，需绑定来源快照。</dd>
          </div>
          <div>
            <dt>OpenAlex Work ID</dt>
            <dd>来自 OpenAlex 元数据图。</dd>
          </div>
          <div>
            <dt>Matlas theorem_id</dt>
            <dd>来自外部 Matlas 薄客户端，仅表示定理候选。</dd>
          </div>
        </dl>
        <p className="lit-warning">
          当前部署不包含 Matlas 服务端、语料、依赖图或向量索引。
        </p>
      </aside>
      {receipt && (
        <div className="lit-receipt" role="status">
          <div>
            <b>检索命令已受理</b>
            <span>{receipt.state}</span>
          </div>
          <details>
            <summary>技术详情</summary>
            <dl>
              <div>
                <dt>回执标识</dt>
                <dd>{receipt.receiptId}</dd>
              </div>
              {receipt.jobId && (
                <div>
                  <dt>批任务标识</dt>
                  <dd>{receipt.jobId}</dd>
                </div>
              )}
            </dl>
          </details>
          <p>
            结果对象列表尚未由当前服务发布；命令受理不代表检索成功，也不代表发现新颖结果。
          </p>
        </div>
      )}
    </section>
  );
}

function SourceLibrary({ receipt }: { receipt?: QueryReceipt }) {
  return (
    <section className="lit-panel lit-empty-page">
      <header>
        <h2>来源库</h2>
        <p>用于选择已导入来源、精确版本、快照和原文锚点。</p>
      </header>
      <Unavailable
        title="来源列表尚未发布"
        text={
          receipt
            ? "已有检索回执，但当前 QuerySpec 没有返回可枚举的 LiteratureSource / SourceSnapshot 列表。这里不会提供内部 ID 手填入口。"
            : "先在“检索”页提交真实检索。当前服务仍需发布按研究加载的 LiteratureSource / SourceSnapshot 列表后，才能在此选择来源。"
        }
      />
      <div className="lit-source-schema">
        <h3>列表发布后必须显示</h3>
        <ul>
          <li>题名、作者和精确版本</li>
          <li>来源系统及外部标识符类型</li>
          <li>在线查询或快照重放状态</li>
          <li>原文锚点与内容摘要</li>
          <li>冲突来源和去重组</li>
        </ul>
      </div>
    </section>
  );
}

function NoveltyReview() {
  return (
    <section className="lit-panel lit-empty-page">
      <header>
        <h2>新颖性审查</h2>
        <p>由独立文献审查者基于已选来源和覆盖快照逐项确认。</p>
      </header>
      <Unavailable
        title="尚无可选择的审查对象"
        text="当前服务未发布按研究加载的 Claim、来源、适用性对照和文献审查任务列表，因此无法构造合法的 ObjectPicker。页面不会要求用户抄写 Claim ID、摘要或审查人 ID。"
      />
      <div className="lit-principles">
        <article>
          <b>无命中</b>
          <p>只能说明本次覆盖范围内未返回记录。</p>
        </article>
        <article>
          <b>Matlas 命中</b>
          <p>只是定理候选，需回到精确 arXiv 版本核对。</p>
        </article>
        <article>
          <b>独立审查</b>
          <p>只确认检索边界，不成为第二数学真值。</p>
        </article>
      </div>
    </section>
  );
}

function TabButton({
  value,
  current,
  onSelect,
  children,
}: {
  value: Tab;
  current: Tab;
  onSelect: (value: Tab) => void;
  children: string;
}) {
  return (
    <button
      type="button"
      aria-current={current === value ? "page" : undefined}
      onClick={() => onSelect(value)}
    >
      {children}
    </button>
  );
}
function Unavailable({ title, text }: { title: string; text: string }) {
  return (
    <div className="lit-unavailable">
      <b>{title}</b>
      <p>{text}</p>
    </div>
  );
}
function FailureBanner({ failure }: { failure: FeatureFailure }) {
  return (
    <div
      className={"lit-failure " + (failure.unavailable ? "is-unavailable" : "")}
      role="alert"
    >
      <strong>{failure.unavailable ? "能力暂不可用" : "请求未完成"}</strong>
      <code>{failure.code}</code>
      <span>{failure.message}</span>
      <p>{failure.action}</p>
    </div>
  );
}
function connectorSource(value: ConnectorId) {
  return value === "ARXIV"
    ? "arXiv · 精确 ID 与版本"
    : value === "MATLAS"
      ? "外部 Matlas · 定理候选"
      : value === "OPENALEX"
        ? "OpenAlex · Work / Author"
        : "Crossref · DOI 元数据";
}
function shortId(value: string) {
  return value.length <= 6 ? value : value.slice(-6);
}
function toFailure(error: unknown): FeatureFailure {
  if (error instanceof LiteratureApiError) return error.toFailure();
  return {
    code: "CLIENT_FAILURE",
    message: error instanceof Error ? error.message : String(error),
    unavailable: false,
    action: "检查检索条件和服务回执；不要把失败解释为检索完成。",
  };
}

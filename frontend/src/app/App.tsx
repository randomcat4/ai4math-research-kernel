import { type FormEvent, useEffect, useMemo, useState } from "react";

import { Icon } from "../design/Icon";
import { MonospaceValue, StatusMark } from "../design/StatusMark";
import { LiteratureWorkspace } from "../features/literature/LiteratureWorkspace";
import { ResearchWorkspace } from "../features/research/ResearchWorkspace";
import { RuntimeStatusBar } from "../features/runtime";
import { PublishedWorkspaces } from "./PublishedWorkspaces";
import type { ResearchSummary } from "./api";
import { useProductConnection } from "./useProductConnection";

const navigation = [
  ["research", "研究"],
  ["literature", "文献与新颖性"],
  ["routes", "路线与任务"],
  ["facts", "事实与谱系"],
  ["tools", "计算与工具"],
  ["review", "人工与审查"],
  ["dossier", "状态卷宗"],
  ["admin", "管理"],
] as const;

const evidenceStages = [
  {
    key: "claim",
    kicker: "CANDIDATE",
    title: "候选 Claim",
    note: "逐个原子陈述进入验证门，不因模型或工具成功而变绿。",
  },
  {
    key: "verification",
    kicker: "VERIFY",
    title: "验证与审查",
    note: "六轴结果、工具回执与独立审查保持分栏。",
  },
  {
    key: "revocation",
    kicker: "REVOKE",
    title: "撤销与影响闭包",
    note: "保留旧版本、撤销理由、替代 Claim 与受影响路径。",
  },
  {
    key: "root",
    kicker: "ROOT",
    title: "ROOT 与终态链",
    note: "ClosureWitness、唯一 ROOT、Finalize、整篇复核与编译依次闭合。",
  },
] as const;

function statusTone(value: string | undefined) {
  if (value === "PROVED" || value === "AVAILABLE" || value === "RUNNING") return "jade";
  if (value === "FAILED" || value === "DISPROVED") return "vermilion";
  if (value === "PAUSED" || value === "WAITING" || value === "UNRESOLVED") return "ochre";
  return "neutral";
}

function displayAction(value: unknown): string {
  if (typeof value === "string") return value.replaceAll("_", " ");
  if (typeof value === "object" && value !== null) {
    const record = value as Record<string, unknown>;
    const label = record.label ?? record.type ?? record.action;
    if (typeof label === "string") return label.replaceAll("_", " ");
  }
  return "查看结构化待办";
}

function LoginPanel({
  onLogin,
  busy,
}: {
  onLogin: (identityId: string, secret: string) => Promise<void>;
  busy: boolean;
}) {
  const [identityId, setIdentityId] = useState("");
  const [secret, setSecret] = useState("");

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    await onLogin(identityId, secret);
    setSecret("");
  };

  return (
    <form className="login-panel" onSubmit={(event) => void submit(event)}>
      <div className="section-kicker">IDENTITY SESSION</div>
      <h2>以托管身份进入</h2>
      <p>角色与权限由服务端身份决定，页面不接收 capability。</p>
      <label>
        身份编号
        <input
          autoComplete="username"
          onChange={(event) => setIdentityId(event.target.value)}
          required
          value={identityId}
        />
      </label>
      <label>
        登录密钥
        <input
          autoComplete="current-password"
          onChange={(event) => setSecret(event.target.value)}
          required
          type="password"
          value={secret}
        />
      </label>
      <button className="primary-button" disabled={busy} type="submit">
        {busy ? "正在建立会话…" : "建立会话"}
      </button>
    </form>
  );
}

function ResearchPicker({
  items,
  selected,
  onSelect,
  loading,
}: {
  items: ResearchSummary[];
  selected: ResearchSummary | null;
  onSelect: (runId: string) => void;
  loading: boolean;
}) {
  return (
    <label className="research-picker">
      <span>当前研究</span>
      <select
        disabled={loading || items.length === 0}
        onChange={(event) => onSelect(event.target.value)}
        value={selected?.run_id ?? ""}
      >
        {items.length === 0 ? (
          <option value="">{loading ? "正在读取研究…" : "尚无可见研究"}</option>
        ) : null}
        {items.map((item) => (
          <option key={item.run_id} value={item.run_id}>
            {item.title}
          </option>
        ))}
      </select>
    </label>
  );
}

function EvidenceSpine({ research }: { research: ResearchSummary | null }) {
  return (
    <div className="evidence-spine" aria-label="本轮证据脊柱">
      <div className="spine-ruler" aria-hidden="true">
        <div className="ruler-head">
          <span>REV</span>
          <strong>{research ? research.research_revision : "—"}</strong>
        </div>
        <div className="ruler-track" />
        <div className="ruler-foot">
          <span>CURSOR</span>
          <strong>{research ? research.last_cursor : "—"}</strong>
        </div>
      </div>
      <ol className="spine-stages">
        {evidenceStages.map((stage, index) => (
          <li className="spine-stage" key={stage.key}>
            <div className="spine-tick">
              <span>{String(index + 1).padStart(2, "0")}</span>
            </div>
            <article className="evidence-entry">
              <header>
                <span className="section-kicker">{stage.kicker}</span>
                <StatusMark tone="neutral">未载入</StatusMark>
              </header>
              <h3>{stage.title}</h3>
              <p>{stage.note}</p>
              <div className="entry-empty">
                {research
                  ? "证据查询尚未执行；不会用示例节点填充当前研究。"
                  : "选择真实研究后读取这一段证据链。"}
              </div>
            </article>
          </li>
        ))}
      </ol>
    </div>
  );
}

export function App() {
  const connection = useProductConnection();
  const [activeNav, setActiveNav] = useState("research");
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [view, setView] = useState<"graph" | "list">("graph");
  const [researchView, setResearchView] = useState<"evidence" | "setup">("evidence");
  const [loginBusy, setLoginBusy] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(true);

  useEffect(() => {
    if (!selectedRunId && connection.research.length > 0) {
      setSelectedRunId(connection.research[0].run_id);
    }
  }, [connection.research, selectedRunId]);

  const selectedResearch = useMemo(
    () => connection.research.find((item) => item.run_id === selectedRunId) ?? null,
    [connection.research, selectedRunId],
  );

  const login = async (identityId: string, secret: string) => {
    setLoginBusy(true);
    try {
      await connection.login(identityId, secret);
    } finally {
      setLoginBusy(false);
    }
  };

  const primaryAction = connection.phase === "offline"
    ? "重试连接"
    : !connection.session
      ? "建立身份会话"
      : selectedResearch?.next_actions.length
        ? displayAction(selectedResearch.next_actions[0])
        : "查看路线与任务";

  return (
    <div className="app-shell">
      <a className="skip-link" href="#research-workspace">跳到研究工作区</a>
      <aside className="global-rail">
        <div className="brand-lockup">
          <div className="brand-mark">RK</div>
          <div>
            <strong>研究指挥台</strong>
            <span>MATHEMATICAL EVIDENCE</span>
          </div>
        </div>
        <nav aria-label="一级导航">
          {navigation.map(([key, label], index) => (
            <button
              aria-current={activeNav === key ? "page" : undefined}
              className={activeNav === key ? "nav-item active" : "nav-item"}
              key={key}
              onClick={() => setActiveNav(key)}
              type="button"
            >
              <span className="nav-index">{String(index + 1).padStart(2, "0")}</span>
              <Icon name={key} />
              <span>{label}</span>
            </button>
          ))}
        </nav>
        <div className="rail-foot">
          <span>RK-PRODUCT-1.1</span>
          <span>{connection.meta?.deployment_id ?? "deployment —"}</span>
        </div>
      </aside>

      <div className="main-stage" id="research-workspace" role="main">
        <header className="context-bar">
          <ResearchPicker
            items={connection.research}
            loading={connection.researchLoading}
            onSelect={setSelectedRunId}
            selected={selectedResearch}
          />
          <div className="context-facts" aria-label="研究上下文">
            <span>
              合同 <MonospaceValue>v{selectedResearch?.contract_version ?? "—"}</MonospaceValue>
            </span>
            <span>
              修订 <MonospaceValue>r{selectedResearch?.research_revision ?? "—"}</MonospaceValue>
            </span>
            <StatusMark tone={statusTone(selectedResearch?.outcome_state)}>
              {selectedResearch?.outcome_state ?? "NO RUN"}
            </StatusMark>
            <StatusMark tone={statusTone(selectedResearch?.execution_state)}>
              {selectedResearch?.execution_state ?? "IDLE"}
            </StatusMark>
          </div>
          <div className="identity-context">
            <span className={`connection-dot ${connection.phase}`} />
            <div>
              <strong>
                {connection.phase === "connected" ? "ResearchProduct 已连接" : "尚未连接"}
              </strong>
              <span>
                {connection.session
                  ? `${connection.session.display_name} · ${connection.session.role}`
                  : connection.phase === "connecting"
                    ? "正在确认部署与会话"
                    : "无有效身份会话"}
              </span>
            </div>
          </div>
        </header>

        {connection.error ? (
          <div className="service-notice" role="alert">
            <span>{connection.error}</span>
            <button onClick={() => void connection.retry()} type="button">重新连接</button>
          </div>
        ) : null}

        <RuntimeStatusBar
          runId={selectedResearch?.run_id}
          snapshotCursor={selectedResearch?.last_cursor ?? 0}
          onReloadProjections={connection.refreshResearch}
        />

        <section className="round-header">
          <div>
            <div className="section-kicker">CURRENT EVIDENCE ROUND</div>
            <h1>{selectedResearch?.title ?? "本轮证据链尚未展开"}</h1>
            <p>
              {selectedResearch?.question_summary ??
                "连接守护进程并选择研究后，这里按 revision 与 cursor 还原候选、验证、撤销和 ROOT。"}
            </p>
          </div>
          <button
            className="primary-button round-action"
            onClick={() => {
              if (connection.phase === "offline") void connection.retry();
              else setActiveNav(connection.session ? "routes" : "research");
            }}
            type="button"
          >
            <span>下一动作</span>
            <strong>{primaryAction}</strong>
            <span aria-hidden="true">→</span>
          </button>
        </section>

        {activeNav === "research" ? (
          <div className="research-subnav" aria-label="研究二级视图">
            <button
              aria-current={researchView === "evidence" ? "page" : undefined}
              onClick={() => setResearchView("evidence")}
              type="button"
            >
              本轮证据
            </button>
            <button
              aria-current={researchView === "setup" ? "page" : undefined}
              onClick={() => setResearchView("setup")}
              type="button"
            >
              合同与材料
            </button>
          </div>
        ) : null}

        {activeNav === "research" && researchView === "setup" ? (
          connection.meta ? (
            <div className="feature-mount">
              <ResearchWorkspace
                contractVersion={selectedResearch?.contract_version}
                deploymentId={connection.meta.deployment_id}
                initialRunId={selectedResearch?.run_id}
                researchRevision={selectedResearch?.research_revision}
              />
            </div>
          ) : (
            <div className="feature-connection-empty">连接 ResearchProduct 后进入合同与材料。</div>
          )
        ) : null}

        {activeNav === "literature" ? (
          selectedResearch ? (
            <div className="feature-mount">
              <LiteratureWorkspace
                run={{
                  runId: selectedResearch.run_id,
                  revision: selectedResearch.research_revision,
                  contractVersion: selectedResearch.contract_version,
                }}
              />
            </div>
          ) : (
            <div className="feature-connection-empty">选择真实研究后进入文献与新颖性工作台。</div>
          )
        ) : null}

        <PublishedWorkspaces
          activeNav={activeNav}
          research={selectedResearch ?? undefined}
          session={connection.session}
          meta={connection.meta}
          onReload={connection.refreshResearch}
        />

        <div
          className={
            activeNav === "research" && researchView === "evidence"
              ? "workspace-grid"
              : "workspace-grid feature-hidden"
          }
        >
          <section className="evidence-workspace">
            <div className="workspace-toolbar">
              <div>
                <span className="section-kicker">EVIDENCE SPINE</span>
                <strong>研究记忆 · 局部证据</strong>
              </div>
              <div className="view-tabs" role="tablist" aria-label="证据视图">
                <button
                  aria-selected={view === "graph"}
                  onClick={() => setView("graph")}
                  role="tab"
                  type="button"
                >
                  图
                </button>
                <button
                  aria-selected={view === "list"}
                  onClick={() => setView("list")}
                  role="tab"
                  type="button"
                >
                  列表
                </button>
              </div>
            </div>
            {view === "graph" ? (
              <EvidenceSpine research={selectedResearch} />
            ) : (
              <div className="evidence-list-empty">
                <span className="empty-glyph">∅</span>
                <h2>没有已载入的 Claim 记录</h2>
                <p>列表与图共享同一查询结果；拒绝、撤销和有效事实不会混用颜色。</p>
              </div>
            )}
          </section>

          <aside className="claim-inspector">
            {connection.sessionRequired ? (
              <LoginPanel busy={loginBusy} onLogin={login} />
            ) : (
              <>
                <div className="inspector-heading">
                  <span className="section-kicker">CLAIM INSPECTOR</span>
                  <StatusMark>未选择</StatusMark>
                </div>
                <h2>Claim 检查器</h2>
                <div className="formula-field" aria-label="Claim 公式原文空态">
                  <span>statement</span>
                  <p>选择证据脊柱中的真实 Claim 后显示原始数学陈述。</p>
                </div>
                <dl className="claim-fields">
                  <div><dt>stable label</dt><dd>—</dd></div>
                  <div><dt>合同版本</dt><dd>—</dd></div>
                  <div><dt>来源工作项</dt><dd>—</dd></div>
                  <div><dt>权威上限</dt><dd>—</dd></div>
                </dl>
                <div className="axis-preview">
                  <h3>六轴验证</h3>
                  {["定义与类型", "量词与假设", "逻辑有效性", "计算回执", "来源与适用性", "独立审查"].map(
                    (axis) => (
                      <div key={axis}>
                        <span>{axis}</span>
                        <MonospaceValue>NOT LOADED</MonospaceValue>
                      </div>
                    ),
                  )}
                </div>
              </>
            )}
            {connection.session ? (
              <button className="text-button inspector-logout" onClick={() => void connection.logout()} type="button">
                退出当前身份
              </button>
            ) : null}
          </aside>
        </div>

        <section
          className={
            activeNav === "research" && researchView === "evidence"
              ? drawerOpen
                ? "work-drawer open"
                : "work-drawer"
              : "work-drawer feature-hidden"
          }
        >
          <button
            aria-expanded={drawerOpen}
            className="drawer-handle"
            onClick={() => setDrawerOpen((current) => !current)}
            type="button"
          >
            <span><span className="live-pip" /> 工作项与 Worker 活动</span>
            <strong>{selectedResearch ? "尚未载入活动流" : "等待研究上下文"}</strong>
            <span aria-hidden="true">{drawerOpen ? "↓" : "↑"}</span>
          </button>
          {drawerOpen ? (
            <div className="drawer-content">
              <div className="activity-rules">
                <span>路线</span><i /> <span>里程碑</span><i /> <span>工作项</span><i />
                <span>worker run</span><i /> <span>attempt</span>
              </div>
              <p>只有真实 worker_run 生命周期事件才会出现在这里；日志文字不会被猜成进度。</p>
            </div>
          ) : null}
        </section>
      </div>
    </div>
  );
}

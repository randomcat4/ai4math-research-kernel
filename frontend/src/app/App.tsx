import { type FormEvent, useEffect, useMemo, useState } from "react";

import { Icon } from "../design/Icon";
import {
  executionStatusTone,
  MonospaceValue,
  outcomeStatusTone,
  StatusMark,
} from "../design/StatusMark";
import { LiteratureWorkspace } from "../features/literature/LiteratureWorkspace";
import { ResearchWorkspace } from "../features/research/ResearchWorkspace";
import { RuntimeStatusBar } from "../features/runtime";
import { PublishedWorkspaces } from "./PublishedWorkspaces";
import { ResearchOverview } from "./ResearchOverview";
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

export function App() {
  const connection = useProductConnection();
  const [activeNav, setActiveNav] = useState("research");
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [researchView, setResearchView] = useState<"evidence" | "setup">(
    "evidence",
  );
  const [loginBusy, setLoginBusy] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);

  useEffect(() => {
    if (!selectedRunId && connection.research.length > 0) {
      setSelectedRunId(connection.research[0].run_id);
    }
  }, [connection.research, selectedRunId]);

  const selectedResearch = useMemo(
    () =>
      connection.research.find((item) => item.run_id === selectedRunId) ?? null,
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

  return (
    <div className="app-shell">
      <a className="skip-link" href="#research-workspace">
        跳到研究工作区
      </a>
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
              <span className="nav-index">
                {String(index + 1).padStart(2, "0")}
              </span>
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
              合同{" "}
              <MonospaceValue>
                v{selectedResearch?.contract_version ?? "—"}
              </MonospaceValue>
            </span>
            <span>
              修订{" "}
              <MonospaceValue>
                r{selectedResearch?.research_revision ?? "—"}
              </MonospaceValue>
            </span>
            <span className="context-axis">
              <span>结果</span>
              <StatusMark
                tone={outcomeStatusTone(selectedResearch?.outcome_state)}
              >
                {selectedResearch?.outcome_state ?? "NO RUN"}
              </StatusMark>
            </span>
            <span className="context-axis">
              <span>执行</span>
              <StatusMark
                tone={executionStatusTone(selectedResearch?.execution_state)}
              >
                {selectedResearch?.execution_state ?? "IDLE"}
              </StatusMark>
            </span>
          </div>
          <div className="identity-context">
            <span className={`connection-dot ${connection.phase}`} />
            <div>
              <strong>
                {connection.phase === "connected"
                  ? "ResearchProduct 已连接"
                  : "尚未连接"}
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
            <button onClick={() => void connection.retry()} type="button">
              重新连接
            </button>
          </div>
        ) : null}

        <RuntimeStatusBar
          runId={selectedResearch?.run_id}
          snapshotCursor={selectedResearch?.last_cursor ?? 0}
          onReloadProjections={connection.refreshResearch}
        />

        {connection.sessionRequired ? (
          <section className="login-gate" aria-label="登录">
            <LoginPanel busy={loginBusy} onLogin={login} />
          </section>
        ) : (
          <ResearchOverview
            research={selectedResearch}
            onNavigate={setActiveNav}
          />
        )}

        {!connection.sessionRequired && activeNav === "research" ? (
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

        {!connection.sessionRequired &&
        activeNav === "research" &&
        researchView === "setup" ? (
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
            <div className="feature-connection-empty">
              连接 ResearchProduct 后进入合同与材料。
            </div>
          )
        ) : null}

        {!connection.sessionRequired && activeNav === "literature" ? (
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
            <div className="feature-connection-empty">
              选择真实研究后进入文献与新颖性工作台。
            </div>
          )
        ) : null}

        {!connection.sessionRequired ? (
          <PublishedWorkspaces
            activeNav={activeNav}
            research={selectedResearch ?? undefined}
            session={connection.session}
            meta={connection.meta}
            onReload={connection.refreshResearch}
          />
        ) : null}

        <section
          className={
            !connection.sessionRequired &&
            activeNav === "research" &&
            researchView === "evidence" &&
            selectedResearch?.recent_activity_summary
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
            <span>
              <span className="live-pip" /> 最近活动
            </span>
            <strong>cursor {selectedResearch?.last_cursor ?? 0}</strong>
            <span aria-hidden="true">{drawerOpen ? "↓" : "↑"}</span>
          </button>
          {drawerOpen ? (
            <div className="drawer-content">
              <p data-state-binding="research.recent_activity_summary">
                {selectedResearch?.recent_activity_summary}
              </p>
              <p>
                {selectedResearch?.recent_activity_at ?? "服务端未记录活动时间"}
              </p>
            </div>
          ) : null}
        </section>
      </div>
    </div>
  );
}

import { BoundStatement } from "../design/ProductState";
import { StatusMark } from "../design/StatusMark";
import type { ResearchSummary } from "./api";

type NavKey =
  | "research"
  | "literature"
  | "routes"
  | "facts"
  | "tools"
  | "review"
  | "dossier";

type NextStep = {
  kind: "act" | "blocked" | "wait" | "none";
  title: string;
  why: string;
  nav?: NavKey;
};

const controls = new Set([
  "PAUSE_RESEARCH",
  "CANCEL_RESEARCH",
  "STOP",
  "SET_PRIORITY",
  "SET_BUDGET",
]);

const actions: Record<string, { title: string; nav: NavKey }> = {
  CONFIRM_CONTRACT: { title: "检查并确认合同", nav: "research" },
  AMEND_CONTRACT: { title: "修订当前合同", nav: "research" },
  APPLY_ROUTE_PLAN: { title: "审阅路线计划", nav: "routes" },
  START_RESEARCH: { title: "启动已批准的研究路线", nav: "routes" },
  SUBMIT_CLAIM: { title: "记录一条待验证 Claim", nav: "facts" },
  IMPORT_VERIFICATION: { title: "处理验证与审查回执", nav: "review" },
  CREATE_REVIEW_TASK: { title: "安排独立审查", nav: "review" },
  RUN_TOOL: { title: "运行受管工具", nav: "tools" },
  RUN_LITERATURE_QUERY: { title: "检索相关文献", nav: "literature" },
  FINALIZE_RESEARCH: { title: "检查闭合条件", nav: "dossier" },
};

export function ResearchOverview({
  research,
  onNavigate,
}: {
  research: ResearchSummary | null;
  onNavigate: (nav: NavKey) => void;
}) {
  if (!research) {
    return (
      <section className="research-overview research-overview--empty">
        <h1>选择或新建一项研究</h1>
        <p>选择后会显示被录入的阶段、阻塞、下一步和最近活动。</p>
        <button type="button" onClick={() => onNavigate("research")}>
          进入合同与材料
        </button>
      </section>
    );
  }

  const next = deriveNextStep(research);
  const blockers = research.blockers.map(describeBlocker);
  return (
    <section className="research-overview" aria-label="当前研究概览">
      <header className="overview-title">
        <div>
          <h1>{research.title}</h1>
          <p>{research.question_summary}</p>
        </div>
        <div className="overview-axes" aria-label="研究结果与执行状态">
          <div>
            <span>数学结果</span>
            <StatusMark tone={outcomeTone(research.outcome_state)}>
              <BoundStatement binding="research.outcome_state">
                {translateState(research.outcome_state)}
              </BoundStatement>
            </StatusMark>
          </div>
          <div>
            <span>执行状态</span>
            <StatusMark tone={executionTone(research.execution_state)}>
              <BoundStatement binding="research.execution_state">
                {translateState(research.execution_state)}
              </BoundStatement>
            </StatusMark>
          </div>
        </div>
      </header>

      <div className="overview-strip">
        <article className="stage-summary">
          <span>服务端记录的阶段</span>
          <strong data-state-binding="research.phase">
            {translatePhase(research.phase)}
          </strong>
          <small>
            修订 r{research.research_revision} · 合同 v
            {research.contract_version}
          </small>
        </article>

        <article className={`next-step next-step--${next.kind}`}>
          <span>下一步</span>
          <strong>{next.title}</strong>
          <p>{next.why}</p>
          {next.nav ? (
            <button type="button" onClick={() => onNavigate(next.nav!)}>
              前往处理
            </button>
          ) : null}
        </article>

        <article className="blocker-summary">
          <span>阻塞</span>
          {blockers.length ? (
            <ul>
              {blockers.slice(0, 3).map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : (
            <strong data-state-binding="research.blockers">
              当前投影未报告阻塞
            </strong>
          )}
        </article>

        <article className="activity-summary">
          <span>最近活动</span>
          <strong data-state-binding="research.recent_activity_summary">
            {research.recent_activity_summary || "尚无公开活动摘要"}
          </strong>
          <small>{research.recent_activity_at || "未记录时间"}</small>
        </article>
      </div>

      <p className="knowledge-boundary">
        本产品记录的是这项研究被录入的部分。任何未被录入的东西——以及任何其他未录入的东西——不在其中。
      </p>
    </section>
  );
}

function actionType(value: unknown): string | undefined {
  if (typeof value === "string") return value;
  if (value && typeof value === "object") {
    const row = value as Record<string, unknown>;
    const candidate = row.type ?? row.action;
    if (typeof candidate === "string") return candidate;
  }
  return undefined;
}

function deriveNextStep(research: ResearchSummary): NextStep {
  if (research.blockers.length) {
    return {
      kind: "blocked",
      title: "先解除当前阻塞",
      why: describeBlocker(research.blockers[0]),
    };
  }
  for (const raw of research.next_actions) {
    const type = actionType(raw);
    if (!type || controls.has(type)) continue;
    const mapped = actions[type];
    if (mapped)
      return {
        kind: "act",
        title: mapped.title,
        why: `来自服务端待办 ${type}`,
        nav: mapped.nav,
      };
  }
  if (["WAITING", "QUEUED"].includes(research.execution_state)) {
    return {
      kind: "wait",
      title: "等待已提交任务返回",
      why: "任务完成前不显示成功，也不会自动重试未知结果。",
    };
  }
  return {
    kind: "none",
    title: "系统未给出可执行建议",
    why: "控制类动作不会占用下一步位置；可从路线、事实或工具页继续查看。",
  };
}

function describeBlocker(value: unknown): string {
  if (typeof value === "string") return value.replaceAll("_", " ");
  if (value && typeof value === "object") {
    const row = value as Record<string, unknown>;
    for (const key of ["title", "summary", "kind", "code"]) {
      if (typeof row[key] === "string")
        return String(row[key]).replaceAll("_", " ");
    }
  }
  return "服务端报告了一个尚未说明的阻塞";
}

function translateState(value: string): string {
  const labels: Record<string, string> = {
    OPEN: "开放中",
    PROVED: "已证明",
    PROVED_CONDITIONAL: "在显式前提下已证明",
    DISPROVED: "已反驳",
    UNRESOLVED: "未解决",
    RUNNING: "执行中",
    PAUSED: "已暂停",
    WAITING: "等待中",
    QUEUED: "排队中",
    FAILED: "执行失败",
    CLOSED: "已关闭",
  };
  return labels[value] ?? value.replaceAll("_", " ");
}

function translatePhase(value: string): string {
  const labels: Record<string, string> = {
    CONTRACT: "立题与合同",
    MATERIALS: "材料与文献",
    ROUTING: "路线规划",
    EXECUTION: "执行与取证",
    VERIFICATION: "验证与晋级",
    CLOSURE: "闭合检查",
    PUBLICATION: "独立复核与交付",
  };
  return labels[value] ?? (value.replaceAll("_", " ") || "尚未计算");
}

function outcomeTone(
  value: string,
): "neutral" | "jade" | "ochre" | "vermilion" | "ink" {
  if (["PROVED", "PROVED_CONDITIONAL"].includes(value)) return "jade";
  if (value === "DISPROVED") return "vermilion";
  if (["OPEN", "UNRESOLVED"].includes(value)) return "ochre";
  return "neutral";
}

function executionTone(
  value: string,
): "neutral" | "jade" | "ochre" | "vermilion" | "ink" {
  if (value === "FAILED") return "vermilion";
  if (["WAITING", "PAUSED", "QUEUED"].includes(value)) return "ochre";
  if (value === "RUNNING") return "ink";
  return "neutral";
}

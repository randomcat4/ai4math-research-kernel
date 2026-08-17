import { useEffect, useMemo, useState } from "react";
import {
  identityLabel,
  opaqueSuffix,
  type SessionView,
} from "../identity/model.js";
import { ReviewApiError, ReviewGateway } from "./api.js";
import {
  CHECKS,
  canReview,
  canReviewTask,
  explainError,
  type DraftCheck,
  type FeatureFailure,
  type ReviewTask,
} from "./model.js";
import "./review.css";

export interface ReviewWorkbenchProps {
  session?: SessionView;
  baseUrl?: string;
}

export function ReviewWorkbench({
  session,
  baseUrl = "",
}: ReviewWorkbenchProps) {
  const gateway = useMemo(() => new ReviewGateway(baseUrl), [baseUrl]);
  const [tasks, setTasks] = useState<ReviewTask[]>([]);
  const [selected, setSelected] = useState<ReviewTask>();
  const [failure, setFailure] = useState<FeatureFailure>();
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (canReview(session)) void load();
  }, [session]);
  async function act<T>(fn: () => Promise<T>) {
    setBusy(true);
    setFailure(undefined);
    try {
      return await fn();
    } catch (error) {
      setFailure(
        error instanceof ReviewApiError
          ? error.toFailure()
          : explainError(String(error)),
      );
      return undefined;
    } finally {
      setBusy(false);
    }
  }
  async function load() {
    const result = await act(() => gateway.inbox());
    if (result) {
      const visible = result.filter((task) =>
        canReviewTask(session, task.type),
      );
      setTasks(visible);
      setSelected((current) => visible.find((task) => task.id === current?.id));
    }
  }
  async function claim(task: ReviewTask) {
    const result = await act(() => gateway.claim(task.id));
    if (result) {
      setSelected(result);
      setTasks((all) =>
        all.map((item) => (item.id === result.id ? result : item)),
      );
    }
  }
  if (!session || !canReview(session))
    return (
      <main className="review-shell">
        <header className="review-hero">
          <p>独立审查</p>
          <h1>审查工作台</h1>
        </header>
        <section className="review-denied">
          <b>当前角色：{session?.role ?? "未登录"}</b>
          <p>当前身份不能领取或提交独立审查。</p>
          <span>请切换到同行审查者或整篇审查者；文献审查者请进入文献页。</span>
        </section>
      </main>
    );
  return (
    <main className="review-shell">
      <header className="review-hero">
        <div>
          <p>独立审查</p>
          <h1>审查收件箱</h1>
          <span>{identityLabel(session)}</span>
        </div>
        <button onClick={load} disabled={busy}>
          刷新收件箱
        </button>
      </header>
      {failure && <Failure failure={failure} />}
      <section className="review-grid">
        <aside className="review-panel">
          <Title title="待我审查" note="从服务端分派的真实任务中选择" />
          <div className="review-inbox">
            {tasks.length ? (
              tasks.map((task) => (
                <button
                  className={selected?.id === task.id ? "active" : ""}
                  key={task.id}
                  onClick={() => setSelected(task)}
                >
                  <header>
                    <b>{typeLabel(task.type)}</b>
                    <span>{stateLabel(task.state)}</span>
                  </header>
                  <small>任务 · {opaqueSuffix(task.id)}</small>
                  <small>截止 {formatTime(task.expiresAt)}</small>
                </button>
              ))
            ) : (
              <p>当前身份没有待审查任务。</p>
            )}
          </div>
        </aside>
        <section className="review-main">
          {selected ? (
            <>
              <TaskSummary task={selected} />
              {selected.state === "OPEN" && (
                <button
                  className="review-claim"
                  onClick={() => claim(selected)}
                  disabled={busy}
                >
                  领取此任务
                </button>
              )}
              {selected.state === "CLAIMED" && (
                <DraftEditor task={selected} session={session} />
              )}
              {selected.state === "SUBMITTED" && (
                <div className="review-complete">
                  <b>签名审查已提交</b>
                  <span>
                    工件 ·{" "}
                    {opaqueSuffix(
                      selected.signedArtifactRef?.artifact_id ?? "",
                    )}
                  </span>
                </div>
              )}
            </>
          ) : (
            <section className="review-panel">
              <Title title="选择任务" note="检查项默认保持未判断" />
              <p>从左侧收件箱选择一个任务，查看范围并逐项审查。</p>
            </section>
          )}
        </section>
      </section>
    </main>
  );
}

function TaskSummary({ task }: { task: ReviewTask }) {
  return (
    <section className="review-panel">
      <Title
        title={typeLabel(task.type)}
        note="先确认范围和独立性，再开始判断"
      />
      <dl className="review-summary">
        <div>
          <dt>状态</dt>
          <dd>{stateLabel(task.state)}</dd>
        </div>
        <div>
          <dt>版本</dt>
          <dd>
            研究 r{task.researchRevision} · 合同 v{task.contractVersion}
          </dd>
        </div>
        <div>
          <dt>独立性</dt>
          <dd>{task.independenceStatus}</dd>
        </div>
        <div>
          <dt>截止时间</dt>
          <dd>{formatTime(task.expiresAt)}</dd>
        </div>
      </dl>
      <details className="review-technical">
        <summary>技术详情</summary>
        <dl className="review-rows">
          <div>
            <dt>任务标识</dt>
            <dd>{task.id}</dd>
          </div>
          <div>
            <dt>研究标识</dt>
            <dd>{task.runId}</dd>
          </div>
          <div>
            <dt>目标标识</dt>
            <dd>{task.targetId ?? "服务未发布"}</dd>
          </div>
          <div>
            <dt>目标摘要</dt>
            <dd>{task.targetDigest}</dd>
          </div>
          <div>
            <dt>受理主体</dt>
            <dd>{task.assigneeSubjectId}</dd>
          </div>
          <div>
            <dt>作者主体</dt>
            <dd>{task.authorSubjectIds.join(", ")}</dd>
          </div>
        </dl>
      </details>
    </section>
  );
}

function DraftEditor({
  task,
  session,
}: {
  task: ReviewTask;
  session: SessionView;
}) {
  const [checks, setChecks] = useState<DraftCheck[]>(() =>
    CHECKS[task.type].map((item) => ({
      ...item,
      passed: null,
      conclusion: "",
      evidenceRefs: "",
    })),
  );
  const [verdict, setVerdict] = useState("");
  function update(index: number, patch: Partial<DraftCheck>) {
    setChecks((current) =>
      current.map((item, itemIndex) =>
        itemIndex === index ? { ...item, ...patch } : item,
      ),
    );
  }
  const bindingReady = Boolean(task.targetId);
  const draft = {
    schema_version: "rk.product.review.draft.v1",
    unsigned: true,
    review_type: task.type,
    review_task_id: task.id,
    reviewer_subject_id: session.principalSubjectId,
    binding: {
      run_id: task.runId,
      kernel_revision: task.researchRevision,
      contract_version: task.contractVersion,
      target_id: task.targetId ?? null,
      target_digest: task.targetDigest,
    },
    verdict: verdict || null,
    checks: Object.fromEntries(
      checks.map((check) => [
        check.key,
        {
          passed: check.passed,
          status: check.passed === null ? "UNSELECTED" : "HUMAN_ATTESTED",
          conclusion: check.conclusion,
          evidence_refs: [],
        },
      ]),
    ),
  };
  function download() {
    const blob = new Blob([JSON.stringify(draft, null, 2)], {
      type: "application/json",
    });
    const anchor = document.createElement("a");
    anchor.href = URL.createObjectURL(blob);
    anchor.download = "review-draft-" + opaqueSuffix(task.id) + ".json";
    anchor.click();
    URL.revokeObjectURL(anchor.href);
  }
  const complete =
    Boolean(verdict) &&
    checks.every((check) => check.passed !== null && check.conclusion.trim());
  return (
    <section className="review-panel">
      <Title
        title="逐项检查"
        note="每项默认为未判断；通过与不通过都必须由审查者明确选择"
      />
      <label className="review-verdict">
        总体意见
        <select
          value={verdict}
          onChange={(event) => setVerdict(event.target.value)}
        >
          <option value="">未判断</option>
          <option value="ACCEPT">接受</option>
          <option value="REJECT">拒绝</option>
          <option value="NEEDS_REVISION">要求修订</option>
        </select>
      </label>
      <div className="review-checks">
        {checks.map((check, index) => (
          <article key={check.key}>
            <header>
              <b>{check.label}</b>
              <fieldset>
                <legend>判断</legend>
                {[
                  ["", "未判断"],
                  ["true", "通过"],
                  ["false", "不通过"],
                ].map(([value, label]) => (
                  <label key={value}>
                    <input
                      type="radio"
                      name={check.key}
                      value={value}
                      checked={
                        (check.passed === null ? "" : String(check.passed)) ===
                        value
                      }
                      onChange={() =>
                        update(index, {
                          passed: value === "" ? null : value === "true",
                        })
                      }
                    />
                    {label}
                  </label>
                ))}
              </fieldset>
            </header>
            <label>
              审查结论
              <textarea
                required
                value={check.conclusion}
                onChange={(event) =>
                  update(index, { conclusion: event.target.value })
                }
                placeholder="说明判断依据与需要修订的内容"
              />
            </label>
          </article>
        ))}
      </div>
      {!bindingReady && (
        <Unavailable text="审查收件箱尚未发布目标标识，无法生成完整的精确绑定草稿。请先升级 review_task.v1 响应；页面不会要求人工抄写内部 ID。" />
      )}
      <button onClick={download} disabled={!bindingReady || !complete}>
        下载未签审查草稿
      </button>
      <p className="review-warning">
        下载不等于签名或提交。签名工件选择器尚未由服务发布，因此本页不提供手填
        Artifact ID 的替代入口。
      </p>
    </section>
  );
}

function Title({ title, note }: { title: string; note: string }) {
  return (
    <header className="review-title">
      <div>
        <h2>{title}</h2>
        <p>{note}</p>
      </div>
    </header>
  );
}
function Failure({ failure }: { failure: FeatureFailure }) {
  return (
    <div className="review-failure" role="alert">
      <b>{failure.title}</b>
      <code>{failure.code}</code>
      <span>{failure.detail}</span>
      <p>{failure.action}</p>
    </div>
  );
}
function Unavailable({ text }: { text: string }) {
  return (
    <div className="review-unavailable">
      <b>此步骤暂不可用</b>
      <p>{text}</p>
    </div>
  );
}
function typeLabel(type: ReviewTask["type"]) {
  return type === "ATOMIC"
    ? "单项证明审查"
    : type === "COMPOSITION"
      ? "组合证明审查"
      : "整篇论文审查";
}
function stateLabel(state: string) {
  return (
    (
      {
        OPEN: "待领取",
        CLAIMED: "审查中",
        SUBMITTED: "已提交",
        EXPIRED: "已过期",
        REASSIGNED: "已改派",
      } as Record<string, string>
    )[state] ?? state
  );
}
function formatTime(value: string) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleString("zh-CN", { hour12: false });
}

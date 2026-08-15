import type { ReactNode } from "react";

import type { QueryPhase } from "../app/usePublishedProjections";

const stateCopy: Record<
  Exclude<QueryPhase, "ready">,
  { title: string; action: string }
> = {
  loading: { title: "正在读取", action: "请稍候，已显示的内容仍可查看。" },
  empty: { title: "尚未开始", action: "从本页的创建或选择入口开始。" },
  not_found: {
    title: "对象未找到",
    action: "返回上一步重新选择，或重新读取当前研究。",
  },
  unpublished: {
    title: "本部署未提供",
    action: "查看部署能力清单，确认该投影是否已发布。",
  },
  error: {
    title: "读取失败",
    action: "检查连接状态后重新读取；若持续失败，请导出诊断。",
  },
};

export function StateNotice({
  phase,
  detail,
  onRetry,
}: {
  phase: Exclude<QueryPhase, "ready">;
  detail: string;
  onRetry?: () => void;
}) {
  const copy = stateCopy[phase];
  return (
    <section
      className={`product-state product-state--${phase}`}
      role={phase === "error" ? "alert" : "status"}
    >
      <div>
        <strong>{copy.title}</strong>
        <p>{detail}</p>
        <span>下一步：{copy.action}</span>
      </div>
      {onRetry && ["not_found", "error"].includes(phase) ? (
        <button type="button" onClick={onRetry}>
          重新读取
        </button>
      ) : null}
    </section>
  );
}

export function BoundStatement({
  binding,
  children,
}: {
  binding: string;
  children: ReactNode;
}) {
  return <span data-state-binding={binding}>{children}</span>;
}

import { useEffect, useMemo, useState } from "react";

import { BoundStatement } from "../../design/ProductState";
import { AdminApiError, AdminGateway } from "./api.js";
import {
  failure,
  type AdminFailure,
  type AdminHealth,
  type DeploymentStatus,
} from "./model.js";
import "./admin.css";

export interface AdminCenterProps {
  deploymentId: string;
  deploymentRevision?: number;
  emptyRoot?: boolean;
  baseUrl?: string;
}

export function AdminCenter({
  deploymentId,
  deploymentRevision,
  emptyRoot = false,
  baseUrl = "",
}: AdminCenterProps) {
  const gateway = useMemo(
    () => new AdminGateway(deploymentId, deploymentRevision, baseUrl),
    [baseUrl, deploymentId, deploymentRevision],
  );
  const [status, setStatus] = useState<DeploymentStatus>();
  const [health, setHealth] = useState<AdminHealth>();
  const [problem, setProblem] = useState<AdminFailure>();
  const [busy, setBusy] = useState(false);

  async function refresh() {
    setBusy(true);
    setProblem(undefined);
    try {
      const [nextStatus, nextHealth] = await Promise.all([
        gateway.status(),
        gateway.health(),
      ]);
      setStatus(nextStatus);
      setHealth(nextHealth);
    } catch (error) {
      setProblem(
        error instanceof AdminApiError
          ? error.failure
          : failure(String(error), 0),
      );
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (!emptyRoot) void refresh();
  }, [emptyRoot, gateway]);

  const faults = [
    ...new Set([...(status?.faultCodes ?? []), ...(health?.faultCodes ?? [])]),
  ];
  const capabilities = status?.capabilityKeys ?? [];
  const currentRevision = Math.max(
    deploymentRevision ?? 0,
    status?.revision ?? 0,
    health?.revision ?? 0,
  );

  return (
    <main className="admin-center">
      <header className="admin-heading">
        <div>
          <p>部署与健康</p>
          <h1>当前部署状态</h1>
          <span>
            这里只读展示服务端探测结果。备份、恢复、升级与迁移请使用受管 CLI。
          </span>
        </div>
        {!emptyRoot ? (
          <button onClick={() => void refresh()} disabled={busy}>
            {busy ? "正在读取…" : "重新读取"}
          </button>
        ) : null}
      </header>

      {problem ? (
        <FailurePanel value={problem} onRetry={() => void refresh()} />
      ) : null}

      {emptyRoot ? (
        <section className="admin-panel admin-empty-root">
          <h2>部署尚未初始化</h2>
          <p>
            先在服务器使用受管引导命令建立数据根。图形界面不会接收主机路径或管理员密钥。
          </p>
        </section>
      ) : (
        <>
          <section className="admin-rail" aria-label="部署摘要">
            <Rail label="部署绑定" value="已绑定" tone="ok" />
            <Rail
              label="整体健康"
              value={health?.state ?? "读取中"}
              tone={tone(health?.state)}
            />
            <Rail
              label="能力数量"
              value={String(capabilities.length)}
              tone={capabilities.length ? "ok" : "warn"}
            />
            <Rail
              label="投影修订"
              value={String(currentRevision)}
              tone="neutral"
            />
          </section>

          <section className="admin-layout admin-layout--readonly">
            <section className="admin-panel">
              <SectionTitle
                title="能力发布清单"
                subtitle="每项能力独立显示，不因相邻能力成功而变绿。"
              />
              {capabilities.length ? (
                <ul className="admin-capability-list">
                  {capabilities.map((item) => (
                    <li key={item}>
                      <span aria-hidden="true">◆</span>
                      <strong>{humanize(item)}</strong>
                      <BoundStatement binding={`deployment.capability.${item}`}>
                        <em>已发布</em>
                      </BoundStatement>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="admin-empty">
                  服务端尚未返回能力清单。重新读取仍为空时，请检查部署探测。
                </p>
              )}
            </section>

            <section className="admin-panel">
              <SectionTitle
                title="故障与下一步"
                subtitle="面向使用者解释影响；技术代码保留用于诊断。"
              />
              {faults.length ? (
                <ul className="admin-fault-list">
                  {faults.map((item) => (
                    <li key={item}>
                      <strong>{faultTitle(item)}</strong>
                      <p>{faultImpact(item)}</p>
                      <span>{faultAction(item)}</span>
                      <details>
                        <summary>技术详情</summary>
                        <code>{item}</code>
                      </details>
                    </li>
                  ))}
                </ul>
              ) : (
                <p
                  className="admin-empty"
                  data-state-binding="deployment.fault_codes"
                >
                  当前探测没有报告公开故障。
                </p>
              )}
            </section>
          </section>
        </>
      )}
    </main>
  );
}

function FailurePanel({
  value,
  onRetry,
}: {
  value: AdminFailure;
  onRetry: () => void;
}) {
  return (
    <section
      className={
        value.rethlasBlocked ? "admin-failure is-504" : "admin-failure"
      }
      role="alert"
    >
      <header>
        <b>{value.title}</b>
      </header>
      <p>{value.detail}</p>
      <span>下一步：{value.action}</span>
      <div>
        <button type="button" onClick={onRetry}>
          重新读取
        </button>
        <details>
          <summary>技术详情</summary>
          <code>
            HTTP {value.status} · {value.code}
          </code>
        </details>
      </div>
    </section>
  );
}

function Rail({
  label,
  value,
  tone: railTone,
}: {
  label: string;
  value: string;
  tone: string;
}) {
  return (
    <div className={`admin-rail-cell is-${railTone}`}>
      <span>{label}</span>
      <b>{value}</b>
    </div>
  );
}

function SectionTitle({
  title,
  subtitle,
}: {
  title: string;
  subtitle: string;
}) {
  return (
    <header className="admin-section-title">
      <h2>{title}</h2>
      <p>{subtitle}</p>
    </header>
  );
}

function tone(value?: string): string {
  if (!value) return "neutral";
  if (["AVAILABLE", "SUCCEEDED", "BOUND"].includes(value)) return "ok";
  if (["UNAVAILABLE", "FAILED", "OUTCOME_UNKNOWN"].includes(value))
    return "bad";
  return "warn";
}

function humanize(value: string): string {
  return value
    .replaceAll("_", " ")
    .toLocaleLowerCase()
    .replace(/^./, (head) => head.toLocaleUpperCase());
}

function faultTitle(code: string): string {
  if (
    code.includes("RETHLAS") &&
    (code.includes("504") || code.includes("TIMEOUT"))
  )
    return "Rethlas 当前无法响应";
  if (code.includes("GPU") || code.includes("ROCM"))
    return "GPU 能力探测未通过";
  if (code.includes("DATABASE")) return "研究数据库探测异常";
  return "一项部署能力需要处理";
}

function faultImpact(code: string): string {
  if (code.includes("RETHLAS"))
    return "依赖 Rethlas 的外部验证当前不可用；其他验证器状态不受此条故障代表。";
  return "受影响能力不会被显示为可用，现有研究记录仍保持只读可见。";
}

function faultAction(code: string): string {
  if (code.includes("RETHLAS"))
    return "保留失败回执，等待外部服务恢复后由人工发起新的尝试。";
  return "在服务器查看对应探测回执，修复后重新执行部署探测。";
}

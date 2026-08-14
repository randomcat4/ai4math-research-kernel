export type StreamPhase = "IDLE" | "CONNECTING" | "LIVE" | "RECONNECTING" | "CURSOR_EXPIRED" | "AUTH_REQUIRED" | "FAILED";

export interface PublicActivity {
  cursor: number;
  eventType: string;
  payload: Record<string, unknown>;
}

export type RecoveryKind = "CURSOR_EXPIRED" | "STALE_QUERY" | "OUTCOME_UNKNOWN" | "UPGRADE_REQUIRED" | "STRICT_ERROR";

export interface RecoveryNotice {
  kind: RecoveryKind;
  code: string;
  title: string;
  detail: string;
}

export const outcomeUnknownTreatments = [
  ["QUERY_REMOTE", "查询外部系统，以新证据确认原调用结果"],
  ["ACCEPT_RECEIPT", "接受现有未知回执，保持不晋级"],
  ["RETRY", "以新 request_id 重试，并引用原 Receipt"],
  ["MARK_ABANDONED", "正式放弃原调用，保留失败分母"],
] as const;

export function classifyRecovery(code: string): RecoveryNotice {
  if (code === "CURSOR_EXPIRED") return { kind: code, code, title: "活动游标已过期", detail: "必须重新读取服务端投影，再从新快照 cursor 续传；不会猜测缺失事件。" };
  if (code === "STALE_QUERY" || code.endsWith("_STALE")) return { kind: "STALE_QUERY", code, title: "查询快照已失效", detail: "丢弃旧 continuation 与局部结果，按最新 revision 重新执行当前查询。" };
  if (code === "OUTCOME_UNKNOWN") return { kind: code, code, title: "外部调用结果未知", detail: "禁止自动重试；四种处置都会创建引用原 Receipt 的新请求。" };
  if (code === "UNKNOWN_VARIANT" || code === "VARIANT_UNAVAILABLE" || code === "UPGRADE_REQUIRED") return { kind: "UPGRADE_REQUIRED", code, title: "服务端契约尚未发布", detail: "当前前端需要更高版本的 ResearchProduct；请升级守护进程后重新连接。" };
  return { kind: "STRICT_ERROR", code, title: "请求未完成", detail: `服务返回 ${code}；保持当前证据状态，不把失败解释为成功。` };
}

const recoveryEvent = "rk:interaction-failure";

export function reportInteractionFailure(code: string): void {
  window.dispatchEvent(new CustomEvent(recoveryEvent, { detail: classifyRecovery(code) }));
}

export function subscribeInteractionFailures(listener: (notice: RecoveryNotice) => void): () => void {
  const receive = (event: Event) => listener((event as CustomEvent<RecoveryNotice>).detail);
  window.addEventListener(recoveryEvent, receive);
  return () => window.removeEventListener(recoveryEvent, receive);
}

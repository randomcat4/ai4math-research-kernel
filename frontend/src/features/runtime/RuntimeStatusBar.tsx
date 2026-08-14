import { useEffect, useState } from "react";

import { classifyRecovery, outcomeUnknownTreatments, type RecoveryNotice, subscribeInteractionFailures } from "./model";
import { useActivityStream } from "./useActivityStream";
import "./runtime.css";

interface Props {
  runId?: string;
  snapshotCursor: number;
  onReloadProjections: () => Promise<void>;
}

export function RuntimeStatusBar({ runId, snapshotCursor, onReloadProjections }: Props) {
  const stream = useActivityStream(runId, snapshotCursor);
  const [notice, setNotice] = useState<RecoveryNotice>();
  const [reloading, setReloading] = useState(false);

  useEffect(() => subscribeInteractionFailures(setNotice), []);
  useEffect(() => {
    if (stream.errorCode) setNotice(classifyRecovery(stream.errorCode));
  }, [stream.errorCode]);

  async function reload() {
    setReloading(true);
    try {
      await onReloadProjections();
      setNotice(undefined);
    } finally {
      setReloading(false);
    }
  }

  return <aside className="runtime-status" data-phase={stream.phase} aria-live="polite">
    <div className="runtime-line">
      <span className="runtime-pulse" aria-hidden="true" />
      <strong>{stream.phase === "LIVE" ? "活动链实时" : stream.phase === "RECONNECTING" ? "正在续接活动链" : stream.phase === "CURSOR_EXPIRED" ? "投影需要重载" : stream.phase === "AUTH_REQUIRED" ? "活动链需要身份" : stream.phase === "IDLE" ? "尚未选择研究" : "正在连接活动链"}</strong>
      <code>cursor {stream.cursor}</code>
      <small>{stream.lastActivity ? `${stream.lastActivity.eventType} · 已按 Last-Event-ID 持久续传` : "心跳不计作业务事件"}</small>
    </div>
    {notice ? <div className="runtime-recovery" role="alert">
      <div><b>{notice.title}</b><p>{notice.detail}</p></div>
      {(notice.kind === "CURSOR_EXPIRED" || notice.kind === "STALE_QUERY") && <button disabled={reloading} onClick={() => void reload()} type="button">{reloading ? "正在重载真实投影" : "丢弃旧状态并重载"}</button>}
      {notice.kind === "UPGRADE_REQUIRED" && <strong className="runtime-upgrade">需要升级守护进程</strong>}
      {notice.kind === "OUTCOME_UNKNOWN" && <ol>{outcomeUnknownTreatments.map(([code, label]) => <li key={code}><code>{code}</code><span>{label}</span></li>)}</ol>}
      <button className="runtime-dismiss" onClick={() => setNotice(undefined)} type="button" aria-label="关闭恢复提示">×</button>
    </div> : null}
  </aside>;
}

import { useEffect, useRef, useState } from "react";
import type { PublicActivity } from "./model.js";
import "./activity.css";
export function ActivityFeed({
  runId,
  baseUrl = "",
  initialCursor = 0,
  limit = 120,
}: {
  runId?: string;
  baseUrl?: string;
  initialCursor?: number;
  limit?: number;
}) {
  const [items, setItems] = useState<PublicActivity[]>([]);
  const [state, setState] = useState<"CONNECTING" | "LIVE" | "UNAVAILABLE">(
    "CONNECTING",
  );
  const cursor = useRef(initialCursor);
  useEffect(() => {
    if (!runId) {
      setState("UNAVAILABLE");
      return;
    }
    const stream = new EventSource(
      `${baseUrl}/v1/research/${encodeURIComponent(runId)}/events?after_cursor=${cursor.current}`,
      { withCredentials: true },
    );
    stream.onopen = () => setState("LIVE");
    stream.onerror = () => setState("UNAVAILABLE");
    stream.addEventListener("activity", (event) => {
      const parsed: unknown = JSON.parse((event as MessageEvent<string>).data);
      if (
        parsed === null ||
        Array.isArray(parsed) ||
        typeof parsed !== "object"
      )
        return;
      const v = parsed as Record<string, unknown>;
      const next = {
        cursor: Number((event as MessageEvent<string>).lastEventId),
        type: String(v.type ?? "PUBLIC_ACTIVITY"),
        occurredAt: String(v.occurred_at ?? ""),
        source: String(v.source ?? ""),
        entityLabel: String(v.stable_label ?? v.entity_id ?? "—"),
        summary: String(v.public_summary ?? v.summary ?? "状态发生变化"),
      };
      cursor.current = next.cursor;
      setItems((current) => [...current, next].slice(-limit));
    });
    return () => stream.close();
  }, [baseUrl, limit, runId]);
  return (
    <section className="rk-activity">
      <header>
        <div>
          <p>PUBLIC ACTIVITY</p>
          <h2>动态活动</h2>
        </div>
        <span data-state={state}>
          {state === "LIVE"
            ? "实时连接"
            : state === "CONNECTING"
              ? "正在连接"
              : "活动流不可用"}
        </span>
      </header>
      <p className="rk-boundary">
        仅显示类型化公开摘要；不请求、不缓存、不展示原始推理或思维链。
      </p>
      <ol>
        {items.map((item) => (
          <li key={item.cursor}>
            <time>{item.occurredAt || `cursor ${item.cursor}`}</time>
            <strong>{item.type}</strong>
            <span>{item.entityLabel}</span>
            <p>{item.summary}</p>
          </li>
        ))}
      </ol>
      {items.length === 0 && (
        <div className="rk-empty">
          等待服务器真实活动；心跳不会伪造业务事件。
        </div>
      )}
    </section>
  );
}

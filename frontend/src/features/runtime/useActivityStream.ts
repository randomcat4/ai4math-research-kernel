import { useEffect, useRef, useState } from "react";

import type { PublicActivity, StreamPhase } from "./model";

interface StreamState {
  phase: StreamPhase;
  cursor: number;
  lastActivity?: PublicActivity;
  errorCode?: string;
}

function problemCode(value: unknown, fallback: string): string {
  return value !== null && typeof value === "object" && !Array.isArray(value) && "code" in value
    ? String(value.code)
    : fallback;
}

function decodeFrame(frame: string): PublicActivity | undefined {
  let cursor: number | undefined;
  let eventType = "message";
  const data: string[] = [];
  for (const line of frame.split(/\r?\n/)) {
    if (line.startsWith("id:")) cursor = Number(line.slice(3).trim());
    else if (line.startsWith("event:")) eventType = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (!Number.isSafeInteger(cursor) || cursor === undefined || cursor < 0 || data.length === 0) return undefined;
  const parsed: unknown = JSON.parse(data.join("\n"));
  if (parsed === null || Array.isArray(parsed) || typeof parsed !== "object") throw new Error("INVALID_ACTIVITY_ENVELOPE");
  return { cursor, eventType, payload: parsed as Record<string, unknown> };
}

export function useActivityStream(runId: string | undefined, snapshotCursor: number, baseUrl = ""): StreamState {
  const [state, setState] = useState<StreamState>({ phase: runId ? "CONNECTING" : "IDLE", cursor: snapshotCursor });
  const cursor = useRef(snapshotCursor);

  useEffect(() => {
    cursor.current = snapshotCursor;
    if (!runId) {
      setState({ phase: "IDLE", cursor: snapshotCursor });
      return;
    }
    const controller = new AbortController();
    let reconnectTimer: number | undefined;
    let attempt = 0;

    const connect = async () => {
      if (controller.signal.aborted) return;
      setState((current) => ({ ...current, phase: attempt ? "RECONNECTING" : "CONNECTING", errorCode: undefined }));
      try {
        const currentCursor = cursor.current;
        const response = await fetch(`${baseUrl}/v1/research/${encodeURIComponent(runId)}/events?after_cursor=${currentCursor}`, {
          credentials: "include",
          headers: { accept: "text/event-stream", "Last-Event-ID": String(currentCursor) },
          signal: controller.signal,
        });
        if (!response.ok) {
          const code = problemCode(await response.json().catch(() => undefined), `HTTP_${response.status}`);
          const phase: StreamPhase = code === "CURSOR_EXPIRED" ? "CURSOR_EXPIRED" : response.status === 401 ? "AUTH_REQUIRED" : "FAILED";
          setState((current) => ({ ...current, phase, errorCode: code }));
          return;
        }
        if (!response.body || !response.headers.get("content-type")?.startsWith("text/event-stream")) throw new Error("INVALID_ACTIVITY_STREAM");
        attempt = 0;
        setState((current) => ({ ...current, phase: "LIVE", errorCode: undefined }));
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!controller.signal.aborted) {
          const part = await reader.read();
          if (part.done) break;
          buffer += decoder.decode(part.value, { stream: true }).replace(/\r\n/g, "\n");
          let boundary = buffer.indexOf("\n\n");
          while (boundary >= 0) {
            const activity = decodeFrame(buffer.slice(0, boundary));
            buffer = buffer.slice(boundary + 2);
            if (activity) {
              if (activity.cursor <= cursor.current) throw new Error("NON_MONOTONIC_ACTIVITY_CURSOR");
              cursor.current = activity.cursor;
              setState({ phase: "LIVE", cursor: activity.cursor, lastActivity: activity });
            }
            boundary = buffer.indexOf("\n\n");
          }
        }
        if (!controller.signal.aborted) throw new Error("ACTIVITY_STREAM_ENDED");
      } catch (reason) {
        if (controller.signal.aborted) return;
        const code = reason instanceof Error ? reason.message : "ACTIVITY_STREAM_UNAVAILABLE";
        attempt += 1;
        setState((current) => ({ ...current, phase: "RECONNECTING", errorCode: code }));
        reconnectTimer = window.setTimeout(() => void connect(), Math.min(1000 * 2 ** Math.min(attempt, 4), 15000));
      }
    };

    void connect();
    return () => {
      controller.abort();
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
    };
  }, [baseUrl, runId, snapshotCursor]);

  return state;
}

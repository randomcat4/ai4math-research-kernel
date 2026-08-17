import { useCallback, useEffect, useState } from "react";

import {
  ProductApiError,
  type ProductMeta,
  type ProductSession,
  type ResearchSummary,
  type SessionOption,
  productApi,
} from "./api";

export type ConnectionModel = {
  phase: "connecting" | "connected" | "offline";
  meta: ProductMeta | null;
  session: ProductSession | null;
  sessionRequired: boolean;
  error: string | null;
  research: ResearchSummary[];
  researchLoading: boolean;
  sessionOptions: SessionOption[];
  enter: (option: string) => Promise<void>;
  logout: () => Promise<void>;
  retry: () => Promise<void>;
  refreshResearch: () => Promise<void>;
};

function readableError(error: unknown): string {
  if (error instanceof ProductApiError) {
    if (error.status === 401) return "会话尚未建立";
    return `服务拒绝了请求 · ${error.code}`;
  }
  return "无法连接 ResearchProduct 守护进程";
}

export function useProductConnection(): ConnectionModel {
  const [phase, setPhase] = useState<ConnectionModel["phase"]>("connecting");
  const [meta, setMeta] = useState<ProductMeta | null>(null);
  const [session, setSession] = useState<ProductSession | null>(null);
  const [sessionRequired, setSessionRequired] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [research, setResearch] = useState<ResearchSummary[]>([]);
  const [researchLoading, setResearchLoading] = useState(false);
  const [sessionOptions, setSessionOptions] = useState<SessionOption[]>([]);

  const load = useCallback(async () => {
    setPhase("connecting");
    setError(null);
    const [metaResult, sessionResult, optionsResult] = await Promise.allSettled(
      [productApi.meta(), productApi.session(), productApi.sessionOptions()],
    );

    if (metaResult.status === "rejected") {
      setMeta(null);
      setSession(null);
      setResearch([]);
      setPhase("offline");
      setError(readableError(metaResult.reason));
      return;
    }

    setMeta(metaResult.value);
    setPhase("connected");
    setSessionOptions(
      optionsResult.status === "fulfilled" ? optionsResult.value.options : [],
    );
    if (sessionResult.status === "fulfilled") {
      setSession(sessionResult.value);
      setSessionRequired(false);
    } else if (
      sessionResult.reason instanceof ProductApiError &&
      sessionResult.reason.status === 401
    ) {
      try {
        const defaultOption =
          optionsResult.status === "fulfilled"
            ? optionsResult.value.default
            : "SHARED";
        setSession(await productApi.enter(defaultOption));
        setSessionRequired(false);
      } catch (reason) {
        setSession(null);
        setSessionRequired(true);
        setError(readableError(reason));
      }
    } else {
      setSession(null);
      setSessionRequired(false);
      setError(readableError(sessionResult.reason));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const refreshResearch = useCallback(async () => {
    if (!session) {
      setResearch([]);
      return;
    }
    setResearchLoading(true);
    try {
      setResearch(await productApi.research());
    } catch (reason) {
      setError(readableError(reason));
      throw reason;
    } finally {
      setResearchLoading(false);
    }
  }, [session]);

  useEffect(() => {
    void refreshResearch().catch(() => undefined);
  }, [refreshResearch]);

  const enter = useCallback(async (option: string) => {
    setError(null);
    try {
      const next = await productApi.enter(option);
      setSession(next);
      setSessionRequired(false);
    } catch (reason) {
      setError(readableError(reason));
      throw reason;
    }
  }, []);

  const logout = useCallback(async () => {
    await productApi.logout();
    setSession(null);
    setSessionRequired(true);
    setResearch([]);
  }, []);

  return {
    phase,
    meta,
    session,
    sessionRequired,
    error,
    research,
    researchLoading,
    sessionOptions,
    enter,
    logout,
    retry: load,
    refreshResearch,
  };
}

export type ProductMeta = {
  schema_version: string;
  product_version: string;
  deployment_id: string;
  limits: Record<string, number>;
};

export type ProductSession = {
  schema_version: string;
  session_id: string;
  principal_subject_id: string;
  identity_id: string;
  display_name: string;
  role: string;
  linked_identity_ids: string[];
  session_version: number;
  issued_at: string;
  expires_at: string;
  access_mode: "SHARED_READ_ONLY" | "MANAGED";
};

export type SessionOption = {
  id: string;
  label: string;
  description: string;
};

export type ResearchSummary = {
  run_id: string;
  title: string;
  question_summary: string;
  owner: string;
  labels: string[];
  outcome_state: string;
  execution_state: string;
  authority_state: string;
  publication_state: string;
  phase: string;
  blockers: unknown[];
  next_actions: unknown[];
  available_actions: unknown[];
  recent_activity_at: string;
  recent_activity_summary: string;
  research_revision: number;
  contract_version: number;
  last_cursor: number;
};

export class ProductApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
  ) {
    super(code);
  }
}

async function jsonRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "include",
    ...init,
    headers: {
      accept: "application/json",
      ...init?.headers,
    },
  });
  const value: unknown = await response.json();
  if (!response.ok) {
    const code =
      typeof value === "object" && value !== null && "code" in value
        ? String(value.code)
        : `HTTP_${response.status}`;
    throw new ProductApiError(response.status, code);
  }
  return value as T;
}

export const productApi = {
  meta: () => jsonRequest<ProductMeta>("/v1/meta"),
  session: () => jsonRequest<ProductSession>("/v1/session/me"),
  sessionOptions: async () => {
    const value = await jsonRequest<{
      default: string;
      options: SessionOption[];
    }>("/v1/session/options");
    return value;
  },
  enter: (option: string) =>
    jsonRequest<ProductSession>("/v1/session/enter", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ option }),
    }),
  login: (identityId: string, loginSecret: string) =>
    jsonRequest<ProductSession>("/v1/session/login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        identity_id: identityId,
        login_secret: loginSecret,
      }),
    }),
  logout: () =>
    jsonRequest<{ logged_out: boolean }>("/v1/session/logout", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    }),
  research: async () => {
    const envelope = await jsonRequest<{
      result?: { items?: ResearchSummary[] };
    }>("/v1/research?limit=20&sort=RECENT_ACTIVITY_DESC");
    return Array.isArray(envelope.result?.items) ? envelope.result.items : [];
  },
};

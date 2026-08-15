import type { ProductRole, SessionOption, SessionView } from "./model.js";
export class IdentityApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super(code);
  }
}
async function json(
  path: string,
  init?: RequestInit,
): Promise<Record<string, unknown>> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      credentials: "include",
      headers: { "content-type": "application/json", ...init?.headers },
    });
  } catch (error) {
    throw new IdentityApiError(0, "NETWORK_UNAVAILABLE");
  }
  const value: unknown = await response.json().catch(() => null);
  if (value === null || Array.isArray(value) || typeof value !== "object")
    throw new IdentityApiError(response.status, "INVALID_SESSION_ENVELOPE");
  const result = value as Record<string, unknown>;
  if (!response.ok)
    throw new IdentityApiError(
      response.status,
      typeof result.code === "string" ? result.code : "SESSION_REQUEST_FAILED",
    );
  return result;
}
export class IdentityGateway {
  constructor(private readonly baseUrl = "") {}
  async me() {
    return session(
      await json(this.baseUrl + "/v1/session/me", { method: "GET" }),
    );
  }
  async options(): Promise<SessionOption[]> {
    const value = await json(this.baseUrl + "/v1/session/options", {
      method: "GET",
    });
    if (!Array.isArray(value.options))
      throw new IdentityApiError(200, "INVALID_SESSION_OPTIONS");
    return value.options.map((item) => {
      if (item === null || Array.isArray(item) || typeof item !== "object")
        throw new IdentityApiError(200, "INVALID_SESSION_OPTION");
      const option = item as Record<string, unknown>;
      return {
        id: required(option.id, "option_id"),
        label: required(option.label, "option_label"),
        description: required(option.description, "option_description"),
      };
    });
  }
  async enter(option: string) {
    return session(
      await json(this.baseUrl + "/v1/session/enter", {
        method: "POST",
        body: JSON.stringify({ option }),
      }),
    );
  }
  async login(identityId: string, loginSecret: string) {
    return session(
      await json(this.baseUrl + "/v1/session/login", {
        method: "POST",
        body: JSON.stringify({
          identity_id: identityId,
          login_secret: loginSecret,
        }),
      }),
    );
  }
  async switchIdentity(identityId: string) {
    return session(
      await json(this.baseUrl + "/v1/session/switch", {
        method: "POST",
        body: JSON.stringify({ identity_id: identityId }),
      }),
    );
  }
  async logout() {
    await json(this.baseUrl + "/v1/session/logout", {
      method: "POST",
      body: "{}",
    });
  }
}
function session(value: Record<string, unknown>): SessionView {
  const role = required(value.role, "role");
  if (
    ![
      "VIEWER",
      "MAIN",
      "LITERATURE_REVIEWER",
      "WORKER",
      "MACHINE_VERIFIER",
      "PEER_REVIEWER",
      "PAPER_REVIEWER",
      "PUBLICATION_WORKER",
      "ADMIN",
    ].includes(role)
  )
    throw new IdentityApiError(200, "UNKNOWN_PRODUCT_ROLE");
  return {
    sessionId: required(value.session_id, "session_id"),
    principalSubjectId: required(
      value.principal_subject_id,
      "principal_subject_id",
    ),
    identityId: required(value.identity_id, "identity_id"),
    displayName: required(value.display_name, "display_name"),
    role: role as ProductRole,
    linkedIdentityIds: strings(value.linked_identity_ids),
    sessionVersion: number(value.session_version),
    issuedAt: required(value.issued_at, "issued_at"),
    expiresAt: required(value.expires_at, "expires_at"),
    accessMode:
      required(value.access_mode, "access_mode") === "SHARED_READ_ONLY"
        ? "SHARED_READ_ONLY"
        : "MANAGED",
  };
}
function required(value: unknown, path: string) {
  if (typeof value !== "string" || !value)
    throw new IdentityApiError(200, "INVALID_SESSION_" + path.toUpperCase());
  return value;
}
function strings(value: unknown) {
  if (!Array.isArray(value) || value.some((v) => typeof v !== "string"))
    throw new IdentityApiError(200, "INVALID_SESSION_IDENTITIES");
  return value as string[];
}
function number(value: unknown) {
  if (typeof value !== "number" || !Number.isSafeInteger(value))
    throw new IdentityApiError(200, "INVALID_SESSION_VERSION");
  return value;
}

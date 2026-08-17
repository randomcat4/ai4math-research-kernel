import {
  explainError,
  type FeatureFailure,
  type ReviewTask,
  type ReviewType,
  type SignedArtifactRef,
} from "./model.js";
export class ReviewApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly unavailable: boolean,
  ) {
    super(code);
  }
  toFailure(): FeatureFailure {
    return explainError(this.code);
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
    throw new ReviewApiError(0, "NETWORK_UNAVAILABLE", true);
  }
  const value: unknown = await response.json().catch(() => null);
  if (value === null || Array.isArray(value) || typeof value !== "object")
    throw new ReviewApiError(response.status, "INVALID_REVIEW_ENVELOPE", false);
  const result = value as Record<string, unknown>;
  if (!response.ok) {
    const code =
      typeof result.code === "string" ? result.code : "REVIEW_REQUEST_FAILED";
    throw new ReviewApiError(
      response.status,
      code,
      response.status === 404 ||
        response.status === 501 ||
        response.status === 503 ||
        code.includes("UNAVAILABLE"),
    );
  }
  return result;
}
export class ReviewGateway {
  constructor(private readonly baseUrl = "") {}
  async inbox(): Promise<ReviewTask[]> {
    const value = await json(this.baseUrl + "/v1/reviews/inbox", {
      method: "GET",
    });
    if (!Array.isArray(value.tasks))
      throw new ReviewApiError(200, "INVALID_REVIEW_INBOX", false);
    return value.tasks.map((task) => parseTask(record(task)));
  }
  async claim(reviewTaskId: string) {
    return parseTask(
      await json(this.baseUrl + "/v1/reviews/claim", {
        method: "POST",
        body: JSON.stringify({ review_task_id: reviewTaskId }),
      }),
    );
  }
  async submit(reviewTaskId: string, ref: SignedArtifactRef) {
    return parseTask(
      await json(this.baseUrl + "/v1/reviews/submit", {
        method: "POST",
        body: JSON.stringify({
          review_task_id: reviewTaskId,
          signed_artifact_ref: ref,
        }),
      }),
    );
  }
}
function parseTask(value: Record<string, unknown>): ReviewTask {
  const type = req(value.review_type);
  if (!["ATOMIC", "COMPOSITION", "PAPER"].includes(type))
    throw new ReviewApiError(200, "UNKNOWN_REVIEW_TYPE", false);
  return {
    id: req(value.review_task_id),
    type: type as ReviewType,
    runId: req(value.run_id),
    assigneeSubjectId: req(value.assignee_subject_id),
    authorSubjectIds: strings(value.author_subject_ids),
    targetId: optional(value.target_id),
    targetDigest: req(value.target_digest),
    contractVersion: num(value.contract_version),
    researchRevision: num(value.research_revision),
    independenceRequired: bool(value.independence_required),
    state: req(value.state),
    createdAt: req(value.created_at),
    expiresAt: req(value.expires_at),
    independenceStatus: req(value.independence_status),
    signedArtifactRef: value.signed_artifact_ref
      ? ref(record(value.signed_artifact_ref))
      : undefined,
  };
}
function ref(v: Record<string, unknown>): SignedArtifactRef {
  return {
    artifact_id: req(v.artifact_id),
    sha256: req(v.sha256),
    byte_count: num(v.byte_count),
    media_type: req(v.media_type),
  };
}
function record(value: unknown) {
  if (value === null || Array.isArray(value) || typeof value !== "object")
    throw new ReviewApiError(200, "INVALID_REVIEW_TASK", false);
  return value as Record<string, unknown>;
}
function req(v: unknown) {
  if (typeof v !== "string" || !v)
    throw new ReviewApiError(200, "INVALID_REVIEW_FIELD", false);
  return v;
}
function optional(v: unknown) {
  return typeof v === "string" && v ? v : undefined;
}
function strings(v: unknown) {
  if (!Array.isArray(v) || v.some((x) => typeof x !== "string"))
    throw new ReviewApiError(200, "INVALID_REVIEW_FIELD", false);
  return v as string[];
}
function num(v: unknown) {
  if (typeof v !== "number" || !Number.isSafeInteger(v))
    throw new ReviewApiError(200, "INVALID_REVIEW_FIELD", false);
  return v;
}
function bool(v: unknown) {
  if (typeof v !== "boolean")
    throw new ReviewApiError(200, "INVALID_REVIEW_FIELD", false);
  return v;
}

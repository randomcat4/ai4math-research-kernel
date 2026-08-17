import {
  ResearchProductClient,
  type JsonObject,
  type ProductTransport,
} from "../../../../sdk/typescript/src/client.js";
import type { GraphMode, GraphSlice, RunFence, SearchPage } from "./model.js";

export class GraphApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super(code);
  }

  get stale() {
    return this.status === 409 || this.code === "STALE_QUERY";
  }
}

class GraphTransport implements ProductTransport {
  constructor(private readonly baseUrl: string) {}

  async request(
    operation: "command" | "query" | "subscribe" | "artifact",
    body: JsonObject,
  ): Promise<JsonObject> {
    if (operation !== "query") throw new GraphApiError(501, "QUERY_ONLY_TRANSPORT");
    const scope = body.scope;
    if (scope === null || Array.isArray(scope) || typeof scope !== "object") {
      throw new GraphApiError(400, "RUN_SCOPE_REQUIRED");
    }
    const runId = scope.run_id;
    if (typeof runId !== "string") throw new GraphApiError(400, "RUN_SCOPE_REQUIRED");
    let response: Response;
    try {
      response = await fetch(
        `${this.baseUrl}/v1/research/${encodeURIComponent(runId)}/queries`,
        {
          method: "POST",
          credentials: "include",
          headers: { "content-type": "application/json", accept: "application/json" },
          body: JSON.stringify(body),
        },
      );
    } catch {
      throw new GraphApiError(0, "NETWORK_UNAVAILABLE");
    }
    const value: unknown = await response.json().catch(() => null);
    if (value === null || Array.isArray(value) || typeof value !== "object") {
      throw new GraphApiError(response.status, "INVALID_GRAPH_ENVELOPE");
    }
    const result = value as JsonObject;
    if (!response.ok) {
      throw new GraphApiError(
        response.status,
        typeof result.code === "string" ? result.code : "GRAPH_QUERY_FAILED",
      );
    }
    return result;
  }
}

export class GraphGateway {
  private readonly client: ResearchProductClient;

  constructor(baseUrl = "") {
    this.client = new ResearchProductClient(new GraphTransport(baseUrl));
  }

  async search(
    run: RunFence,
    mode: GraphMode,
    text: string,
    cursor?: string,
  ): Promise<SearchPage> {
    const value = await this.client.query({
      scope: scope(run),
      type: "GRAPH_SEARCH",
      payload: {
        at_revision: run.revision,
        mode,
        text,
        page: { limit: 40, ...(cursor ? { cursor } : {}) },
      },
    });
    const result = object(value.result, "result");
    const items = array(result.items, "result.items").map((item) => object(item, "search item"));
    const page = object(result.page, "result.page");
    return {
      items: items.map((item) => ({
        claim_id: string(item.claim_id, "claim_id"),
        stable_label: string(item.stable_label, "stable_label"),
        statement: string(item.statement, "statement"),
        lifecycle: string(item.lifecycle, "lifecycle"),
        dependable: boolean(item.dependable, "dependable"),
        route_id: string(item.route_id, "route_id"),
      })),
      total: number(page.total, "page.total"),
      nextCursor: optionalString(page.next_cursor),
    };
  }

  slice(
    run: RunFence,
    mode: GraphMode,
    seedIds: string[],
    options: { routeId?: string; continuation?: string; depth?: number } = {},
  ): Promise<GraphSlice> {
    return this.graphQuery(run, "GRAPH_SLICE", {
      at_revision: run.revision,
      mode,
      seed_ids: seedIds,
      direction: "BOTH",
      depth: options.depth ?? 3,
      filters: options.routeId ? { route_ids: [options.routeId] } : {},
      node_limit: 200,
      ...(options.continuation ? { continuation_cursor: options.continuation } : {}),
    });
  }

  closure(
    run: RunFence,
    claimId: string,
    direction: "DEPENDENCY_CLOSURE" | "REVERSE_CLOSURE",
    continuation?: string,
  ): Promise<GraphSlice> {
    return this.graphQuery(run, direction, {
      at_revision: run.revision,
      claim_id: claimId,
      node_limit: 200,
      ...(continuation ? { continuation_cursor: continuation } : {}),
    });
  }

  private async graphQuery(
    run: RunFence,
    type: "GRAPH_SLICE" | "DEPENDENCY_CLOSURE" | "REVERSE_CLOSURE",
    payload: JsonObject,
  ): Promise<GraphSlice> {
    const value = type === "GRAPH_SLICE"
      ? await this.client.query({ scope: scope(run), type, payload: payload as never })
      : type === "DEPENDENCY_CLOSURE"
        ? await this.client.query({ scope: scope(run), type, payload: payload as never })
        : await this.client.query({ scope: scope(run), type, payload: payload as never });
    return value.result as unknown as GraphSlice;
  }
}

function scope(run: RunFence) {
  return {
    kind: "RUN" as const,
    run_id: run.runId,
    at_revision: run.revision,
    at_contract_version: run.contractVersion,
  };
}

function object(value: unknown, name: string): JsonObject {
  if (value === null || Array.isArray(value) || typeof value !== "object") {
    throw new GraphApiError(0, `INVALID_${name.toUpperCase().replaceAll(".", "_")}`);
  }
  return value as JsonObject;
}

function array(value: unknown, name: string): unknown[] {
  if (!Array.isArray(value)) throw new GraphApiError(0, `INVALID_${name.toUpperCase()}`);
  return value;
}

function string(value: unknown, name: string): string {
  if (typeof value !== "string") throw new GraphApiError(0, `INVALID_${name.toUpperCase()}`);
  return value;
}

function number(value: unknown, name: string): number {
  if (!Number.isSafeInteger(value)) throw new GraphApiError(0, `INVALID_${name.toUpperCase()}`);
  return value as number;
}

function boolean(value: unknown, name: string): boolean {
  if (typeof value !== "boolean") throw new GraphApiError(0, `INVALID_${name.toUpperCase()}`);
  return value;
}

function optionalString(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

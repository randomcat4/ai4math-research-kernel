import type {ArtifactOperationType, CommandType, QueryType} from "./types.js";

export const MAX_SAFE_CONTRACT_INTEGER = Number.MAX_SAFE_INTEGER;

export type JsonScalar = null | boolean | number | string;
export type JsonValue = JsonScalar | JsonValue[] | {[key: string]: JsonValue};
export type JsonObject = {[key: string]: JsonValue};

export type Scope =
  | {kind: "GLOBAL"; deployment_id: string}
  | {kind: "RUN"; run_id: string; expected_revision: number; expected_contract_version: number}
  | {kind: "DEPLOYMENT"; deployment_id: string; expected_deployment_revision: number};

export interface ProductTransport {
  request(operation: "command" | "query" | "subscribe" | "artifact", body: JsonObject): Promise<JsonObject>;
}

export class ProductSdkError extends Error {}
export class UnsafeJsonValueError extends ProductSdkError {}
export class InvalidEnvelopeError extends ProductSdkError {}

const FORBIDDEN_IDENTITY_KEYS = new Set([
  "actor", "role", "capability", "capability_id", "principal_subject_id",
]);

export function assertLosslessJson(value: JsonValue, path = "$"): void {
  if (value === null || typeof value === "boolean" || typeof value === "string") return;
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) {
      throw new UnsafeJsonValueError(`${path}: contract numbers must be safe integers`);
    }
    return;
  }
  if (Array.isArray(value)) {
    value.forEach((item, index) => assertLosslessJson(item, `${path}[${index}]`));
    return;
  }
  for (const [key, item] of Object.entries(value)) assertLosslessJson(item, `${path}.${key}`);
}

export function rejectIdentityInjection(value: JsonValue, path = "$"): void {
  if (value === null || typeof value !== "object") return;
  if (Array.isArray(value)) {
    value.forEach((item, index) => rejectIdentityInjection(item, `${path}[${index}]`));
    return;
  }
  for (const [key, item] of Object.entries(value)) {
    if (FORBIDDEN_IDENTITY_KEYS.has(key)) {
      throw new InvalidEnvelopeError(`${path}.${key}: identity fields come from Session`);
    }
    rejectIdentityInjection(item, `${path}.${key}`);
  }
}

export class ResearchProductClient {
  constructor(private readonly transport: ProductTransport) {}

  command(input: {
    request_id: string; scope: Scope; type: CommandType; payload: JsonObject; artifact_inputs?: JsonObject[];
  }): Promise<JsonObject> {
    rejectIdentityInjection(input.payload);
    return this.send("command", {
      schema_version: "rk.product.command.v1", request_id: input.request_id, scope: input.scope,
      command: {type: input.type, payload: input.payload}, artifact_inputs: input.artifact_inputs ?? [],
    });
  }

  query(input: {scope: Scope; type: QueryType; payload: JsonObject}): Promise<JsonObject> {
    rejectIdentityInjection(input.payload);
    return this.send("query", {
      schema_version: "rk.product.query.v1", scope: input.scope,
      query: {type: input.type, payload: input.payload},
    });
  }

  subscribe(input: {run_id: string; after_cursor: number; event_types?: string[]}): Promise<JsonObject> {
    return this.send("subscribe", {
      schema_version: "rk.product.subscription.v1", run_id: input.run_id,
      after_cursor: input.after_cursor, event_types: input.event_types ?? [],
    });
  }

  artifact(input: {request_id: string; type: ArtifactOperationType; payload: JsonObject}): Promise<JsonObject> {
    rejectIdentityInjection(input.payload);
    return this.send("artifact", {
      schema_version: "rk.product.artifact.v1", request_id: input.request_id,
      operation: {type: input.type, payload: input.payload},
    });
  }

  private async send(operation: "command" | "query" | "subscribe" | "artifact", body: JsonObject): Promise<JsonObject> {
    assertLosslessJson(body);
    const response = await this.transport.request(operation, body);
    assertLosslessJson(response);
    if (typeof response.schema_version !== "string") {
      throw new InvalidEnvelopeError(`${operation} response has no schema_version`);
    }
    return response;
  }
}

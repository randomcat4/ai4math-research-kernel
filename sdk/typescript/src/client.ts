import {COMMAND_CONTRACTS, QUERY_CONTRACTS, QUERY_RESULT_CONTRACTS} from "./types.js";
import type {ArtifactOperationType, CommandPayloadMap, CommandType, QueryPayloadMap, QueryType} from "./types.js";

export const MAX_SAFE_CONTRACT_INTEGER = Number.MAX_SAFE_INTEGER;

export type JsonScalar = null | boolean | number | string;
export type JsonValue = JsonScalar | JsonValue[] | {[key: string]: JsonValue};
export type JsonObject = {[key: string]: JsonValue};

export type Scope =
  | {kind: "GLOBAL"; deployment_id: string}
  | {kind: "RUN"; run_id: string; expected_revision: number; expected_contract_version: number}
  | {kind: "DEPLOYMENT"; deployment_id: string; expected_deployment_revision: number};
export type QueryScope =
  | {kind: "GLOBAL"; deployment_id: string; at_catalog_revision?: number}
  | {kind: "RUN"; run_id: string; at_revision?: number; at_contract_version?: number}
  | {kind: "DEPLOYMENT"; deployment_id: string; at_deployment_revision?: number};

export interface ProductTransport {
  request(operation: "command" | "query" | "subscribe" | "artifact", body: JsonObject): Promise<JsonObject>;
}

export class ProductSdkError extends Error {}
export class UnsafeJsonValueError extends ProductSdkError {}
export class InvalidEnvelopeError extends ProductSdkError {}
export class UnknownVariantError extends ProductSdkError {}

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

function assertCommandContract(type: CommandType, payload: JsonObject, scope: Scope): void {
  const contract = COMMAND_CONTRACTS[type];
  if (!(contract.scope_kinds as readonly string[]).includes(scope.kind)) {
    throw new InvalidEnvelopeError(
      `${type} requires scope in ${contract.scope_kinds.join(",")}, got ${scope.kind}`,
    );
  }
  const keys = new Set(Object.keys(payload));
  const missing = contract.required_payload_fields.filter((field) => !keys.has(field));
  const allowed = new Set<string>([
    ...contract.required_payload_fields, ...contract.optional_payload_fields,
  ]);
  const unknown = [...keys].filter((field) => !allowed.has(field));
  if (missing.length > 0) {
    throw new InvalidEnvelopeError(`${type} payload is missing fields ${missing.join(",")}`);
  }
  if (unknown.length > 0) {
    throw new InvalidEnvelopeError(`${type} payload has unknown fields ${unknown.join(",")}`);
  }
}

function assertQueryContract(type: QueryType, payload: JsonObject, scope: QueryScope): void {
  const contract = QUERY_CONTRACTS[type];
  if (!(contract.scope_kinds as readonly string[]).includes(scope.kind)) {
    throw new InvalidEnvelopeError(`${type} requires query scope in ${contract.scope_kinds.join(",")}, got ${scope.kind}`);
  }
  const keys = new Set(Object.keys(payload));
  const missing = contract.required_payload_fields.filter((field) => !keys.has(field));
  const allowed = new Set<string>([...contract.required_payload_fields, ...contract.optional_payload_fields]);
  const unknown = [...keys].filter((field) => !allowed.has(field));
  if (missing.length > 0) throw new InvalidEnvelopeError(`${type} payload is missing fields ${missing.join(",")}`);
  if (unknown.length > 0) throw new InvalidEnvelopeError(`${type} payload has unknown fields ${unknown.join(",")}`);
}

function assertExactFields(
  value: JsonObject, required: readonly string[], allowed: readonly string[], label: string,
): void {
  const keys = Object.keys(value);
  const missing = required.filter((field) => !(field in value));
  const unknown = keys.filter((field) => !allowed.includes(field));
  if (missing.length > 0) throw new InvalidEnvelopeError(`${label} is missing fields ${missing.join(",")}`);
  if (unknown.length > 0) throw new InvalidEnvelopeError(`${label} has unknown fields ${unknown.join(",")}`);
}

function assertQueryResult(value: JsonObject, expectedType: QueryType, scopeKind: QueryScope["kind"]): void {
  const resultType = value.result_type;
  if (typeof resultType !== "string" || !(resultType in QUERY_RESULT_CONTRACTS)) {
    throw new UnknownVariantError(`unknown query result variant ${String(resultType)}; upgrade the SDK`);
  }
  if (resultType !== expectedType) {
    throw new InvalidEnvelopeError(`query returned ${resultType}, expected exact result ${expectedType}`);
  }
  if (value.scope_kind !== scopeKind) {
    throw new InvalidEnvelopeError("query result scope fence does not match the request");
  }
  const base = ["schema_version", "result_type", "stable_entity_id", "scope_kind", "last_cursor", "result"];
  const fences = scopeKind === "RUN"
    ? ["run_id", "research_revision", "contract_version"]
    : scopeKind === "GLOBAL"
      ? ["deployment_id", "catalog_revision"]
      : ["deployment_id", "deployment_revision"];
  assertExactFields(value, [...base, ...fences], [...base, ...fences], "query result envelope");
  const payload = value.result;
  if (payload === null || Array.isArray(payload) || typeof payload !== "object") {
    throw new InvalidEnvelopeError("query result payload must be an object");
  }
  const contract = QUERY_RESULT_CONTRACTS[resultType as QueryType] as {
    result_kind: string;
    required_projection_fields: readonly string[];
    projection_fields: readonly string[];
    required_domain_fields: readonly string[];
    domain_fields: readonly string[];
  };
  let projections: JsonObject[];
  if (contract.result_kind === "graph") {
    projections = [payload];
  } else if (contract.result_kind === "entity") {
    assertExactFields(payload, ["entity"], ["entity"], "entity result");
    const entity = payload.entity;
    if (entity === null || Array.isArray(entity) || typeof entity !== "object") {
      throw new InvalidEnvelopeError("entity result must contain an object");
    }
    projections = [entity];
  } else {
    assertExactFields(payload, ["items", "page"], ["items", "page"], "list result");
    if (!Array.isArray(payload.items) || payload.items.some((item) => item === null || Array.isArray(item) || typeof item !== "object")) {
      throw new InvalidEnvelopeError("list result items must be objects");
    }
    const page = payload.page;
    if (page === null || Array.isArray(page) || typeof page !== "object") {
      throw new InvalidEnvelopeError("list result page must be an object");
    }
    assertExactFields(page, ["returned", "total", "truncated"], ["returned", "total", "truncated", "next_cursor"], "query result page");
    projections = payload.items as JsonObject[];
  }
  for (const projection of projections) {
    assertExactFields(projection, contract.required_projection_fields, contract.projection_fields, `${resultType} projection`);
    if (contract.required_domain_fields.length > 0) {
      const domain = projection.domain;
      if (domain === null || Array.isArray(domain) || typeof domain !== "object") {
        throw new InvalidEnvelopeError(`${resultType} projection has no domain object`);
      }
      assertExactFields(domain, contract.required_domain_fields, contract.domain_fields, `${resultType} domain`);
    }
  }
}

export class ResearchProductClient {
  constructor(private readonly transport: ProductTransport) {}

  command<T extends CommandType>(input: {
    request_id: string; scope: Scope; type: T; payload: CommandPayloadMap[T]; artifact_inputs?: JsonObject[];
  }): Promise<JsonObject> {
    rejectIdentityInjection(input.payload);
    assertCommandContract(input.type, input.payload, input.scope);
    return this.send("command", {
      schema_version: "rk.product.command.v1", request_id: input.request_id, scope: input.scope,
      command: {type: input.type, payload: input.payload}, artifact_inputs: input.artifact_inputs ?? [],
    });
  }

  query<T extends QueryType>(input: {scope: QueryScope; type: T; payload: QueryPayloadMap[T]}): Promise<JsonObject> {
    rejectIdentityInjection(input.payload);
    assertQueryContract(input.type, input.payload, input.scope);
    return this.send("query", {
      schema_version: "rk.product.query.v1", scope: input.scope,
      query: {type: input.type, payload: input.payload},
    }).then((response) => {
      assertQueryResult(response, input.type, input.scope.kind);
      return response;
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
    if (operation === "query" && response.schema_version !== "rk.product.query_result.v1") {
      throw new UnknownVariantError(`unknown query result schema ${response.schema_version}; upgrade the SDK`);
    }
    return response;
  }
}

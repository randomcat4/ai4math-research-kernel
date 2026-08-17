import {assertLosslessJson, type JsonObject} from "./client.js";

export function roundTrip(value: JsonObject): JsonObject {
  assertLosslessJson(value);
  const decoded: unknown = JSON.parse(JSON.stringify(value));
  if (decoded === null || Array.isArray(decoded) || typeof decoded !== "object") {
    throw new TypeError("round-trip result is not an object");
  }
  const result = decoded as JsonObject;
  assertLosslessJson(result);
  return result;
}

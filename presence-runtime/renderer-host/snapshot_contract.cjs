"use strict";

const TOP_LEVEL = new Set([
  "schema",
  "binding_id",
  "revision",
  "avatar_ref",
  "model_fingerprint",
  "preset_ref",
  "semantic",
  "renderer",
  "capabilities",
]);

function assertObject(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError(label + " must be an object");
  }
}

function deepFreeze(value) {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const child of Object.values(value)) {
      deepFreeze(child);
    }
  }
  return value;
}

function acceptEffectiveSnapshot(input) {
  assertObject(input, "renderer snapshot");
  const unknown = Object.keys(input).filter((field) => !TOP_LEVEL.has(field));
  if (unknown.length) {
    throw new TypeError("renderer snapshot contains unresolved fields: " + unknown.join(", "));
  }
  if (input.schema !== "presence/renderer-snapshot/v0.2") {
    throw new TypeError("renderer snapshot schema is unsupported");
  }
  if (typeof input.binding_id !== "string" || !input.binding_id) {
    throw new TypeError("renderer snapshot binding_id is required");
  }
  if (!Number.isInteger(input.revision) || input.revision < 1) {
    throw new TypeError("renderer snapshot revision must be positive");
  }
  assertObject(input.semantic, "renderer snapshot semantic");
  assertObject(input.renderer, "renderer snapshot renderer");
  if (!Array.isArray(input.semantic.persistent_actions)
      || !Array.isArray(input.semantic.effective_actions)
      || !Array.isArray(input.capabilities)) {
    throw new TypeError("renderer snapshot lists are invalid");
  }
  const serialized = JSON.stringify(input);
  for (const forbidden of [
    "project_patch",
    "session_patch",
    "operations",
    "provider",
    "microphone_permission",
    "route_key",
    "orb_port",
  ]) {
    if (serialized.includes("\"" + forbidden + "\"")) {
      throw new TypeError("renderer snapshot leaked " + forbidden);
    }
  }
  return deepFreeze(structuredClone(input));
}

module.exports = { acceptEffectiveSnapshot };


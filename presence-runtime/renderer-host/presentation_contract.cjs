"use strict";

const TOP_LEVEL = new Set([
  "schema",
  "binding_id",
  "configuration_revision",
  "sequence",
  "event_id",
  "activity",
  "base_actions",
  "target_actions",
  "enter_ms",
  "minimum_visible_ms",
  "exit_ms",
  "easing",
]);
const ACTIVITIES = new Set(["thinking", "tool", "skill", "cli", "waiting", "error"]);

function deepFreeze(value) {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const child of Object.values(value)) deepFreeze(child);
  }
  return value;
}

function stringList(value, label) {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string" || !item)) {
    throw new TypeError(label + " must be a non-empty string list");
  }
}

function duration(value, label) {
  if (!Number.isInteger(value) || value < 0 || value > 30000) {
    throw new TypeError(label + " must be an integer from 0 through 30000");
  }
}

function acceptPresentationCue(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new TypeError("presentation cue must be an object");
  }
  const unknown = Object.keys(input).filter((field) => !TOP_LEVEL.has(field));
  if (unknown.length) {
    throw new TypeError("presentation cue contains unknown fields: " + unknown.join(", "));
  }
  if (input.schema !== "presence/presentation-cue/v0.1") {
    throw new TypeError("presentation cue schema is unsupported");
  }
  if (typeof input.binding_id !== "string" || !input.binding_id) {
    throw new TypeError("presentation cue binding_id is required");
  }
  if (!Number.isInteger(input.configuration_revision) || input.configuration_revision < 1) {
    throw new TypeError("presentation cue configuration revision must be positive");
  }
  if (!Number.isInteger(input.sequence) || input.sequence < 1) {
    throw new TypeError("presentation cue sequence must be positive");
  }
  if (typeof input.event_id !== "string" || !input.event_id) {
    throw new TypeError("presentation cue event_id is required");
  }
  if (!ACTIVITIES.has(input.activity)) {
    throw new TypeError("presentation cue activity is unsupported");
  }
  stringList(input.base_actions, "presentation cue base_actions");
  stringList(input.target_actions, "presentation cue target_actions");
  duration(input.enter_ms, "presentation cue enter_ms");
  duration(input.minimum_visible_ms, "presentation cue minimum_visible_ms");
  duration(input.exit_ms, "presentation cue exit_ms");
  if (input.minimum_visible_ms < input.enter_ms) {
    throw new TypeError("presentation cue lifetime must include entry easing");
  }
  if (input.easing !== "easeInOutCubic") {
    throw new TypeError("presentation cue easing is unsupported");
  }
  return deepFreeze(structuredClone(input));
}

module.exports = { acceptPresentationCue };


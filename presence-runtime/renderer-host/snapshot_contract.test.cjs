"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { acceptEffectiveSnapshot } = require("./snapshot_contract.cjs");

function snapshot() {
  return {
    schema: "presence/renderer-snapshot/v0.2",
    binding_id: "ebf6ea73-9ef8-4b06-bb3e-dab542e10440",
    revision: 1,
    avatar_ref: "builtin@1",
    model_fingerprint: "sha256:" + "0".repeat(64),
    preset_ref: null,
    semantic: {
      persistent_actions: [],
      effective_actions: [],
      activity: null,
    },
    renderer: {
      visible: true,
      progress_visible: true,
      kind: "builtin",
    },
    capabilities: [],
  };
}

test("accepts and freezes resolved snapshots", () => {
  const accepted = acceptEffectiveSnapshot(snapshot());
  assert.equal(Object.isFrozen(accepted), true);
  assert.equal(Object.isFrozen(accepted.semantic), true);
});

test("rejects raw profile resolution fields", () => {
  assert.throws(
    () => acceptEffectiveSnapshot({ ...snapshot(), project_patch: {} }),
    /unresolved fields/,
  );
});


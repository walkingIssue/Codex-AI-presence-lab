"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { acceptPresentationCue } = require("./presentation_contract.cjs");

function cue() {
  return {
    schema: "presence/presentation-cue/v0.1",
    binding_id: "ebf6ea73-9ef8-4b06-bb3e-dab542e10440",
    configuration_revision: 2,
    sequence: 4,
    event_id: "activity:thinking:1",
    activity: "thinking",
    base_actions: ["pose.default"],
    target_actions: ["pose.thinking"],
    enter_ms: 180,
    minimum_visible_ms: 900,
    exit_ms: 180,
    easing: "easeInOutCubic",
  };
}

test("accepts and freezes a deterministic presentation cue", () => {
  const accepted = acceptPresentationCue(cue());
  assert.equal(Object.isFrozen(accepted), true);
  assert.equal(Object.isFrozen(accepted.target_actions), true);
});

test("rejects an invalid lifetime or learned-model payload", () => {
  assert.throws(
    () => acceptPresentationCue({ ...cue(), minimum_visible_ms: 100 }),
    /include entry easing/,
  );
  assert.throws(
    () => acceptPresentationCue({ ...cue(), logits: [0.5] }),
    /unknown fields/,
  );
});


"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { WindowRegistry } = require("./window_registry.cjs");

function record(bindingId, label, ready = true) {
  return {
    bindingId,
    label,
    ready,
    destroyed: false,
    destroy() {
      this.destroyed = true;
    },
  };
}

test("hot swap replaces exactly one binding after readiness", async () => {
  const registry = new WindowRegistry();
  const first = record("binding-a", "first");
  const sibling = record("binding-b", "sibling");
  await registry.swap("binding-a", async () => first);
  await registry.swap("binding-b", async () => sibling);
  const replacement = record("binding-a", "replacement");
  await registry.swap("binding-a", async (previous) => {
    assert.equal(previous, first);
    return replacement;
  });
  assert.equal(first.destroyed, true);
  assert.equal(sibling.destroyed, false);
  assert.equal(registry.current("binding-a"), replacement);
  assert.equal(registry.current("binding-b"), sibling);
});

test("failed preload retains the prior renderer", async () => {
  const registry = new WindowRegistry();
  const previous = record("binding-a", "previous");
  await registry.swap("binding-a", async () => previous);
  await assert.rejects(
    registry.swap("binding-a", async () => record("binding-a", "failed", false)),
    /did not acknowledge/,
  );
  assert.equal(previous.destroyed, false);
  assert.equal(registry.current("binding-a"), previous);
});

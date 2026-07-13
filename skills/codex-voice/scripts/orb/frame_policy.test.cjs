"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  framePolicyFromEnvironment,
  installFrameScheduler,
  normalizeFramePolicy,
  setFrameSchedulerMode,
} = require("./frame_policy.cjs");

test("normalizes and bounds the frame policy", () => {
  assert.deepEqual(normalizeFramePolicy({ idleFps: 0, activeFps: 100 }), {
    enabled: true,
    idleFps: 1,
    activeFps: 60,
  });
  assert.deepEqual(normalizeFramePolicy({ idleFps: 40, activeFps: 20 }), {
    enabled: true,
    idleFps: 40,
    activeFps: 40,
  });
});

test("supports environment overrides and an explicit escape hatch", () => {
  assert.deepEqual(framePolicyFromEnvironment({
    CODEX_ORB_FRAME_LIMIT: "off",
    CODEX_ORB_IDLE_FPS: "12",
    CODEX_ORB_ACTIVE_FPS: "24",
  }), {
    enabled: false,
    idleFps: 12,
    activeFps: 24,
  });
});

test("budgets independent animation callbacks and preserves cancellation", () => {
  const nativeCallbacks = new Map();
  let nativeId = 0;
  global.window = {
    requestAnimationFrame(callback) {
      nativeId += 1;
      nativeCallbacks.set(nativeId, callback);
      return nativeId;
    },
    cancelAnimationFrame(requestId) {
      nativeCallbacks.delete(requestId);
    },
  };

  function step(timestamp) {
    const callbacks = [...nativeCallbacks.values()];
    nativeCallbacks.clear();
    callbacks.forEach((callback) => callback(timestamp));
  }

  try {
    installFrameScheduler(20, 30);
    let firstRuns = 0;
    let secondRuns = 0;
    const first = () => { firstRuns += 1; };
    const second = () => { secondRuns += 1; };

    window.requestAnimationFrame(first);
    window.requestAnimationFrame(second);
    step(0);
    assert.equal(firstRuns, 1);
    assert.equal(secondRuns, 1);

    window.requestAnimationFrame(first);
    window.requestAnimationFrame(second);
    step(16);
    step(33);
    assert.equal(firstRuns, 1);
    assert.equal(secondRuns, 1);
    step(50);
    assert.equal(firstRuns, 2);
    assert.equal(secondRuns, 2);

    setFrameSchedulerMode("active");
    window.requestAnimationFrame(first);
    step(67);
    step(84);
    assert.equal(firstRuns, 3);

    const cancelled = window.requestAnimationFrame(second);
    window.cancelAnimationFrame(cancelled);
    step(120);
    assert.equal(secondRuns, 2);
  } finally {
    delete global.window;
  }
});

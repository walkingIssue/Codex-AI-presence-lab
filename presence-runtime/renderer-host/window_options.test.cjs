"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  enforcePresenceWindow,
  presenceWindowOptions,
} = require("./window_options.cjs");

test("every presence window is constructed and reinforced as topmost", () => {
  const options = presenceWindowOptions({ width: 420, height: 640 }, "preload.cjs");
  assert.equal(options.alwaysOnTop, true);
  assert.equal(options.show, false);
  assert.equal(options.webPreferences.preload, "preload.cjs");

  const calls = [];
  enforcePresenceWindow({
    setAlwaysOnTop(...arguments_) { calls.push(arguments_); },
  });
  assert.deepEqual(calls, [[true, "floating"]]);
});


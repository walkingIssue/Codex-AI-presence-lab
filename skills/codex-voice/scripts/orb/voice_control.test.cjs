"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { PassThrough } = require("node:stream");
const test = require("node:test");

const { createVoiceControlRunner } = require("./voice_control.cjs");

function fakeChild() {
  const child = new EventEmitter();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.killed = false;
  child.kill = () => {
    child.killed = true;
  };
  return child;
}

function nextTurn() {
  return new Promise((resolve) => setImmediate(resolve));
}

test("runs voice controls asynchronously while preserving their order", async () => {
  const children = [];
  const calls = [];
  const run = createVoiceControlRunner({
    python: "python",
    script: "voice_input.py",
    voiceRoot: "voice-root",
    projectRoot: "project-root",
    pathExists: () => true,
    spawnProcess(command, args, options) {
      calls.push({ command, args, options });
      const child = fakeChild();
      children.push(child);
      return child;
    },
  });

  const first = run(["control", "capture-start"]);
  const second = run(["control", "capture-finish"]);
  await nextTurn();
  assert.equal(children.length, 1);

  children[0].stdout.end('{"ok":true,"step":1}\n');
  children[0].emit("close", 0, null);
  assert.deepEqual(await first, { ok: true, step: 1 });
  await nextTurn();
  assert.equal(children.length, 2);

  children[1].stdout.end('{"ok":true,"step":2}\n');
  children[1].emit("close", 0, null);
  assert.deepEqual(await second, { ok: true, step: 2 });
  assert.equal(calls[0].options.stdio[0], "ignore");
});

test("kills and releases a control process that exceeds its deadline", async () => {
  const child = fakeChild();
  const run = createVoiceControlRunner({
    python: "python",
    script: "voice_input.py",
    voiceRoot: "voice-root",
    projectRoot: "project-root",
    pathExists: () => true,
    spawnProcess: () => child,
    timeoutMilliseconds: 5,
  });

  assert.deepEqual(await run(["control", "capture-cancel"]), {
    ok: false,
    error: "voice_input_timeout",
  });
  assert.equal(child.killed, true);
});

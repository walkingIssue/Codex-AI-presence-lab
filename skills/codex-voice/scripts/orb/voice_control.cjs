"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");

const MAX_CAPTURED_OUTPUT = 256 * 1024;

function createVoiceControlRunner({
  python,
  script,
  voiceRoot,
  projectRoot,
  spawnProcess = spawn,
  pathExists = fs.existsSync,
  timeoutMilliseconds = 5000,
}) {
  let tail = Promise.resolve();

  function spawnOne(args) {
    if (!pathExists(python) || !pathExists(script)) {
      return Promise.resolve({ ok: false, error: "voice_input_runtime_missing" });
    }
    return new Promise((resolve) => {
      let stdout = "";
      let stderr = "";
      let settled = false;
      let timer = null;
      const finish = (result) => {
        if (settled) return;
        settled = true;
        if (timer !== null) clearTimeout(timer);
        resolve(result);
      };
      const child = spawnProcess(python, [script, "--voice-root", voiceRoot, ...args], {
        cwd: projectRoot,
        windowsHide: true,
        stdio: ["ignore", "pipe", "pipe"],
      });
      const appendBounded = (value, chunk) => `${value}${chunk}`.slice(-MAX_CAPTURED_OUTPUT);
      child.stdout.on("data", (chunk) => {
        stdout = appendBounded(stdout, chunk);
      });
      child.stderr.on("data", (chunk) => {
        stderr = appendBounded(stderr, chunk);
      });
      child.once("error", (error) => {
        finish({ ok: false, error: error.message || "voice_input_failed" });
      });
      child.once("close", () => {
        const lines = stdout.trim().split(/\r?\n/).filter(Boolean);
        try {
          finish(lines.length
            ? JSON.parse(lines[lines.length - 1])
            : { ok: false, error: "voice_input_no_response" });
        } catch (_) {
          finish({ ok: false, error: stderr.trim() || "voice_input_failed" });
        }
      });
      timer = setTimeout(() => {
        child.kill();
        finish({ ok: false, error: "voice_input_timeout" });
      }, timeoutMilliseconds);
    });
  }

  return function runVoiceControl(args) {
    const result = tail.then(() => spawnOne(args));
    // Preserve state-machine ordering without blocking Electron's main loop.
    tail = result.catch(() => {});
    return result;
  };
}

module.exports = { createVoiceControlRunner };

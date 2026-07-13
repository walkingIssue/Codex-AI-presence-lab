"use strict";

const DEFAULT_FRAME_POLICY = Object.freeze({
  enabled: true,
  idleFps: 60,
  activeFps: 60,
});
const FRAME_POLICY_ARGUMENT_PREFIX = "--codex-frame-policy=";

function boundedFps(value, fallback) {
  const fps = Number(value);
  return Number.isFinite(fps) ? Math.max(1, Math.min(60, Math.round(fps))) : fallback;
}

function normalizeFramePolicy(value = {}) {
  const enabled = value.enabled !== false;
  const idleFps = boundedFps(value.idleFps, DEFAULT_FRAME_POLICY.idleFps);
  const activeFps = Math.max(
    idleFps,
    boundedFps(value.activeFps, DEFAULT_FRAME_POLICY.activeFps),
  );
  return Object.freeze({ enabled, idleFps, activeFps });
}

function framePolicyFromEnvironment(environment = process.env) {
  const switchValue = String(environment.CODEX_ORB_FRAME_LIMIT ?? "").trim().toLowerCase();
  const enabled = !["0", "false", "off", "disabled"].includes(switchValue);
  return normalizeFramePolicy({
    enabled,
    idleFps: environment.CODEX_ORB_IDLE_FPS,
    activeFps: environment.CODEX_ORB_ACTIVE_FPS,
  });
}

function framePolicyArgument(value = {}) {
  return `${FRAME_POLICY_ARGUMENT_PREFIX}${encodeURIComponent(JSON.stringify(normalizeFramePolicy(value)))}`;
}

function framePolicyFromArguments(args = []) {
  const encoded = args.find((value) => typeof value === "string" && value.startsWith(FRAME_POLICY_ARGUMENT_PREFIX));
  if (!encoded) return normalizeFramePolicy();
  try {
    return normalizeFramePolicy(JSON.parse(decodeURIComponent(encoded.slice(FRAME_POLICY_ARGUMENT_PREFIX.length))));
  } catch (_) {
    return normalizeFramePolicy();
  }
}

// This function is serialized by contextBridge.executeInMainWorld. Keep it
// self-contained: closure state and module imports are intentionally unavailable.
function installFrameScheduler(idleFps, activeFps) {
  if (window.__codexPresenceFrameScheduler) return window.__codexPresenceFrameScheduler.policy();

  const nativeRequestAnimationFrame = window.requestAnimationFrame.bind(window);
  const nativeCancelAnimationFrame = window.cancelAnimationFrame.bind(window);
  const callbackLastRun = new WeakMap();
  const pending = new Map();
  let nextRequestId = 1;
  let mode = "idle";
  let idle = Math.max(1, Math.min(60, Number(idleFps) || 60));
  let active = Math.max(idle, Math.min(60, Number(activeFps) || 60));

  function requestAnimationFrameAtBudget(callback) {
    if (typeof callback !== "function") {
      throw new TypeError("requestAnimationFrame callback must be a function");
    }
    const requestId = nextRequestId;
    nextRequestId = nextRequestId >= Number.MAX_SAFE_INTEGER ? 1 : nextRequestId + 1;
    const request = { nativeId: 0 };
    pending.set(requestId, request);

    function pump(timestamp) {
      if (!pending.has(requestId)) return;
      const fps = mode === "active" ? active : idle;
      const minimumInterval = 1000 / fps;
      const lastRun = callbackLastRun.get(callback);
      if (lastRun === undefined || timestamp - lastRun >= minimumInterval - 0.5) {
        pending.delete(requestId);
        callbackLastRun.set(callback, timestamp);
        callback(timestamp);
        return;
      }
      request.nativeId = nativeRequestAnimationFrame(pump);
    }

    request.nativeId = nativeRequestAnimationFrame(pump);
    return requestId;
  }

  function cancelAnimationFrameAtBudget(requestId) {
    const request = pending.get(requestId);
    if (!request) return;
    pending.delete(requestId);
    nativeCancelAnimationFrame(request.nativeId);
  }

  const scheduler = Object.freeze({
    setMode(nextMode) {
      mode = nextMode === "active" ? "active" : "idle";
      return mode;
    },
    setPolicy(nextIdleFps, nextActiveFps) {
      idle = Math.max(1, Math.min(60, Number(nextIdleFps) || idle));
      active = Math.max(idle, Math.min(60, Number(nextActiveFps) || active));
      return this.policy();
    },
    policy() {
      return Object.freeze({ mode, idleFps: idle, activeFps: active });
    },
  });

  Object.defineProperty(window, "requestAnimationFrame", {
    configurable: true,
    writable: true,
    value: requestAnimationFrameAtBudget,
  });
  Object.defineProperty(window, "cancelAnimationFrame", {
    configurable: true,
    writable: true,
    value: cancelAnimationFrameAtBudget,
  });
  Object.defineProperty(window, "__codexPresenceFrameScheduler", {
    configurable: false,
    enumerable: false,
    writable: false,
    value: scheduler,
  });
  return scheduler.policy();
}

function setFrameSchedulerMode(mode) {
  return window.__codexPresenceFrameScheduler?.setMode(mode) ?? "unavailable";
}

module.exports = {
  DEFAULT_FRAME_POLICY,
  framePolicyArgument,
  framePolicyFromArguments,
  framePolicyFromEnvironment,
  installFrameScheduler,
  normalizeFramePolicy,
  setFrameSchedulerMode,
};
